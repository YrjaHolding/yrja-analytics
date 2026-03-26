"""Notion sync — fetch and write product data to Yrja's Prisoversikt database.

Requires NOTION_API_KEY environment variable (Notion internal integration token).
Database: "Pris og volum per produkt" (30db4d1b-8dbf-80f9-b805-e9bcd12e6192)

Usage:
    # As a library (called automatically by products.get_products()):
    export NOTION_API_KEY=secret_...
    uv run python -c "from products import get_products; print(get_products())"

    # Standalone — prints fetched products:
    uv run python notion_sync.py
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from dotenv import load_dotenv
from products import Product

# Load .env from project root
load_dotenv(Path(__file__).resolve().parent / ".env")


# ── Notion API config ────────────────────────────────────────────────

DATABASE_ID = "30db4d1b-8dbf-80f9-b805-e9bcd12e6192"
DB_QUERY_URL = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
PAGE_URL = "https://api.notion.com/v1/pages"  # + /{page_id} for PATCH
NOTION_VERSION = "2022-06-28"

# Default nominal weight for products missing f-pack weight (kg).
DEFAULT_WEIGHT_KG = 0.50


@dataclass
class NotionRow:
    """A product with its Notion page ID for write-back."""
    product: Product
    page_id: str


# ── Notion property helpers ──────────────────────────────────────────────────


def _get_title(props: dict, key: str) -> str:
    """Extract plain text from a Notion title property."""
    arr = props.get(key, {}).get("title", [])
    return "".join(item.get("plain_text", "") for item in arr).strip()


def _get_text(props: dict, key: str) -> str:
    """Extract plain text from a Notion rich_text property."""
    arr = props.get(key, {}).get("rich_text", [])
    return "".join(item.get("plain_text", "") for item in arr).strip()


def _get_number(props: dict, key: str) -> float | None:
    """Extract a number from a Notion number property."""
    return props.get(key, {}).get("number")


def _get_select(props: dict, key: str) -> str | None:
    """Extract the selected option name from a Notion select property."""
    sel = props.get(key, {}).get("select")
    return sel.get("name") if sel else None


def _get_url(props: dict, key: str) -> str:
    """Extract a URL from a Notion url property."""
    return props.get(key, {}).get("url") or ""


# ── Row parsing ──────────────────────────────────────────────────────────────


def _normalize_name(name: str, producer: str) -> str:
    """Fix typos and disambiguate duplicate product names across producers."""
    # "Kyllling" (3 l's) → "Kylling"
    if name.startswith("Kyllling"):
        name = "Kylling" + name[8:]

    # "Kjøttdeig" appears under both Opaker and Stølsvidda
    if name.lower() == "kjøttdeig":
        if producer == "Opaker":
            return "Kjøttdeig Storfe"
        elif producer == "Stølsvidda":
            return "Kjøttdeig Svin"

    return name


def _row_to_product(row: dict) -> NotionRow | None:
    """Convert a Notion database row into a NotionRow, or None if incomplete."""
    page_id = row.get("id", "")
    props = row.get("properties", {})

    name = _get_title(props, "Produktnavn")
    producer = _get_text(props, "Produsent")
    if not name or not producer:
        return None

    # kr/kg: prefer "Utpris", fall back to legacy "Enhetspris"
    utpris = _get_number(props, "Utpris") or _get_number(props, "Enhetspris")
    if utpris is None or utpris <= 0:
        return None  # Can't price without kr/kg

    name = _normalize_name(name, producer)

    f_pack = _get_number(props, "f-pack vekt (kg)")
    stykkpris = _get_number(props, "stykkpris")
    innpris = _get_number(props, "Innpris")
    unit = _get_text(props, "Enhet") or "kg"
    image_url = _get_url(props, "Bilde URL")
    shopify = _get_select(props, "Shopify public")
    fri_fpack = _get_select(props, "Fri fpack vekt")
    espen_antall = _get_number(props, "Espen - antall enheter")

    # Derive weight: f-pack → stykkpris/utpris → default
    if f_pack and f_pack > 0:
        weight = f_pack
    elif stykkpris and utpris > 0:
        weight = round(stykkpris / utpris, 2)
    else:
        weight = DEFAULT_WEIGHT_KG

    # Derive stykkpris if missing
    if stykkpris is None or stykkpris <= 0:
        stykkpris = round(weight * utpris, 2)

    # Purchase price: use Innpris if set, otherwise derive from retail
    if innpris and innpris > 0:
        purchase_price = round(innpris * weight, 2)
    else:
        purchase_price = round(stykkpris / 1.28, 2)

    is_kjottdeig = "kjøttdeig" in name.lower()
    # "Fri fpack vekt" == "1" means adjustable; fall back to producer check
    is_adjustable = fri_fpack == "1" if fri_fpack is not None else False

    product = Product(
        name=name,
        producer=producer,
        f_pack_weight_kg=weight,
        retail_price=stykkpris,
        retail_price_per_kg=utpris,
        purchase_price=purchase_price,
        unit=unit,
        image_url=image_url,
        fixed_weight=is_kjottdeig,
        adjustable_size=is_adjustable,
        shopify_visible=(shopify == "Ja"),
        innpris_per_kg=innpris if innpris and innpris > 0 else None,
        espen_n_units=int(espen_antall) if fri_fpack == "0" and espen_antall and espen_antall > 0 else None,
    )
    return NotionRow(product=product, page_id=page_id)


# ── Public API ───────────────────────────────────────────────────────────────


def _get_notion_headers(api_key: str | None = None) -> dict[str, str]:
    """Build Notion API headers, resolving the API key."""
    key = api_key or os.environ.get("NOTION_API_KEY")
    if not key:
        raise ValueError(
            "Notion API key required. Set NOTION_API_KEY environment variable "
            "or pass api_key argument."
        )
    return {
        "Authorization": f"Bearer {key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def fetch_rows_from_notion(api_key: str | None = None) -> list[NotionRow]:
    """Fetch all products with their Notion page IDs."""
    headers = _get_notion_headers(api_key)

    raw_rows: list[dict] = []
    start_cursor: str | None = None

    while True:
        body: dict = {}
        if start_cursor:
            body["start_cursor"] = start_cursor

        resp = requests.post(DB_QUERY_URL, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        raw_rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    rows: list[NotionRow] = []
    for raw in raw_rows:
        nr = _row_to_product(raw)
        if nr:
            rows.append(nr)

    rows.sort(key=lambda r: (r.product.producer, r.product.name))
    return rows


def fetch_products_from_notion(api_key: str | None = None) -> list[Product]:
    """Fetch all products from the Notion Prisoversikt database.

    Returns:
        List of Product instances, sorted by (producer, name).
    """
    return [r.product for r in fetch_rows_from_notion(api_key)]


# ── Write-back ───────────────────────────────────────────────────────────────


def write_slot_results(
    results: dict[str, dict],
    rows: list[NotionRow],
    api_key: str | None = None,
) -> int:
    """Write optimizer SLOT results back to Notion.

    Args:
        results: {product_name: {"n_units": int, "unit_weight": float, "total_weight": float}}
        rows: NotionRows from fetch_rows_from_notion (provides page IDs).
        api_key: Notion API key override.

    Returns:
        Number of rows updated.
    """
    headers = _get_notion_headers(api_key)
    page_ids = {r.product.name: r.page_id for r in rows}
    updated = 0

    for name, slot in results.items():
        page_id = page_ids.get(name)
        if not page_id:
            continue

        now_oslo = datetime.now(ZoneInfo("Europe/Oslo"))
        today = now_oslo.strftime("%Y-%m-%d")
        slot_value = round(slot["n_units"] * slot["unit_weight"] * slot["price_per_kg"], 2)
        properties: dict = {
            "SLOT: antall enheter": {"number": int(slot["n_units"])},
            "SLOT: f-pack kg": {"number": round(slot["unit_weight"], 2)},
            "SLOT: tot slot vekt": {"number": round(slot["total_weight"], 2)},
            "SLOT: verdi": {"number": slot_value},
            "SLOT: last updated": {"date": {"start": today}},
        }

        resp = requests.patch(
            f"{PAGE_URL}/{page_id}",
            headers=headers,
            json={"properties": properties},
            timeout=60,
        )
        if not resp.ok:
            print(f"  ❌ Notion error for {name}: {resp.status_code} {resp.text}")
        resp.raise_for_status()
        updated += 1
        # time.sleep(0.35)  # Notion rate limit: ~3 req/s

    return updated


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    products = fetch_products_from_notion()
    print(f"Fetched {len(products)} products from Notion:\n")
    fmt = "{:<22s} {:<14s} {:>6s} {:>8s} {:>8s}  {}"
    print(
        fmt.format("Product", "Producer", "Wt(kg)", "kr/kg", "Stykk", "Shopify")
    )
    print("-" * 80)
    for p in products:
        print(
            fmt.format(
                p.name,
                p.producer,
                f"{p.f_pack_weight_kg:.2f}",
                f"{p.retail_price_per_kg:.0f}",
                f"{p.retail_price:.0f}",
                "✓" if p.shopify_visible else "",
            )
        )
