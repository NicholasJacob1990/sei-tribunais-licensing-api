#!/usr/bin/env python3
"""
Script para criar produtos e precos no Stripe para o Iudex Licensing API.

Execute com:
    python scripts/setup_stripe_products.py --api-key sk_test_xxx

Ou via variavel de ambiente:
    STRIPE_SECRET_KEY=sk_test_xxx python scripts/setup_stripe_products.py

Ou configure no .env:
    python scripts/setup_stripe_products.py
"""
import argparse
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import stripe
from dotenv import load_dotenv

# Parse arguments
parser = argparse.ArgumentParser(description="Setup Stripe products and prices")
parser.add_argument("--api-key", "-k", help="Stripe API key (sk_test_xxx ou sk_live_xxx)")
args = parser.parse_args()

# Load .env if exists
load_dotenv()

# Get API key from argument, environment, or .env
STRIPE_SECRET_KEY = args.api_key or os.getenv("STRIPE_SECRET_KEY", "")

if not STRIPE_SECRET_KEY or STRIPE_SECRET_KEY == "sk_test_xxx":
    print("=" * 60)
    print("ERRO: STRIPE_SECRET_KEY nao configurada!")
    print("=" * 60)
    print()
    print("Configure de uma das formas:")
    print("1. Variavel de ambiente: STRIPE_SECRET_KEY=sk_test_xxx python scripts/setup_stripe_products.py")
    print("2. Arquivo .env: STRIPE_SECRET_KEY=sk_test_xxx")
    print()
    print("Obtenha sua chave em: https://dashboard.stripe.com/test/apikeys")
    sys.exit(1)

stripe.api_key = STRIPE_SECRET_KEY

# Verificar se esta em modo teste
is_test = STRIPE_SECRET_KEY.startswith("sk_test_")
mode = "TESTE" if is_test else "PRODUCAO"

print("=" * 60)
print(f"CONFIGURANDO STRIPE BILLING - MODO {mode}")
print("=" * 60)
print()

# ============================================================================
# CONFIGURACAO DOS PLANOS
# ============================================================================

PLANS = {
    "professional": {
        "name": "Iudex Professional",
        "description": "Plano Professional - 500 requisicoes/mes com suporte prioritario",
        "features": [
            "500 requisicoes/mes",
            "Acesso completo a API",
            "Suporte prioritario",
            "Webhooks personalizados",
            "Dashboard de uso",
        ],
        "prices": {
            "monthly": {
                "amount": 2990,  # R$ 29,90
                "interval": "month",
            },
            "yearly": {
                "amount": 29900,  # R$ 299,00 (2 meses gratis)
                "interval": "year",
            },
        },
    },
    "enterprise": {
        "name": "Iudex Enterprise",
        "description": "Plano Enterprise - Requisicoes ilimitadas com suporte dedicado",
        "features": [
            "Requisicoes ilimitadas",
            "Acesso completo a API",
            "Suporte dedicado 24/7",
            "Webhooks personalizados",
            "Dashboard avancado",
            "API dedicada",
            "SLA garantido",
            "Integracao customizada",
        ],
        "prices": {
            "monthly": {
                "amount": 9990,  # R$ 99,90
                "interval": "month",
            },
            "yearly": {
                "amount": 99900,  # R$ 999,00 (2 meses gratis)
                "interval": "year",
            },
        },
    },
}

# Armazenar IDs criados
created_products = {}
created_prices = {}


def create_product(plan_id: str, plan_config: dict) -> stripe.Product:
    """Cria um produto no Stripe."""
    print(f"\nCriando produto: {plan_config['name']}...")

    # Verificar se produto ja existe
    existing = stripe.Product.search(query=f'name:"{plan_config["name"]}"')
    if existing.data:
        product = existing.data[0]
        print(f"  -> Produto ja existe: {product.id}")
        return product

    product = stripe.Product.create(
        name=plan_config["name"],
        description=plan_config["description"],
        metadata={
            "plan_id": plan_id,
            "features": ", ".join(plan_config["features"]),
        },
        default_price_data=None,  # Criaremos os precos separadamente
    )
    print(f"  -> Produto criado: {product.id}")
    return product


def create_price(
    product: stripe.Product,
    plan_id: str,
    interval: str,
    amount: int,
    recurring_interval: str,
) -> stripe.Price:
    """Cria um preco no Stripe."""
    lookup_key = f"{plan_id}_{interval}"
    print(f"  Criando preco: {lookup_key} (R$ {amount/100:.2f}/{recurring_interval})...")

    # Verificar se preco ja existe com essa lookup_key
    existing = stripe.Price.search(query=f'lookup_key:"{lookup_key}"')
    if existing.data:
        price = existing.data[0]
        print(f"    -> Preco ja existe: {price.id}")
        return price

    price = stripe.Price.create(
        product=product.id,
        unit_amount=amount,
        currency="brl",
        recurring={"interval": recurring_interval},
        lookup_key=lookup_key,
        metadata={
            "plan_id": plan_id,
            "interval": interval,
        },
    )
    print(f"    -> Preco criado: {price.id}")
    return price


def main():
    """Funcao principal."""
    print("Iniciando criacao de produtos e precos...\n")

    # Criar produtos e precos
    for plan_id, plan_config in PLANS.items():
        # Criar produto
        product = create_product(plan_id, plan_config)
        created_products[plan_id] = product.id

        # Criar precos
        for interval, price_config in plan_config["prices"].items():
            price = create_price(
                product=product,
                plan_id=plan_id,
                interval=interval,
                amount=price_config["amount"],
                recurring_interval=price_config["interval"],
            )
            key = f"{plan_id}_{interval}"
            created_prices[key] = price.id

    # Exibir resumo
    print("\n" + "=" * 60)
    print("CONFIGURACAO CONCLUIDA!")
    print("=" * 60)

    print("\n--- PRODUTOS CRIADOS ---")
    for plan_id, product_id in created_products.items():
        print(f"  {plan_id}: {product_id}")

    print("\n--- PRECOS CRIADOS ---")
    for key, price_id in created_prices.items():
        print(f"  {key}: {price_id}")

    # Gerar codigo para atualizar stripe_service.py
    print("\n" + "=" * 60)
    print("ATUALIZE app/services/stripe_service.py")
    print("=" * 60)
    print("""
Substitua a secao PRICE_IDS por:

PRICE_IDS: dict[str, dict[str, str]] = {
    "default": {""")
    print(f'        "professional_monthly": "{created_prices.get("professional_monthly", "price_xxx")}",')
    print(f'        "professional_yearly": "{created_prices.get("professional_yearly", "price_xxx")}",')
    print(f'        "enterprise_monthly": "{created_prices.get("enterprise_monthly", "price_xxx")}",')
    print(f'        "enterprise_yearly": "{created_prices.get("enterprise_yearly", "price_xxx")}",')
    print("""    },
}
""")

    # Gerar variaveis de ambiente
    print("=" * 60)
    print("VARIAVEIS DE AMBIENTE PARA RENDER")
    print("=" * 60)
    print(f"""
# Stripe API Keys (obtenha em https://dashboard.stripe.com/test/apikeys)
STRIPE_SECRET_KEY={STRIPE_SECRET_KEY}
STRIPE_PUBLISHABLE_KEY=pk_test_XXX  # Substitua pela sua chave publicavel

# Stripe Webhook Secret (crie webhook em https://dashboard.stripe.com/test/webhooks)
# Endpoint: https://api.iudex.com.br/api/v1/webhooks/stripe
# Eventos: checkout.session.completed, customer.subscription.created,
#          customer.subscription.updated, customer.subscription.deleted,
#          invoice.paid, invoice.payment_failed
STRIPE_WEBHOOK_SECRET=whsec_XXX

# Price IDs criados
STRIPE_PRICE_PROFESSIONAL_MONTHLY={created_prices.get("professional_monthly", "price_xxx")}
STRIPE_PRICE_PROFESSIONAL_YEARLY={created_prices.get("professional_yearly", "price_xxx")}
STRIPE_PRICE_ENTERPRISE_MONTHLY={created_prices.get("enterprise_monthly", "price_xxx")}
STRIPE_PRICE_ENTERPRISE_YEARLY={created_prices.get("enterprise_yearly", "price_xxx")}
""")

    print("=" * 60)
    print("PROXIMOS PASSOS")
    print("=" * 60)
    print("""
1. Atualize PRICE_IDS em app/services/stripe_service.py com os IDs acima
2. Configure webhook no Stripe Dashboard:
   - URL: https://api.iudex.com.br/api/v1/webhooks/stripe
   - Eventos: checkout.session.completed, customer.subscription.*,
              invoice.paid, invoice.payment_failed
3. Copie o webhook secret (whsec_xxx) para STRIPE_WEBHOOK_SECRET
4. Configure variaveis no Render
5. Teste o checkout com cartao de teste: 4242 4242 4242 4242
""")


if __name__ == "__main__":
    main()
