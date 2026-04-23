"""Flatten Shopify orders into dataframes for CSV/Excel export and pick lists."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd

from .models import Order, VariantMetadata

log = logging.getLogger(__name__)


# ── Flattening ───────────────────────────────────────────────────────────


def _order_base_row(order: Order) -> dict[str, Any]:
    """Fields shared by every row belonging to an order."""
    addr = order.shipping_address
    return {
        "order_number": order.name,
        "created_at": order.created_at,
        "financial_status": order.financial_status,
        "fulfillment_status": order.fulfillment_status,
        "shipping_name": addr.name,
        "shipping_address1": addr.address1,
        "shipping_address2": addr.address2,
        "shipping_city": addr.city,
        "shipping_zip": addr.zip,
        "shipping_country": addr.country_code,
        "shipping_phone": addr.phone,
        "order_note": order.note,
    }


def _collect_attribute_keys(orders: list[Order]) -> list[str]:
    """Discover all unique customAttribute keys across all line items."""
    keys: dict[str, None] = {}  # ordered dict to preserve discovery order
    for order in orders:
        for li in order.line_items:
            for attr in li.custom_attributes:
                if attr.key not in keys:
                    keys[attr.key] = None
    return list(keys)


def flatten_orders(
    orders: list[Order],
    *,
    include_shopify_internal: bool = False,
) -> pd.DataFrame:
    """One row per line item, all customAttributes as columns."""
    attr_keys = _collect_attribute_keys(orders)
    if not include_shopify_internal:
        attr_keys = [k for k in attr_keys if not k.startswith("__shopify")]

    rows: list[dict[str, Any]] = []
    for order in orders:
        base = _order_base_row(order)
        for li in order.line_items:
            row = {
                **base,
                "line_item_name": li.name,
                "sku": li.sku,
                "quantity": li.quantity,
                "unit_price": li.unit_price,
                "currency": li.currency,
                "is_bundle": li.is_bundle,
            }
            # Add every custom attribute as its own column
            for key in attr_keys:
                row[f"attr:{key}"] = li.get_attribute(key) or ""
            rows.append(row)

    return pd.DataFrame(rows)


def _parse_weight_kg(weight_str: str) -> float:
    """Parse a weight string like '0.9 kg' or '1.27 kg' into a float."""
    m = re.match(r"([\d.,]+)\s*kg", weight_str.strip())
    if m:
        return float(m.group(1).replace(",", "."))
    return 0.0


def flatten_orders_exploded(
    orders: list[Order],
    variant_lookup: dict[str, VariantMetadata] | None = None,
) -> pd.DataFrame:
    """Explode bundle line items into a warehouse pick list.

    Each bundle gets a parent row followed by one row per sub-product.
    Non-bundle line items in orders that contain bundles are skipped
    (they are duplicates of the bundle contents).
    """
    lookup = variant_lookup or {}
    rows: list[dict[str, Any]] = []

    for order in orders:
        has_bundle = any(li.is_bundle for li in order.line_items)

        for li in order.line_items:
            if not li.is_bundle:
                if has_bundle:
                    # Skip non-bundle items in orders with bundles (duplicates)
                    continue
                rows.append(
                    {
                        "order_number": order.name,
                        "bundle_order_name": "",
                        "SKU_name": li.name,
                        "number_of_SKUs": "",
                    }
                )
                continue

            # Parent row for the bundle
            rows.append(
                {
                    "order_number": order.name,
                    "bundle_order_name": li.name,
                    "SKU_name": "",
                    "number_of_SKUs": 0,
                }
            )

            # Sub-product rows
            sub_index = 0
            for attr in li.custom_attributes:
                if not attr.is_pvgid:
                    continue
                sub_index += 1
                variant_id = attr.variant_id or ""
                qty = attr.quantity

                # Look up SKU name and f-packs from variant metafields
                variant_meta = lookup.get(variant_id)
                sku_name = variant_meta.sku_name if variant_meta else ""
                f_packs_per_unit = 0
                if variant_meta and variant_meta.slot_antall_enheter > 0:
                    f_packs_per_unit = variant_meta.slot_antall_enheter

                number_of_skus = qty * f_packs_per_unit if f_packs_per_unit else ""

                rows.append(
                    {
                        "order_number": order.name,
                        "bundle_order_name": "",
                        "SKU_name": sku_name,
                        "number_of_SKUs": number_of_skus,
                    }
                )

    return pd.DataFrame(rows)


# ── Export ────────────────────────────────────────────────────────────


def export_dataframe(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".xlsx":
        df.to_excel(path, index=False, engine="openpyxl")
    else:
        df.to_csv(path, index=False)
    log.info("Exported %d rows to %s", len(df), path)


# ── Query filter builder ─────────────────────────────────────────────────


def build_query_filter(
    *,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    exclude_tags: list[str] | None = None,
) -> str | None:
    parts: list[str] = []
    if status:
        parts.append(f"fulfillment_status:{status}")
    if since:
        parts.append(f"created_at:>={since}")
    if until:
        parts.append(f"created_at:<={until}")
    for tag in exclude_tags or []:
        if tag:
            parts.append(f"tag_not:{tag}")
    return " ".join(parts) if parts else None
