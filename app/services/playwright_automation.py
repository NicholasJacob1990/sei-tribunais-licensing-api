"""
Playwright Automation Service

Automação do SEI via Playwright puro (sem extensão Chrome).
Usado como fallback quando extensão não está conectada.

Best practices aplicadas (pesquisa 2026-01-27):
- waitUntil: 'domcontentloaded' em vez de 'networkidle' (desencorajado pelo Playwright)
- frameLocator() para iframes (auto-waiting) em vez de frame()
- getByRole()/getByText() quando possível (resilientes a mudanças de DOM)
- ARIA snapshots com scope por iframe (tree/view/main/full)
- Cache em memória com TTL para reduzir navegações redundantes

Refs: microsoft/playwright-mcp, browser-use/browser-use, browserbase/stagehand
"""

import asyncio
import base64
import logging
import os
import re
import time
from datetime import datetime
from typing import Dict, Optional, Any
from dataclasses import dataclass, field

from app.services.resilience import smart_query, smart_click, smart_fill, smart_select

logger = logging.getLogger(__name__)

# Lazy import para não falhar se playwright não estiver instalado
playwright_available = False
try:
    from playwright.async_api import async_playwright, Browser, Page, BrowserContext, Frame
    playwright_available = True
except ImportError:
    logger.warning("Playwright not installed. Browser automation disabled.")
    Browser = None
    Page = None
    BrowserContext = None
    Frame = None


@dataclass
class CacheEntry:
    """Entrada de cache com TTL."""
    data: Any
    expires: float


@dataclass
class PlaywrightSession:
    """Representa uma sessão Playwright ativa."""
    id: str
    browser: Any  # Browser
    context: Any  # BrowserContext
    page: Any  # Page
    base_url: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_activity: datetime = field(default_factory=datetime.utcnow)
    logged_in: bool = False
    user: Optional[str] = None
    current_process_number: Optional[str] = None
    cache: Dict[str, CacheEntry] = field(default_factory=dict)


class PlaywrightManager:
    """Gerencia sessões Playwright para automação do SEI."""

    def __init__(self):
        self.sessions: Dict[str, PlaywrightSession] = {}
        self._playwright = None
        self._browser: Optional[Any] = None
        self._lock = asyncio.Lock()

        # Configurações
        self.headless = os.environ.get("SEI_MCP_HEADLESS", "true").lower() == "true"
        self.timeout_ms = int(os.environ.get("SEI_MCP_TIMEOUT_MS", "30000"))

    async def _ensure_browser(self):
        """Garante que o browser está iniciado."""
        if not playwright_available:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

        if self._browser is None:
            async with self._lock:
                if self._browser is None:
                    self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch(
                        headless=self.headless,
                        args=['--no-sandbox', '--disable-dev-shm-usage']
                    )
                    logger.info(f"Playwright browser started (headless={self.headless})")

    async def create_session(self, session_id: str, base_url: str) -> PlaywrightSession:
        """Cria nova sessão Playwright."""
        await self._ensure_browser()

        context = await self._browser.new_context(
            viewport={'width': 1280, 'height': 720},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )
        page = await context.new_page()
        page.set_default_timeout(self.timeout_ms)

        session = PlaywrightSession(
            id=session_id,
            browser=self._browser,
            context=context,
            page=page,
            base_url=base_url
        )
        self.sessions[session_id] = session

        logger.info(f"Playwright session created: {session_id}")
        return session

    async def get_or_create_session(self, session_id: str, base_url: str) -> PlaywrightSession:
        """Obtém sessão existente ou cria nova."""
        if session_id in self.sessions:
            session = self.sessions[session_id]
            session.last_activity = datetime.utcnow()
            return session
        return await self.create_session(session_id, base_url)

    async def close_session(self, session_id: str):
        """Fecha uma sessão."""
        if session_id in self.sessions:
            session = self.sessions.pop(session_id)
            await session.context.close()
            logger.info(f"Playwright session closed: {session_id}")

    async def close_all(self):
        """Fecha todas as sessões e o browser."""
        for session_id in list(self.sessions.keys()):
            await self.close_session(session_id)

        if self._browser:
            await self._browser.close()
            self._browser = None

        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    def is_available(self) -> bool:
        """Verifica se Playwright está disponível."""
        return playwright_available

    def list_sessions(self) -> list:
        """Lista sessões ativas."""
        return [
            {
                "session_id": s.id,
                "base_url": s.base_url,
                "logged_in": s.logged_in,
                "user": s.user,
                "created_at": s.created_at.isoformat(),
                "last_activity": s.last_activity.isoformat()
            }
            for s in self.sessions.values()
        ]

    # ============================================
    # Cache helpers
    # ============================================

    def _get_cached(self, session: PlaywrightSession, key: str) -> Optional[Any]:
        """Retorna dados do cache se não expirados."""
        entry = session.cache.get(key)
        if entry and entry.expires > time.time():
            return entry.data
        if entry:
            del session.cache[key]
        return None

    def _set_cache(self, session: PlaywrightSession, key: str, data: Any, ttl_s: float = 60.0):
        """Armazena dados no cache com TTL."""
        session.cache[key] = CacheEntry(data=data, expires=time.time() + ttl_s)

    def _invalidate_cache(self, session: PlaywrightSession, prefix: str = ""):
        """Invalida entradas de cache (todas ou por prefixo)."""
        if not prefix:
            session.cache.clear()
        else:
            keys_to_del = [k for k in session.cache if k.startswith(prefix)]
            for k in keys_to_del:
                del session.cache[k]

    async def _ensure_process_open(self, session: PlaywrightSession, process_number: str) -> dict:
        """Abre processo somente se não for o processo atual (evita navegação redundante)."""
        if session.current_process_number == process_number:
            return {"success": True, "message": f"Processo {process_number} já aberto (skip)"}
        result = await self.open_process(session.id, process_number)
        if result.get("success"):
            session.current_process_number = process_number
        return result

    # ============================================
    # Snapshot helpers (ARIA tree)
    # ============================================

    @staticmethod
    def _clean_snapshot(snap: str) -> str:
        """Remove redundâncias do snapshot ARIA (menu lateral, ícones decorativos, etc.)."""
        # Remover linhas de "Menu cópia protocolo" repetidas
        snap = re.sub(r'^\s*- link "Menu cópia protocolo".*$\n?', '', snap, flags=re.MULTILINE)
        # Resumir assinaturas longas
        snap = re.sub(
            r'link "Assinado por: (.+?)"',
            lambda m: f'link "Assinado: {m.group(1).split(chr(10))[0]}"',
            snap
        )
        # Remover imgs decorativas sem texto
        snap = re.sub(r'^\s*- img \[ref=\w+\]( \[cursor=pointer\])?\s*$\n?', '', snap, flags=re.MULTILINE)
        return snap

    @staticmethod
    def _truncate_snapshot(snap: str, max_length: int) -> str:
        """Trunca snapshot no último newline antes do limite."""
        if len(snap) <= max_length:
            return snap
        truncated = snap[:max_length]
        last_nl = truncated.rfind('\n')
        if last_nl > 0:
            truncated = truncated[:last_nl]
        return truncated + f'\n... [truncado em {max_length} chars]'

    async def snapshot(self, session_id: str, scope: str = "full",
                       max_length: int = 50000, include_hidden: bool = False) -> dict:
        """Captura ARIA snapshot da página (ou de um iframe específico).

        scope: 'full' | 'tree' (ifrArvore) | 'view' (ifrVisualizacao) | 'main' (sem iframes)
        """
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada"}

        session = self.sessions[session_id]
        page = session.page

        try:
            target = page
            scope_label = "full"

            if scope == "tree":
                frame = page.frame(name="ifrArvore")
                if frame:
                    target = frame
                    scope_label = "ifrArvore"
                else:
                    return {"success": False, "error": "iframe ifrArvore não encontrado"}
            elif scope == "view":
                frame = page.frame(name="ifrVisualizacao")
                if frame:
                    target = frame
                    scope_label = "ifrVisualizacao"
                else:
                    return {"success": False, "error": "iframe ifrVisualizacao não encontrado"}
            elif scope == "main":
                scope_label = "main (sem iframes)"

            # Capturar ARIA snapshot
            snap_data = await target.accessibility.snapshot(interesting_only=not include_hidden)

            if not snap_data:
                return {"success": True, "snapshot": "(vazio)", "scope": scope_label}

            # Converter para YAML-style string (formato usado pelo playwright-mcp)
            snap_str = self._serialize_aria_tree(snap_data)

            # Limpar e truncar
            snap_str = self._clean_snapshot(snap_str)
            snap_str = self._truncate_snapshot(snap_str, max_length)

            return {
                "success": True,
                "snapshot": snap_str,
                "scope": scope_label,
                "length": len(snap_str)
            }

        except Exception as e:
            logger.error(f"Snapshot error: {e}")
            return {"success": False, "error": str(e)}

    @staticmethod
    def _serialize_aria_tree(node: dict, indent: int = 0) -> str:
        """Serializa nó da árvore de acessibilidade em formato YAML-style legível."""
        lines = []
        prefix = "  " * indent
        role = node.get("role", "")
        name = node.get("name", "")

        # Formatar nó
        if name:
            lines.append(f'{prefix}- {role} "{name}"')
        elif role:
            lines.append(f'{prefix}- {role}')

        # Propriedades relevantes
        if node.get("value"):
            lines.append(f'{prefix}  value: "{node["value"]}"')

        # Filhos
        for child in node.get("children", []):
            lines.append(PlaywrightManager._serialize_aria_tree(child, indent + 1))

        return "\n".join(lines)

    # ============================================
    # Tool composta: search_and_open
    # ============================================

    async def search_and_open(self, session_id: str, query: str,
                               search_type: str = "numero",
                               include_documents: bool = True) -> dict:
        """Busca + abre + lista documentos em uma única chamada."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada. Faça login primeiro."}

        # 1. Buscar
        search_result = await self.search_process(session_id, query, search_type)
        if not search_result.get("success"):
            return search_result

        results = search_result.get("results", [])
        if not results:
            return {"success": False, "found": False, "query": query}

        # 2. Abrir o primeiro resultado
        # Tentar extrair número do processo do texto
        first_result = results[0].get("text", query)
        open_result = await self.open_process(session_id, query)
        if not open_result.get("success"):
            return open_result

        # Atualizar estado
        session = self.sessions[session_id]
        session.current_process_number = query

        # 3. Listar documentos se solicitado
        documents = []
        if include_documents:
            doc_result = await self.list_documents(session_id)
            if doc_result.get("success"):
                documents = doc_result.get("documents", [])

        return {
            "success": True,
            "found": True,
            "query": query,
            "search_results_count": len(results),
            "process": first_result,
            "documents": documents,
            "documents_count": len(documents)
        }

    # ============================================
    # Ações do SEI
    # ============================================

    async def login(self, session_id: str, url: str, username: str, password: str, orgao: str = None) -> dict:
        """Faz login no SEI."""
        session = await self.get_or_create_session(session_id, url)
        page = session.page

        try:
            # Navegar para página de login
            await page.goto(url, wait_until='domcontentloaded')

            # Preencher credenciais (com fail-fast + self-healing)
            if not await smart_fill(page, 'input[name="txtUsuario"], input[id="txtUsuario"], #usuario', username, context="login:usuario"):
                return {"success": False, "error": "Campo de usuário não encontrado"}

            if not await smart_fill(page, 'input[name="pwdSenha"], input[id="pwdSenha"], #senha', password, context="login:senha"):
                return {"success": False, "error": "Campo de senha não encontrado"}

            # Selecionar órgão se necessário
            if orgao:
                await smart_select(page, 'select[name="selOrgao"], #selOrgao', label=orgao, context="login:orgao")

            # Clicar em login
            if not await smart_click(page, 'button[type="submit"], input[type="submit"], #sbmLogin', context="login:submit"):
                return {"success": False, "error": "Botão de login não encontrado"}

            # Aguardar navegação (domcontentloaded é mais rápido e confiável que networkidle)
            await page.wait_for_load_state('domcontentloaded', timeout=10000)

            # Verificar se login foi bem sucedido
            current_url = page.url
            if 'login' not in current_url.lower() or 'principal' in current_url.lower():
                session.logged_in = True
                session.user = username
                return {"success": True, "message": "Login realizado com sucesso", "url": current_url}
            else:
                return {"success": False, "error": "Login falhou - verifique credenciais"}

        except Exception as e:
            logger.error(f"Login error: {e}")
            return {"success": False, "error": str(e)}

    async def search_process(self, session_id: str, query: str, search_type: str = "numero") -> dict:
        """Busca processos no SEI."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada. Faça login primeiro."}

        session = self.sessions[session_id]
        page = session.page

        # Cache check
        cache_key = f"search:{search_type}:{query}"
        cached = self._get_cached(session, cache_key)
        if cached is not None:
            return cached

        try:
            # Navegar para pesquisa (fail-fast + self-healing)
            await smart_click(page, 'a:has-text("Pesquisa"), #lnkPesquisar', context="search:nav")
            await page.wait_for_load_state('domcontentloaded')

            # Preencher busca
            if await smart_fill(page, 'input[name="txtPesquisa"], #txtPesquisaRapida', query, context="search:input"):
                await page.keyboard.press('Enter')
                await page.wait_for_load_state('domcontentloaded')

            # Coletar resultados
            results = await page.query_selector_all('tr.processoVisitado, tr.processoNaoVisitado, .processo')

            processes = []
            for result in results[:10]:
                text = await result.inner_text()
                processes.append({"text": text.strip()})

            result = {"success": True, "results": processes, "count": len(processes)}
            self._set_cache(session, cache_key, result, ttl_s=30.0)
            return result

        except Exception as e:
            logger.error(f"Search error: {e}")
            return {"success": False, "error": str(e)}

    async def screenshot(self, session_id: str, full_page: bool = False) -> dict:
        """Captura screenshot da página."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada"}

        session = self.sessions[session_id]
        page = session.page

        try:
            screenshot_bytes = await page.screenshot(full_page=full_page)
            base64_image = base64.b64encode(screenshot_bytes).decode('utf-8')

            return {
                "success": True,
                "image": base64_image,
                "mimeType": "image/png"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_page_content(self, session_id: str) -> dict:
        """Obtém conteúdo da página atual."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada"}

        session = self.sessions[session_id]
        page = session.page

        try:
            title = await page.title()
            url = page.url
            content = await page.content()

            return {
                "success": True,
                "title": title,
                "url": url,
                "content_length": len(content)
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def open_process(self, session_id: str, process_number: str) -> dict:
        """Abre/navega para um processo específico."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada. Faça login primeiro."}

        session = self.sessions[session_id]
        page = session.page

        try:
            # Método 1: Pesquisa rápida no topo (fail-fast + self-healing)
            if await smart_fill(page, '#txtPesquisaRapida, input[name="txtPesquisaRapida"]', process_number, context="open_process:pesquisa_rapida"):
                await page.keyboard.press('Enter')
                await page.wait_for_load_state('domcontentloaded')

                # Verificar se encontrou
                current_url = page.url
                if 'processo_visualizar' in current_url or 'processo' in current_url.lower():
                    return {"success": True, "message": f"Processo {process_number} aberto", "url": current_url}

            # Método 2: Clicar no link do processo se listado
            if await smart_click(page, f'a:has-text("{process_number}")', context="open_process:link"):
                await page.wait_for_load_state('domcontentloaded')
                return {"success": True, "message": f"Processo {process_number} aberto", "url": page.url}

            return {"success": False, "error": f"Processo {process_number} não encontrado"}

        except Exception as e:
            logger.error(f"Open process error: {e}")
            return {"success": False, "error": str(e)}

    async def list_documents(self, session_id: str, process_number: str = None) -> dict:
        """Lista todos os documentos de um processo."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada"}

        session = self.sessions[session_id]
        page = session.page

        # Cache check
        cache_key = f"list_docs:{process_number or 'current'}"
        cached = self._get_cached(session, cache_key)
        if cached is not None:
            return cached

        try:
            # Se informou número, abre somente se necessário (evita navegação redundante)
            if process_number:
                open_result = await self._ensure_process_open(session, process_number)
                if not open_result.get("success"):
                    return open_result

            # Coletar documentos da árvore de processos
            doc_elements = await page.query_selector_all(
                '#divArvore a.arvoreNo, .arvore-documento, tr[class*="documento"], .infraArvoreNo'
            )

            documents = []
            for elem in doc_elements:
                try:
                    text = await elem.inner_text()
                    href = await elem.get_attribute('href') or ''
                    doc_id = ''

                    # Extrair ID do documento do href
                    if 'id_documento=' in href:
                        doc_id = href.split('id_documento=')[1].split('&')[0]
                    elif 'documento_visualizar' in href:
                        doc_id = href.split('/')[-1].split('?')[0]

                    if text.strip():
                        documents.append({
                            "name": text.strip(),
                            "id": doc_id,
                            "href": href
                        })
                except:
                    continue

            result = {"success": True, "documents": documents, "count": len(documents)}
            self._set_cache(session, cache_key, result, ttl_s=60.0)
            return result

        except Exception as e:
            logger.error(f"List documents error: {e}")
            return {"success": False, "error": str(e)}

    async def get_status(self, session_id: str, process_number: str, include_history: bool = True) -> dict:
        """Consulta andamento e histórico do processo."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada"}

        session = self.sessions[session_id]
        page = session.page

        # Cache check
        cache_key = f"status:{process_number or 'current'}:{include_history}"
        cached = self._get_cached(session, cache_key)
        if cached is not None:
            return cached

        try:
            # Abre o processo somente se necessário
            if process_number:
                open_result = await self._ensure_process_open(session, process_number)
                if not open_result.get("success"):
                    return open_result

            # Clicar em "Consultar Andamento" ou aba similar (fail-fast + self-healing)
            if await smart_click(page, 'a:has-text("Consultar Andamento"), a:has-text("Andamento"), #lnkAndamento', context="get_status:andamento"):
                await page.wait_for_load_state('domcontentloaded')

            # Coletar histórico
            history = []
            if include_history:
                history_rows = await page.query_selector_all(
                    'table.infraTable tr, #tblHistorico tr, .historico-item'
                )
                for row in history_rows[:20]:  # Limitar a 20 entradas
                    try:
                        text = await row.inner_text()
                        if text.strip():
                            history.append({"entry": text.strip()})
                    except:
                        continue

            # Tentar extrair status atual (fail-fast + self-healing)
            status_elem = await smart_query(page, '.status-processo, #spanStatus, .situacao', context="get_status:status_elem")
            status = ""
            if status_elem:
                status = await status_elem.inner_text()

            result = {
                "success": True,
                "process_number": process_number,
                "status": status.strip() if status else "Status não identificado",
                "history": history,
                "history_count": len(history)
            }
            self._set_cache(session, cache_key, result, ttl_s=30.0)
            return result

        except Exception as e:
            logger.error(f"Get status error: {e}")
            return {"success": False, "error": str(e)}

    async def create_document(self, session_id: str, process_number: str, document_type: str,
                              content: str = None, description: str = None,
                              nivel_acesso: str = "publico") -> dict:
        """Cria um novo documento no processo."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada"}

        session = self.sessions[session_id]
        page = session.page

        # Invalidar cache (operação de escrita)
        self._invalidate_cache(session, "list_docs:")

        try:
            # Abre o processo somente se necessário
            open_result = await self._ensure_process_open(session, process_number)
            if not open_result.get("success"):
                return open_result

            # Clicar em "Incluir Documento" (fail-fast + self-healing)
            if not await smart_click(page, 'a:has-text("Incluir Documento"), #lnkIncluirDocumento, img[title*="Incluir Documento"]', context="create_doc:incluir"):
                return {"success": False, "error": "Botão 'Incluir Documento' não encontrado"}

            await page.wait_for_load_state('domcontentloaded')

            # Selecionar tipo de documento (fail-fast + self-healing)
            type_input = await smart_query(page, '#txtFiltro, input[name="txtFiltro"], #selTipoDocumento', context="create_doc:tipo")
            if type_input:
                tag_name = await type_input.evaluate('el => el.tagName.toLowerCase()')
                if tag_name == 'input':
                    await type_input.fill(document_type)
                    await asyncio.sleep(0.5)
                    # Clicar no resultado da busca
                    await smart_click(page, f'a:has-text("{document_type}"), li:has-text("{document_type}")', context="create_doc:tipo_option")
                else:
                    await page.select_option('#txtFiltro, input[name="txtFiltro"], #selTipoDocumento', label=document_type)

            await page.wait_for_load_state('domcontentloaded')

            # Preencher descrição se informada
            if description:
                await smart_fill(page, '#txtDescricao, input[name="txtDescricao"], textarea[name="txtDescricao"]', description, context="create_doc:descricao")

            # Selecionar nível de acesso
            nivel_map = {"publico": "0", "restrito": "1", "sigiloso": "2"}
            nivel_value = nivel_map.get(nivel_acesso.lower(), "0")
            await smart_click(page, f'input[name="staNivelAcesso"][value="{nivel_value}"]', context="create_doc:nivel_acesso")

            # Salvar/Confirmar
            if await smart_click(page, 'button[type="submit"], input[type="submit"], #btnSalvar, #btnConfirmar', context="create_doc:salvar"):
                await page.wait_for_load_state('domcontentloaded')

            # Verificar se criou
            current_url = page.url
            if 'editor' in current_url.lower() or 'documento' in current_url.lower():
                # Se tem conteúdo, preencher no editor
                if content:
                    # Tentar preencher no iframe do editor
                    editor_frame = page.frame_locator('iframe#txtAreaEditor, iframe.cke_wysiwyg_frame')
                    try:
                        editor_body = editor_frame.locator('body')
                        await editor_body.fill(content)
                    except:
                        # Fallback: textarea simples
                        textarea = await page.query_selector('textarea#txtAreaEditor, textarea.editor')
                        if textarea:
                            await textarea.fill(content)

                return {"success": True, "message": f"Documento {document_type} criado", "url": current_url}

            return {"success": True, "message": f"Documento {document_type} iniciado"}

        except Exception as e:
            logger.error(f"Create document error: {e}")
            return {"success": False, "error": str(e)}

    async def sign_document(self, session_id: str, document_id: str, password: str) -> dict:
        """Assina documento eletronicamente."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada"}

        session = self.sessions[session_id]
        page = session.page

        try:
            # Navegar para o documento se tiver ID (fail-fast + self-healing)
            if document_id:
                if await smart_click(page, f'a[href*="id_documento={document_id}"]', context="sign_doc:nav"):
                    await page.wait_for_load_state('domcontentloaded')

            # Clicar em "Assinar"
            if not await smart_click(page, 'a:has-text("Assinar"), img[title*="Assinar"], #btnAssinar, button:has-text("Assinar")', context="sign_doc:assinar"):
                return {"success": False, "error": "Botão de assinatura não encontrado"}

            await page.wait_for_load_state('domcontentloaded')

            # Preencher senha
            await smart_fill(page, 'input[type="password"], #pwdSenha, input[name="pwdSenha"]', password, context="sign_doc:senha")

            # Confirmar assinatura
            if await smart_click(page, 'button[type="submit"]:has-text("Assinar"), #btnConfirmar, input[value="Assinar"]', context="sign_doc:confirmar"):
                await page.wait_for_load_state('domcontentloaded')

            # Verificar sucesso
            success_msg = await smart_query(page, '.alert-success, .mensagem-sucesso, :has-text("assinado com sucesso")', context="sign_doc:success_msg")
            if success_msg:
                return {"success": True, "message": "Documento assinado com sucesso"}

            # Verificar erro
            error_msg = await smart_query(page, '.alert-danger, .mensagem-erro, .infraException', context="sign_doc:error_msg")
            if error_msg:
                error_text = await error_msg.inner_text()
                return {"success": False, "error": error_text.strip()}

            return {"success": True, "message": "Assinatura processada"}

        except Exception as e:
            logger.error(f"Sign document error: {e}")
            return {"success": False, "error": str(e)}

    async def forward_process(self, session_id: str, process_number: str, target_unit: str,
                              keep_open: bool = False, note: str = None) -> dict:
        """Tramita processo para outra unidade."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada"}

        session = self.sessions[session_id]
        page = session.page

        # Invalidar cache (operação de escrita)
        self._invalidate_cache(session)

        try:
            # Abre o processo somente se necessário
            open_result = await self._ensure_process_open(session, process_number)
            if not open_result.get("success"):
                return open_result

            # Clicar em "Enviar Processo" (fail-fast + self-healing)
            if not await smart_click(page, 'a:has-text("Enviar Processo"), img[title*="Enviar"], #lnkEnviarProcesso', context="forward:enviar"):
                return {"success": False, "error": "Botão 'Enviar Processo' não encontrado"}

            await page.wait_for_load_state('domcontentloaded')

            # Selecionar unidade destino
            unit_input = await smart_query(page, '#txtUnidade, input[name="txtUnidade"], #selUnidadesDestino', context="forward:unidade")
            if unit_input:
                tag_name = await unit_input.evaluate('el => el.tagName.toLowerCase()')
                if tag_name == 'input':
                    await unit_input.fill(target_unit)
                    await asyncio.sleep(0.5)
                    # Clicar na sugestão
                    await smart_click(page, f'.autocomplete-suggestion:has-text("{target_unit}"), li:has-text("{target_unit}")', context="forward:sugestao")
                else:
                    await page.select_option('#txtUnidade, input[name="txtUnidade"], #selUnidadesDestino', label=target_unit)

            # Manter aberto na unidade atual
            if keep_open:
                await smart_click(page, '#chkManterAberto, input[name="chkManterAberto"]', context="forward:manter_aberto")

            # Adicionar observação
            if note:
                await smart_fill(page, '#txtObservacao, textarea[name="txtObservacao"]', note, context="forward:observacao")

            # Enviar
            if await smart_click(page, 'button[type="submit"], #btnEnviar, input[value="Enviar"]', context="forward:submit"):
                await page.wait_for_load_state('domcontentloaded')

            return {"success": True, "message": f"Processo tramitado para {target_unit}"}

        except Exception as e:
            logger.error(f"Forward process error: {e}")
            return {"success": False, "error": str(e)}

    async def navigate(self, session_id: str, url: str) -> dict:
        """Navega para uma URL específica."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada"}

        session = self.sessions[session_id]
        page = session.page

        try:
            await page.goto(url, wait_until='domcontentloaded')
            await page.wait_for_load_state('domcontentloaded')

            return {
                "success": True,
                "url": page.url,
                "title": await page.title()
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def click(self, session_id: str, selector: str) -> dict:
        """Clica em um elemento na página."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada"}

        session = self.sessions[session_id]
        page = session.page

        try:
            if await smart_click(page, selector, context=f"click:{selector[:30]}"):
                await page.wait_for_load_state('domcontentloaded')
                return {"success": True, "message": f"Clicado em {selector}"}
            return {"success": False, "error": f"Elemento não encontrado: {selector}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def fill(self, session_id: str, selector: str, value: str) -> dict:
        """Preenche um campo na página."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada"}

        session = self.sessions[session_id]
        page = session.page

        try:
            if await smart_fill(page, selector, value, context=f"fill:{selector[:30]}"):
                return {"success": True, "message": f"Campo {selector} preenchido"}
            return {"success": False, "error": f"Campo não encontrado: {selector}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def logout(self, session_id: str) -> dict:
        """Faz logout do SEI."""
        if session_id not in self.sessions:
            return {"success": False, "error": "Sessão não encontrada"}

        session = self.sessions[session_id]
        page = session.page

        try:
            # Clicar em sair (fail-fast + self-healing)
            if await smart_click(page, 'a:has-text("Sair"), #lnkSair, a[href*="logout"], img[title*="Sair"]', context="logout:sair"):
                await page.wait_for_load_state('domcontentloaded')

            session.logged_in = False
            session.user = None

            return {"success": True, "message": "Logout realizado"}

        except Exception as e:
            return {"success": False, "error": str(e)}


# Instância global
playwright_manager = PlaywrightManager()
