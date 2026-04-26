"""Typed dataclasses for Shopify order data.

Vendored from ``yrja-fulfilment-analytics/src/models.py``. See
``shopify_order_queries.py`` for the GraphQL queries these are parsed from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Property:
    key: str
    value: str

    @property
    def is_hidden(self) -> bool:
        """Underscore-prefixed properties are hidden from customers."""
        return self.key.startswith("_")

    @property
    def is_shopify_internal(self) -> bool:
        return self.key.startswith("__shopify")

    @property
    def is_pvgid(self) -> bool:
        return self.key.startswith("_pvgid://shopify/ProductVariant/")

    @property
    def variant_id(self) -> str | None:
        """Extract the numeric variant ID from a _pvgid key."""
        if not self.is_pvgid:
            return None
        m = re.search(r"/ProductVariant/(\d+)$", self.key)
        return m.group(1) if m else None

    @property
    def quantity(self) -> int:
        """Parse the quantity value (used for _pvgid properties)."""
        try:
            return int(self.value)
        except (ValueError, TypeError):
            return 0


@dataclass
class LineItem:
    id: str
    name: str
    quantity: int
    sku: str
    unit_price: str
    currency: str
    variant_id: str | None = None
    custom_attributes: list[Property] = field(default_factory=list)

    @property
    def is_bundle(self) -> bool:
        """True if this line item contains _pvgid sub-product references."""
        return any(p.is_pvgid for p in self.custom_attributes)

    def get_attribute(self, key: str) -> str | None:
        for p in self.custom_attributes:
            if p.key == key:
                return p.value
        return None

    @classmethod
    def from_graphql(cls, node: dict[str, Any]) -> LineItem:
        price_set = node.get("originalUnitPriceSet", {}).get("shopMoney", {})
        variant = node.get("variant") or {}
        variant_gid = variant.get("id") or ""
        variant_id = variant_gid.rsplit("/", 1)[-1] if variant_gid else None
        return cls(
            id=node["id"],
            name=node["name"],
            quantity=node["quantity"],
            sku=node.get("sku") or "",
            unit_price=price_set.get("amount", "0"),
            currency=price_set.get("currencyCode", "NOK"),
            variant_id=variant_id,
            custom_attributes=[
                Property(key=a["key"], value=a["value"] or "")
                for a in node.get("customAttributes", [])
            ],
        )


@dataclass
class ShippingAddress:
    name: str = ""
    address1: str = ""
    address2: str = ""
    city: str = ""
    zip: str = ""
    province_code: str = ""
    country_code: str = ""
    phone: str = ""
    latitude: float | None = None
    longitude: float | None = None

    @classmethod
    def from_graphql(cls, node: dict[str, Any] | None) -> ShippingAddress:
        if not node:
            return cls()
        return cls(
            name=node.get("name", "") or "",
            address1=node.get("address1", "") or "",
            address2=node.get("address2") or "",
            city=node.get("city", "") or "",
            zip=node.get("zip", "") or "",
            province_code=node.get("provinceCode") or "",
            country_code=node.get("countryCode", "") or "",
            phone=node.get("phone") or "",
            latitude=node.get("latitude"),
            longitude=node.get("longitude"),
        )


@dataclass
class Order:
    id: str
    name: str  # e.g. "#1042"
    created_at: str
    financial_status: str
    fulfillment_status: str
    note: str
    email: str
    total_price: float
    currency: str
    shipping_address: ShippingAddress
    custom_attributes: list[Property] = field(default_factory=list)
    line_items: list[LineItem] = field(default_factory=list)

    def get_attribute(self, key: str) -> str | None:
        for p in self.custom_attributes:
            if p.key == key:
                return p.value
        return None

    @classmethod
    def from_graphql(cls, node: dict[str, Any]) -> Order:
        # Fall back to billingAddress for local delivery orders
        addr = node.get("shippingAddress") or node.get("billingAddress")
        price_set = node.get("totalPriceSet", {}).get("shopMoney", {})
        return cls(
            id=node["id"],
            name=node["name"],
            created_at=node["createdAt"],
            financial_status=node.get("displayFinancialStatus", "") or "",
            fulfillment_status=node.get("displayFulfillmentStatus", "") or "",
            note=node.get("note") or "",
            email=node.get("email") or "",
            total_price=float(price_set.get("amount", "0")),
            currency=price_set.get("currencyCode", "NOK"),
            shipping_address=ShippingAddress.from_graphql(addr),
            custom_attributes=[
                Property(key=a["key"], value=a["value"] or "")
                for a in node.get("customAttributes", [])
            ],
            line_items=[
                LineItem.from_graphql(edge["node"])
                for edge in node.get("lineItems", {}).get("edges", [])
            ],
        )


@dataclass
class VariantMetadata:
    """Product variant metadata populated from Shopify custom metafields."""

    variant_id: str  # numeric string, e.g. "51793822581048"
    display_name: str  # variant displayName from Shopify
    metafields: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """Product name from metafields, falling back to display name."""
        return self.metafields.get("name", "") or self.display_name

    @property
    def sku_name(self) -> str:
        """SKU name from custom.sku_name metafield, falling back to name."""
        return self.metafields.get("sku_name", "") or self.name

    @property
    def slot_antall_enheter(self) -> int:
        """SLOT: antall enheter — f-packs per box slot."""
        val = self.metafields.get("slot_antall_enheter", "")
        try:
            return int(float(val))
        except (ValueError, TypeError):
            return 0


def collect_variant_ids(orders: list[Order]) -> list[str]:
    """Extract all unique variant IDs across orders.

    Covers both:
    - ``_pvgid`` custom attributes on bundle line items (picked SKUs)
    - ``variant.id`` on regular line items (non-bundle / internal orders)
    """
    ids: set[str] = set()
    for order in orders:
        for li in order.line_items:
            if li.variant_id:
                ids.add(li.variant_id)
            for attr in li.custom_attributes:
                if attr.is_pvgid and attr.variant_id:
                    ids.add(attr.variant_id)
    return sorted(ids)
