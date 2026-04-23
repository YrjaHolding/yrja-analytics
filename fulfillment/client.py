"""Shopify GraphQL Admin API client (fulfillment pipeline).

Kept separate from the main ``shopify_client.py`` because the fulfillment flow
needs full shipping addresses + line-item custom attributes and queries variant
metafields by explicit ID list, whereas the main client paginates all products.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_VERSION = "2026-01"
PAGE_SIZE = 50  # conservative to keep query cost low


class ShopifyClient:
    """Thin wrapper around the Shopify GraphQL Admin API."""

    def __init__(self, shop_domain: str, access_token: str) -> None:
        self.url = f"https://{shop_domain}/admin/api/{API_VERSION}/graphql.json"
        self._http = httpx.Client(
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": access_token,
            },
            timeout=30.0,
        )

    # ── Public ───────────────────────────────────────────────────────────

    def fetch_orders(
        self,
        query_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all orders matching *query_filter*, handling pagination."""
        from .queries import ORDERS_QUERY

        orders: list[dict[str, Any]] = []
        cursor: str | None = None
        page = 0

        while True:
            page += 1
            variables: dict[str, Any] = {"first": PAGE_SIZE}
            if cursor:
                variables["after"] = cursor
            if query_filter:
                variables["query"] = query_filter

            data = self._execute(ORDERS_QUERY, variables)
            connection = data["orders"]

            for edge in connection["edges"]:
                orders.append(edge["node"])

            page_info = connection["pageInfo"]
            log.info(
                "Page %d: fetched %d orders (total so far: %d)",
                page,
                len(connection["edges"]),
                len(orders),
            )

            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

        return orders

    # ── Internal ─────────────────────────────────────────────────────────

    def _execute(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        resp = self._http.post(self.url, json=payload)
        resp.raise_for_status()
        body = resp.json()

        # Handle throttling
        if "errors" in body:
            for err in body["errors"]:
                if "THROTTLED" in str(err.get("extensions", {}).get("code", "")):
                    cost = body.get("extensions", {}).get("cost", {})
                    restore_rate = cost.get("throttleStatus", {}).get(
                        "restoreRate", 50
                    )
                    requested = cost.get("requestedQueryCost", 100)
                    wait = requested / restore_rate + 1
                    log.warning("Throttled — waiting %.1fs", wait)
                    time.sleep(wait)
                    return self._execute(query, variables)
            raise RuntimeError(f"GraphQL errors: {body['errors']}")

        return body["data"]

    def fetch_variant_metafields(
        self,
        variant_ids: list[str],
    ) -> dict[str, Any]:
        """Fetch custom metafields for variants by numeric ID.

        Returns a dict mapping numeric variant ID → VariantMetadata.
        """
        from .models import VariantMetadata
        from .queries import VARIANT_METAFIELDS_QUERY

        if not variant_ids:
            return {}

        result: dict[str, VariantMetadata] = {}
        # Batch in groups of 50 to stay within query cost limits
        for i in range(0, len(variant_ids), 50):
            batch = variant_ids[i : i + 50]
            gids = [f"gid://shopify/ProductVariant/{vid}" for vid in batch]
            data = self._execute(VARIANT_METAFIELDS_QUERY, {"ids": gids})

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
                    display_name=node.get("displayName", ""),
                    metafields=metafields,
                )

        log.info("Fetched metafields for %d / %d variants", len(result), len(variant_ids))
        return result

    def close(self) -> None:
        self._http.close()
