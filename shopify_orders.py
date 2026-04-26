"""Fetch Shopify orders with the richer query needed for delivery handoff.

``shopify_client.py`` already talks to the Admin GraphQL API, but its
``RECENT_ORDERS_*_QUERY`` queries strip out the shipping address, email, and
note fields we need to hand off to Gordon Delivery. Rather than edit the
existing client (which other tabs in ``app.py`` rely on), this module runs the
vendored ``ORDERS_QUERY`` directly with the same throttling behaviour.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from shopify_order_models import Order, VariantMetadata
from shopify_order_queries import ORDERS_QUERY, VARIANT_METAFIELDS_QUERY

load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger(__name__)

API_VERSION = "2026-01"
PAGE_SIZE = 50


def _graphql_endpoint(shop_domain: str) -> str:
    return f"https://{shop_domain}/admin/api/{API_VERSION}/graphql.json"


def _execute(
    http: httpx.Client,
    url: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"query": query}
    if variables:
        payload["variables"] = variables

    resp = http.post(url, json=payload)
    resp.raise_for_status()
    body = resp.json()

    if "errors" in body:
        for err in body["errors"]:
            if "THROTTLED" in str(err.get("extensions", {}).get("code", "")):
                cost = body.get("extensions", {}).get("cost", {})
                restore_rate = cost.get("throttleStatus", {}).get("restoreRate", 50)
                requested = cost.get("requestedQueryCost", 100)
                wait = requested / restore_rate + 1
                log.warning("Shopify throttled — waiting %.1fs", wait)
                time.sleep(wait)
                return _execute(http, url, query, variables)
        raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")

    return body["data"]


def _build_http(access_token: str) -> httpx.Client:
    return httpx.Client(
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
        timeout=30.0,
    )


def fetch_orders(
    query_filter: str | None = None,
    *,
    shop_domain: str | None = None,
    access_token: str | None = None,
    limit: int | None = None,
) -> list[Order]:
    """Fetch all orders matching *query_filter*.

    Args:
        query_filter: Shopify search syntax, e.g.
            ``"fulfillment_status:unfulfilled created_at:>=2026-04-20 tag_not:Test"``.
        shop_domain / access_token: optional overrides; otherwise read from env.
        limit: stop after this many orders (None = fetch everything).

    Returns newest-first list of typed ``Order`` objects.
    Requires ``read_orders`` scope on the Shopify app.
    """
    shop = shop_domain or os.environ.get("SHOPIFY_SHOP_DOMAIN", "")
    token = access_token or os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    if not shop or not token:
        raise ValueError(
            "Shopify credentials required. Set SHOPIFY_SHOP_DOMAIN and "
            "SHOPIFY_ACCESS_TOKEN in .env or pass as arguments."
        )

    url = _graphql_endpoint(shop)
    http = _build_http(token)
    orders: list[Order] = []
    cursor: str | None = None
    page = 0

    try:
        while True:
            page += 1
            variables: dict[str, Any] = {"first": PAGE_SIZE}
            if cursor:
                variables["after"] = cursor
            if query_filter:
                variables["query"] = query_filter

            data = _execute(http, url, ORDERS_QUERY, variables)
            connection = data["orders"]

            for edge in connection["edges"]:
                orders.append(Order.from_graphql(edge["node"]))
                if limit is not None and len(orders) >= limit:
                    log.info("Reached --limit=%d, stopping", limit)
                    return orders

            log.info(
                "Page %d: fetched %d orders (total so far: %d)",
                page,
                len(connection["edges"]),
                len(orders),
            )

            page_info = connection["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]
    finally:
        http.close()

    return orders


def fetch_variant_metadata(
    variant_ids: list[str],
    *,
    shop_domain: str | None = None,
    access_token: str | None = None,
) -> dict[str, VariantMetadata]:
    """Fetch custom metafields for variants, keyed by numeric variant ID."""
    shop = shop_domain or os.environ.get("SHOPIFY_SHOP_DOMAIN", "")
    token = access_token or os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    if not shop or not token:
        raise ValueError("Shopify credentials required")

    if not variant_ids:
        return {}

    url = _graphql_endpoint(shop)
    http = _build_http(token)
    result: dict[str, VariantMetadata] = {}

    try:
        for i in range(0, len(variant_ids), 50):
            batch = variant_ids[i : i + 50]
            gids = [f"gid://shopify/ProductVariant/{vid}" for vid in batch]
            data = _execute(http, url, VARIANT_METAFIELDS_QUERY, {"ids": gids})

            for node in data.get("nodes", []):
                if not node or "id" not in node:
                    continue
                numeric_id = node["id"].rsplit("/", 1)[-1]
                metafields: dict[str, str] = {}
                for edge in node.get("metafields", {}).get("edges", []):
                    mf = edge["node"]
                    metafields[mf["key"]] = mf["value"]
                result[numeric_id] = VariantMetadata(
                    variant_id=numeric_id,
                    display_name=node.get("displayName", "") or "",
                    metafields=metafields,
                )
    finally:
        http.close()

    log.info("Fetched metafields for %d / %d variants", len(result), len(variant_ids))
    return result


def build_query_filter(
    *,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    tag_exclude: str | None = None,
    name: str | None = None,
) -> str | None:
    """Compose a Shopify search query from the CLI flags."""
    parts: list[str] = []
    if name:
        # Shopify search uses the numeric part without the '#' prefix.
        parts.append(f"name:{name.lstrip('#').strip()}")
    if status and status != "any":
        parts.append(f"fulfillment_status:{status}")
    if since:
        parts.append(f"created_at:>={since}")
    if until:
        parts.append(f"created_at:<={until}")
    if tag_exclude:
        parts.append(f"tag_not:{tag_exclude}")
    return " ".join(parts) if parts else None
