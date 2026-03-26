"""Shopify GraphQL client — fetch product variant metafields.

Follows the pattern from yrja-fulfilment-analytics/src/client.py.
Requires SHOPIFY_SHOP_DOMAIN and SHOPIFY_ACCESS_TOKEN in .env.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger(__name__)

API_VERSION = "2026-01"
PAGE_SIZE = 50


# ── GraphQL queries ──────────────────────────────────────────────────────

PRODUCTS_WITH_METAFIELDS_QUERY = """
query ProductsWithMetafields($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      node {
        title
        variants(first: 50) {
          edges {
            node {
              id
              displayName
              price
              metafields(first: 25, namespace: "custom") {
                edges {
                  node {
                    key
                    value
                    type
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


# ── Data model ───────────────────────────────────────────────────────────


@dataclass
class VariantMetafields:
    """Metafield data for a single product variant."""

    variant_id: str
    product_title: str
    display_name: str
    price: str  # base variant price from Shopify
    metafields: dict[str, str] = field(default_factory=dict)

    def get_float(self, *keys: str) -> float | None:
        """Try multiple metafield keys, return the first valid float."""
        for key in keys:
            val = self.metafields.get(key, "")
            if val:
                try:
                    return float(val.replace(",", "."))
                except (ValueError, TypeError):
                    continue
        return None

    @property
    def price_per_kg(self) -> float | None:
        """Customer-facing price per kg from metafields."""
        return self.get_float(
            "price_per_kg", "pris_per_kg",
            "utpris_per_kg", "utpris",
        )

    @property
    def price_per_portion(self) -> float | None:
        """Customer-facing price per portion from metafields."""
        return self.get_float(
            "price_per_portion", "pris_per_porsjon",
            "pris_porsjon", "portion_price",
        )

    @property
    def sku_name(self) -> str:
        """SKU name from metafields."""
        return self.metafields.get("sku_name", "") or self.metafields.get("name", "") or self.display_name

    @property
    def porsjoner(self) -> float | None:
        """Number of portions per slot."""
        return self.get_float("porsjoner")

    @property
    def slot_antall_enheter(self) -> float | None:
        """SLOT: antall enheter (f-packs per slot)."""
        return self.get_float("slot_antall_enheter")

    @property
    def slot_fpack_kg(self) -> float | None:
        """SLOT: f-pack weight in kg."""
        return self.get_float("slot_fpack_kg")


# ── Client ───────────────────────────────────────────────────────────────


class ShopifyClient:
    """Thin wrapper around the Shopify GraphQL Admin API."""

    def __init__(
        self,
        shop_domain: str | None = None,
        access_token: str | None = None,
    ) -> None:
        shop = shop_domain or os.environ.get("SHOPIFY_SHOP_DOMAIN", "")
        token = access_token or os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
        if not shop or not token:
            raise ValueError(
                "Shopify credentials required. Set SHOPIFY_SHOP_DOMAIN and "
                "SHOPIFY_ACCESS_TOKEN in .env or pass as arguments."
            )
        self.url = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
        self._http = httpx.Client(
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": token,
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
        resp.raise_for_status()
        body = resp.json()

        if "errors" in body:
            for err in body["errors"]:
                code = str(err.get("extensions", {}).get("code", ""))
                if "THROTTLED" in code:
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

    def fetch_all_variant_metafields(self) -> dict[str, VariantMetafields]:
        """Fetch all products with variant metafields.

        Returns a dict mapping product_title → VariantMetafields for the
        first variant of each product.
        """
        result: dict[str, VariantMetafields] = {}
        cursor: str | None = None
        page = 0

        while True:
            page += 1
            variables: dict[str, Any] = {"first": PAGE_SIZE}
            if cursor:
                variables["after"] = cursor

            data = self._execute(PRODUCTS_WITH_METAFIELDS_QUERY, variables)
            connection = data["products"]

            for edge in connection["edges"]:
                product = edge["node"]
                title = product["title"]
                variants = product.get("variants", {}).get("edges", [])
                if not variants:
                    continue

                # Use the first variant (most products have one)
                var_node = variants[0]["node"]
                metafields: dict[str, str] = {}
                for mf_edge in var_node.get("metafields", {}).get("edges", []):
                    mf = mf_edge["node"]
                    metafields[mf["key"]] = mf["value"]

                result[title] = VariantMetafields(
                    variant_id=var_node["id"].rsplit("/", 1)[-1],
                    product_title=title,
                    display_name=var_node.get("displayName", title),
                    price=var_node.get("price", "0"),
                    metafields=metafields,
                )

            page_info = connection["pageInfo"]
            log.info(
                "Page %d: fetched %d products (total: %d)",
                page,
                len(connection["edges"]),
                len(result),
            )

            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

        return result

    def close(self) -> None:
        self._http.close()


# ── Convenience ──────────────────────────────────────────────────────────


def has_shopify_credentials() -> bool:
    """Check if Shopify credentials are available."""
    return bool(
        os.environ.get("SHOPIFY_SHOP_DOMAIN")
        and os.environ.get("SHOPIFY_ACCESS_TOKEN")
    )
