"""
MCP Server - Streamable HTTP Transport

Implementa o Model Context Protocol via Streamable HTTP para que
clientes MCP (Claude, GPT, etc.) possam chamar ferramentas do SEI.

Arquitetura:
- Cliente MCP (Claude) ──Streamable HTTP──► Este servidor ──WebSocket──► Extensão Chrome
"""

import asyncio
import json
import logging
import os
import platform
import subprocess
from datetime import datetime
from typing import Dict, Any, Optional
from uuid import uuid4

try:
    import redis.asyncio as aioredis
    REDIS_AVAILABLE = True
except ImportError:
    aioredis = None
    REDIS_AVAILABLE = False

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["MCP Server"])

# Importar o gerenciador de conexões WebSocket
from app.api.endpoints.mcp_websocket import manager as ws_manager

# Playwright automation (fallback when extension not connected)
try:
    from app.services.playwright_automation import playwright_manager
    PLAYWRIGHT_AVAILABLE = playwright_manager.is_available()
except ImportError:
    playwright_manager = None
    PLAYWRIGHT_AVAILABLE = False

# Agent fallback (quando Extension + Playwright falham)
try:
    from app.services.resilience import create_agent_fallback_response
    AGENT_FALLBACK_AVAILABLE = os.environ.get("AGENT_FALLBACK_ENABLED", "false").lower() == "true"
except ImportError:
    create_agent_fallback_response = None
    AGENT_FALLBACK_AVAILABLE = False

logger.info(f"Playwright automation available: {PLAYWRIGHT_AVAILABLE}")
logger.info(f"Agent fallback available: {AGENT_FALLBACK_AVAILABLE}")

# Armazena respostas pendentes de comandos
pending_responses: Dict[str, asyncio.Future] = {}

# ============================================
# Cache Redis para tools de leitura
# ============================================
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
_redis_client: Optional[aioredis.Redis] = None

CACHEABLE_TOOLS = {"sei_search_process", "sei_list_documents", "sei_get_status"}
CACHE_TTL = {"sei_search_process": 30, "sei_list_documents": 60, "sei_get_status": 30}
CACHE_INVALIDATING_TOOLS = {"sei_create_document", "sei_forward_process", "sei_sign_document"}


async def _get_redis():
    """Lazy-init Redis client."""
    global _redis_client
    if not REDIS_AVAILABLE:
        return None
    if _redis_client is None:
        try:
            _redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
            await _redis_client.ping()
            logger.info("[MCP] Redis cache connected")
        except Exception as e:
            logger.warning(f"[MCP] Redis not available, cache disabled: {e}")
            _redis_client = None
    return _redis_client


async def _get_cached_result(tool_name: str, tool_args: dict) -> Optional[dict]:
    """Busca resultado em cache Redis."""
    if tool_name not in CACHEABLE_TOOLS:
        return None
    r = await _get_redis()
    if not r:
        return None
    try:
        cache_key = f"sei:mcp:{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
        cached = await r.get(cache_key)
        if cached:
            logger.debug(f"[MCP] Cache hit: {tool_name}")
            return json.loads(cached)
    except Exception as e:
        logger.debug(f"[MCP] Cache read error: {e}")
    return None


async def _set_cached_result(tool_name: str, tool_args: dict, result: dict):
    """Armazena resultado em cache Redis."""
    if tool_name not in CACHEABLE_TOOLS:
        return
    r = await _get_redis()
    if not r:
        return
    try:
        cache_key = f"sei:mcp:{tool_name}:{json.dumps(tool_args, sort_keys=True)}"
        ttl = CACHE_TTL.get(tool_name, 30)
        await r.setex(cache_key, ttl, json.dumps(result, ensure_ascii=False))
        logger.debug(f"[MCP] Cache set: {tool_name} (TTL={ttl}s)")
    except Exception as e:
        logger.debug(f"[MCP] Cache write error: {e}")


async def _invalidate_cache(tool_name: str):
    """Invalida cache quando operação de escrita ocorre."""
    if tool_name not in CACHE_INVALIDATING_TOOLS:
        return
    r = await _get_redis()
    if not r:
        return
    try:
        # Invalidar todas as chaves de tools cacheáveis
        for pattern in ["sei:mcp:sei_list_documents:*", "sei:mcp:sei_get_status:*", "sei:mcp:sei_search_process:*"]:
            async for key in r.scan_iter(match=pattern):
                await r.delete(key)
        logger.debug(f"[MCP] Cache invalidated by {tool_name}")
    except Exception as e:
        logger.debug(f"[MCP] Cache invalidation error: {e}")

# Timeout padrão configurável via env var (em ms)
DEFAULT_TIMEOUT_MS = int(os.environ.get("SEI_MCP_COMMAND_TIMEOUT_MS", "30000"))

# Campos comuns para todas as tools
COMMON_FIELDS = {
    "session_id": {
        "type": "string",
        "description": "ID da sessão específica (opcional, usa mais recente)"
    },
    "timeout_ms": {
        "type": "integer",
        "description": f"Timeout em milissegundos (padrão: {DEFAULT_TIMEOUT_MS})"
    }
}

# Tools que são executadas localmente no servidor (não precisam de extensão)
LOCAL_TOOLS = ["sei_open_url", "sei_get_connection_status", "sei_wait_for_extension"]

# Tools compostas: orquestradas server-side (múltiplos comandos WebSocket sequenciais)
COMPOSITE_TOOLS = ["sei_search_and_open"]

# ============================================
# Definição das Ferramentas MCP
# ============================================

def with_common_fields(schema: dict, exclude_fields: list = None) -> dict:
    """Adiciona campos comuns (session_id, timeout_ms) ao schema."""
    exclude = exclude_fields or []
    props = schema.get("properties", {}).copy()
    for field, definition in COMMON_FIELDS.items():
        if field not in exclude:
            props[field] = definition
    return {**schema, "properties": props}


MCP_TOOLS = [
    {
        "name": "sei_open_url",
        "description": "Abre uma URL no navegador padrão do sistema (não requer extensão conectada). Use este comando PRIMEIRO se a extensão não estiver conectada.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL para abrir no navegador"}
            },
            "required": ["url"]
        }
    },
    {
        "name": "sei_wait_for_extension",
        "description": "Aguarda até que uma extensão Chrome se conecte ao servidor. Use antes de outros comandos se não houver extensão conectada.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {"type": "integer", "description": "Tempo máximo de espera em segundos (padrão: 30)", "default": 30},
                "open_url": {"type": "string", "description": "URL para abrir no navegador enquanto aguarda (opcional)"}
            }
        }
    },
    {
        "name": "sei_login",
        "description": "Faz login no sistema SEI com usuário e senha",
        "inputSchema": with_common_fields({
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL base do SEI"},
                "username": {"type": "string", "description": "Nome de usuário"},
                "password": {"type": "string", "description": "Senha"},
                "orgao": {"type": "string", "description": "Órgão (opcional)"}
            },
            "required": ["url", "username", "password"]
        })
    },
    {
        "name": "sei_search_process",
        "description": "Busca processos no SEI por número, texto, interessado ou assunto",
        "inputSchema": with_common_fields({
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Termo de busca"},
                "type": {"type": "string", "enum": ["numero", "texto", "interessado", "assunto"], "default": "numero"},
                "limit": {"type": "integer", "default": 10}
            },
            "required": ["query"]
        })
    },
    {
        "name": "sei_open_process",
        "description": "Abre/navega para um processo específico",
        "inputSchema": with_common_fields({
            "type": "object",
            "properties": {
                "process_number": {"type": "string", "description": "Número do processo"}
            },
            "required": ["process_number"]
        })
    },
    {
        "name": "sei_list_documents",
        "description": "Lista todos os documentos de um processo",
        "inputSchema": with_common_fields({
            "type": "object",
            "properties": {
                "process_number": {"type": "string", "description": "Número do processo"}
            },
            "required": ["process_number"]
        })
    },
    {
        "name": "sei_create_document",
        "description": "Cria um novo documento no processo",
        "inputSchema": with_common_fields({
            "type": "object",
            "properties": {
                "process_number": {"type": "string"},
                "document_type": {"type": "string", "description": "Tipo (Ofício, Despacho, etc)"},
                "content": {"type": "string", "description": "Conteúdo HTML"},
                "description": {"type": "string"},
                "nivel_acesso": {"type": "string", "enum": ["publico", "restrito", "sigiloso"], "default": "publico"}
            },
            "required": ["process_number", "document_type"]
        })
    },
    {
        "name": "sei_sign_document",
        "description": "Assina documento eletronicamente",
        "inputSchema": with_common_fields({
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "password": {"type": "string"}
            },
            "required": ["document_id", "password"]
        })
    },
    {
        "name": "sei_forward_process",
        "description": "Tramita processo para outra unidade",
        "inputSchema": with_common_fields({
            "type": "object",
            "properties": {
                "process_number": {"type": "string"},
                "target_unit": {"type": "string"},
                "keep_open": {"type": "boolean", "default": False},
                "note": {"type": "string"}
            },
            "required": ["process_number", "target_unit"]
        })
    },
    {
        "name": "sei_get_status",
        "description": "Consulta andamento e histórico do processo",
        "inputSchema": with_common_fields({
            "type": "object",
            "properties": {
                "process_number": {"type": "string"},
                "include_history": {"type": "boolean", "default": True}
            },
            "required": ["process_number"]
        })
    },
    {
        "name": "sei_screenshot",
        "description": "Captura screenshot da página atual do SEI",
        "inputSchema": with_common_fields({
            "type": "object",
            "properties": {
                "full_page": {"type": "boolean", "default": False}
            }
        })
    },
    {
        "name": "sei_get_connection_status",
        "description": "Verifica status da conexão com a extensão Chrome",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "sei_search_and_open",
        "description": "Busca, abre e lista documentos de um processo em uma única chamada (combina sei_search_process + sei_open_process + sei_list_documents). Use esta ferramenta PRIMEIRO ao trabalhar com processos — economiza 3 chamadas.",
        "inputSchema": with_common_fields({
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Número ou texto do processo"},
                "type": {"type": "string", "enum": ["numero", "texto", "interessado", "assunto"], "default": "numero"},
                "include_documents": {"type": "boolean", "default": True, "description": "Incluir lista de documentos na resposta"}
            },
            "required": ["query"]
        })
    },
    {
        "name": "sei_snapshot",
        "description": "Captura ARIA snapshot (árvore de acessibilidade) da página SEI. Muito mais leve que screenshot — retorna texto estruturado. Use scope para limitar: 'tree' (árvore de documentos), 'view' (visualização do documento), 'main' (página sem iframes), 'full' (tudo).",
        "inputSchema": with_common_fields({
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["full", "tree", "view", "main"], "default": "full", "description": "Escopo: full=tudo, tree=ifrArvore, view=ifrVisualizacao, main=sem iframes"},
                "max_length": {"type": "integer", "default": 50000, "description": "Tamanho máximo do snapshot em caracteres"},
                "include_hidden": {"type": "boolean", "default": False, "description": "Incluir elementos ocultos"}
            }
        })
    }
]

# ============================================
# Handlers MCP
# ============================================

async def handle_initialize(params: dict) -> dict:
    """Handle MCP initialize request - supports 2024-11-05 and 2025-06-18."""
    # Aceitar a versão do protocolo do cliente (2024-11-05 ou 2025-06-18)
    client_version = params.get("protocolVersion", "2024-11-05")
    # Suportar versões conhecidas - v2 deploy fix
    supported_versions = ["2024-11-05", "2025-06-18"]
    protocol_version = client_version if client_version in supported_versions else "2025-06-18"

    logger.info(f"[MCP] Initialize: client={client_version}, responding={protocol_version}")

    return {
        "protocolVersion": protocol_version,
        "capabilities": {
            "tools": {}
        },
        "serverInfo": {
            "name": "sei-mcp",
            "version": "1.0.0"
        }
    }


async def handle_list_tools(params: dict) -> dict:
    """Handle MCP tools/list request."""
    return {
        "tools": MCP_TOOLS
    }


def open_url_in_system_browser(url: str) -> dict:
    """Abre URL no navegador padrão do sistema (execução local)."""
    try:
        system = platform.system().lower()
        if system == "darwin":  # macOS
            subprocess.Popen(["open", url])
        elif system == "linux":
            subprocess.Popen(["xdg-open", url])
        elif system == "windows":
            subprocess.Popen(["start", url], shell=True)
        else:
            return {"success": False, "error": f"Sistema não suportado: {system}"}
        return {"success": True, "message": f"URL aberta no navegador: {url}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def handle_local_tool(tool_name: str, tool_args: dict) -> dict:
    """Executa ferramentas locais (que não precisam de extensão)."""
    if tool_name == "sei_open_url":
        url = tool_args.get("url", "")
        if not url:
            return {
                "content": [{"type": "text", "text": json.dumps({"error": "URL é obrigatória"})}],
                "isError": True
            }
        result = open_url_in_system_browser(url)
        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
            "isError": not result.get("success", False)
        }

    elif tool_name == "sei_wait_for_extension":
        timeout_seconds = tool_args.get("timeout_seconds", 30)
        open_url = tool_args.get("open_url")

        # Se já está conectado, retorna imediatamente
        if ws_manager.is_connected():
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "connected": True,
                        "message": "Extensão já está conectada",
                        "sessions": ws_manager.list_sessions()
                    }, indent=2, ensure_ascii=False)
                }]
            }

        # Opcionalmente abre URL enquanto aguarda
        if open_url:
            open_url_in_system_browser(open_url)

        # Aguarda conexão com polling
        start_time = datetime.utcnow()
        while (datetime.utcnow() - start_time).total_seconds() < timeout_seconds:
            if ws_manager.is_connected():
                return {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "connected": True,
                            "message": "Extensão conectada com sucesso",
                            "wait_time_seconds": (datetime.utcnow() - start_time).total_seconds(),
                            "sessions": ws_manager.list_sessions()
                        }, indent=2, ensure_ascii=False)
                    }]
                }
            await asyncio.sleep(1)

        # Timeout
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "connected": False,
                    "error": f"Timeout após {timeout_seconds}s aguardando extensão",
                    "message": "Verifique se a extensão SEI-MCP está instalada e ativada no Chrome",
                    "tip": "Use sei_open_url para abrir o SEI manualmente"
                }, indent=2, ensure_ascii=False)
            }],
            "isError": True
        }

    elif tool_name == "sei_get_connection_status":
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "connected": ws_manager.is_connected(),
                    "sessions": ws_manager.list_sessions(),
                    "default_session": ws_manager.get_default_session()
                }, indent=2)
            }]
        }

    return {"content": [{"type": "text", "text": "Tool local não implementada"}], "isError": True}


async def handle_playwright_tool(tool_name: str, tool_args: dict, session_id: str = None) -> dict:
    """Executa ferramenta via Playwright (fallback quando extensão não conectada)."""
    if not playwright_manager:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": "Playwright não disponível"})}],
            "isError": True
        }

    # Usar session_id ou criar default
    pw_session_id = session_id or "default"

    try:
        if tool_name == "sei_login":
            url = tool_args.get("url", "")
            username = tool_args.get("username", "")
            password = tool_args.get("password", "")
            orgao = tool_args.get("orgao")
            result = await playwright_manager.login(pw_session_id, url, username, password, orgao)

        elif tool_name == "sei_search_process":
            query = tool_args.get("query", "")
            search_type = tool_args.get("type", "numero")
            result = await playwright_manager.search_process(pw_session_id, query, search_type)

        elif tool_name == "sei_open_process":
            process_number = tool_args.get("process_number", "")
            result = await playwright_manager.open_process(pw_session_id, process_number)

        elif tool_name == "sei_list_documents":
            process_number = tool_args.get("process_number")
            result = await playwright_manager.list_documents(pw_session_id, process_number)

        elif tool_name == "sei_create_document":
            result = await playwright_manager.create_document(
                pw_session_id,
                tool_args.get("process_number", ""),
                tool_args.get("document_type", ""),
                tool_args.get("content"),
                tool_args.get("description"),
                tool_args.get("nivel_acesso", "publico")
            )

        elif tool_name == "sei_sign_document":
            document_id = tool_args.get("document_id", "")
            password = tool_args.get("password", "")
            result = await playwright_manager.sign_document(pw_session_id, document_id, password)

        elif tool_name == "sei_forward_process":
            result = await playwright_manager.forward_process(
                pw_session_id,
                tool_args.get("process_number", ""),
                tool_args.get("target_unit", ""),
                tool_args.get("keep_open", False),
                tool_args.get("note")
            )

        elif tool_name == "sei_get_status":
            process_number = tool_args.get("process_number", "")
            include_history = tool_args.get("include_history", True)
            result = await playwright_manager.get_status(pw_session_id, process_number, include_history)

        elif tool_name == "sei_screenshot":
            full_page = tool_args.get("full_page", False)
            result = await playwright_manager.screenshot(pw_session_id, full_page)

            if result.get("success") and "image" in result:
                return {
                    "content": [{
                        "type": "image",
                        "data": result["image"],
                        "mimeType": result.get("mimeType", "image/png")
                    }]
                }

        elif tool_name == "sei_search_and_open":
            query = tool_args.get("query", "")
            search_type = tool_args.get("type", "numero")
            include_documents = tool_args.get("include_documents", True)
            result = await playwright_manager.search_and_open(
                pw_session_id, query, search_type, include_documents
            )

        elif tool_name == "sei_snapshot":
            scope = tool_args.get("scope", "full")
            max_length = tool_args.get("max_length", 50000)
            include_hidden = tool_args.get("include_hidden", False)
            result = await playwright_manager.snapshot(
                pw_session_id, scope, max_length, include_hidden
            )

        elif tool_name == "sei_navigate":
            url = tool_args.get("url", "")
            result = await playwright_manager.navigate(pw_session_id, url)

        elif tool_name == "sei_click":
            selector = tool_args.get("selector", "")
            result = await playwright_manager.click(pw_session_id, selector)

        elif tool_name == "sei_fill":
            selector = tool_args.get("selector", "")
            value = tool_args.get("value", "")
            result = await playwright_manager.fill(pw_session_id, selector, value)

        elif tool_name == "sei_logout":
            result = await playwright_manager.logout(pw_session_id)

        elif tool_name == "sei_get_connection_status":
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "connected": True,
                        "driver": "playwright",
                        "sessions": playwright_manager.list_sessions()
                    }, indent=2)
                }]
            }

        else:
            result = {"success": False, "error": f"Tool {tool_name} não implementada no Playwright"}

        # Formatar resposta
        if result.get("success"):
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps(result, indent=2, ensure_ascii=False)
                }]
            }
        else:
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps(result, indent=2, ensure_ascii=False)
                }],
                "isError": True
            }

    except Exception as e:
        logger.error(f"[MCP] Playwright error for {tool_name}: {e}")
        # Agent fallback: screenshot + ARIA para Claude analisar
        if AGENT_FALLBACK_AVAILABLE and create_agent_fallback_response:
            pw_session_id = session_id or "default"
            if playwright_manager and pw_session_id in playwright_manager.sessions:
                try:
                    page = playwright_manager.sessions[pw_session_id].page
                    return await create_agent_fallback_response(page, tool_name, tool_args, str(e))
                except Exception as fb_err:
                    logger.error(f"[MCP] Agent fallback failed: {fb_err}")
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({"error": str(e)}, indent=2)
            }],
            "isError": True
        }


async def handle_composite_tool(tool_name: str, tool_args: dict,
                                 session_id: str = None, timeout_sec: float = 30) -> dict:
    """Orquestra tools compostas server-side (múltiplos comandos sequenciais)."""

    # Determinar backend: extensão ou Playwright
    use_extension = ws_manager.is_connected()
    target_session = None
    if use_extension:
        target_session = ws_manager.get_session_by_id(session_id) if session_id else ws_manager.get_most_recent_session()

    async def _call(action: str, params: dict) -> dict:
        """Executa sub-comando via extensão ou Playwright."""
        if use_extension and target_session:
            try:
                return await send_command_and_wait(action, params, target_session, timeout_sec)
            except asyncio.TimeoutError:
                pass
        # Fallback para Playwright
        if PLAYWRIGHT_AVAILABLE and playwright_manager:
            pw_result = await handle_playwright_tool(action, params, session_id)
            # Extrair data do formato MCP
            if isinstance(pw_result.get("content"), list) and pw_result["content"]:
                try:
                    return json.loads(pw_result["content"][0].get("text", "{}"))
                except (json.JSONDecodeError, KeyError):
                    pass
            return {"success": False, "error": "Formato inesperado do Playwright"}
        return {"success": False, "error": "Sem backend disponível (extensão ou Playwright)"}

    try:
        if tool_name == "sei_search_and_open":
            query = tool_args.get("query", "")
            search_type = tool_args.get("type", "numero")
            include_documents = tool_args.get("include_documents", True)

            # 1. Buscar
            search_result = await _call("sei_search_process", {"query": query, "type": search_type})
            if not search_result.get("success"):
                return {
                    "content": [{"type": "text", "text": json.dumps(
                        {"found": False, "query": query, "error": search_result.get("error", "Busca falhou")},
                        indent=2, ensure_ascii=False
                    )}],
                    "isError": True
                }

            results = search_result.get("results", search_result.get("data", {}).get("results", []))

            # 2. Abrir
            open_result = await _call("sei_open_process", {"process_number": query})

            # 3. Listar documentos
            documents = []
            if include_documents and open_result.get("success"):
                doc_result = await _call("sei_list_documents", {"process_number": query})
                if doc_result.get("success"):
                    documents = doc_result.get("documents", doc_result.get("data", {}).get("documents", []))

            return {
                "content": [{"type": "text", "text": json.dumps({
                    "found": bool(open_result.get("success")),
                    "query": query,
                    "search_results_count": len(results),
                    "documents": documents,
                    "documents_count": len(documents)
                }, indent=2, ensure_ascii=False)}]
            }

        return {
            "content": [{"type": "text", "text": json.dumps(
                {"error": f"Tool composta {tool_name} não implementada"}
            )}],
            "isError": True
        }

    except Exception as e:
        logger.error(f"[MCP] Composite tool error {tool_name}: {e}")
        return {
            "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
            "isError": True
        }


async def handle_call_tool(params: dict) -> dict:
    """Handle MCP tools/call request - executa ferramenta via extensão Chrome."""
    tool_name = params.get("name")
    tool_args = params.get("arguments", {})

    logger.info(f"[MCP] Calling tool: {tool_name}")

    # Extrair campos comuns e remover do args para envio à extensão
    session_id = tool_args.pop("session_id", None)
    timeout_ms = tool_args.pop("timeout_ms", DEFAULT_TIMEOUT_MS)
    timeout_sec = timeout_ms / 1000

    # Verificar se é tool local (não precisa de extensão)
    if tool_name in LOCAL_TOOLS:
        return await handle_local_tool(tool_name, tool_args)

    # Tools compostas: orquestração server-side (múltiplos WebSocket commands)
    if tool_name in COMPOSITE_TOOLS:
        return await handle_composite_tool(tool_name, tool_args, session_id, timeout_sec)

    # Cache Redis: verificar se resultado já está em cache (tools de leitura)
    cached_result = await _get_cached_result(tool_name, tool_args)
    if cached_result is not None:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({**cached_result, "_cache": "hit"}, indent=2, ensure_ascii=False)
            }]
        }

    # Invalidar cache se for operação de escrita
    await _invalidate_cache(tool_name)

    # Verificar se há extensão conectada OU se Playwright está disponível
    if not ws_manager.is_connected():
        # Tentar usar Playwright como fallback
        if PLAYWRIGHT_AVAILABLE and playwright_manager:
            logger.info(f"[MCP] Using Playwright fallback for {tool_name}")
            return await handle_playwright_tool(tool_name, tool_args, session_id)

        available_sessions = ws_manager.list_sessions()
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Nenhuma extensão Chrome conectada e Playwright não disponível",
                    "action_required": "Use sei_wait_for_extension ou sei_open_url primeiro",
                    "recommended_flow": [
                        "1. Chame sei_wait_for_extension com open_url da página do SEI",
                        "2. Aguarde a extensão conectar",
                        "3. Então execute o comando desejado"
                    ],
                    "alternative": "Use sei_open_url para apenas abrir o navegador (sem automação)",
                    "playwright_available": PLAYWRIGHT_AVAILABLE,
                    "available_sessions": available_sessions
                }, indent=2, ensure_ascii=False)
            }],
            "isError": True
        }

    # Determinar sessão: específica ou mais recente
    target_session = ws_manager.get_session_by_id(session_id) if session_id else ws_manager.get_most_recent_session()

    if not target_session:
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Sessão não encontrada",
                    "requested_session": session_id,
                    "available_sessions": ws_manager.list_sessions()
                }, indent=2, ensure_ascii=False)
            }],
            "isError": True
        }

    # Enviar comando para extensão e aguardar resposta
    try:
        response = await send_command_and_wait(tool_name, tool_args, target_session, timeout_sec)

        if response.get("success"):
            data = response.get("data", {})

            # Se for screenshot, retornar como imagem
            if tool_name == "sei_screenshot" and isinstance(data, dict) and "image" in data:
                return {
                    "content": [{
                        "type": "image",
                        "data": data["image"],
                        "mimeType": data.get("mimeType", "image/png")
                    }]
                }

            # Cache Redis: salvar resultado de tools cacheáveis
            if isinstance(data, dict):
                await _set_cached_result(tool_name, tool_args, data)

            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps(data, indent=2, ensure_ascii=False) if isinstance(data, (dict, list)) else str(data)
                }]
            }
        else:
            return {
                "content": [{
                    "type": "text",
                    "text": json.dumps({
                        "error": response.get("error", "Erro desconhecido")
                    }, indent=2)
                }],
                "isError": True
            }

    except asyncio.TimeoutError:
        # Fallback para Playwright quando extensão não responde
        if PLAYWRIGHT_AVAILABLE and playwright_manager:
            logger.warning(f"[MCP] Extension timeout for {tool_name}, falling back to Playwright")
            try:
                pw_result = await handle_playwright_tool(tool_name, tool_args, session_id)
                # Adicionar aviso de que usou fallback
                if isinstance(pw_result.get("content"), list) and pw_result["content"]:
                    first_content = pw_result["content"][0]
                    if first_content.get("type") == "text":
                        try:
                            data = json.loads(first_content["text"])
                            data["_fallback"] = "playwright"
                            data["_reason"] = "extension_timeout"
                            first_content["text"] = json.dumps(data, indent=2, ensure_ascii=False)
                        except:
                            pass
                return pw_result
            except Exception as pw_error:
                logger.error(f"[MCP] Playwright fallback also failed: {pw_error}")
                # Agent fallback: captura screenshot+ARIA para Claude analisar
                if AGENT_FALLBACK_AVAILABLE and create_agent_fallback_response and playwright_manager:
                    pw_session_id = session_id or "default"
                    if pw_session_id in playwright_manager.sessions:
                        page = playwright_manager.sessions[pw_session_id].page
                        return await create_agent_fallback_response(page, tool_name, tool_args, str(pw_error))

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Timeout",
                    "message": f"A extensão não respondeu em {timeout_sec}s para {tool_name}",
                    "session": target_session,
                    "playwright_available": PLAYWRIGHT_AVAILABLE,
                    "tip": "Aumente timeout_ms ou verifique se a extensão está respondendo"
                }, indent=2, ensure_ascii=False)
            }],
            "isError": True
        }
    except Exception as e:
        # Fallback para Playwright em caso de erro
        if PLAYWRIGHT_AVAILABLE and playwright_manager:
            logger.warning(f"[MCP] Extension error for {tool_name}: {e}, falling back to Playwright")
            try:
                return await handle_playwright_tool(tool_name, tool_args, session_id)
            except Exception as pw_error:
                logger.error(f"[MCP] Playwright fallback also failed: {pw_error}")
                # Agent fallback: captura screenshot+ARIA para Claude analisar
                if AGENT_FALLBACK_AVAILABLE and create_agent_fallback_response:
                    pw_session_id = session_id or "default"
                    if pw_session_id in playwright_manager.sessions:
                        page = playwright_manager.sessions[pw_session_id].page
                        return await create_agent_fallback_response(page, tool_name, tool_args, str(pw_error))

        logger.error(f"[MCP] Error calling tool {tool_name}: {e}")
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": str(e),
                    "playwright_available": PLAYWRIGHT_AVAILABLE
                }, indent=2)
            }],
            "isError": True
        }


async def send_command_and_wait(action: str, params: dict, session_id: str = None, timeout: float = 30) -> dict:
    """Envia comando para extensão via WebSocket e aguarda resposta."""
    command_id = f"cmd_{uuid4().hex[:8]}"

    # Criar future para aguardar resposta
    future = asyncio.get_event_loop().create_future()
    pending_responses[command_id] = future

    # Usar sessão especificada ou default
    target_session = session_id or ws_manager.get_default_session()

    command = {
        "type": "command",
        "id": command_id,
        "action": action,
        "params": params,
        "session_id": target_session
    }

    logger.debug(f"[MCP] Enviando comando {command_id} para sessão {target_session}, timeout={timeout}s")

    await ws_manager.send_message(target_session, command)

    try:
        # Aguardar resposta com timeout
        response = await asyncio.wait_for(future, timeout=timeout)
        return response
    finally:
        # Limpar
        pending_responses.pop(command_id, None)


def receive_response(command_id: str, response: dict):
    """Chamado quando a extensão envia uma resposta."""
    if command_id in pending_responses:
        future = pending_responses[command_id]
        if not future.done():
            future.set_result(response)


# ============================================
# Streamable HTTP Endpoint
# ============================================

@router.post("")
@router.post("/")
async def mcp_endpoint(request: Request):
    """
    MCP Streamable HTTP Endpoint.

    Recebe requisições JSON-RPC do cliente MCP e retorna respostas.
    Suporta SSE para streaming de respostas longas.
    """
    try:
        body = await request.json()
    except Exception as e:
        return Response(
            content=json.dumps({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None}),
            media_type="application/json",
            status_code=400
        )

    # Pode ser um único request ou batch
    if isinstance(body, list):
        responses = [await process_jsonrpc_request(req) for req in body]
        return Response(
            content=json.dumps(responses),
            media_type="application/json"
        )
    else:
        response = await process_jsonrpc_request(body)
        return Response(
            content=json.dumps(response),
            media_type="application/json"
        )


async def process_jsonrpc_request(request: dict) -> dict:
    """Processa uma requisição JSON-RPC."""
    jsonrpc = request.get("jsonrpc", "2.0")
    method = request.get("method", "")
    params = request.get("params", {})
    request_id = request.get("id")

    logger.info(f"[MCP] Request: method={method}, id={request_id}, params_keys={list(params.keys())}")

    try:
        if method == "initialize":
            result = await handle_initialize(params)
        elif method == "tools/list":
            result = await handle_list_tools(params)
        elif method == "tools/call":
            result = await handle_call_tool(params)
        elif method == "notifications/initialized":
            # Notificação, não precisa de resposta
            return {"jsonrpc": jsonrpc, "result": {}, "id": request_id}
        elif method == "ping":
            result = {}
        else:
            return {
                "jsonrpc": jsonrpc,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
                "id": request_id
            }

        return {
            "jsonrpc": jsonrpc,
            "result": result,
            "id": request_id
        }

    except Exception as e:
        logger.error(f"[MCP] Error processing request: {e}")
        return {
            "jsonrpc": jsonrpc,
            "error": {"code": -32603, "message": str(e)},
            "id": request_id
        }


@router.get("")
@router.get("/")
async def mcp_sse_endpoint(request: Request):
    """
    MCP SSE Endpoint para Server-Sent Events.

    Usado para streaming de notificações do servidor para o cliente.
    """
    async def event_generator():
        # Enviar evento de conexão
        yield {
            "event": "open",
            "data": json.dumps({"status": "connected"})
        }

        # Manter conexão aberta para notificações
        while True:
            await asyncio.sleep(30)  # Heartbeat
            yield {
                "event": "ping",
                "data": json.dumps({"timestamp": datetime.utcnow().isoformat()})
            }

    return EventSourceResponse(event_generator())


@router.get("/info")
async def mcp_info():
    """Informações sobre o servidor MCP."""
    return {
        "name": "sei-mcp",
        "version": "1.0.0",
        "protocol": "2024-11-05",
        "transport": "streamable-http",
        "tools_count": len(MCP_TOOLS),
        "extension_connected": ws_manager.is_connected(),
        "active_sessions": len(ws_manager.list_sessions())
    }
