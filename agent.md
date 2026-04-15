# yrja-analytics

## Overview
Internal financial analytics dashboard and tooling for **Yrja** — a Norwegian meat/fish subscription box company. The product catalog lives in Notion; prices and metafields are enriched from Shopify. The main UI is a Streamlit dashboard (`app.py`).

## Tech stack
- **Python 3.13**, managed with **uv** (`pyproject.toml` + `uv.lock`)
- **Streamlit** — dashboard UI
- **Pandas / NumPy** — data wrangling and Monte Carlo simulation
- **Plotly** — charts
- **Pyomo + HiGHS** — MILP slot-value optimizer
- **httpx / requests** — Notion REST API + Shopify GraphQL Admin API
- **python-dotenv** — secrets from `.env`

## Running
```bash
# Install deps
uv sync

# Start the dashboard
uv run streamlit run app.py

# Run the slot optimizer → write results to Notion
uv run python sync_slots.py            # one-shot
uv run python sync_slots.py --watch    # poll mode

# New-customer Slack alerts
uv run python new_customer_alerts.py        # real
uv run python new_customer_alerts.py --dry  # preview
```

## Environment variables (see `.env.example`)
- `NOTION_API_KEY` — Notion integration token
- `NOTION_PRISOVERSIKT_DATABASE_ID` — Notion database ID for the product table
- `SHOPIFY_SHOP_DOMAIN` / `SHOPIFY_ACCESS_TOKEN` — Shopify Admin API
- `SLACK_WEBHOOK_URL` — Slack incoming webhook for new-customer alerts
- `APP_PASSWORD` *(optional)* — password-gate the Streamlit dashboard

## Key modules

### `app.py` (~900 lines)
Streamlit dashboard with four tabs:
1. **Dashboard** — Product table (from Notion + Shopify), subscription tiers (4/6/8 slots), single-box random simulation with cost breakdown.
2. **Unit Economics** — Monte Carlo simulation across many random box configurations; histograms of COGS, margin, driftsresultat.
3. **Pris benchmarking** — Compare Yrja prices against ODA/AMOI competitors (kr/kg).
4. **Forretningshelse** — Business health metrics.

Sidebar controls subscription prices and operational cost parameters (warehouse, distribution, packaging, Shopify Payments, Skio).

### `products.py`
`Product` dataclass and a hardcoded fallback catalog. Key fields: `f_pack_weight_kg`, `retail_price_per_kg`, `innpris_per_kg`, `purchase_price`, `adjustable_size`, `shopify_visible`. Derives margin and purchase-price-per-kg.

### `notion_sync.py`
Reads/writes the Notion "Prisoversikt" database. Parses rows into `Product` objects, handles name normalization (typos, disambiguating duplicate names across producers). Writes optimized SLOT columns back to Notion.

### `shopify_client.py`
`ShopifyClient` — GraphQL client for the Shopify Admin API. Handles pagination and throttle-retry. Fetches:
- Product variant metafields (price/kg, price/portion, slot configs)
- Recent orders (for new-customer detection)
- Shop-level metafields (unique customer count)

Data models: `VariantMetafields`, `Order`, `OrderLineItem`.

### `optimize_weights.py`
Slot-value optimizer. Goal: make every product's "slot value" (n_units × unit_weight × price_per_kg) as equal as possible across the catalog.

Two strategies:
1. **Anchor-based** — uses "critical products" (where only 1 unit fits per slot) as anchors, then optimizes around them.
2. **MILP fallback** — Pyomo model minimizing max deviation from a target slot value.

Output: `OptimizerOutput` with per-product `SlotResult` (n_units, unit_weight, total_weight, slot_value).

### `simulator.py`
Monte Carlo weight-variation engine. Simulates actual package weights (Normal distribution, μ ≈ 1.052 × nominal, σ = 5%) and computes per-box COGS, margin, and under-weight probability.

### `sync_slots.py`
CLI tool that fetches products from Notion → runs the optimizer → writes SLOT columns back. Supports `--watch` mode with change-detection fingerprinting.

### `new_customer_alerts.py`
Fetches recent Shopify orders, identifies first-time buyers, and posts a Slack notification. Tracks already-notified orders in `.notified_orders.json`.

### `delivery_zones/`
Jupyter notebook for delivery zone analysis (separate from the main app).

## Domain glossary
- **Slot** — one product position in a subscription box (a box has 4/6/8 slots)
- **f-pack** — a single frozen package unit from a producer
- **Innpris** — purchase price (what Yrja pays producers), in kr/kg
- **Utpris** — retail/customer-facing price, in kr/kg
- **Stykkpris** — retail price per unit (weight × utpris)
- **MVA** — Norwegian VAT (15% for food)
- **Dekningsbidrag** — contribution margin (revenue ex-MVA minus COGS)
- **Driftsresultat** — operating result (dekningsbidrag minus ops costs)
- **Fri fpack vekt** — flag: producer can adjust package weight (Stølsvidda, Opaker)

## Conventions
- All monetary values are in NOK (kr).
- Notion is the source of truth for the product catalog.
- Shopify metafields enrich with customer-facing prices and slot configs.
- The optimizer targets equal slot value across all Shopify-visible products.
- `.env` is gitignored; copy `.env.example` and fill in secrets.
