"""Send Skio-style flat-product Shopify orders to Gordon Last Mile.

Unlike ``gordon_exporter.py`` — which builds one Gordon inventory entry per
Shopify line item — this script flattens the whole order into a **single**
inventory entry: the Råvareboks line item itself becomes the inventory
``name``, and every other Shopify line item is folded in as an article
inside that one box.

This matches how Yrja's pickers expect Skio subscription orders to appear in
Gordon Last Mile: one physical box per order, with its contents listed as
SKU-named articles (from the variant's ``custom.sku_name`` metafield).

Input format — JSON array, supplied via ``--orders`` (inline string),
``--orders-file`` (path), or stdin:

    [
      {"order_id": "#1065", "date": "2026-06-09", "window": "14:00 - 22:00"},
      {"order_id": "#1066", "date": "2026-05-29", "window": "16:00 - 22:00"}
    ]

``window`` may be written with or without spaces around the dash; both
``"14:00-22:00"`` and ``"14:00 - 22:00"`` are normalized to Gordon's
expected ``"HH:mm - HH:mm"`` shape.

Run dry-run first (prints JSON, no Gordon call):

    uv run python skio_orders_to_gordon.py --orders-file orders.json --dry-run

Real send:

    uv run python skio_orders_to_gordon.py --orders-file orders.json

Both default to the AWS-fronted staging host; override with ``--base-url``.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from gordon_client import GordonAPIError, GordonClient
from shopify_order_models import LineItem, Order, VariantMetadata, collect_variant_ids
from shopify_orders import build_query_filter, fetch_orders, fetch_variant_metadata

log = logging.getLogger("skio_orders_to_gordon")

# ── Constants ────────────────────────────────────────────────────────────

DEFAULT_INVENTORY_TYPE = "frozen"
DEFAULT_DELIVERY_GROUP = "Yrja"
DEFAULT_BASE_URL = "https://backend.aws.gordondelivery.com"

# Identify the "physical box" line item — its title starts with "Råvareboks"
# (case-insensitive, with or without the diacritic). Every other line item
# in the order becomes a flat article inside that box.
RAVAREBOKS_RE = re.compile(r"^\s*r[aå]vareboks\b", re.IGNORECASE)

# Shopify custom-attribute keys that mirror the Norwegian checkout label
# "Leilighet, etasje osv. (valgfritt)". Used for the Gordon ``notes`` value.
NOTES_ATTR_KEYS = (
    "Leilighet, etasje osv. (valgfritt)",
    "Leilighet, etasje osv.",
    "Leilighet",
    "Apartment, floor, etc.",
)

# ISO 3166-1 alpha-2 → E.164 calling code. Mirrors gordon_exporter.py.
COUNTRY_DIAL_CODES: dict[str, str] = {
    "NO": "47",
    "SE": "46",
    "DK": "45",
    "FI": "358",
    "IS": "354",
    "DE": "49",
    "GB": "44",
    "NL": "31",
    "FR": "33",
}

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})\s*$")


# ── Helpers ──────────────────────────────────────────────────────────────


def _to_e164(phone: str, country_code: str | None) -> str | None:
    """Normalize a Shopify phone string into E.164 (``+<dial><number>``)."""
    if not phone:
        return None
    cleaned = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if cleaned.startswith("+"):
        return cleaned
    dial = COUNTRY_DIAL_CODES.get((country_code or "").upper())
    if not dial:
        return None
    if cleaned.startswith(dial) and len(cleaned) > len(dial) + 6:
        return "+" + cleaned
    return f"+{dial}{cleaned}"


def _pick_attribute(order: Order, keys: tuple[str, ...]) -> str | None:
    """Return the first matching custom-attribute value (order or line item)."""
    for key in keys:
        val = order.get_attribute(key)
        if val:
            return val.strip()
    for li in order.line_items:
        for key in keys:
            val = li.get_attribute(key)
            if val:
                return val.strip()
    return None


def _normalize_window(window: str) -> str:
    """Normalize ``HH:mm-HH:mm`` to Gordon's ``"HH:mm - HH:mm"`` shape."""
    m = _WINDOW_RE.match(window or "")
    if not m:
        return (window or "").strip()
    return f"{m.group(1)} - {m.group(2)}"


def _article_name(li: LineItem, lookup: dict[str, VariantMetadata]) -> str:
    """Resolve a Gordon article name from the variant's ``custom.sku_name``.

    Falls back to the Shopify line item title only when the metafield is
    unavailable (e.g. deleted variant, or sku_name not yet populated).
    """
    meta = lookup.get(li.variant_id) if li.variant_id else None
    if meta and meta.sku_name:
        return meta.sku_name
    return li.name


# ── Payload builder ──────────────────────────────────────────────────────


def _build_payload(
    order: Order,
    *,
    delivery_date: str,
    time_window: str,
    delivery_group: str,
    inventory_type: str,
    variant_lookup: dict[str, VariantMetadata],
) -> dict[str, Any] | None:
    """Map a Shopify ``Order`` to a Gordon ``POST /api/orders/bulk`` entry.

    Returns ``None`` (with a warning log) if the order can't be sent — e.g.
    missing address fields, or no Råvareboks line item to act as the box.
    """
    addr = order.shipping_address
    notes = _pick_attribute(order, NOTES_ATTR_KEYS) or addr.address2 or None

    missing: list[str] = []
    if not addr.name:
        missing.append("customer-name")
    if not addr.address1:
        missing.append("address")
    if not addr.zip:
        missing.append("zip")
    if not addr.city:
        missing.append("city")
    if missing:
        log.warning(
            "Skipping order %s — missing required address fields: %s",
            order.name, ", ".join(missing),
        )
        return None

    # Locate the Råvareboks line item (the physical box).
    box_items = [li for li in order.line_items if RAVAREBOKS_RE.match(li.name)]
    if not box_items:
        log.warning(
            "Skipping order %s — no Råvareboks line item found "
            "(line items: %s)",
            order.name, [li.name for li in order.line_items],
        )
        return None
    if len(box_items) > 1:
        log.warning(
            "Order %s has %d Råvareboks line items; using the first (%r). "
            "Other Råvareboks line items will be ignored.",
            order.name, len(box_items), box_items[0].name,
        )
    box = box_items[0]

    # Every non-box line item becomes a flat article inside the box.
    articles: list[dict[str, Any]] = []
    for li in order.line_items:
        if li is box:
            continue
        if li.quantity <= 0:
            continue
        articles.append({
            "name": _article_name(li, variant_lookup),
            "quantity": li.quantity,
        })

    if not articles:
        log.warning(
            "Skipping order %s — Råvareboks found but no other line items "
            "to include as articles.",
            order.name,
        )
        return None

    # Gordon wants a numeric external_ref with a "1000" prefix, not Shopify's
    # "#1065" order name. Strip non-digits and prepend "1000".
    clean_name = "".join(ch for ch in order.name if ch.isdigit())
    external_ref = f"1000{clean_name}" if clean_name else order.name

    payload: dict[str, Any] = {
        "external_ref": external_ref,
        "customer-name": addr.name,
        "address": addr.address1,
        "zip": addr.zip,
        "city": addr.city,
        "deliverydate": delivery_date,
        "time-window": time_window,
    }
    if order.email:
        payload["email"] = order.email
    mobile = _to_e164(addr.phone, addr.country_code) if addr.phone else None
    if mobile:
        payload["mobile"] = mobile
    if addr.country_code:
        payload["country_code"] = addr.country_code
    if notes:
        payload["notes"] = notes
    if delivery_group:
        payload["deliverygroup"] = delivery_group

    payload["inventory"] = [
        {
            "name": box.name,
            "quantity": 1,
            "type": inventory_type,
            "articles": articles,
        }
    ]
    return payload


# ── Input loading ────────────────────────────────────────────────────────


def _load_input(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Read the JSON orders array from --orders, --orders-file, or stdin."""
    if args.orders and args.orders_file:
        raise SystemExit("--orders and --orders-file are mutually exclusive.")
    if args.orders:
        raw = args.orders
    elif args.orders_file:
        raw = Path(args.orders_file).read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            raise SystemExit(
                "No input provided. Pass --orders, --orders-file, or pipe "
                "JSON on stdin."
            )
        raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"Invalid JSON input: {e}") from e
    if not isinstance(data, list):
        raise SystemExit(
            'Input JSON must be an array of '
            '{"order_id":..., "date":..., "window":...} entries.'
        )
    return data


# ── CLI ──────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Send Skio-style flat-product Shopify orders to Gordon Last Mile.",
    )
    p.add_argument(
        "--orders",
        default=None,
        help="Inline JSON array of {order_id, date, window} entries.",
    )
    p.add_argument(
        "--orders-file",
        default=None,
        help="Path to a JSON file containing the orders array.",
    )
    p.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Gordon base URL (defaults to the AWS-fronted staging host: {DEFAULT_BASE_URL}).",
    )
    p.add_argument(
        "--delivery-group",
        default=DEFAULT_DELIVERY_GROUP,
        help=f"Gordon delivery group name (default: {DEFAULT_DELIVERY_GROUP!r}).",
    )
    p.add_argument(
        "--inventory-type",
        choices=["ambient", "chilled", "frozen"],
        default=DEFAULT_INVENTORY_TYPE,
        help=(
            f"Temperature zone applied to the inventory entry "
            f"(default: {DEFAULT_INVENTORY_TYPE!r})."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print payloads but make no network call to Gordon.",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    entries = _load_input(args)
    if not entries:
        print("No orders in input; nothing to do.")
        return

    # 1. Fetch each Shopify order by name. Shopify's `name:` filter is an
    #    exact-digit match, so one round-trip per order is cheap and
    #    cleanly avoids picking up unrelated orders.
    orders_by_id: dict[str, Order] = {}
    for entry in entries:
        order_id = str(entry.get("order_id", "")).strip()
        if not order_id:
            log.warning("Skipping entry with missing 'order_id': %r", entry)
            continue
        q = build_query_filter(name=order_id, status="any", tag_exclude=None)
        log.info("Fetching Shopify order %s …", order_id)
        matches = fetch_orders(q, limit=5)
        if not matches:
            log.warning("Shopify order %s not found", order_id)
            continue
        # `name:` is exact on digits, so a single hit is the norm. Be defensive
        # and pick the exact name match if Shopify returns multiple.
        wanted = order_id.lstrip("#").strip()
        exact = next(
            (o for o in matches if o.name.lstrip("#").strip() == wanted),
            matches[0],
        )
        orders_by_id[order_id] = exact

    if not orders_by_id:
        print("No matching Shopify orders found; nothing to send.")
        return

    # 2. Pull variant metafields once, in a single batched call, for SKU-name
    #    resolution on every line item across every order.
    variant_ids = collect_variant_ids(list(orders_by_id.values()))
    variant_lookup = fetch_variant_metadata(variant_ids) if variant_ids else {}

    # 3. Build payloads. Date/window come from the input entry, not from any
    #    Shopify field — Skio orders don't carry a slot in shippingLine.title.
    payloads: list[dict[str, Any]] = []
    for entry in entries:
        order_id = str(entry.get("order_id", "")).strip()
        if order_id not in orders_by_id:
            continue
        date = str(entry.get("date", "")).strip()
        window = _normalize_window(str(entry.get("window", "")))
        if not date or not window:
            log.warning(
                "Skipping %s — missing date and/or window in input entry: %r",
                order_id, entry,
            )
            continue
        payload = _build_payload(
            orders_by_id[order_id],
            delivery_date=date,
            time_window=window,
            delivery_group=args.delivery_group,
            inventory_type=args.inventory_type,
            variant_lookup=variant_lookup,
        )
        if payload:
            payloads.append(payload)

    log.info("Built %d Gordon payloads", len(payloads))

    # 4. Dry-run stops here.
    if args.dry_run:
        print(json.dumps(payloads, indent=2, ensure_ascii=False))
        return

    if not payloads:
        print("Nothing to send to Gordon. Exiting.")
        return

    # 5. POST to Gordon.
    with GordonClient(
        base_url=args.base_url,
        delivery_group=args.delivery_group,
    ) as client:
        log.info(
            "Sending %d orders to Gordon (%s)",
            len(payloads), client.base_url,
        )
        try:
            resp = client.create_orders_bulk(payloads)
            log.info("Response: %r", resp)
        except GordonAPIError as e:
            log.error("Gordon API error %d: %r", e.status_code, e.body)
            sys.exit(1)


if __name__ == "__main__":
    main()
