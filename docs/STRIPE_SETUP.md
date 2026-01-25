# Configuracao do Stripe Billing - Iudex Licensing API

Este documento descreve como configurar o Stripe para processar pagamentos.

## Planos Disponiveis

| Plano | Preco Mensal | Preco Anual | Requisicoes/mes |
|-------|-------------|-------------|-----------------|
| FREE | R$ 0,00 | R$ 0,00 | 50 |
| PROFESSIONAL | R$ 29,90 | R$ 299,00 | 500 |
| ENTERPRISE | R$ 99,90 | R$ 999,00 | Ilimitado |

## Passo 1: Criar Conta Stripe

1. Acesse https://dashboard.stripe.com/register
2. Complete o cadastro
3. Ative o modo de teste (Test Mode) para desenvolvimento

## Passo 2: Obter API Keys

1. Acesse https://dashboard.stripe.com/test/apikeys
2. Copie:
   - **Publishable key** (pk_test_xxx) - para frontend
   - **Secret key** (sk_test_xxx) - para backend

## Passo 3: Criar Produtos e Precos

### Opcao A: Via Script (Recomendado)

```bash
# No diretorio do projeto
cd /Users/nicholasjacob/Documents/Aplicativos/api-licensing

# Instalar dependencias
pip install stripe python-dotenv

# Executar script com sua API key
python scripts/setup_stripe_products.py --api-key sk_test_SEU_KEY_AQUI
```

### Opcao B: Via Dashboard

1. Acesse https://dashboard.stripe.com/test/products
2. Crie os produtos:

**Produto: Iudex Professional**
- Nome: `Iudex Professional`
- Preco mensal: R$ 29,90 (2990 centavos)
- Preco anual: R$ 299,00 (29900 centavos)

**Produto: Iudex Enterprise**
- Nome: `Iudex Enterprise`
- Preco mensal: R$ 99,90 (9990 centavos)
- Preco anual: R$ 999,00 (99900 centavos)

## Passo 4: Configurar Webhook

1. Acesse https://dashboard.stripe.com/test/webhooks
2. Clique "Add endpoint"
3. Configure:
   - **Endpoint URL**: `https://api.iudex.com.br/api/v1/webhooks/stripe`
   - **Description**: Iudex Licensing API Webhook
   - **Eventos**:
     - `checkout.session.completed`
     - `customer.subscription.created`
     - `customer.subscription.updated`
     - `customer.subscription.deleted`
     - `invoice.paid`
     - `invoice.payment_failed`
4. Copie o **Signing secret** (whsec_xxx)

## Passo 5: Configurar Variaveis de Ambiente

### Desenvolvimento Local (.env)

```env
# Stripe API Keys
STRIPE_SECRET_KEY=sk_test_xxx
STRIPE_PUBLISHABLE_KEY=pk_test_xxx
STRIPE_WEBHOOK_SECRET=whsec_xxx

# Price IDs (substitua pelos IDs reais)
STRIPE_PRICE_PROFESSIONAL_MONTHLY=price_xxx
STRIPE_PRICE_PROFESSIONAL_YEARLY=price_xxx
STRIPE_PRICE_ENTERPRISE_MONTHLY=price_xxx
STRIPE_PRICE_ENTERPRISE_YEARLY=price_xxx
```

### Producao (Render)

Configure as mesmas variaveis no dashboard do Render:
1. Acesse seu servico no Render
2. Va em "Environment"
3. Adicione cada variavel

**IMPORTANTE**: Em producao, use as chaves LIVE (sk_live_xxx, pk_live_xxx)

## Passo 6: Testar Integracao

### Cartoes de Teste

| Numero | Cenario |
|--------|---------|
| 4242 4242 4242 4242 | Pagamento bem-sucedido |
| 4000 0000 0000 0002 | Cartao recusado |
| 4000 0000 0000 3220 | Autenticacao 3D Secure |

Use qualquer data futura e CVC de 3 digitos.

### Testar Webhook Localmente

```bash
# Instalar Stripe CLI (se nao instalado)
brew install stripe/stripe-cli/stripe

# Login
stripe login

# Forward webhooks para localhost
stripe listen --forward-to localhost:8000/api/v1/webhooks/stripe

# Em outro terminal, disparar evento de teste
stripe trigger checkout.session.completed
```

## Variaveis de Ambiente para Render

Copie e configure no Render Dashboard:

```
# =============================================================================
# STRIPE CONFIGURATION
# =============================================================================

# API Keys (obter em https://dashboard.stripe.com/apikeys)
STRIPE_SECRET_KEY=sk_live_xxx
STRIPE_PUBLISHABLE_KEY=pk_live_xxx

# Webhook Secret (obter ao criar webhook em https://dashboard.stripe.com/webhooks)
STRIPE_WEBHOOK_SECRET=whsec_xxx

# Price IDs (criar produtos/precos e copiar IDs)
STRIPE_PRICE_PROFESSIONAL_MONTHLY=price_xxx
STRIPE_PRICE_PROFESSIONAL_YEARLY=price_xxx
STRIPE_PRICE_ENTERPRISE_MONTHLY=price_xxx
STRIPE_PRICE_ENTERPRISE_YEARLY=price_xxx
```

## Troubleshooting

### Erro: "No such price"
- Verifique se os Price IDs estao corretos
- Confirme que os precos foram criados no modo correto (test/live)

### Erro: "Signature verification failed"
- Verifique se STRIPE_WEBHOOK_SECRET esta correto
- O secret muda se voce recriar o webhook

### Pagamento nao aparece
- Confirme que o webhook esta configurado
- Verifique os logs do webhook no Stripe Dashboard

## Links Uteis

- [Dashboard Stripe](https://dashboard.stripe.com)
- [Documentacao API](https://stripe.com/docs/api)
- [Webhooks](https://stripe.com/docs/webhooks)
- [Testing](https://stripe.com/docs/testing)
