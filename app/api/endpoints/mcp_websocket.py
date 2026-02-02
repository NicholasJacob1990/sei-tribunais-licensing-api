"""
MCP WebSocket Endpoint

Permite que extensões Chrome se conectem ao servidor MCP via WebSocket.
A extensão envia comandos e recebe respostas para automação do SEI/Tribunais.

OTIMIZADO: Retry com backoff, timeout handling, resposta assíncrona
SEGURANÇA: Requer API token (sei_xxx) via query param ?token=
"""

import asyncio
import json
import logging
from datetime import datetime
from hashlib import sha256
from typing import Dict, Optional, Any
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from fastapi.websockets import WebSocketState
from sqlalchemy import select

from app.database import async_session_factory
from app.models.user import User

logger = logging.getLogger(__name__)

# Retry configuration
DEFAULT_MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.5  # seconds
RETRY_MAX_DELAY = 4.0  # seconds
RETRYABLE_ERRORS = ['timeout', 'connection', 'reset']

# Pending responses storage
pending_responses: Dict[str, asyncio.Future] = {}

router = APIRouter(prefix="/ws", tags=["MCP WebSocket"])

# Armazena conexões ativas por session_id
active_connections: Dict[str, WebSocket] = {}
# Armazena metadados das sessões
session_metadata: Dict[str, dict] = {}


class ConnectionManager:
    """Gerencia conexões WebSocket das extensões Chrome."""

    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.session_metadata: Dict[str, dict] = {}
        self.session_urls: Dict[str, str] = {}  # Tracking de URL por sessão

    async def connect(self, websocket: WebSocket, session_id: str, metadata: dict = None):
        """Aceita nova conexão WebSocket."""
        await websocket.accept()
        self.active_connections[session_id] = websocket
        self.session_metadata[session_id] = {
            "connected_at": datetime.utcnow().isoformat(),
            "last_activity": datetime.utcnow().isoformat(),
            "user_agent": metadata.get("user_agent") if metadata else None,
            "extension_version": metadata.get("version") if metadata else None,
            **(metadata or {})
        }
        logger.info(f"[MCP-WS] Nova conexão: {session_id}")

    def disconnect(self, session_id: str):
        """Remove conexão."""
        if session_id in self.active_connections:
            del self.active_connections[session_id]
        if session_id in self.session_metadata:
            del self.session_metadata[session_id]
        if session_id in self.session_urls:
            del self.session_urls[session_id]
        logger.info(f"[MCP-WS] Desconectado: {session_id}")

    def update_session_url(self, session_id: str, url: str):
        """Atualiza URL atual de uma sessão."""
        if session_id in self.session_metadata:
            self.session_urls[session_id] = url
            self.session_metadata[session_id]["current_url"] = url
            self.session_metadata[session_id]["last_activity"] = datetime.utcnow().isoformat()

    async def send_message(self, session_id: str, message: dict):
        """Envia mensagem para uma sessão específica."""
        if session_id in self.active_connections:
            websocket = self.active_connections[session_id]
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json(message)
                self.session_metadata[session_id]["last_activity"] = datetime.utcnow().isoformat()

    async def broadcast(self, message: dict):
        """Envia mensagem para todas as conexões."""
        disconnected = []
        for session_id, websocket in self.active_connections.items():
            try:
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json(message)
            except Exception as e:
                logger.error(f"[MCP-WS] Erro ao enviar para {session_id}: {e}")
                disconnected.append(session_id)

        # Limpar conexões mortas
        for session_id in disconnected:
            self.disconnect(session_id)

    def list_sessions(self) -> list:
        """Lista todas as sessões ativas."""
        return [
            {
                "session_id": session_id,
                **self.session_metadata.get(session_id, {})
            }
            for session_id in self.active_connections.keys()
        ]

    def is_connected(self, session_id: str = None) -> bool:
        """Verifica se há conexão ativa."""
        if session_id:
            return session_id in self.active_connections
        return len(self.active_connections) > 0

    def get_default_session(self) -> Optional[str]:
        """Retorna a sessão mais recente/ativa."""
        return self.get_most_recent_session()

    def get_most_recent_session(self) -> Optional[str]:
        """
        Retorna a sessão com atividade mais recente.
        Prioriza sessões com URL do SEI ativa.
        """
        if not self.active_connections:
            return None

        # Ordenar por last_activity (mais recente primeiro)
        sessions_with_activity = []
        for session_id in self.active_connections.keys():
            meta = self.session_metadata.get(session_id, {})
            last_activity = meta.get("last_activity", "")
            current_url = meta.get("current_url", "")
            is_sei_url = "/sei/" in current_url or "controlador.php" in current_url
            sessions_with_activity.append((session_id, last_activity, is_sei_url))

        # Priorizar: 1) URL do SEI, 2) atividade mais recente
        sessions_with_activity.sort(key=lambda x: (not x[2], x[1]), reverse=True)

        if sessions_with_activity:
            return sessions_with_activity[0][0]
        return None

    def get_session_by_id(self, session_id: str) -> Optional[str]:
        """Retorna session_id se válido e conectado."""
        if session_id and session_id in self.active_connections:
            return session_id
        return None


# Instância global do gerenciador
manager = ConnectionManager()


async def _authenticate_ws_token(token: str) -> Optional[User]:
    """Valida API token (sei_xxx) e retorna o User ou None."""
    if not token or not token.startswith("sei_"):
        return None
    token_hash = sha256(token.encode()).hexdigest()
    async with async_session_factory() as db:
        result = await db.execute(
            select(User).where(User.api_token_hash == token_hash)
        )
        user = result.scalar_one_or_none()
        if user and user.is_active:
            return user
    return None


@router.websocket("/mcp")
async def websocket_mcp_endpoint(
    websocket: WebSocket,
    token: str = Query(default=None),
    session_id: str = Query(default=None),
    version: str = Query(default="1.0.0"),
):
    """
    Endpoint WebSocket para conexão da extensão Chrome.

    REQUER: ?token=sei_xxx (API token gerado via /auth/api-token/generate)

    Protocolo:
    - Extensão conecta com ?token=sei_xxx&session_id=xxx
    - Servidor valida token e aceita ou rejeita conexão
    - Servidor envia: { type: "connected", session_id: "xxx" }
    - Extensão envia: { type: "event", event: "...", data: {...} }
    - Servidor envia comandos: { type: "command", id: "...", action: "...", params: {...} }
    - Extensão responde: { type: "response", id: "...", success: true/false, data/error: {...} }
    """
    # Autenticação obrigatória
    user = await _authenticate_ws_token(token)
    if not user:
        await websocket.close(code=4001, reason="Authentication required: invalid or missing API token")
        logger.warning(f"[MCP-WS] Conexão rejeitada: token inválido")
        return

    # Gerar session_id se não fornecido
    if not session_id:
        session_id = f"session_{uuid4().hex[:8]}"

    metadata = {
        "version": version,
        "user_agent": websocket.headers.get("user-agent"),
        "user_email": user.email,
        "user_id": user.id,
    }

    await manager.connect(websocket, session_id, metadata)
    logger.info(f"[MCP-WS] Autenticado: {user.email} → {session_id}")

    # Enviar confirmação de conexão
    await websocket.send_json({
        "type": "connected",
        "session_id": session_id,
        "server_time": datetime.utcnow().isoformat(),
    })

    try:
        while True:
            # Receber mensagem da extensão
            data = await websocket.receive_json()

            msg_type = data.get("type")

            if msg_type == "event":
                # Evento da extensão (login_detected, page_changed, etc.)
                event = data.get("event")
                event_data = data.get("data", {})
                logger.debug(f"[MCP-WS] Evento de {session_id}: {event}")

                # Atualizar metadata se for login
                if event == "login_detected":
                    manager.session_metadata[session_id].update({
                        "user": event_data.get("user"),
                        "tribunal": event_data.get("tribunal"),
                    })

                # Tracking de URL para page_changed
                elif event == "page_changed":
                    url = event_data.get("url", "")
                    if url:
                        manager.update_session_url(session_id, url)
                        logger.debug(f"[MCP-WS] URL atualizada para {session_id}: {url}")

            elif msg_type == "response":
                # Resposta a um comando - rotear para futures pendentes
                cmd_id = data.get("id")
                success = data.get("success", False)
                logger.debug(f"[MCP-WS] Resposta de {session_id} para {cmd_id}: success={success}")

                # Resolver future pendente
                receive_response(cmd_id, data)

            elif msg_type == "register":
                # Registro da extensão com informações adicionais
                manager.session_metadata[session_id].update({
                    "window_id": data.get("windowId"),
                    "tribunal": data.get("tribunal"),
                    "user": data.get("user"),
                })
                logger.info(f"[MCP-WS] Extensão registrada: {session_id}")

            elif msg_type == "ping":
                # Heartbeat
                await websocket.send_json({"type": "pong"})

            else:
                logger.warning(f"[MCP-WS] Tipo de mensagem desconhecido: {msg_type}")

    except WebSocketDisconnect:
        manager.disconnect(session_id)
    except Exception as e:
        logger.error(f"[MCP-WS] Erro na conexão {session_id}: {e}")
        manager.disconnect(session_id)


@router.get("/mcp/sessions")
async def list_mcp_sessions():
    """Lista todas as sessões WebSocket ativas (info pública limitada)."""
    return {
        "total": len(manager.active_connections),
        "has_connections": manager.is_connected(),
    }


@router.get("/mcp/status")
async def mcp_status():
    """Status do serviço MCP WebSocket."""
    return {
        "status": "running",
        "connected_extensions": len(manager.active_connections),
    }


def receive_response(command_id: str, response: dict):
    """
    Recebe resposta de um comando da extensão.
    Chamado pelo handler de mensagens WebSocket.
    """
    if command_id in pending_responses:
        future = pending_responses.pop(command_id)
        if not future.done():
            future.set_result(response)


def _is_retryable_error(error: str) -> bool:
    """Check if error message indicates a retryable error."""
    error_lower = error.lower()
    return any(e in error_lower for e in RETRYABLE_ERRORS)


def _get_retry_delay(attempt: int) -> float:
    """Calculate delay with exponential backoff and jitter."""
    import random
    delay = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
    jitter = delay * 0.2 * (random.random() - 0.5)
    return delay + jitter


# Função para enviar comando para extensão (usado pelo MCP server)
async def send_command_to_extension(
    action: str,
    params: dict,
    session_id: str = None,
    timeout: int = 30,
    max_retries: int = DEFAULT_MAX_RETRIES
) -> dict:
    """
    Envia comando para uma extensão Chrome conectada.

    OTIMIZADO: Retry com backoff exponencial e espera por resposta.

    Args:
        action: Nome da ação (sei_login, sei_search_process, etc.)
        params: Parâmetros do comando
        session_id: ID da sessão específica (opcional, usa default)
        timeout: Timeout em segundos
        max_retries: Número máximo de tentativas

    Returns:
        Resposta da extensão
    """
    last_error = None

    for attempt in range(max_retries + 1):
        try:
            return await _send_command_once(action, params, session_id, timeout)
        except Exception as e:
            last_error = str(e)

            # Don't retry non-retryable errors
            if not _is_retryable_error(last_error):
                break

            # Don't retry on last attempt
            if attempt < max_retries:
                delay = _get_retry_delay(attempt)
                logger.warning(
                    f"[MCP-WS] Command failed, retrying in {delay:.2f}s "
                    f"(attempt {attempt + 1}/{max_retries}): {action}"
                )
                await asyncio.sleep(delay)

    return {
        "success": False,
        "error": last_error or f"Command failed after {max_retries} retries: {action}"
    }


async def _send_command_once(
    action: str,
    params: dict,
    session_id: str = None,
    timeout: int = 30
) -> dict:
    """
    Envia um único comando (sem retry).
    """
    target_session = session_id or manager.get_default_session()

    if not target_session:
        return {
            "success": False,
            "error": "Nenhuma extensão conectada"
        }

    if not manager.is_connected(target_session):
        return {
            "success": False,
            "error": f"Sessão não conectada: {target_session}"
        }

    command_id = f"cmd_{uuid4().hex[:8]}_{int(datetime.utcnow().timestamp() * 1000)}"

    command = {
        "type": "command",
        "id": command_id,
        "action": action,
        "params": params,
        "session_id": target_session,
    }

    # Create future for response
    loop = asyncio.get_event_loop()
    response_future: asyncio.Future = loop.create_future()
    pending_responses[command_id] = response_future

    try:
        # Send command
        await manager.send_message(target_session, command)
        logger.debug(f"[MCP-WS] Sent command {command_id}: {action}")

        # Wait for response with timeout
        try:
            response = await asyncio.wait_for(response_future, timeout=timeout)
            return response
        except asyncio.TimeoutError:
            raise Exception(f"Command timeout after {timeout}s: {action}")

    finally:
        # Cleanup pending response
        pending_responses.pop(command_id, None)
