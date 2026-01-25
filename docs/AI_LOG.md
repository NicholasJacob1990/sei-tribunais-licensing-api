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
