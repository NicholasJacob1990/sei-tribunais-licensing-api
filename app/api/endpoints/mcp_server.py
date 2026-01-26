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
from datetime import datetime
from typing import Dict, Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["MCP Server"])

# Importar o gerenciador de conexões WebSocket
from app.api.endpoints.mcp_websocket import manager as ws_manager

# Armazena respostas pendentes de comandos
pending_responses: Dict[str, asyncio.Future] = {}

# ============================================
# Definição das Ferramentas MCP
# ============================================

MCP_TOOLS = [
    {
        "name": "sei_login",
        "description": "Faz login no sistema SEI com usuário e senha",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL base do SEI"},
                "username": {"type": "string", "description": "Nome de usuário"},
                "password": {"type": "string", "description": "Senha"},
                "orgao": {"type": "string", "description": "Órgão (opcional)"}
            },
            "required": ["url", "username", "password"]
        }
    },
    {
        "name": "sei_search_process",
        "description": "Busca processos no SEI por número, texto, interessado ou assunto",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Termo de busca"},
                "type": {"type": "string", "enum": ["numero", "texto", "interessado", "assunto"], "default": "numero"},
                "limit": {"type": "integer", "default": 10}
            },
            "required": ["query"]
        }
    },
    {
        "name": "sei_open_process",
        "description": "Abre/navega para um processo específico",
        "inputSchema": {
            "type": "object",
            "properties": {
                "process_number": {"type": "string", "description": "Número do processo"}
            },
            "required": ["process_number"]
        }
    },
    {
        "name": "sei_list_documents",
        "description": "Lista todos os documentos de um processo",
        "inputSchema": {
            "type": "object",
            "properties": {
                "process_number": {"type": "string", "description": "Número do processo"}
            },
            "required": ["process_number"]
        }
    },
    {
        "name": "sei_create_document",
        "description": "Cria um novo documento no processo",
        "inputSchema": {
            "type": "object",
            "properties": {
                "process_number": {"type": "string"},
                "document_type": {"type": "string", "description": "Tipo (Ofício, Despacho, etc)"},
                "content": {"type": "string", "description": "Conteúdo HTML"},
                "description": {"type": "string"},
                "nivel_acesso": {"type": "string", "enum": ["publico", "restrito", "sigiloso"], "default": "publico"}
            },
            "required": ["process_number", "document_type"]
        }
    },
    {
        "name": "sei_sign_document",
        "description": "Assina documento eletronicamente",
        "inputSchema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "password": {"type": "string"}
            },
            "required": ["document_id", "password"]
        }
    },
    {
        "name": "sei_forward_process",
        "description": "Tramita processo para outra unidade",
        "inputSchema": {
            "type": "object",
            "properties": {
                "process_number": {"type": "string"},
                "target_unit": {"type": "string"},
                "keep_open": {"type": "boolean", "default": False},
                "note": {"type": "string"}
            },
            "required": ["process_number", "target_unit"]
        }
    },
    {
        "name": "sei_get_status",
        "description": "Consulta andamento e histórico do processo",
        "inputSchema": {
            "type": "object",
            "properties": {
                "process_number": {"type": "string"},
                "include_history": {"type": "boolean", "default": True}
            },
            "required": ["process_number"]
        }
    },
    {
        "name": "sei_screenshot",
        "description": "Captura screenshot da página atual do SEI",
        "inputSchema": {
            "type": "object",
            "properties": {
                "full_page": {"type": "boolean", "default": False}
            }
        }
    },
    {
        "name": "sei_get_connection_status",
        "description": "Verifica status da conexão com a extensão Chrome",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    }
]

# ============================================
# Handlers MCP
# ============================================

async def handle_initialize(params: dict) -> dict:
    """Handle MCP initialize request."""
    return {
        "protocolVersion": "2024-11-05",
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


async def handle_call_tool(params: dict) -> dict:
    """Handle MCP tools/call request - executa ferramenta via extensão Chrome."""
    tool_name = params.get("name")
    tool_args = params.get("arguments", {})

    logger.info(f"[MCP] Calling tool: {tool_name}")

    # Verificar conexão com extensão
    if tool_name == "sei_get_connection_status":
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

    # Verificar se há extensão conectada
    if not ws_manager.is_connected():
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Nenhuma extensão Chrome conectada",
                    "message": "Por favor, abra o SEI no Chrome e conecte a extensão SEI-MCP Bridge"
                }, indent=2)
            }],
            "isError": True
        }

    # Enviar comando para extensão e aguardar resposta
    try:
        response = await send_command_and_wait(tool_name, tool_args)

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
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": "Timeout",
                    "message": f"A extensão não respondeu em tempo hábil para {tool_name}"
                }, indent=2)
            }],
            "isError": True
        }
    except Exception as e:
        logger.error(f"[MCP] Error calling tool {tool_name}: {e}")
        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "error": str(e)
                }, indent=2)
            }],
            "isError": True
        }


async def send_command_and_wait(action: str, params: dict, timeout: int = 30) -> dict:
    """Envia comando para extensão via WebSocket e aguarda resposta."""
    command_id = f"cmd_{uuid4().hex[:8]}"

    # Criar future para aguardar resposta
    future = asyncio.get_event_loop().create_future()
    pending_responses[command_id] = future

    # Enviar comando
    session_id = ws_manager.get_default_session()
    command = {
        "type": "command",
        "id": command_id,
        "action": action,
        "params": params,
        "session_id": session_id
    }

    await ws_manager.send_message(session_id, command)

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

    logger.debug(f"[MCP] Request: method={method}, id={request_id}")

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
