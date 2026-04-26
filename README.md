# yrja-analytics

Streamlit dashboard + scripts for Yrja's box-subscription economics and
operations.

## Setup

```bash
uv sync
cp .env.example .env
# fill in NOTION_API_KEY, SHOPIFY_SHOP_DOMAIN, SHOPIFY_ACCESS_TOKEN, …
```

Run the dashboard:

```bash
uv run streamlit run app.py
```

## Gordon Delivery exporter

`gordon_exporter.py` pulls unfulfilled Shopify orders and forwards them to
Gordon Delivery's `POST /api/orders/bulk` endpoint.

### Endpoint

We currently POST to:

```
https://backend.aws.gordondelivery.com/api/orders/bulk?deliverygroup=Yrja
```

Gordon staging was provisioned for Yrja behind that AWS-fronted host. The
standard `--env test` URL (`backend.staging.gordondelivery.com`) is still
supported and remains the documented "test mode" URL on Gordon's side, but
for day-to-day use we pass the AWS host explicitly via `--base-url`.

### Authentication

OAuth2 `client_credentials`: `gordon_client.py` exchanges your Client ID +
Secret for a JWT at `/oauth/token` and sends it as `Authorization: Bearer`
on every subsequent call. Tokens are cached until ~60s before their stated
expiry. Get credentials from GLMP → Account → API Credentials
(`https://lastmile.gordondelivery.com/account/<account_id>/api-credentials`).

### Env vars

```
GORDON_ENV=test                       # 'test' (staging URL) or 'production'
GORDON_CLIENT_ID=…
GORDON_CLIENT_SECRET=…
GORDON_DELIVERY_GROUP=Yrja            # optional, also sent as ?deliverygroup=…
GORDON_PRODUCTION_BASE_URL=…          # only needed when GORDON_ENV=production
```

### CLI flags

All flags on `gordon_exporter`:

- `--base-url URL` — override the Gordon base URL (e.g.
  `https://backend.aws.gordondelivery.com`). Takes precedence over `--env`.
- `--env {test, production}` — picks staging vs prod URL when `--base-url`
  is not set. Defaults to `test`.
- `--date YYYY-MM-DD` — `deliverydate` Gordon uses. Required unless `--dry-run`.
- `--window "HH:mm - HH:mm"` — `time-window` field. Required unless `--dry-run`.
- `--delivery-group NAME` — Gordon delivery group name (overrides env).
- `--inventory-type {ambient, chilled, frozen}` — temperature zone applied to
  every inventory entry. Defaults to **frozen** (Yrja ships frozen).
- `--name "#1040"` — only the order with this Shopify name.
- `--include-orders "#1040"` — allow-list of orders to export. Repeatable.
- `--exclude "#1040"` — skip a specific order. Repeatable.
- `--internal-orders "#1035"` — treat the order as a flat internal/bulk order
  (one inventory entry named `"Internal order"` with every line item as an
  article). Repeatable.
- `--status {unfulfilled, partial, fulfilled, any}` — Shopify fulfillment
  status filter. Default `unfulfilled`.
- `--since YYYY-MM-DD` / `--until YYYY-MM-DD` — created-at range.
- `--tag-exclude TAG` — Shopify tag to exclude. Default `Test`.
- `--limit N` — stop after N orders (debug).
- `--dry-run` — build payloads, print as JSON, no network call to Gordon.
- `--test-auth` — only exchange credentials for a Gordon JWT and report.
- `--verbose` / `-v`.

### Example workflows

**Sanity-check credentials before sending anything:**

```bash
uv run python -m gordon_exporter \
  --base-url "https://backend.aws.gordondelivery.com" --test-auth
```

**Dry-run a batch** (prints the JSON the exporter would POST):

```bash
uv run python -m gordon_exporter \
  --base-url "https://backend.aws.gordondelivery.com" \
  --date 2026-05-03 --window "08:00 - 22:00" --delivery-group "Yrja" \
  --internal-orders "#1035" --dry-run
```

**Re-send a specific subset of orders with a new delivery date:**

```bash
uv run python -m gordon_exporter \
  --base-url "https://backend.aws.gordondelivery.com" \
  --date 2026-05-03 --window "08:00 - 22:00" --delivery-group "Yrja" \
  --internal-orders "#1035" \
  --include-orders "#1019" --include-orders "#1026" \
  --include-orders "#1029" --include-orders "#1032" \
  --include-orders "#1035" --include-orders "#1037" \
  --include-orders "#1038"
```

**One-off send for a single order to a different date:**

```bash
uv run python -m gordon_exporter \
  --base-url "https://backend.aws.gordondelivery.com" \
  --date 2026-05-02 --window "08:00 - 22:00" --delivery-group "Yrja" \
  --include-orders "#1032"
```

### Outgoing request format

Each order in the bulk array looks like:

```json
{
  "external_ref": "10001040",
  "customer-name": "Sander Grønli Nordeide",
  "address": "Eindrides vei 3",
  "zip": "0575",
  "city": "Oslo",
  "deliverydate": "2026-05-03",
  "time-window": "08:00 - 22:00",
  "email": "nordeidesander@gmail.com",
  "mobile": "+4747352127",
  "country_code": "NO",
  "deliverygroup": "Yrja",
  "inventory": [
    {
      "name": "Råvareboks - 4",
      "quantity": 1,
      "type": "frozen",
      "articles": [
        { "name": "Svin Kjøttdeig",    "quantity": 4 },
        { "name": "Kylling, wokkjøtt", "quantity": 2 },
        { "name": "Nakkekoteletter",   "quantity": 2 },
        { "name": "Laks",              "quantity": 1 }
      ]
    }
  ]
}
```

Field mapping rules:

- `external_ref` = `"1000" + Shopify order number` (digits only, no `#`).
- `mobile` = E.164 format (`_to_e164` in `gordon_exporter.py`). Adds the
  country dial code based on `shipping_address.country_code`.
- `notes` = first non-empty value from a Shopify custom attribute matching
  the Norwegian "Leilighet, etasje osv. (valgfritt)" label, falling back to
  `shipping_address.address2`.
- `inventory[*].name` = bundle line-item title (e.g. `"Råvareboks - 4"`).
  Internal orders use the literal string `"Internal order"`.
- `inventory[*].quantity` = always `1` (one physical box).
- `inventory[*].type` = from `--inventory-type` (default `frozen`).
- `inventory[*].articles[*].quantity` = Shopify-ordered slot count × the
  variant's `custom.slot_antall_enheter` metafield (synced from Notion's
  "SLOT: antall enheter"). Same rule for both bundle and internal orders.

### Real Gordon responses we've seen

**Successful bulk create on the AWS-fronted staging host
(`POST /api/orders/bulk?deliverygroup=Yrja`):**

```json
[
  { "external_ref": "10001038", "tracking_id": "tLk4mNR1D", "status": "OK",
    "depot": "gordon oslo", "orderId": "69ee870996b45b84098bda59",
    "deliveryDate": "2026-05-03", "labelReadyToPrint": false },
  { "external_ref": "10001037", "tracking_id": "b9wU9lbgw", "status": "OK",
    "depot": "gordon oslo", "orderId": "69ee870996b45b84098bdaaf",
    "deliveryDate": "2026-05-03", "labelReadyToPrint": false },
  { "external_ref": "10001035", "tracking_id": "1Wm6gKxTz", "status": "OK",
    "depot": "gordon oslo", "orderId": "69ee870996b45b84098bda7a",
    "deliveryDate": "2026-05-03", "labelReadyToPrint": false }
]
```

The exporter logs each chunk as `Chunk N/M: K orders accepted` with the
full Gordon response inline so you can grep the tracking IDs later.

**Per-order error inside an otherwise-successful bulk** — Gordon returns
200 OK on the HTTP call but flags the bad row in-line:

```json
{
  "external_ref": "10001032",
  "status": "error",
  "message": "Address is not within your area or not available selected weekday, please contact Gordon for more information"
}
```

The exporter prints these and continues; re-send that one order with a new
day using `--include-orders`.

**OAuth failure** (wrong credentials / wrong tenant):

```
HTTP/1.1 401 Unauthorized
{"error":"access_denied","error_description":"Unauthorized"}
```

If you see this, run `--test-auth` for a clearer hint, and verify Client ID
/ Secret in `.env` against the **staging** GLMP — production credentials
from `lastmile.gordondelivery.com` will not authenticate against the staging
backend.

### Other notes

- `time-window` is sent on every order (Gordon's schema requires it).
- Shopify orders tagged `Test` are excluded by default (`--tag-exclude Test`).
- Bundle line items (Råvareboks) are expanded into article rows using
  `_pvgid://shopify/ProductVariant/…` custom attributes.
- On a chunk-level 400 (e.g. `External Ref in Use`), the exporter falls back
  to per-order POSTs so one bad row doesn't poison the whole batch.
- Underlying Shopify GraphQL query lives in `shopify_order_queries.py`; the
  data model is in `shopify_order_models.py`. Both are vendored from
  `yrja-fulfilment-analytics` so this repo can deploy standalone.

## Order CSV helper

A quick helper that dumps order name, ZIP, phone (E.164), and the Shopify
admin URL for every unfulfilled order. Useful when the ops team needs to
look customers up after a Gordon-side error.

Output is written to stdout as CSV — redirect to a file:

```bash
uv run python /tmp/orders_csv.py > exports/orders.csv
```

Columns: `order_name`, `zip`, `phone`, `shopify_admin_url`. URL format is
`https://admin.shopify.com/store/<handle>/orders/<numeric_id>`, where the
store handle comes from the part of `SHOPIFY_SHOP_DOMAIN` before
`.myshopify.com`.
