"""
MCP WebSocket Endpoint

Permite que extensões Chrome se conectem ao servidor MCP via WebSocket.
A extensão envia comandos e recebe respostas para automação do SEI/Tribunais.
"""

import json
import logging
from datetime import datetime
from typing import Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from fastapi.websockets import WebSocketState

logger = logging.getLogger(__name__)

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


@router.websocket("/mcp")
async def websocket_mcp_endpoint(
    websocket: WebSocket,
    session_id: str = Query(default=None),
    version: str = Query(default="1.0.0"),
):
    """
    Endpoint WebSocket para conexão da extensão Chrome.

    A extensão se conecta aqui e recebe comandos do servidor MCP.

    Protocolo:
    - Extensão conecta com ?session_id=xxx (opcional, gera automaticamente)
    - Servidor envia: { type: "connected", session_id: "xxx" }
    - Extensão envia: { type: "event", event: "...", data: {...} }
    - Servidor envia comandos: { type: "command", id: "...", action: "...", params: {...} }
    - Extensão responde: { type: "response", id: "...", success: true/false, data/error: {...} }
    """
    # Gerar session_id se não fornecido
    if not session_id:
        session_id = f"session_{uuid4().hex[:8]}"

    metadata = {
        "version": version,
        "user_agent": websocket.headers.get("user-agent"),
    }

    await manager.connect(websocket, session_id, metadata)

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
                # Resposta a um comando - rotear para o MCP Server
                cmd_id = data.get("id")
                success = data.get("success", False)
                logger.debug(f"[MCP-WS] Resposta de {session_id} para {cmd_id}: success={success}")

                # Rotear resposta para o MCP Server
                try:
                    from app.api.endpoints.mcp_server import receive_response
                    receive_response(cmd_id, data)
                except ImportError:
                    logger.warning("[MCP-WS] mcp_server não disponível para rotear resposta")

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
    """Lista todas as sessões WebSocket ativas."""
    return {
        "sessions": manager.list_sessions(),
        "total": len(manager.active_connections),
    }


@router.get("/mcp/status")
async def mcp_status():
    """Status do serviço MCP WebSocket."""
    return {
        "status": "running",
        "connected_extensions": len(manager.active_connections),
        "default_session": manager.get_default_session(),
    }


# Função para enviar comando para extensão (usado pelo MCP server)
async def send_command_to_extension(
    action: str,
    params: dict,
    session_id: str = None,
    timeout: int = 30
) -> dict:
    """
    Envia comando para uma extensão Chrome conectada.

    Args:
        action: Nome da ação (sei_login, sei_search_process, etc.)
        params: Parâmetros do comando
        session_id: ID da sessão específica (opcional, usa default)
        timeout: Timeout em segundos

    Returns:
        Resposta da extensão
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

    command_id = f"cmd_{uuid4().hex[:8]}"

    command = {
        "type": "command",
        "id": command_id,
        "action": action,
        "params": params,
        "session_id": target_session,
    }

    await manager.send_message(target_session, command)

    # Nota: Para implementação completa, você precisaria de um mecanismo
    # de aguardar a resposta (usando asyncio.Event ou similar)
    # Por agora, retornamos que o comando foi enviado

    return {
        "success": True,
        "command_id": command_id,
        "session_id": target_session,
        "message": "Comando enviado, aguardando resposta da extensão"
    }
