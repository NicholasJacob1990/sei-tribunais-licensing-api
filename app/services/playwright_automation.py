"""
Playwright Automation Service

Automação do SEI via Playwright puro (sem extensão Chrome).
Usado como fallback quando extensão não está conectada.
"""

import asyncio
import base64
import logging
import os
from datetime import datetime
from typing import Dict, Optional, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Lazy import para não falhar se playwright não estiver instalado
playwright_available = False
try:
    from playwright.async_api import async_playwright, Browser, Page, BrowserContext
    playwright_available = True
except ImportError:
    logger.warning("Playwright not installed. Browser automation disabled.")
    Browser = None
    Page = None
    BrowserContext = None


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
    # Ações do SEI
    # ============================================

    async def login(self, session_id: str, url: str, username: str, password: str, orgao: str = None) -> dict:
        """Faz login no SEI."""
        session = await self.get_or_create_session(session_id, url)
        page = session.page

        try:
            # Navegar para página de login
            await page.goto(url, wait_until='domcontentloaded')

            # Preencher credenciais
            await page.fill('input[name="txtUsuario"], input[id="txtUsuario"], #usuario', username)
            await page.fill('input[name="pwdSenha"], input[id="pwdSenha"], #senha', password)

            # Selecionar órgão se necessário
            if orgao:
                try:
                    await page.select_option('select[name="selOrgao"], #selOrgao', label=orgao)
                except:
                    pass  # Órgão pode não existir

            # Clicar em login
            await page.click('button[type="submit"], input[type="submit"], #sbmLogin')

            # Aguardar navegação
            await page.wait_for_load_state('networkidle', timeout=10000)

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

        try:
            # Navegar para pesquisa
            await page.click('a:has-text("Pesquisa"), #lnkPesquisar')
            await page.wait_for_load_state('networkidle')

            # Preencher busca
            search_input = await page.query_selector('input[name="txtPesquisa"], #txtPesquisaRapida')
            if search_input:
                await search_input.fill(query)
                await page.keyboard.press('Enter')
                await page.wait_for_load_state('networkidle')

            # Coletar resultados
            results = await page.query_selector_all('tr.processoVisitado, tr.processoNaoVisitado, .processo')

            processes = []
            for result in results[:10]:
                text = await result.inner_text()
                processes.append({"text": text.strip()})

            return {"success": True, "results": processes, "count": len(processes)}

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


# Instância global
playwright_manager = PlaywrightManager()
