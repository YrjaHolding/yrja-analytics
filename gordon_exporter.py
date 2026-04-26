"""Push Shopify orders to Gordon Delivery.

The ``deliverydate`` field in the Gordon payload is the **date the courier
should deliver the package to the recipient** (YYYY-MM-DD), not the order-
placed date. It's set batch-wide via ``--date``.

The ``time-window`` field is intentionally **not sent** — Gordon assigns it
from the delivery group's default window in GLMP.

Usage examples (all default to the Gordon **staging** / test environment):

    # Dry-run — build payloads, print JSON, no network call to Gordon
    uv run python -m gordon_exporter --since 2026-04-20 --dry-run --date 2026-04-25

    # Send to Gordon staging for real
    uv run python -m gordon_exporter --since 2026-04-20 --date 2026-04-25

    # Send to production (once GORDON_PRODUCTION_BASE_URL is configured)
    uv run python -m gordon_exporter --env production --since 2026-04-20 --date 2026-04-25
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from gordon_client import GordonAPIError, GordonClient
from shopify_order_models import LineItem, Order, VariantMetadata, collect_variant_ids
from shopify_orders import build_query_filter, fetch_orders, fetch_variant_metadata

log = logging.getLogger("gordon_exporter")

# Forward-compatible custom-attribute keys. If Yrja later wires a timeslot app
# that writes one of these onto the order, they override the CLI flag per-order.
DELIVERY_DATE_KEYS = ("Delivery Date", "Leveringsdato", "delivery_date", "Leveringsdag")
TIME_WINDOW_KEYS = ("Delivery Time", "Leveringstid", "time_window", "Tidsvindu")

# Shopify custom-attribute keys that mirror the Norwegian checkout label
# "Leilighet, etasje osv. (valgfritt)". If the customer fills this in, we use
# it as the Gordon `notes` value. Otherwise we fall back to ``shipping_address.address2``
# (Shopify's standard apartment/suite field, which Yrja's checkout uses for the
# same information).
NOTES_ATTR_KEYS = (
    "Leilighet, etasje osv. (valgfritt)",
    "Leilighet, etasje osv.",
    "Leilighet",
    "Apartment, floor, etc.",
)

# ISO 3166-1 alpha-2 → E.164 calling code. Only Nordic + a handful of
# frequently shipped-to countries — extend as needed.
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

# Gordon's bulk endpoint has a request limit; chunk conservatively.
BULK_CHUNK_SIZE = 100

# Inventory name used for internal / bulk orders (non-Råvareboks).
INTERNAL_ORDER_INVENTORY_NAME = "Internal order"

# Default temperature zone for every inventory item. Yrja's products are all
# frozen meat / fish, so this matches the physical reality — override per-run
# with --inventory-type if needed (Gordon's enum: ambient | chilled | frozen).
DEFAULT_INVENTORY_TYPE = "frozen"


# ── Helpers ──────────────────────────────────────────────────────────────


def _pick_attribute(order: Order, keys: tuple[str, ...]) -> str | None:
    """Return the first matching order-level custom-attribute value."""
    for key in keys:
        val = order.get_attribute(key)
        if val:
            return val.strip()
    # Also check line-item attributes, since some Shopify apps store them there.
    for li in order.line_items:
        for key in keys:
            val = li.get_attribute(key)
            if val:
                return val.strip()
    return None


def _to_e164(phone: str, country_code: str | None) -> str | None:
    """Normalize a Shopify phone string into E.164 (``+<dial><number>``).

    - If ``phone`` already starts with ``+``, strip spaces/dashes and return it.
    - Otherwise prepend ``+<dial code>`` based on the order's ISO-3166 country.
    - Returns ``None`` if the phone is empty or we can't resolve the dial code.
    """
    if not phone:
        return None
    cleaned = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if cleaned.startswith("+"):
        return cleaned
    dial = COUNTRY_DIAL_CODES.get((country_code or "").upper())
    if not dial:
        return None
    # Defensive: some stores strip the '+' but still prefix the dial code.
    # Only prepend if the number doesn't already start with it AND is short
    # enough that prepending doesn't create an obviously wrong number.
    if cleaned.startswith(dial) and len(cleaned) > len(dial) + 6:
        return "+" + cleaned
    return f"+{dial}{cleaned}"


def _inventory_from_bundle(
    li: LineItem,
    lookup: dict[str, VariantMetadata],
    *,
    inventory_type: str = DEFAULT_INVENTORY_TYPE,
) -> dict[str, Any] | None:
    """Turn a bundle line item into ONE Gordon inventory entry.

    The inventory entry represents the physical box as shipped:
    - ``name`` = the bundle line-item title (e.g. ``"Råvareboks 4"``)
    - ``quantity`` = 1 (one physical box)
    - ``articles[*]`` = one per picked SKU (from ``_pvgid`` custom attributes).
      ``articles[*].quantity`` = Shopify-ordered slot count × the variant's
      ``slot_antall_enheter`` metafield (f-packs per slot, mirrored from
      Notion "SLOT: antall enheter").

    Returns ``None`` if the bundle has no ``_pvgid`` sub-products — caller
    should fall back or skip.
    """
    articles: list[dict[str, Any]] = []
    for attr in li.custom_attributes:
        if not attr.is_pvgid or not attr.variant_id:
            continue
        qty = attr.quantity
        if qty <= 0:
            continue
        meta = lookup.get(attr.variant_id)
        name = meta.sku_name if meta else f"variant:{attr.variant_id}"
        f_packs_per_slot = max(1, meta.slot_antall_enheter) if meta else 1
        articles.append({"name": name, "quantity": qty * f_packs_per_slot})
    if not articles:
        return None
    return {
        "name": li.name,
        "quantity": 1,
        "type": inventory_type,
        "articles": articles,
    }


def _inventory_from_internal_order(
    order: Order,
    lookup: dict[str, VariantMetadata],
    *,
    inventory_type: str = DEFAULT_INVENTORY_TYPE,
) -> dict[str, Any] | None:
    """Turn an internal / bulk order into ONE Gordon inventory entry.

    Internal orders don't use the Råvareboks bundle mechanism — they have
    flat line items with real product names (e.g. wholesale / restaurant
    orders, one-off bulk shipments). All Shopify line items become articles
    under a single ``"Internal order"`` inventory entry with ``quantity: 1``.

    ``articles[].quantity`` = Shopify-ordered line-item quantity × the
    variant's ``slot_antall_enheter`` metafield (f-packs per slot) — same
    rule as the Råvareboks bundles so Gordon's picker sees the real f-pack
    count rather than one 'slot' per article.
    """
    articles: list[dict[str, Any]] = []
    for li in order.line_items:
        if li.quantity <= 0:
            continue
        # Prefer SKU name from the variant metafield (matches bundle article
        # naming); fall back to the Shopify line item title.
        meta = lookup.get(li.variant_id) if li.variant_id else None
        name = meta.sku_name if meta else li.name
        f_packs_per_slot = max(1, meta.slot_antall_enheter) if meta else 1
        articles.append({"name": name, "quantity": li.quantity * f_packs_per_slot})
    if not articles:
        return None
    return {
        "name": INTERNAL_ORDER_INVENTORY_NAME,
        "quantity": 1,
        "type": inventory_type,
        "articles": articles,
    }


def _inventory_from_line_items(
    order: Order,
    lookup: dict[str, VariantMetadata],
    *,
    inventory_type: str = DEFAULT_INVENTORY_TYPE,
) -> list[dict[str, Any]]:
    """Build the Gordon ``inventory`` array from an order's line items.

    One inventory entry per line item (one physical package):
    - For bundle line items ("Råvareboks N"), the entry's ``name`` is the
      bundle title and ``articles`` lists the picked SKUs with their real
      per-SKU f-pack counts.
    - For non-bundle line items, the entry's ``name`` is the line-item name
      and it gets one self-referencing article.
    """
    has_bundle = any(li.is_bundle for li in order.line_items)
    items: list[dict[str, Any]] = []
    for li in order.line_items:
        if li.is_bundle:
            entry = _inventory_from_bundle(li, lookup, inventory_type=inventory_type)
            if entry:
                items.append(entry)
            continue
        # Skip the duplicate non-bundle rows Shopify emits alongside bundles.
        if has_bundle:
            continue
        items.append(
            {
                "name": li.name,
                "quantity": 1,
                "type": inventory_type,
                "articles": [
                    {"name": li.name, "quantity": li.quantity},
                ],
            }
        )
    return items


# ── Payload builder ──────────────────────────────────────────────────────


def build_gordon_order(
    order: Order,
    variant_lookup: dict[str, VariantMetadata] | None = None,
    *,
    default_date: str | None = None,
    default_window: str | None = None,
    delivery_group: str | None = None,
    is_internal: bool = False,
    inventory_type: str = DEFAULT_INVENTORY_TYPE,
) -> dict[str, Any] | None:
    """Map a Shopify ``Order`` to a Gordon ``POST /api/orders/bulk`` entry.

    ``deliverydate`` is the day the courier should deliver to the recipient
    (YYYY-MM-DD). ``time-window`` is the time window on that date, format
    ``"HH:mm - HH:mm"``. Both are required per Gordon's schema; they come from
    per-order custom attributes if present, otherwise the batch-level CLI
    defaults.

    Returns ``None`` if required Gordon fields can't be filled in (the caller
    should log + skip). Schema:
    ``developer.gordondelivery.com/reference/add-orders-bulk``.
    """
    addr = order.shipping_address
    deliverydate = _pick_attribute(order, DELIVERY_DATE_KEYS) or default_date
    time_window = _pick_attribute(order, TIME_WINDOW_KEYS) or default_window
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
    if not deliverydate:
        missing.append("deliverydate")
    if not time_window:
        missing.append("time-window")

    if missing:
        log.warning(
            "Skipping order %s — missing required fields: %s",
            order.name,
            ", ".join(missing),
        )
        return None

    # Gordon wants a numeric external_ref with a "1000" prefix, not Shopify's
    # "#1040" order name. Strip non-digit characters and prepend "1000".
    clean_name = "".join(ch for ch in order.name if ch.isdigit())
    external_ref = f"1000{clean_name}" if clean_name else order.name

    payload: dict[str, Any] = {
        "external_ref": external_ref,
        "customer-name": addr.name,
        "address": addr.address1,
        "zip": addr.zip,
        "city": addr.city,
        "deliverydate": deliverydate,
        "time-window": time_window,
    }

    # Optional fields — only include when populated so we don't send empty strings.
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

    if is_internal:
        entry = _inventory_from_internal_order(
            order, variant_lookup or {}, inventory_type=inventory_type
        )
        inventory = [entry] if entry else []
    else:
        inventory = _inventory_from_line_items(
            order, variant_lookup or {}, inventory_type=inventory_type
        )
    if inventory:
        payload["inventory"] = inventory

    return payload


# ── CLI ───────────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Push Shopify orders to Gordon Delivery.",
    )
    p.add_argument(
        "--env",
        choices=["test", "production"],
        default=None,
        help="Gordon environment (default: value of GORDON_ENV, or 'test').",
    )
    p.add_argument(
        "--base-url",
        default=None,
        help=(
            "Override the Gordon base URL (e.g. "
            "'https://backend.aws.gordondelivery.com'). Takes precedence over "
            "--env. Useful when Gordon hands out a new environment URL."
        ),
    )
    p.add_argument(
        "--status",
        choices=["unfulfilled", "partial", "fulfilled", "any"],
        default="unfulfilled",
        help="Shopify fulfillment status filter (default: unfulfilled).",
    )
    p.add_argument("--since", help="Only orders created on or after YYYY-MM-DD.")
    p.add_argument("--until", help="Only orders created on or before YYYY-MM-DD.")
    p.add_argument(
        "--name",
        help='Only the Shopify order with this name, e.g. "#1040" (the "#" is optional).',
    )
    p.add_argument(
        "--exclude",
        action="append",
        default=[],
        help=(
            'Skip a specific Shopify order name, e.g. --exclude "#1035". '
            "Can be repeated."
        ),
    )
    p.add_argument(
        "--include-orders",
        action="append",
        default=[],
        help=(
            'Only export orders whose name matches one of these. '
            'Takes precedence over --since/--until/--status/--name. '
            'E.g. --include-orders "#1032" --include-orders "#1037". '
            "Can be repeated."
        ),
    )
    p.add_argument(
        "--inventory-type",
        choices=["ambient", "chilled", "frozen"],
        default=DEFAULT_INVENTORY_TYPE,
        help=(
            f"Temperature zone for every inventory item Gordon delivers. "
            f"Defaults to {DEFAULT_INVENTORY_TYPE!r} since Yrja ships frozen meat/fish."
        ),
    )
    p.add_argument(
        "--internal-orders",
        action="append",
        default=[],
        help=(
            'Treat a Shopify order as an internal / bulk order, e.g. '
            '--internal-orders "#1035". Its inventory is collapsed into a '
            'single "Internal order" entry with every line item as an '
            'article. Can be repeated.'
        ),
    )
    p.add_argument(
        "--tag-exclude",
        default="Test",
        help="Shopify tag to exclude (default: 'Test' — so Shopify test orders never go to Gordon).",
    )
    p.add_argument(
        "--date",
        help=(
            "deliverydate (YYYY-MM-DD) — the date the courier should deliver "
            "to the recipient. Applied to all orders that don't carry their own "
            "delivery-date custom attribute."
        ),
    )
    p.add_argument(
        "--window",
        help=(
            'time-window value Gordon expects, format "HH:mm - HH:mm" '
            '(e.g. "08:00 - 22:00"). Applied to all orders that don\'t carry '
            'their own time-window custom attribute.'
        ),
    )
    p.add_argument(
        "--delivery-group",
        default=None,
        help="Gordon delivery group name (overrides GORDON_DELIVERY_GROUP).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print payloads but make no network call to Gordon.",
    )
    p.add_argument(
        "--test-auth",
        action="store_true",
        help="Only exchange credentials for a Gordon token and report, then exit.",
    )
    p.add_argument(
        "--limit", type=int, default=None, help="Stop after N orders (debug)."
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def _chunk(seq: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.test_auth:
        with GordonClient(
            env=args.env,
            delivery_group=args.delivery_group,
            base_url=args.base_url,
        ) as client:
            info = client.test_auth()
        print(json.dumps(info, indent=2))
        return

    if not args.dry_run and (not args.date or not args.window):
        print(
            "Error: --date and --window are required unless --dry-run is set.",
            file=sys.stderr,
        )
        sys.exit(2)

    # 1. Fetch Shopify orders.
    query_filter = build_query_filter(
        status=args.status,
        since=args.since,
        until=args.until,
        tag_exclude=args.tag_exclude,
        name=args.name,
    )
    log.info("Fetching Shopify orders (filter: %s) …", query_filter or "none")
    orders = fetch_orders(query_filter, limit=args.limit)
    log.info(
        "Fetched %d orders with %d total line items",
        len(orders),
        sum(len(o.line_items) for o in orders),
    )

    if not orders:
        print("No orders to forward. Exiting.")
        return

    # 2. Variant metafield lookup for friendly SKU names + slot_antall_enheter.
    # collect_variant_ids pulls both `_pvgid` bundle children and regular
    # line-item variant.id (needed for internal / bulk orders).
    variant_ids = collect_variant_ids(orders)
    variant_lookup: dict[str, VariantMetadata] = {}
    if variant_ids:
        variant_lookup = fetch_variant_metadata(variant_ids)

    # 3. Build Gordon payloads.
    excluded_norm: set[str] = {
        "".join(ch for ch in e if ch.isdigit()) for e in (args.exclude or [])
    }
    internal_norm: set[str] = {
        "".join(ch for ch in e if ch.isdigit())
        for e in (args.internal_orders or [])
    }
    include_norm: set[str] = {
        "".join(ch for ch in e if ch.isdigit())
        for e in (args.include_orders or [])
    }
    payloads: list[dict[str, Any]] = []
    skipped = 0
    for o in orders:
        order_digits = "".join(ch for ch in o.name if ch.isdigit())
        if include_norm and order_digits not in include_norm:
            skipped += 1
            continue
        if excluded_norm and order_digits in excluded_norm:
            log.info("Skipping order %s (matched --exclude)", o.name)
            skipped += 1
            continue
        is_internal = bool(internal_norm and order_digits in internal_norm)
        if is_internal:
            log.info("Treating order %s as internal/bulk", o.name)
        payload = build_gordon_order(
            o,
            variant_lookup,
            default_date=args.date,
            default_window=args.window,
            delivery_group=args.delivery_group,
            is_internal=is_internal,
            inventory_type=args.inventory_type,
        )
        if payload is None:
            skipped += 1
            continue
        payloads.append(payload)

    log.info("Built %d Gordon payloads (skipped %d)", len(payloads), skipped)

    # 4. Dry-run stops here.
    if args.dry_run:
        print(json.dumps(payloads, indent=2, ensure_ascii=False))
        return

    if not payloads:
        print("Nothing to send to Gordon after filtering. Exiting.")
        return

    # 5. POST to Gordon in chunks.
    with GordonClient(
        env=args.env,
        delivery_group=args.delivery_group,
        base_url=args.base_url,
    ) as client:
        log.info(
            "Sending %d orders to Gordon (%s, base=%s) in chunks of %d",
            len(payloads),
            client.env,
            client.base_url,
            BULK_CHUNK_SIZE,
        )
        created = 0
        errors: list[tuple[str, str]] = []
        for chunk_idx, chunk in enumerate(_chunk(payloads, BULK_CHUNK_SIZE), 1):
            try:
                resp = client.create_orders_bulk(chunk)
                created += len(chunk)
                log.info(
                    "Chunk %d/%d: %d orders accepted (response=%r)",
                    chunk_idx,
                    (len(payloads) + BULK_CHUNK_SIZE - 1) // BULK_CHUNK_SIZE,
                    len(chunk),
                    resp,
                )
            except GordonAPIError as e:
                # On a chunk-level 400, log and fall back to per-order retry so
                # a single bad row doesn't poison the whole chunk.
                log.warning(
                    "Chunk %d failed (%d): %r — retrying individually",
                    chunk_idx,
                    e.status_code,
                    e.body,
                )
                for p in chunk:
                    try:
                        client.create_order(p)
                        created += 1
                    except GordonAPIError as inner:
                        errors.append((p["external_ref"], f"{inner.status_code}: {inner.body!r}"))

    print(
        f"Done. Gordon created={created}, "
        f"shopify_skipped={skipped}, gordon_errors={len(errors)}"
    )
    if errors:
        print("Errors:", file=sys.stderr)
        for ref, msg in errors:
            print(f"  {ref}: {msg}", file=sys.stderr)


if __name__ == "__main__":
    main()
