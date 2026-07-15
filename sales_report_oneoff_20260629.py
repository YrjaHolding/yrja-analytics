"""One-off all-time sales report for Yrja (store ``yrja-2``).

Answers two questions over the *entire* order history of the store:

1. How many units of a single Shopify product have been sold forever
   (default: ``10398485971256`` = "Kylling, hel, økologisk, 2 kg").
2. How many items each *producer* has sold, plus a per-order listing.

It deliberately routes through the **order-exporter** Shopify app installed on
``yrja-2`` (the app that holds the ``read_all_orders`` scope needed to read the
full history — Shopify only exposes the last 60 days otherwise). The
order-exporter integration lives in the sibling ``yrja-fulfilment-analytics``
repo; this script imports that app's GraphQL client + token minting so we are
genuinely calling the same app.

Auth resolution (verbose, logged at runtime):
  1. If ``SHOPIFY_CLIENT_ID`` + ``SHOPIFY_CLIENT_SECRET`` are configured (the
     order-exporter custom app's API credentials), mint a short-lived token via
     the OAuth ``client_credentials`` grant — this is the app talking.
  2. Otherwise (or if the mint fails) fall back to a static
     ``SHOPIFY_ACCESS_TOKEN`` (the long-lived order-exporter token).

The Yrja box model (important for correct counting):
  * A box SKU ("Råvareboks - N", "Yrjas bestselger …") is the *priced* line item
    (``vendor = "Yrja"``) and carries ``_pvgid://shopify/ProductVariant/<id>``
    custom attributes naming the variants + quantities inside it.
  * The individual products inside a box appear as **zero-priced**
    (``discountedUnitPrice == 0``) line items in the same order — OR, for some
    (older) orders, only inside the box's ``_pvgid`` attributes and not as line
    items at all.
  * A product bought on its own is a normal **priced** line item.

To avoid double-counting we therefore, per order:
  * count standalone (priced) product line items as real sales,
  * count zero-priced component line items as box contents,
  * and add ``_pvgid`` contents only for variants that are NOT already present as
    a line item in that same order (covers the "_pvgid-only" orders).

Usage:
    uv run python sales_report_oneoff_20260629.py
    uv run python sales_report_oneoff_20260629.py --product-id 10398485971256
    uv run python sales_report_oneoff_20260629.py --no-order-list   # summary only
    uv run python sales_report_oneoff_20260629.py -v                 # debug logging
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_PRODUCT_ID = "10398485971256"  # "Kylling, hel, økologisk, 2 kg"
THIS_DIR = Path(__file__).resolve().parent
# The order-exporter app integration lives in the sibling repo.
ORDER_EXPORTER_REPO = Path(
    os.environ.get("ORDER_EXPORTER_PATH", THIS_DIR.parent / "yrja-fulfilment-analytics")
)

log = logging.getLogger("sales_report")

# Vendor (Shopify) → producer name as it appears in the Notion catalog.
# Shopify's `vendor` field is the authoritative per-product producer; this map
# only normalises spelling so we can cross-reference the Notion product list.
VENDOR_TO_NOTION_PRODUCER = {
    "Homlagården": "Homlagaarden",
    "Opaker Gård": "Opaker",
    "Kvarøy Laks": "Kvarøy",
    "Stølsvidda": "Stølsvidda",
}

# Vendors that are not external producers (Yrja's own bundle/box SKUs).
HOUSE_VENDORS = {"Yrja"}

# Producer-name normalization: group producers case-insensitively and fold known
# typos, so e.g. "Homlagarden"/"Homlagaarden" and
# "Hadeland viltslakteri"/"Hadeland Viltslakteri" collapse to one producer.
PRODUCER_FOLD = {"homlagarden": "homlagaarden"}  # typo -> canonical (lowercase)
PRODUCER_DISPLAY = {
    "homlagaarden": "Homlagaarden",
    "hadeland viltslakteri": "Hadeland Viltslakteri",
}

# Order-level filters (delivered, real sales only).
EXCLUDE_TAGS = {"Test"}
REFUNDED_STATUSES = {"REFUNDED", "PARTIALLY_REFUNDED"}


def canon_producer(name: str) -> str:
    """Canonical producer label: case-insensitive, with known typo folding."""
    low = (name or "").strip().casefold()
    if not low:
        return "(unknown)"
    low = PRODUCER_FOLD.get(low, low)
    if low in PRODUCER_DISPLAY:
        return PRODUCER_DISPLAY[low]
    return (name or "").strip().title()


def _order_refs(order_names: set[str]) -> str:
    """Comma-separated, ascending order numbers (digits only), e.g. '1074, 1076'."""
    nums = []
    for nm in order_names:
        digits = re.sub(r"\D", "", nm or "")
        if digits:
            nums.append(int(digits))
    return ", ".join(str(x) for x in sorted(nums))


# ── order-exporter app imports ───────────────────────────────────────────────


def _import_order_exporter() -> tuple[Any, Any, str]:
    """Import the order-exporter app's Shopify client + token mint.

    Returns (ShopifyClient, mint_short_lived_token, API_VERSION).
    """
    if not ORDER_EXPORTER_REPO.exists():
        raise SystemExit(
            f"Cannot find the order-exporter repo at {ORDER_EXPORTER_REPO}. "
            "Set ORDER_EXPORTER_PATH to its location."
        )
    sys.path.insert(0, str(ORDER_EXPORTER_REPO))
    try:
        from src.client import API_VERSION, ShopifyClient  # noqa: E402
        from src.auth import mint_short_lived_token  # noqa: E402
    except Exception as exc:  # pragma: no cover - import diagnostics
        raise SystemExit(
            f"Failed to import the order-exporter app modules from "
            f"{ORDER_EXPORTER_REPO}: {exc}"
        ) from exc
    return ShopifyClient, mint_short_lived_token, API_VERSION


# ── credential resolution ────────────────────────────────────────────────────


def _mask(secret: str) -> str:
    if not secret:
        return "<empty>"
    return f"{secret[:6]}…({len(secret)} chars)" if len(secret) > 8 else "***"


def _load_env() -> dict[str, str]:
    """Merge env from the order-exporter repo, this repo, and the process.

    The order-exporter app's client credentials normally live in the
    order-exporter repo's .env; the static token may live in either .env.
    Process env wins last so a shell override always takes precedence.
    """
    merged: dict[str, str] = {}
    for env_path in (ORDER_EXPORTER_REPO / ".env", THIS_DIR / ".env"):
        if env_path.exists():
            vals = {k: v for k, v in dotenv_values(env_path).items() if v is not None}
            log.debug("Loaded %d vars from %s", len(vals), env_path)
            merged.update(vals)
    for key in ("SHOPIFY_SHOP_DOMAIN", "SHOPIFY_ACCESS_TOKEN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET"):
        if os.environ.get(key):
            merged[key] = os.environ[key]
    # Strip accidental surrounding quotes/whitespace.
    return {k: (v or "").strip().strip('"').strip("'") for k, v in merged.items()}


def build_client(mint_fn: Any, client_cls: Any) -> Any:
    """Resolve a working token for the order-exporter app and build its client."""
    env = _load_env()
    shop = env.get("SHOPIFY_SHOP_DOMAIN", "")
    if not shop:
        raise SystemExit("SHOPIFY_SHOP_DOMAIN is not set in either .env.")

    log.info("Target store (order-exporter app): %s", shop)

    client_id = env.get("SHOPIFY_CLIENT_ID")
    client_secret = env.get("SHOPIFY_CLIENT_SECRET")
    static_token = env.get("SHOPIFY_ACCESS_TOKEN")

    token: str | None = None
    if client_id and client_secret:
        log.info(
            "Minting short-lived token via order-exporter app client_credentials "
            "(client_id=%s)…",
            _mask(client_id),
        )
        try:
            payload = mint_fn(shop, client_id, client_secret)
            token = payload.get("access_token")
            log.info(
                "✓ Minted token %s (scope=%s, expires_in=%ss)",
                _mask(token or ""),
                payload.get("scope", "?"),
                payload.get("expires_in", "?"),
            )
        except Exception as exc:  # noqa: BLE001 - we want to fall back verbosely
            log.warning(
                "Client-credentials mint failed (%s). Falling back to static "
                "SHOPIFY_ACCESS_TOKEN.",
                exc,
            )
    else:
        log.info(
            "No client credentials configured — using static SHOPIFY_ACCESS_TOKEN."
        )

    if not token:
        if not static_token:
            raise SystemExit(
                "No usable Shopify credentials. Set SHOPIFY_CLIENT_ID + "
                "SHOPIFY_CLIENT_SECRET (order-exporter app) or SHOPIFY_ACCESS_TOKEN."
            )
        token = static_token
        log.info("Using static order-exporter token %s", _mask(token))

    return client_cls(shop, token)


# ── GraphQL queries ──────────────────────────────────────────────────────────

ORDERS_QUERY = """
query AllOrders($first: Int!, $after: String) {
  orders(first: $first, after: $after, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        createdAt
        cancelledAt
        displayFinancialStatus
        displayFulfillmentStatus
        tags
        lineItems(first: 100) {
          edges {
            node {
              name
              quantity
              sku
              product { id title vendor }
              variant { id }
              discountedUnitPriceSet { shopMoney { amount } }
              originalUnitPriceSet { shopMoney { amount } }
              customAttributes { key value }
            }
          }
        }
      }
    }
  }
}
"""

# Resolve variant -> product (id, vendor, title) for _pvgid-only contents.
VARIANT_PRODUCT_QUERY = """
query VariantProducts($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      displayName
      product { id title vendor }
    }
  }
}
"""

# Per-variant metafields: the package/f-pack multiplier + producer + SKU name.
# NOTE: the user referred to this as "sku_antall_enheter"; the real metafield
# key in this store is custom.slot_antall_enheter (the number of physical
# packages / f-packs that make up one sold unit of the SKU).
VARIANT_META_QUERY = """
query VariantMeta($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      product { id title vendor }
      metafields(first: 50, namespace: "custom") {
        edges { node { key value } }
      }
    }
  }
}
"""

# The metafield key carrying the package count (see note above).
PACKAGE_COUNT_METAFIELD = "slot_antall_enheter"

# Full product catalog incl. current stock ("På lager" = Product.totalInventory)
# and the per-variant metafields, so we can list every product even if unsold.
PRODUCTS_QUERY = """
query AllProducts($first: Int!, $after: String) {
  products(first: $first, after: $after, sortKey: TITLE) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        title
        vendor
        status
        totalInventory
        variants(first: 100) {
          edges {
            node {
              id
              sku
              inventoryQuantity
              metafields(first: 50, namespace: "custom") {
                edges { node { key value } }
              }
            }
          }
        }
      }
    }
  }
}
"""


# ── Data extraction ──────────────────────────────────────────────────────────


@dataclass
class ProductRef:
    """Resolved identity for a sold product."""

    product_id: str  # numeric, "" if unknown
    title: str
    vendor: str


@dataclass
class VariantMeta:
    """Per-variant metafield data needed for the package-count breakdown."""

    variant_id: str
    product_id: str
    title: str
    vendor: str
    sku_name: str
    produsent: str  # Notion-aligned producer name from custom.produsent
    slot_units: int = 1  # custom.slot_antall_enheter (packages per sold unit)


@dataclass
class OrderSales:
    """Per-order, de-duplicated physical product tally."""

    name: str
    created_at: str
    cancelled: bool
    financial_status: str
    # variant_id -> (qty, ProductRef, source)  where source in {standalone, box}
    standalone: dict[str, tuple[int, ProductRef]] = field(default_factory=dict)
    components: dict[str, tuple[int, ProductRef]] = field(default_factory=dict)
    # Yrja house/box/bundle wrapper SKUs sold (variant_id -> (qty, ref)).
    box_products: dict[str, tuple[int, ProductRef]] = field(default_factory=dict)
    boxes_sold: int = 0  # number of Yrja box/bundle wrapper units


def _pvgid_items(custom_attrs: list[dict[str, str]]) -> dict[str, int]:
    """Extract {variant_id: quantity} from a line item's _pvgid attributes."""
    out: dict[str, int] = {}
    for a in custom_attrs:
        key = a.get("key", "")
        if key.startswith("_pvgid://shopify/ProductVariant/"):
            vid = key.rsplit("/", 1)[-1]
            try:
                qty = int(a.get("value") or "0")
            except ValueError:
                qty = 0
            if vid and qty > 0:
                out[vid] = out.get(vid, 0) + qty
    return out


def _variant_id(node: dict[str, Any]) -> str | None:
    var = node.get("variant") or {}
    gid = var.get("id") or ""
    return gid.rsplit("/", 1)[-1] if gid else None


def _unit_price(node: dict[str, Any], key: str) -> float:
    try:
        return float((node.get(key) or {}).get("shopMoney", {}).get("amount", "0") or 0)
    except (ValueError, TypeError):
        return 0.0


def fetch_all_orders(client: Any, page_size: int) -> list[dict[str, Any]]:
    """Paginate the entire order history (newest first)."""
    orders: list[dict[str, Any]] = []
    cursor: str | None = None
    page = 0
    while True:
        page += 1
        variables: dict[str, Any] = {"first": page_size}
        if cursor:
            variables["after"] = cursor
        data = client.execute(ORDERS_QUERY, variables)
        conn = data["orders"]
        batch = [e["node"] for e in conn["edges"]]
        orders.extend(batch)
        log.info("Page %d: fetched %d orders (total %d)", page, len(batch), len(orders))
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return orders


def resolve_pvgid_variants(
    client: Any, variant_ids: list[str]
) -> dict[str, ProductRef]:
    """Batch-resolve _pvgid variant IDs to their product (id, title, vendor)."""
    result: dict[str, ProductRef] = {}
    if not variant_ids:
        return result
    log.info("Resolving %d _pvgid-only variants to products…", len(variant_ids))
    for i in range(0, len(variant_ids), 100):
        batch = variant_ids[i : i + 100]
        gids = [f"gid://shopify/ProductVariant/{v}" for v in batch]
        data = client.execute(VARIANT_PRODUCT_QUERY, {"ids": gids})
        for node in data.get("nodes", []):
            if not node or "id" not in node:
                continue
            vid = node["id"].rsplit("/", 1)[-1]
            prod = node.get("product") or {}
            result[vid] = ProductRef(
                product_id=(prod.get("id") or "").rsplit("/", 1)[-1],
                title=prod.get("title") or node.get("displayName") or f"variant:{vid}",
                vendor=prod.get("vendor") or "(unknown)",
            )
    return result


def fetch_variant_meta(client: Any, variant_ids: list[str]) -> dict[str, VariantMeta]:
    """Fetch per-variant metafields (slot_antall_enheter, produsent, sku_name)."""
    result: dict[str, VariantMeta] = {}
    if not variant_ids:
        return result
    log.info(
        "Fetching metafields (%s, produsent) for %d sold variants…",
        PACKAGE_COUNT_METAFIELD,
        len(variant_ids),
    )
    missing = 0
    for i in range(0, len(variant_ids), 100):
        batch = variant_ids[i : i + 100]
        gids = [f"gid://shopify/ProductVariant/{v}" for v in batch]
        data = client.execute(VARIANT_META_QUERY, {"ids": gids})
        for node in data.get("nodes", []):
            if not node or "id" not in node:
                continue
            vid = node["id"].rsplit("/", 1)[-1]
            prod = node.get("product") or {}
            mfs = {
                e["node"]["key"]: e["node"]["value"]
                for e in node.get("metafields", {}).get("edges", [])
            }
            raw = mfs.get(PACKAGE_COUNT_METAFIELD)
            try:
                slot = int(float(raw)) if raw not in (None, "") else 0
            except (ValueError, TypeError):
                slot = 0
            if slot <= 0:
                slot = 1  # default: 1 package per sold unit
                missing += 1
            result[vid] = VariantMeta(
                variant_id=vid,
                product_id=(prod.get("id") or "").rsplit("/", 1)[-1],
                title=prod.get("title") or "",
                vendor=prod.get("vendor") or "(unknown)",
                sku_name=mfs.get("sku_name") or "",
                produsent=mfs.get("produsent") or "",
                slot_units=slot,
            )
    if missing:
        log.warning(
            "%d variant(s) had no usable %s — defaulted package count to 1",
            missing,
            PACKAGE_COUNT_METAFIELD,
        )
    return result


def fetch_catalog(
    client: Any, page_size: int = 50
) -> tuple[dict[str, dict[str, Any]], dict[str, VariantMeta]]:
    """Fetch the full product catalog with current stock + per-variant metafields.

    Returns (catalog, variant_meta):
      * catalog: product_id -> {title, vendor, producer, slots, sku, pa_lager,
        status, is_house}
      * variant_meta: variant_id -> VariantMeta (covers every catalog variant)
    """
    catalog: dict[str, dict[str, Any]] = {}
    variant_meta: dict[str, VariantMeta] = {}
    cursor: str | None = None
    page = 0
    while True:
        page += 1
        variables: dict[str, Any] = {"first": page_size}
        if cursor:
            variables["after"] = cursor
        data = client.execute(PRODUCTS_QUERY, variables)
        conn = data["products"]
        for e in conn["edges"]:
            n = e["node"]
            pid = n["id"].rsplit("/", 1)[-1]
            vendor = n.get("vendor") or "(unknown)"
            producer = ""
            sku = ""
            slots: set[int] = set()
            for ve in n.get("variants", {}).get("edges", []):
                vn = ve["node"]
                vid = vn["id"].rsplit("/", 1)[-1]
                mfs = {
                    x["node"]["key"]: x["node"]["value"]
                    for x in vn.get("metafields", {}).get("edges", [])
                }
                raw = mfs.get(PACKAGE_COUNT_METAFIELD)
                try:
                    slot = int(float(raw)) if raw not in (None, "") else 0
                except (ValueError, TypeError):
                    slot = 0
                if slot <= 0:
                    slot = 1
                slots.add(slot)
                vm = VariantMeta(
                    variant_id=vid,
                    product_id=pid,
                    title=n.get("title") or "",
                    vendor=vendor,
                    sku_name=mfs.get("sku_name") or "",
                    produsent=mfs.get("produsent") or "",
                    slot_units=slot,
                )
                variant_meta[vid] = vm
                if not producer and vm.produsent:
                    producer = vm.produsent
                if not sku and vm.sku_name:
                    sku = vm.sku_name
            if not producer:
                producer = VENDOR_TO_NOTION_PRODUCER.get(vendor, vendor)
            producer = canon_producer(producer)
            catalog[pid] = {
                "title": n.get("title") or "",
                "vendor": vendor,
                "producer": producer,
                "slots": slots or {1},
                "sku": sku,
                "pa_lager": n.get("totalInventory"),
                "status": n.get("status") or "",
                "is_house": vendor in HOUSE_VENDORS,
            }
        log.info(
            "Catalog page %d: %d products (total %d)",
            page,
            len(conn["edges"]),
            len(catalog),
        )
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    return catalog, variant_meta


def filter_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only delivered, real orders.

    Included only if ALL hold:
      * displayFulfillmentStatus == FULFILLED   ("Sluttført" = delivered)
      * displayFinancialStatus not refunded
      * no "Test" tag
      * not cancelled

    Payment gateway is intentionally NOT used as a filter: Sander's test orders
    use shopify_payments just like the two real orders (#1040, #1079), so the
    above filters already isolate the real sales.
    """
    kept: list[dict[str, Any]] = []
    dropped = {"not_fulfilled": 0, "refunded": 0, "test": 0, "cancelled": 0}
    for o in orders:
        if o.get("displayFulfillmentStatus") != "FULFILLED":
            dropped["not_fulfilled"] += 1
            continue
        if o.get("displayFinancialStatus") in REFUNDED_STATUSES:
            dropped["refunded"] += 1
            continue
        if EXCLUDE_TAGS & set(o.get("tags") or []):
            dropped["test"] += 1
            continue
        if o.get("cancelledAt") is not None:
            dropped["cancelled"] += 1
            continue
        kept.append(o)
    log.info(
        "Order filter: kept %d / %d delivered orders (dropped %s)",
        len(kept),
        len(orders),
        dropped,
    )
    return kept


def build_order_sales(
    orders: list[dict[str, Any]], client: Any
) -> list[OrderSales]:
    """Convert raw orders into de-duplicated per-order physical tallies."""
    # First pass: gather line-item variant identities + collect _pvgid variants.
    variant_info: dict[str, ProductRef] = {}
    pvgid_variants_needed: set[str] = set()

    for o in orders:
        line_variants: set[str] = set()
        boxes: list[dict[str, int]] = []
        for e in o["lineItems"]["edges"]:
            n = e["node"]
            vid = _variant_id(n)
            prod = n.get("product") or {}
            if vid:
                line_variants.add(vid)
                variant_info.setdefault(
                    vid,
                    ProductRef(
                        product_id=(prod.get("id") or "").rsplit("/", 1)[-1],
                        title=prod.get("title") or n.get("name") or "",
                        vendor=prod.get("vendor") or "(unknown)",
                    ),
                )
            pv = _pvgid_items(n.get("customAttributes") or [])
            if pv:
                boxes.append(pv)
        for pv in boxes:
            for vid in pv:
                if vid not in line_variants:
                    pvgid_variants_needed.add(vid)

    # Resolve any _pvgid variants we couldn't see as line items anywhere.
    unresolved = sorted(pvgid_variants_needed - set(variant_info))
    variant_info.update(resolve_pvgid_variants(client, unresolved))

    # Second pass: build per-order de-duplicated tallies.
    sales: list[OrderSales] = []
    for o in orders:
        os_ = OrderSales(
            name=o["name"],
            created_at=(o.get("createdAt") or "")[:10],
            cancelled=o.get("cancelledAt") is not None,
            financial_status=o.get("displayFinancialStatus") or "",
        )
        line_variant_qty: dict[str, int] = {}
        box_pvgids: list[dict[str, int]] = []

        for e in o["lineItems"]["edges"]:
            n = e["node"]
            attrs = n.get("customAttributes") or []
            pv = _pvgid_items(attrs)
            prod = n.get("product") or {}
            vendor = prod.get("vendor") or "(unknown)"
            qty = n.get("quantity", 0)

            # A box/bundle wrapper is either a line item carrying _pvgid
            # contents, or any Yrja house SKU (the paid box the customer buys).
            # Some "Råvareboks" wrappers carry _pvgid and some don't, so keying
            # off the Yrja vendor keeps box accounting consistent either way.
            # Box contents are the zero-priced component line items / _pvgid.
            if pv or vendor in HOUSE_VENDORS:
                os_.boxes_sold += qty
                vid_b = _variant_id(n)
                if vid_b:
                    ref_b = variant_info.get(vid_b) or ProductRef(
                        (prod.get("id") or "").rsplit("/", 1)[-1],
                        n.get("name") or "",
                        vendor,
                    )
                    prev_b = os_.box_products.get(vid_b)
                    os_.box_products[vid_b] = ((prev_b[0] if prev_b else 0) + qty, ref_b)
                if pv:
                    box_pvgids.append(pv)
                continue

            vid = _variant_id(n)
            if not vid:
                continue
            ref = variant_info.get(vid) or ProductRef(
                (prod.get("id") or "").rsplit("/", 1)[-1], n.get("name") or "", vendor
            )
            line_variant_qty[vid] = line_variant_qty.get(vid, 0) + qty

            disc = _unit_price(n, "discountedUnitPriceSet")
            if disc > 0:
                prev = os_.standalone.get(vid)
                os_.standalone[vid] = ((prev[0] if prev else 0) + qty, ref)
            else:
                prev = os_.components.get(vid)
                os_.components[vid] = ((prev[0] if prev else 0) + qty, ref)

        # Add box contents only for variants NOT already present as line items.
        for pv in box_pvgids:
            for vid, qty in pv.items():
                if vid in line_variant_qty:
                    continue  # already represented as a (zero-priced) line item
                ref = variant_info.get(vid) or ProductRef("", f"variant:{vid}", "(unknown)")
                prev = os_.components.get(vid)
                os_.components[vid] = ((prev[0] if prev else 0) + qty, ref)

        sales.append(os_)
    return sales


# ── Reporting ────────────────────────────────────────────────────────────────


def notion_producers() -> set[str]:
    """Producer names from the Notion catalog (falls back to hardcoded list)."""
    try:
        from products import get_products

        prods = get_products()
        return {p.producer for p in prods if p.producer}
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not load Notion/products catalog: %s", exc)
        return set()


def producer_label(vendor: str, notion: set[str]) -> str:
    mapped = VENDOR_TO_NOTION_PRODUCER.get(vendor, vendor)
    flag = ""
    if vendor in HOUSE_VENDORS:
        flag = " [Yrja house SKU]"
    elif mapped not in notion:
        flag = " [not in Notion]"
    return f"{vendor} → {mapped}{flag}"


def report_target_product(
    sales: list[OrderSales], product_id: str, title: str
) -> None:
    target_gid_num = product_id
    orders_with = 0
    standalone_total = standalone_active = 0
    component_total = component_active = 0

    print("\n" + "=" * 78)
    print(f"TARGET PRODUCT  {product_id}  —  {title}")
    print("=" * 78)

    for s in sales:
        st = sum(q for q, ref in s.standalone.values() if ref.product_id == target_gid_num)
        co = sum(q for q, ref in s.components.values() if ref.product_id == target_gid_num)
        if st == 0 and co == 0:
            continue
        orders_with += 1
        standalone_total += st
        component_total += co
        if not s.cancelled:
            standalone_active += st
            component_active += co
        tag = "  CANCELLED" if s.cancelled else ""
        if s.financial_status in ("REFUNDED", "PARTIALLY_REFUNDED"):
            tag += f"  {s.financial_status}"
        print(
            f"  {s.name:<8} {s.created_at}  standalone={st}  in-box={co}"
            f"  (total {st + co}){tag}"
        )

    total_all = standalone_total + component_total
    total_active = standalone_active + component_active
    print("-" * 78)
    print(f"  Orders containing this product : {orders_with}")
    print(f"  Sold standalone (paid)         : {standalone_total}")
    print(f"  Sold as box content (free)     : {component_total}")
    print(f"  TOTAL units sold (all orders)  : {total_all}")
    print(f"  TOTAL units (excl. cancelled)  : {total_active}")


def report_producers(sales: list[OrderSales], notion: set[str]) -> None:
    # vendor -> [items_all, items_active, distinct_orders]
    per_vendor: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    vendor_orders: dict[str, set[str]] = defaultdict(set)
    boxes_all = boxes_active = 0

    for s in sales:
        boxes_all += s.boxes_sold
        if not s.cancelled:
            boxes_active += s.boxes_sold
        for vid, (qty, ref) in list(s.standalone.items()) + list(s.components.items()):
            v = ref.vendor or "(unknown)"
            per_vendor[v][0] += qty
            if not s.cancelled:
                per_vendor[v][1] += qty
            vendor_orders[v].add(s.name)

    for v in per_vendor:
        per_vendor[v][2] = len(vendor_orders[v])

    print("\n" + "=" * 78)
    print("ITEMS SOLD PER PRODUCER  (de-duplicated; box contents counted once)")
    print("=" * 78)
    print(f"  {'Producer (vendor → Notion)':<48}{'items':>7}{'active':>8}{'orders':>8}")
    print("  " + "-" * 71)
    for v, (all_, active, norders) in sorted(
        per_vendor.items(), key=lambda kv: kv[1][0], reverse=True
    ):
        print(f"  {producer_label(v, notion):<48}{all_:>7}{active:>8}{norders:>8}")
    print("  " + "-" * 71)
    grand_all = sum(x[0] for x in per_vendor.values())
    grand_active = sum(x[1] for x in per_vendor.values())
    print(f"  {'TOTAL producer items':<48}{grand_all:>7}{grand_active:>8}")
    print(
        f"\n  Yrja boxes/bundles sold (wrapper SKUs): {boxes_all} "
        f"({boxes_active} excl. cancelled)"
    )


def _aggregate_sales(
    sales: list[OrderSales],
    vmeta: dict[str, VariantMeta],
    source: str,
    valid_variant_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate sales to product level. ``source`` is 'items' or 'box'.

    When ``valid_variant_ids`` is given, sold lines for variants not in the live
    catalog (deleted SKUs) are skipped.
    """
    agg: dict[str, dict[str, Any]] = {}
    for s in sales:
        pairs = (
            list(s.standalone.items()) + list(s.components.items())
            if source == "items"
            else list(s.box_products.items())
        )
        for vid, (qty, ref) in pairs:
            if valid_variant_ids is not None and vid not in valid_variant_ids:
                continue  # deleted variant: not present in the live catalog
            meta = vmeta.get(vid)
            slot = meta.slot_units if meta else 1
            pid = (meta.product_id if meta and meta.product_id else ref.product_id) or f"variant:{vid}"
            vendor = (meta.vendor if meta else ref.vendor) or "(unknown)"
            producer = canon_producer(
                (meta.produsent if meta and meta.produsent else "")
                or VENDOR_TO_NOTION_PRODUCER.get(vendor, vendor)
            )
            d = agg.setdefault(
                pid,
                {
                    "producer": producer,
                    "title": (meta.title if meta and meta.title else ref.title) or "",
                    "sku": (meta.sku_name if meta else "") or "",
                    "slots": set(), "orders": set(),
                    "u": 0, "ua": 0, "p": 0, "pa": 0,
                },
            )
            d["slots"].add(slot)
            d["orders"].add(s.name)
            d["u"] += qty
            d["p"] += qty * slot
            if not s.cancelled:
                d["ua"] += qty
                d["pa"] += qty * slot
    return agg


def _row_info(
    pid: str,
    catalog: dict[str, dict[str, Any]],
    agg: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Merge catalog (display + stock) with sales aggregate for one product."""
    cat = catalog.get(pid)
    sa = agg.get(pid)
    return {
        "producer": (cat["producer"] if cat else (sa or {}).get("producer")) or "(unknown)",
        "title": (cat["title"] if cat else (sa or {}).get("title")) or "",
        "sku": (cat["sku"] if cat else (sa or {}).get("sku")) or "",
        "slots": (cat["slots"] if cat else (sa or {}).get("slots")) or {1},
        "pa_lager": cat["pa_lager"] if cat else None,
        "status": cat["status"] if cat else "DELETED?",
        "u": sa["u"] if sa else 0,
        "ua": sa["ua"] if sa else 0,
        "p": sa["p"] if sa else 0,
        "pa": sa["pa"] if sa else 0,
        "orders": sa["orders"] if sa else set(),
    }


def write_breakdown_csv(
    path: str,
    sales: list[OrderSales],
    catalog: dict[str, dict[str, Any]],
    vmeta: dict[str, VariantMeta],
    valid_variant_ids: set[str],
    delivered_orders: int,
) -> tuple[int, int, int]:
    """Write the per-product + per-producer breakdown CSV (incl. inventory).

    Lists every catalog product, even unsold ones. ``packages_sold`` = units x
    ``slot_antall_enheter``; ``pa_lager`` is current Shopify stock
    (Product.totalInventory). House/box/bundle SKUs (vendor "Yrja") get their
    own section. Deleted variants (not in ``valid_variant_ids``) are excluded.
    A trailing ``delivered_orders_total`` row carries the gift count.
    Returns (n_producer_products, n_house_products, n_producers).
    """
    items_agg = _aggregate_sales(sales, vmeta, "items", valid_variant_ids)
    box_agg = _aggregate_sales(sales, vmeta, "box", valid_variant_ids)

    # Universe of product ids: everything in the catalog, plus anything sold
    # that is missing from the catalog (e.g. deleted SKUs).
    producer_pids = {pid for pid, c in catalog.items() if not c["is_house"]} | set(items_agg)
    house_pids = {pid for pid, c in catalog.items() if c["is_house"]} | set(box_agg)
    house_pids -= set(items_agg)
    producer_pids -= house_pids

    def slot_str(slots: set[int]) -> str:
        return ";".join(str(x) for x in sorted(slots))

    def inv(v: Any) -> int:
        return v if isinstance(v, int) else 0

    def inv_cell(v: Any) -> Any:
        return v if isinstance(v, int) else ""

    header = [
        "row_type", "producer", "product_id", "sku_name", "product_title",
        "slot_antall_enheter", "pa_lager", "status", "distinct_orders",
        "units_sold", "units_sold_excl_cancelled",
        "packages_sold", "packages_sold_excl_cancelled", "order_refs",
    ]

    # Group producer products by producer.
    by_producer: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for pid in producer_pids:
        d = _row_info(pid, catalog, items_agg)
        by_producer[d["producer"]].append((pid, d))

    out = Path(path)
    n_producers = 0
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)

        # Producers ordered by total packages sold desc; within: packages desc,
        # then current stock desc, then title (so unsold items sort by stock).
        def producer_key(pr: str) -> tuple:
            return (-sum(d["p"] for _p, d in by_producer[pr]), pr)

        for producer in sorted(by_producer, key=producer_key):
            items = sorted(
                by_producer[producer],
                key=lambda it: (-it[1]["p"], -inv(it[1]["pa_lager"]), it[1]["title"]),
            )
            tu = tua = tp = tpa = tinv = 0
            porders: set[str] = set()
            for pid, d in items:
                w.writerow([
                    "product", producer, pid, d["sku"], d["title"],
                    slot_str(d["slots"]), inv_cell(d["pa_lager"]), d["status"],
                    len(d["orders"]), d["u"], d["ua"], d["p"], d["pa"],
                    _order_refs(d["orders"]),
                ])
                tu += d["u"]; tua += d["ua"]; tp += d["p"]; tpa += d["pa"]
                tinv += inv(d["pa_lager"]); porders |= d["orders"]
            w.writerow([
                "producer_total", producer, "", "", f"({len(items)} products)",
                "", tinv, "", len(porders), tu, tua, tp, tpa, "",
            ])
            n_producers += 1

        # Grand total across producer products.
        gorders: set[str] = set()
        gu = gua = gp = gpa = ginv = 0
        for pid in producer_pids:
            d = _row_info(pid, catalog, items_agg)
            gu += d["u"]; gua += d["ua"]; gp += d["p"]; gpa += d["pa"]
            ginv += inv(d["pa_lager"]); gorders |= d["orders"]
        w.writerow([
            "grand_total", "ALL PRODUCERS", "", "", f"({len(producer_pids)} products)",
            "", ginv, "", len(gorders), gu, gua, gp, gpa, "",
        ])

        # House / box / bundle SKUs (vendor Yrja), incl. unsold.
        hu = hua = hp = hpa = hinv = 0
        horders: set[str] = set()
        for pid in sorted(
            house_pids,
            key=lambda pid: (-box_agg.get(pid, {}).get("p", 0),
                             -inv(catalog.get(pid, {}).get("pa_lager"))),
        ):
            d = _row_info(pid, catalog, box_agg)
            w.writerow([
                "box_product", d["producer"], pid, d["sku"], d["title"],
                slot_str(d["slots"]), inv_cell(d["pa_lager"]), d["status"],
                len(d["orders"]), d["u"], d["ua"], d["p"], d["pa"],
                _order_refs(d["orders"]),
            ])
            hu += d["u"]; hua += d["ua"]; hp += d["p"]; hpa += d["pa"]
            hinv += inv(d["pa_lager"]); horders |= d["orders"]
        if house_pids:
            w.writerow([
                "box_total", "HOUSE / BOX SKUs", "", "", f"({len(house_pids)} products)",
                "", hinv, "", len(horders), hu, hua, hp, hpa, "",
            ])

        # Total delivered orders (for the per-order Kjøttdeig gift).
        w.writerow([
            "delivered_orders_total", "", "", "",
            "GIFT: +1 Kjottdeig per delivered order",
            "", "", "", delivered_orders, "", "", "", "", "",
        ])

    return len(producer_pids), len(house_pids), n_producers


def report_order_list(sales: list[OrderSales]) -> None:
    print("\n" + "=" * 78)
    print("ALL ORDERS WITH PRODUCTS  (de-duplicated physical contents)")
    print("=" * 78)
    for s in sales:
        items = list(s.standalone.items()) + list(s.components.items())
        if not items and s.boxes_sold == 0:
            continue
        tags = []
        if s.cancelled:
            tags.append("CANCELLED")
        if s.financial_status:
            tags.append(s.financial_status)
        header = f"{s.name:<8} {s.created_at}"
        if s.boxes_sold:
            header += f"  [{s.boxes_sold} box]"
        if tags:
            header += "  " + " ".join(tags)
        print(header)
        for vid, (qty, ref) in items:
            kind = "paid " if vid in s.standalone else "inbox"
            print(f"    {kind} x{qty:<3} {ref.vendor:<22} {ref.title[:46]}")


# ── CLI ──────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--product-id", default=DEFAULT_PRODUCT_ID, help="numeric Shopify product ID to count")
    p.add_argument("--page-size", type=int, default=25, help="orders per GraphQL page (cost control)")
    p.add_argument("--no-order-list", action="store_true", help="skip the verbose per-order listing")
    p.add_argument(
        "--csv",
        default=f"sales_breakdown_{datetime.now():%Y%m%d}.csv",
        help="output path for the per-product/per-producer CSV (packages = units x slot_antall_enheter)",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    ShopifyClient, mint_fn, api_version = _import_order_exporter()
    log.info("Using order-exporter app GraphQL client (Admin API %s)", api_version)

    client = build_client(mint_fn, ShopifyClient)
    try:
        # Sanity check + product title.
        shop_q = (
            'query { shop { name myshopifyDomain } '
            f'product(id: "gid://shopify/Product/{args.product_id}") '
            "{ title vendor } }"
        )
        meta = client.execute(shop_q)
        shop = meta.get("shop", {})
        prod = meta.get("product") or {}
        title = prod.get("title") or "(unknown product)"
        log.info(
            "Connected to '%s' (%s). Target product: %s (vendor=%s)",
            shop.get("name"),
            shop.get("myshopifyDomain"),
            title,
            prod.get("vendor"),
        )

        log.info("Fetching ALL orders (all-time)…")
        orders = fetch_all_orders(client, args.page_size)
        log.info("Fetched %d orders total; applying delivered/real filters…", len(orders))
        orders = filter_orders(orders)
        delivered_orders = len(orders)
        log.info("Building de-duplicated tallies over %d delivered orders…", delivered_orders)
        sales = build_order_sales(orders, client)
        log.info("Fetching full product catalog + current stock (På lager)…")
        catalog, catalog_vmeta = fetch_catalog(client)
        log.info("Catalog: %d products.", len(catalog))
        # Metafields: prefer the catalog; fill any sold variant missing from it
        # (e.g. deleted SKUs) with a targeted lookup.
        sold_variant_ids = sorted(
            {
                vid
                for s in sales
                for vid in (set(s.standalone) | set(s.components) | set(s.box_products))
            }
        )
        vmeta = dict(catalog_vmeta)
        missing = [v for v in sold_variant_ids if v not in vmeta]
        vmeta.update(fetch_variant_meta(client, missing))
    finally:
        client.close()

    notion = notion_producers()
    log.info("Loaded %d producer names from the Notion catalog for cross-reference", len(notion))

    report_target_product(sales, args.product_id, title)
    report_producers(sales, notion)
    if not args.no_order_list:
        report_order_list(sales)

    n_prod, n_house, n_producers = write_breakdown_csv(
        args.csv,
        sales,
        catalog,
        vmeta,
        valid_variant_ids=set(catalog_vmeta),
        delivered_orders=delivered_orders,
    )
    log.info(
        "Wrote CSV: %d producer products + %d house/box products across %d producers to %s",
        n_prod,
        n_house,
        n_producers,
        Path(args.csv).resolve(),
    )

    print("\nDone. (Delivered, non-refunded, non-Test orders for store yrja-2 via the order-exporter app.)")
    print(
        f"CSV breakdown (packages = units x {PACKAGE_COUNT_METAFIELD}, "
        f"pa_lager = current stock): {args.csv}"
    )
    print(
        f"TOTAL DELIVERED ORDERS (Sluttført, non-refunded, non-Test): "
        f"{delivered_orders}  →  gift = +1 Kjøttdeig per order"
    )


if __name__ == "__main__":
    main()
