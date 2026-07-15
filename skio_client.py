"""Skio GraphQL client — fetch subscription orders for the Ordrestatus tab.

Skio's public GraphQL API is a Hasura endpoint. Authentication uses an
``Authorization`` header with the literal ``API `` prefix in front of the token
(case-sensitive, per Skio's own docs).

Generate the token in: Skio dashboard → API & Integrations → API. Store as
``SKIO_API_TOKEN`` in ``.env``. The endpoint can be overridden via
``SKIO_GRAPHQL_URL`` if Skio rotates it.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger(__name__)

DEFAULT_SKIO_GRAPHQL_URL = "https://graphql.skio.com/v1/graphql"
PAGE_SIZE = 50


# ── GraphQL query ────────────────────────────────────────────────────────

# Hasura-style query. Skio exposes ``Orders`` as the root field (capitalised).
# Filters use ``_gte`` / ``_lte`` comparators on ``createdAt``. We exclude
# cancelled orders by default since cancelled subscription orders should not
# count toward inventory needs.
ORDERS_QUERY = """
query SkioOrders($limit: Int!, $offset: Int!, $where: Orders_bool_exp!) {
  Orders(
    limit: $limit
    offset: $offset
    where: $where
    order_by: { createdAt: desc }
  ) {
    id
    createdAt
    cancelledAt
    shopifyId
    platformNumber
    OrderLineItems {
      id
      quantity
      customAttributes
      ProductVariant {
        shopifyId
        sku
        title
        Product {
          title
        }
      }
    }
  }
}
"""


# ── Data model ───────────────────────────────────────────────────────────


@dataclass
class SkioLineItem:
    """A single line item in a Skio order."""

    line_item_id: str
    title: str
    quantity: int
    sku: str | None
    variant_id: str | None  # numeric Shopify variant ID, parsed from gid://
    product_title: str | None
    custom_attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class SkioOrder:
    """A Skio (subscription) order."""

    order_id: str
    shopify_order_id: str | None  # numeric Shopify order ID, parsed from gid://
    platform_number: str | None  # e.g. "1042"
    created_at: str
    cancelled_at: str | None
    line_items: list[SkioLineItem] = field(default_factory=list)


# ── Client ───────────────────────────────────────────────────────────────


class SkioClient:
    """Thin wrapper around the Skio GraphQL API (Hasura)."""

    def __init__(
        self,
        api_token: str | None = None,
        graphql_url: str | None = None,
    ) -> None:
        token = api_token or os.environ.get("SKIO_API_TOKEN", "")
        if not token:
            raise ValueError(
                "Skio credentials required. Set SKIO_API_TOKEN in .env or "
                "pass as argument."
            )
        self.url = (
            graphql_url
            or os.environ.get("SKIO_GRAPHQL_URL")
            or DEFAULT_SKIO_GRAPHQL_URL
        )
        # Skio is strict: the prefix must be ``API `` (uppercase, space, then
        # the token). Anything else returns ``field not found in query_root``.
        self._http = httpx.Client(
            headers={
                "Content-Type": "application/json",
                "Authorization": f"API {token}",
            },
            timeout=30.0,
        )

    def _execute(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = self._http.post(self.url, json=payload)
        # Skio uses Hasura, which returns 200 even for GraphQL errors. But
        # rate-limit responses can be 429 — retry with backoff once.
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "2"))
            log.warning("Skio rate-limited — sleeping %.1fs", retry_after)
            time.sleep(retry_after)
            resp = self._http.post(self.url, json=payload)

        resp.raise_for_status()
        body = resp.json()

        if "errors" in body:
            raise RuntimeError(f"Skio GraphQL errors: {body['errors']}")

        return body["data"]

    def fetch_orders(
        self,
        from_iso: str,
        to_iso: str,
        limit: int | None = None,
        include_cancelled: bool = False,
    ) -> list[SkioOrder]:
        """Fetch Skio orders between two ISO dates.

        Args:
            from_iso: inclusive lower bound on ``createdAt`` (e.g. ``"2026-04-01"``).
            to_iso: inclusive upper bound on ``createdAt``.
            limit: stop after this many orders (None = fetch everything).
            include_cancelled: if False (default), skip orders with a non-null
                ``cancelledAt``.

        Returns newest-first list of typed ``SkioOrder`` objects.
        """
        where: dict[str, Any] = {
            "createdAt": {"_gte": from_iso, "_lte": to_iso},
        }
        if not include_cancelled:
            where["cancelledAt"] = {"_is_null": True}

        orders: list[SkioOrder] = []
        offset = 0
        page = 0

        while True:
            page += 1
            page_size = PAGE_SIZE
            if limit is not None:
                remaining = limit - len(orders)
                if remaining <= 0:
                    break
                page_size = min(PAGE_SIZE, remaining)

            variables = {
                "limit": page_size,
                "offset": offset,
                "where": where,
            }
            data = self._execute(ORDERS_QUERY, variables)
            batch = data.get("Orders", []) or []

            for node in batch:
                orders.append(_parse_order(node))

            log.info(
                "Skio page %d: fetched %d orders (total: %d)",
                page,
                len(batch),
                len(orders),
            )

            if len(batch) < page_size:
                break
            offset += page_size

        return orders

    def close(self) -> None:
        self._http.close()


# ── Parsing helpers ──────────────────────────────────────────────────────


def _parse_gid_numeric(gid: str | None) -> str | None:
    """``gid://shopify/ProductVariant/12345`` → ``"12345"``."""
    if not gid:
        return None
    return gid.rsplit("/", 1)[-1]


def _parse_custom_attributes(raw: Any) -> dict[str, str]:
    """Skio returns ``customAttributes`` as a JSON-shaped value.

    Tolerated shapes:
        * list of ``{"key": str, "value": str}`` (most common)
        * dict ``{key: value}`` (already flattened)
        * None / missing → empty dict
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        result: dict[str, str] = {}
        for item in raw:
            if isinstance(item, dict) and "key" in item:
                result[str(item["key"])] = str(item.get("value", ""))
        return result
    return {}


def _parse_order(node: dict[str, Any]) -> SkioOrder:
    line_items: list[SkioLineItem] = []
    for li in node.get("OrderLineItems", []) or []:
        variant = li.get("ProductVariant") or {}
        product = (variant.get("Product") or {}) if isinstance(variant, dict) else {}
        line_items.append(
            SkioLineItem(
                line_item_id=str(li.get("id", "")),
                title=str(variant.get("title") or product.get("title") or ""),
                quantity=int(li.get("quantity") or 0),
                sku=variant.get("sku"),
                variant_id=_parse_gid_numeric(variant.get("shopifyId")),
                product_title=product.get("title"),
                custom_attributes=_parse_custom_attributes(li.get("customAttributes")),
            )
        )

    return SkioOrder(
        order_id=str(node.get("id", "")),
        shopify_order_id=_parse_gid_numeric(node.get("shopifyId")),
        platform_number=node.get("platformNumber"),
        created_at=str(node.get("createdAt", "")),
        cancelled_at=node.get("cancelledAt"),
        line_items=line_items,
    )


# ── Convenience ──────────────────────────────────────────────────────────


def has_skio_credentials() -> bool:
    """Check if a Skio API token is configured."""
    return bool(os.environ.get("SKIO_API_TOKEN"))


def iter_line_items(orders: Iterable[SkioOrder]) -> Iterable[SkioLineItem]:
    """Flatten orders → line items for callers that don't care about the order."""
    for order in orders:
        yield from order.line_items
