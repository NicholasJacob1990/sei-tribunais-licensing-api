"""
Resilience Layer — Fail-Fast, Self-Healing, Agent Fallback

Camada de resiliência para automação Playwright do SEI.
- Fail-fast: timeout curto (3s) antes de tentar próximo seletor
- Self-healing: persiste seletores descobertos em JSON
- Agent fallback: Claude API analisa screenshot+DOM quando tudo falha
- Retry: exponential backoff para erros transitórios
"""

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ============================================
# Configuração
# ============================================

FAIL_FAST_TIMEOUT_MS = int(os.environ.get("RESILIENCE_FAIL_FAST_MS", "3000"))
MAX_RETRIES = int(os.environ.get("RESILIENCE_MAX_RETRIES", "2"))
RETRY_BACKOFF_MS = int(os.environ.get("RESILIENCE_RETRY_BACKOFF_MS", "500"))
AGENT_FALLBACK_ENABLED = os.environ.get("AGENT_FALLBACK_ENABLED", "false").lower() == "true"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AGENT_MODEL = os.environ.get("AGENT_FALLBACK_MODEL", "claude-sonnet-4-20250514")

STORE_PATH = Path(os.environ.get(
    "SELECTOR_STORE_PATH",
    os.path.expanduser("~/.sei-mcp/selector-cache.json")
))


# ============================================
# Selector Store (Self-Healing)
# ============================================

class SelectorStore:
    """Persiste seletores CSS descobertos pelo agent para reutilização."""

    def __init__(self, path: Path = STORE_PATH):
        self.path = path
        self._cache: Dict[str, dict] = {}
        self._dirty = False
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                self._cache = json.loads(self.path.read_text())
        except Exception:
            self._cache = {}

    def _save(self):
        if not self._dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._cache, indent=2))
            self._dirty = False
        except Exception:
            pass  # best-effort

    def get(self, key: str) -> Optional[str]:
        entry = self._cache.get(key)
        return entry["selector"] if entry else None

    def set(self, key: str, selector: str):
        now = time.time()
        existing = self._cache.get(key, {})
        self._cache[key] = {
            "selector": selector,
            "discovered_at": existing.get("discovered_at", now),
            "success_count": existing.get("success_count", 0),
            "last_success": now,
        }
        self._dirty = True
        self._save()

    def record_success(self, key: str):
        entry = self._cache.get(key)
        if not entry:
            return
        entry["success_count"] = entry.get("success_count", 0) + 1
        entry["last_success"] = time.time()
        self._dirty = True
        # Debounce save — não salva a cada hit
        # (será salvo no próximo set() ou prune())

    def prune(self, max_age_days: int = 30):
        cutoff = time.time() - (max_age_days * 86400)
        to_remove = [
            k for k, v in self._cache.items()
            if v.get("last_success", 0) < cutoff
        ]
        for k in to_remove:
            del self._cache[k]
        if to_remove:
            self._dirty = True
            self._save()
        return len(to_remove)

    @property
    def size(self) -> int:
        return len(self._cache)


# Instância global
selector_store = SelectorStore()


# ============================================
# Fail-Fast
# ============================================

async def fail_fast(coro, timeout_s: float = FAIL_FAST_TIMEOUT_MS / 1000):
    """Executa coroutine com timeout curto. Levanta TimeoutError se exceder."""
    return await asyncio.wait_for(coro, timeout=timeout_s)


# ============================================
# Smart Helpers — Playwright com resiliência
# ============================================

async def smart_query(page, selectors: str, context: str = "default",
                      timeout_ms: int = FAIL_FAST_TIMEOUT_MS) -> Optional[Any]:
    """
    Tenta encontrar elemento com cascata:
    1. Seletores CSS originais (fail-fast)
    2. Selector do store (self-healing)
    3. Agent fallback (Claude API)

    Args:
        page: Playwright Page
        selectors: CSS selectors separados por vírgula (ex: "#btn, .btn, button")
        context: Chave de contexto para o store (ex: "login:usuario")
        timeout_ms: Timeout fail-fast em ms
    """
    store_key = f"sei|{context}|{selectors[:50]}"
    timeout_s = timeout_ms / 1000

    # 1. Seletores CSS originais (fail-fast)
    try:
        el = await fail_fast(
            page.query_selector(selectors),
            timeout_s
        )
        if el:
            selector_store.record_success(store_key)
            return el
    except (asyncio.TimeoutError, Exception):
        pass

    # Espera curta e tenta com wait_for_selector
    try:
        el = await page.wait_for_selector(selectors, timeout=timeout_ms, state="visible")
        if el:
            selector_store.record_success(store_key)
            return el
    except Exception:
        pass

    # 2. Self-healing: selector do store
    cached = selector_store.get(store_key)
    if cached:
        try:
            el = await page.wait_for_selector(cached, timeout=timeout_ms, state="visible")
            if el:
                selector_store.record_success(store_key)
                logger.info(f"[SELF-HEALING] Usando seletor do cache: {cached}")
                return el
        except Exception:
            pass

    # 3. Agent fallback (se habilitado)
    if AGENT_FALLBACK_ENABLED and ANTHROPIC_API_KEY:
        discovered = await _agent_find_selector(page, selectors, context)
        if discovered:
            try:
                el = await page.query_selector(discovered)
                if el:
                    selector_store.set(store_key, discovered)
                    logger.info(f"[SELF-HEALING] Agent descobriu: {discovered}")
                    return el
            except Exception:
                pass

    return None


async def smart_click(page, selectors: str, context: str = "default",
                      timeout_ms: int = FAIL_FAST_TIMEOUT_MS) -> bool:
    """Clica com cascata de resiliência. Retorna True se conseguiu."""
    el = await smart_query(page, selectors, context, timeout_ms)
    if el:
        await el.click()
        return True
    return False


async def smart_fill(page, selectors: str, value: str, context: str = "default",
                     timeout_ms: int = FAIL_FAST_TIMEOUT_MS) -> bool:
    """Preenche campo com cascata de resiliência. Retorna True se conseguiu."""
    el = await smart_query(page, selectors, context, timeout_ms)
    if el:
        await el.fill(value)
        return True
    return False


async def smart_select(page, selectors: str, label: str = None, value: str = None,
                       context: str = "default",
                       timeout_ms: int = FAIL_FAST_TIMEOUT_MS) -> bool:
    """Seleciona opção com cascata de resiliência."""
    el = await smart_query(page, selectors, context, timeout_ms)
    if el:
        tag = await el.evaluate("el => el.tagName.toLowerCase()")
        if tag == "select":
            if label:
                await page.select_option(selectors, label=label)
            elif value:
                await page.select_option(selectors, value=value)
            return True
        elif tag == "input":
            await el.fill(label or value or "")
            return True
    return False


# ============================================
# Agent Fallback (Claude API)
# ============================================

async def _agent_find_selector(page, original_selectors: str, context: str) -> Optional[str]:
    """Usa Claude API para analisar screenshot + DOM e sugerir seletor."""
    try:
        import anthropic
    except ImportError:
        logger.warning("[AGENT-FALLBACK] anthropic SDK não instalado")
        return None

    try:
        # 1. Screenshot (JPEG, qualidade baixa para economia)
        screenshot_bytes = await page.screenshot(type="jpeg", quality=50)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

        # 2. DOM simplificado (só elementos interativos)
        dom_snapshot = await page.evaluate("""() => {
            const tags = ['INPUT', 'BUTTON', 'SELECT', 'TEXTAREA', 'A', 'LABEL'];
            const elements = [];
            for (const tag of tags) {
                const nodes = document.querySelectorAll(tag);
                for (let i = 0; i < nodes.length; i++) {
                    const el = nodes[i];
                    if (el.offsetParent === null) continue;
                    const attrs = [];
                    for (const a of ['id', 'name', 'class', 'type', 'role', 'aria-label', 'placeholder', 'href', 'value']) {
                        const v = el.getAttribute(a);
                        if (v) attrs.push(a + '="' + v.substring(0, 80) + '"');
                    }
                    const text = (el.textContent || '').trim().substring(0, 60);
                    elements.push('<' + tag.toLowerCase() + ' ' + attrs.join(' ') + (text ? ' text="' + text + '"' : '') + '/>');
                }
            }
            return elements.join('\\n').substring(0, 5000);
        }""")

        # 3. Chamar Claude
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        response = client.messages.create(
            model=AGENT_MODEL,
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": screenshot_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": f"""Você é especialista em automação do sistema SEI (governo brasileiro).

TAREFA: O seletor CSS abaixo não encontrou o elemento. Sugira um seletor alternativo.

SELETOR ORIGINAL (falhou): {original_selectors}
CONTEXTO: {context}

ELEMENTOS INTERATIVOS:
{dom_snapshot}

Retorne APENAS o seletor CSS, sem explicação. Formato: SELECTOR: <seletor>"""
                    },
                ],
            }],
        )

        text = "".join(
            c.text for c in response.content if hasattr(c, "text")
        )

        # 4. Extrair selector
        import re
        match = re.search(r"SELECTOR:\s*(.+)", text, re.IGNORECASE)
        if match:
            selector = match.group(1).strip().strip("\"'`")
            # 5. Validar
            el = await page.query_selector(selector)
            if el:
                return selector

        return None

    except Exception as e:
        logger.warning(f"[AGENT-FALLBACK] Erro: {e}")
        return None


# ============================================
# Agent Fallback Final (para MCP server)
# ============================================

async def create_agent_fallback_response(page, tool_name: str, tool_args: dict,
                                          error: str) -> dict:
    """
    Quando Extension + Playwright falham completamente,
    captura screenshot + ARIA snapshot e retorna para Claude analisar.
    """
    try:
        screenshot_bytes = await page.screenshot(type="png")
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode()

        # ARIA snapshot
        snap_data = await page.accessibility.snapshot(interesting_only=True)
        snap_str = _serialize_aria(snap_data) if snap_data else "(vazio)"
        if len(snap_str) > 10000:
            snap_str = snap_str[:10000] + "\n... [truncado]"

        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({
                        "error": f"Automação falhou para {tool_name}",
                        "original_error": error,
                        "action": tool_name,
                        "args": tool_args,
                        "help": "Analise o screenshot e ARIA tree para identificar o problema e sugerir seletores alternativos.",
                        "_fallback": "agent_analysis",
                    }, indent=2, ensure_ascii=False),
                },
                {
                    "type": "image",
                    "data": screenshot_b64,
                    "mimeType": "image/png",
                },
                {
                    "type": "text",
                    "text": f"ARIA Tree da página atual:\n{snap_str}",
                },
            ],
            "isError": True,
        }
    except Exception as e:
        logger.error(f"[AGENT-FALLBACK] Erro ao criar resposta de fallback: {e}")
        return {
            "content": [{"type": "text", "text": json.dumps({
                "error": f"Automação falhou para {tool_name}: {error}",
                "fallback_error": str(e),
            })}],
            "isError": True,
        }


def _serialize_aria(node: dict, indent: int = 0) -> str:
    lines = []
    prefix = "  " * indent
    role = node.get("role", "")
    name = node.get("name", "")
    if name:
        lines.append(f"{prefix}- {role} \"{name}\"")
    elif role:
        lines.append(f"{prefix}- {role}")
    if node.get("value"):
        lines.append(f'{prefix}  value: "{node["value"]}"')
    for child in node.get("children", []):
        lines.append(_serialize_aria(child, indent + 1))
    return "\n".join(lines)
