# AI Log - API Licensing

## 2026-01-25 - Implementacao de Autenticacao Google OAuth

### Objetivo
Adicionar autenticacao Google OAuth a API de licenciamento para permitir login de usuarios via Google.

### Arquivos Criados
- `app/auth/__init__.py` - Modulo de autenticacao
- `app/auth/jwt.py` - Geracao e validacao de tokens JWT
- `app/auth/google.py` - Configuracao OAuth Google
- `app/auth/dependencies.py` - FastAPI dependencies para autenticacao
- `app/api/endpoints/auth.py` - Endpoints de autenticacao
- `app/models/user.py` - Modelo User para usuarios autenticados
- `migrations/001_create_users_table.sql` - Migracao SQL para tabela users

### Arquivos Alterados
- `requirements.txt` - Adicionado authlib e itsdangerous
- `app/config.py` - Adicionado configuracoes Google OAuth
- `app/models/__init__.py` - Exportando modelo User
- `app/api/endpoints/__init__.py` - Exportando auth_router
- `app/main.py` - Incluindo auth_router

### Endpoints Criados
- `GET /api/v1/auth/google/login` - Inicia fluxo OAuth
- `GET /api/v1/auth/google/callback` - Callback do Google
- `POST /api/v1/auth/refresh` - Refresh token
- `GET /api/v1/auth/me` - Dados do usuario atual
- `POST /api/v1/auth/logout` - Logout (invalida refresh token)

### Decisoes Tomadas
- Usar authlib para OAuth (biblioteca madura e bem documentada)
- Implementar token rotation para refresh tokens (maior seguranca)
- Armazenar hash do refresh token em vez do token em si
- Usar itsdangerous para gerar state CSRF seguro
- Modelo User separado do License (relacionamento por email)

### Proximos Passos
1. Configurar credenciais no Google Cloud Console
2. Adicionar GOOGLE_CLIENT_ID e GOOGLE_CLIENT_SECRET ao .env
3. Executar migracao SQL para criar tabela users
4. Testar fluxo de autenticacao

---

## 2026-01-25 - Configuracao Stripe Completa

### Objetivo
Configurar integracao Stripe com planos de assinatura e webhooks.

### Planos Configurados
| Plano | Preco Mensal | Preco Anual | Requisicoes/Mes |
|-------|--------------|-------------|-----------------|
| FREE | R$ 0 | R$ 0 | 50 |
| PROFESSIONAL | R$ 29,90 | R$ 299,00 | 500 |
| ENTERPRISE | R$ 99,90 | R$ 999,00 | Ilimitado |

### Arquivos Alterados

#### `app/services/stripe_service.py`
- Adicionada classe `PlanConfig` para configuracao de planos
- Adicionado dicionario `PLAN_CONFIGS` com todos os planos
- Adicionado `PLAN_REQUEST_LIMITS` para limites de requisicoes
- Novos metodos:
  - `get_plan_config()` - Retorna configuracao de um plano
  - `get_all_plans()` - Lista todos os planos
  - `get_request_limit()` - Retorna limite de requisicoes
  - `get_customer()` - Busca cliente por ID
  - `update_customer()` - Atualiza dados do cliente
  - `create_checkout_session_for_upgrade()` - Checkout para upgrade
  - `update_subscription_plan()` - Atualiza plano de assinatura
  - `list_customer_subscriptions()` - Lista assinaturas do cliente
  - `reactivate_subscription()` - Reativa assinatura cancelada
  - `pause_subscription()` - Pausa assinatura
  - `resume_subscription()` - Resume assinatura pausada
  - `parse_checkout_session_event()` - Parse de evento checkout
  - `parse_invoice_event()` - Parse de evento invoice
  - `list_invoices()` - Lista faturas do cliente
  - `get_upcoming_invoice()` - Proxima fatura
  - `create_usage_record()` - Registro de uso (para billing metrado)

#### `app/api/endpoints/checkout.py`
- Novo endpoint `POST /checkout/free` - Registrar plano gratuito
- Novo endpoint `GET /checkout/session/{session_id}` - Status da sessao
- Novo endpoint `GET /checkout/prices` - Listar precos do Stripe (debug)
- Atualizado `GET /checkout/plans` - Retorna novos planos com precos
- Atualizado `POST /checkout/create` - Suporte a PIX e Boleto

#### `app/api/endpoints/webhooks.py`
- Adicionado handler `handle_checkout_completed` - Checkout finalizado
- Adicionado handler `handle_invoice_paid` - Fatura paga
- Adicionado handler `handle_invoice_payment_failed` - Falha pagamento
- Adicionado handler `handle_subscription_paused` - Assinatura pausada
- Adicionado handler `handle_subscription_resumed` - Assinatura retomada
- Adicionado endpoint `POST /webhooks/stripe/test` - Teste (dev only)
- Melhorado logging de todos os handlers

#### `app/services/license_service.py`
- Atualizado `PLAN_LIMITS` com limites mensais corretos
- Adicionado `PLAN_PRICES` com precos de referencia

#### `app/services/usage_service.py`
- Atualizado `PLAN_LIMITS` com 500 req/mes para Professional

#### `.env.example`
- Adicionada documentacao completa
- Adicionada `STRIPE_PUBLISHABLE_KEY`
- Documentados Price IDs necessarios

### Webhooks Configurados
1. `checkout.session.completed` - Checkout finalizado
2. `customer.subscription.created` - Nova assinatura
3. `customer.subscription.updated` - Assinatura atualizada
4. `customer.subscription.deleted` - Assinatura cancelada
5. `invoice.paid` - Fatura paga
6. `invoice.payment_failed` - Falha no pagamento

### Proximos Passos para Stripe
1. Criar produtos no Stripe Dashboard
2. Criar precos (Price IDs) para cada plano/intervalo
3. Atualizar `PRICE_IDS` em `stripe_service.py` com IDs reais
4. Configurar webhook endpoint no Stripe Dashboard
5. Copiar Webhook Secret para `.env`
6. Testar fluxo de checkout completo

### Comandos Uteis
```bash
# Testar webhook localmente
stripe listen --forward-to localhost:8000/api/v1/webhooks/stripe

# Disparar evento de teste
stripe trigger checkout.session.completed
```

### Decisoes Tomadas
- Limites por MES (nao por dia) para simplificar
- PIX e Boleto habilitados como metodos de pagamento
- Plano FREE criado sem checkout (direto no banco)
- Trial de 7 dias apenas para Professional
- Enterprise sem trial (cliente deve falar com vendas)

---

## 2026-01-25 - Setup Automatizado Stripe Products

### Objetivo
Criar script automatizado para configurar produtos e precos no Stripe.

### Arquivos Criados
- `scripts/setup_stripe_products.py` - Script para criar produtos/precos no Stripe
- `docs/STRIPE_SETUP.md` - Documentacao completa do setup Stripe

### Arquivos Alterados
- `app/services/stripe_service.py` - Price IDs agora carregam de variaveis de ambiente

### Funcionalidades do Script
1. Cria produtos automaticamente (Professional, Enterprise)
2. Cria precos mensais e anuais para cada plano
3. Usa lookup_keys para identificar precos
4. Verifica produtos/precos existentes antes de criar
5. Gera codigo para atualizar stripe_service.py
6. Lista variaveis de ambiente necessarias

### Variaveis de Ambiente Novas
```env
STRIPE_PRICE_PROFESSIONAL_MONTHLY=price_xxx
STRIPE_PRICE_PROFESSIONAL_YEARLY=price_xxx
STRIPE_PRICE_ENTERPRISE_MONTHLY=price_xxx
STRIPE_PRICE_ENTERPRISE_YEARLY=price_xxx
```

### Como Usar
```bash
# Via argumento
python scripts/setup_stripe_products.py --api-key sk_test_xxx

# Via variavel de ambiente
STRIPE_SECRET_KEY=sk_test_xxx python scripts/setup_stripe_products.py

# Via .env
python scripts/setup_stripe_products.py
```

### Proximos Passos
1. Executar script com API key de teste
2. Configurar webhook no Stripe Dashboard
3. Configurar variaveis no Render
4. Testar checkout com cartao de teste (4242 4242 4242 4242)

---

## 2026-01-26 - Sistema Unificado de API Tokens e Debug Endpoints

### Objetivo
Implementar sistema unificado de API tokens para autenticação de clientes MCP, funcionando tanto para sei-mcp quanto para extensão Chrome.

### Arquivos Alterados

#### `app/models/user.py`
- Adicionado campo `api_token_hash` para armazenar hash do token
- Adicionado campo `api_token_created_at` para timestamp
- Adicionado índice para busca rápida por token

#### `app/api/endpoints/auth.py`
- Novo endpoint `POST /auth/api-token/generate` - Gera novo API token
- Novo endpoint `POST /auth/api-token/revoke` - Revoga API token
- Novo endpoint `POST /auth/api-token/validate` - Valida token (para uso externo)
- Schemas: ApiTokenResponse, ValidateTokenRequest, ValidateTokenResponse

#### `app/api/endpoints/usage.py`
- Adicionada função `get_email_from_token()` para extrair email de Bearer token
- Endpoints `/usage/record` e `/usage/check` agora aceitam Bearer token
- Mantida compatibilidade com email no body (legacy)

#### `app/database.py`
- Adicionada migration 003 para criar colunas api_token_hash e api_token_created_at
- Migration roda automaticamente no startup

#### `app/main.py`
- Adicionado endpoint `GET /debug/db-schema` - Verifica estrutura da tabela
- Adicionado endpoint `POST /debug/run-migration` - Executa migration manualmente

#### `migrations/003_add_api_token.sql`
- Script SQL para adicionar colunas (referência)

### Fluxo de Autenticação por API Token

1. Usuário faz login (Google OAuth ou email/senha)
2. Usuário gera API token via `/auth/api-token/generate`
3. Token é mostrado apenas uma vez (hash é armazenado)
4. Cliente MCP usa token como `Authorization: Bearer <token>`
5. Endpoints de usage validam token e extraem email do usuário

### Integração com sei-mcp

Arquivos atualizados no sei-mcp:
- `src/http/auth.ts` - Funções para validar token e registrar uso via API licensing
- `.env` - Configurado com credenciais do Render

### Configuração Claude Desktop

Duas opções disponíveis:
- `sei-mcp` - Via Render (usa extensão/WebSocket)
- `sei-mcp-local` - Local com Playwright (independente de extensão)

### Problemas em Investigação
- Endpoint `/auth/api-token/validate` retornando 500 error
- Possível causa: migration não executando corretamente no startup
- Debug endpoints adicionados para investigar

### Próximos Passos
1. Verificar se debug endpoints foram deployados
2. Executar `/debug/run-migration` para forçar migration
3. Testar endpoints de API token
4. Atualizar extensão para gerar e usar API tokens
