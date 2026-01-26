# AI Log - SEI Tribunais Licensing API

## 2026-01-26 - Fix: OAuth e Checkout Validation

### Problemas Resolvidos
1. OAuth Google não funcionava (erro oauth_error)
2. Checkout retornava 422 Unprocessable Content

### Causa Raiz - OAuth
1. SessionMiddleware não estava configurado
2. Estado OAuth armazenado em formato incorreto (authlib esperava dict, recebeu string)
3. authlib `authorize_access_token()` falhava silenciosamente

### Solucao - OAuth
- Adicionar SessionMiddleware ao app (ANTES do CORSMiddleware)
- Implementar troca de token OAuth manualmente com httpx
- Armazenar estado OAuth na sessão: `request.session["_state_google_"] = state`

### Causa Raiz - Checkout
- `CreateCheckoutRequest` aceitava apenas "professional" e "enterprise"
- Extensão Chrome enviava "starter" e "pro"

### Solucao - Checkout
- Atualizar Literal para: `["starter", "pro", "professional", "enterprise"]`

### Arquivos Alterados
- `app/main.py` - SessionMiddleware
- `app/api/endpoints/auth.py` - OAuth flow manual
- `app/auth/google.py` - Funções `exchange_code_for_token()` e `get_google_user_info()`
- `app/api/endpoints/checkout.py` - Plan IDs expandidos

---

## 2026-01-26 - Fix: Conexao DB e Compatibilidade Python 3.13

### Problema
Usuario nao conseguia se registrar - erro "Database temporarily unavailable"

### Causa Raiz
1. Biblioteca `passlib` incompativel com Python 3.13
2. Erro confuso mascarava o problema real (parecia ser conexao DB)

### Solucao
- Substituir `passlib[bcrypt]` por `bcrypt` diretamente
- Implementar `hash_password()` e `verify_password()` usando bcrypt puro

### Arquivos Alterados
- `requirements.txt` - Trocar passlib por bcrypt
- `app/api/endpoints/auth.py` - Reimplementar funcoes de hash
- `app/database.py` - Melhorias em retry de conexao (commits anteriores)

### Token Gerado
Usuario: nicholasjacob90@gmail.com
API Token: `sei_b4d630e6d79cf61845c7adf91ff6291fd5b7a5c62d87b91f5fad2ed13e3f50a3`

---

## 2026-01-26 - Paridade Playwright/Extensao MCP

### Arquivos Alterados
- `app/services/playwright_automation.py` - Adicionadas 10 novas funcoes SEI
- `app/api/endpoints/mcp_server.py` - Roteamento das novas funcoes Playwright

### Analise Realizada
Comparacao entre funcionalidades da Extensao Chrome vs Playwright:

**Antes:**
- Extensao: 50+ acoes (login, documentos, assinatura, tramitacao, blocos, etc.)
- Playwright: 4 acoes (login, search_process, screenshot, get_page_content)

**Depois:**
- Playwright: 14 acoes implementadas

### Novas Funcoes Playwright
1. `open_process` - Abre/navega para processo
2. `list_documents` - Lista documentos do processo
3. `get_status` - Consulta andamento/historico
4. `create_document` - Cria novo documento
5. `sign_document` - Assina documento eletronicamente
6. `forward_process` - Tramita processo
7. `navigate` - Navega para URL
8. `click` - Clica em elemento
9. `fill` - Preenche campo
10. `logout` - Faz logout

### Decisoes Tomadas
- Playwright usa seletores CSS flexiveis (multiplos fallbacks)
- Cada funcao tem tratamento de erro individual
- Mantida compatibilidade com interface MCP existente

### Gap Restante
A extensao ainda tem mais funcionalidades que o Playwright:
- Blocos de assinatura (create_block, sign_block, release_block)
- Upload de documentos
- Anexacao/relacionamento de processos
- Listagem de usuarios/unidades
- Anotacoes e ciencias

### Proximos Passos Sugeridos
1. Implementar funcoes de bloco no Playwright
2. Adicionar upload de documentos
3. Testes de integracao Playwright

---

## 2026-01-26 - Fix: Database Race Conditions e Timezone

### Problema
Extension mostrava "Database temporarily unavailable" intermitentemente

### Causa Raiz
1. Race conditions no database.py (lazy init sem lock adequado)
2. Comparacao de datetime naive vs aware (`datetime.utcnow()` vs campos DB)
3. Modelo `PlanLimits` com campo errado (`operations_per_day` vs `requests_per_month`)

### Solucao
1. Simplificar database.py - usar eager initialization
2. Substituir `datetime.utcnow()` por `datetime.now(timezone.utc)`
3. Corrigir modelo PlanLimits

### Arquivos Alterados
- `app/database.py` - Simplificado (38 linhas vs 185)
- `app/services/license_service.py` - Timezone-aware datetimes
- `app/models/license.py` - Timezone-aware em days_remaining
- `app/api/endpoints/licenses.py` - Campo correto em PlanLimits

### Configuracao Remote MCP
- URL: `https://sei-tribunais-licensing-api.onrender.com`
- OAuth: `/oauth/authorize`, `/oauth/token`, `/oauth/register`
- API Token: `sei_b4d630e6d79cf61845c7adf91ff6291fd5b7a5c62d87b91f5fad2ed13e3f50a3`
