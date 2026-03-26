"""Yrja product catalog — sourced from Notion Prisoversikt.

Retail prices (stykkpris) are the customer-facing prices.
Purchase prices = retail / 1.28 (what Yrja pays producers).

When NOTION_API_KEY is set, get_products() fetches live data from Notion.
Otherwise it falls back to the hardcoded PRODUCTS list below.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")


@dataclass
class Product:
    name: str
    producer: str
    f_pack_weight_kg: float
    retail_price: float  # stykkpris (kr)
    retail_price_per_kg: float  # enhetspris (kr/kg)
    purchase_price: float  # stykkpris / 1.28 (kr)
    unit: str
    image_url: str
    fixed_weight: bool = False  # True for kjøttdeig — no weight variation
    adjustable_size: bool = (
        False  # True for Stølsvidda/Opaker — package size can be tuned
    )
    shopify_visible: bool = False  # True if listed on Shopify storefront
    innpris_per_kg: float | None = None  # Innpris from Notion (kr/kg)
    espen_n_units: int | None = None  # Forced n_units from "Espen - antall enheter" (when Fri fpack vekt = 0)

    @property
    def purchase_price_per_kg(self) -> float:
        if self.innpris_per_kg is not None:
            return self.innpris_per_kg
        return self.retail_price_per_kg * 0.85 / 1.28

    @property
    def margin_per_unit(self) -> float:
        return self.retail_price - self.purchase_price

    @property
    def margin_pct(self) -> float:
        if self.retail_price == 0:
            return 0.0
        return (self.margin_per_unit / self.retail_price) * 100


def _p(
    name: str,
    producer: str,
    weight_kg: float,
    retail_price_per_kg: float,
    unit: str = "kg",
    image_url: str = "",
) -> Product:
    is_kjottdeig = "kjøttdeig" in name.lower()
    is_adjustable = producer in ("Stølsvidda", "Opaker")
    retail_price = round(weight_kg * retail_price_per_kg, 2)
    return Product(
        name=name,
        producer=producer,
        f_pack_weight_kg=weight_kg,
        retail_price=retail_price,
        retail_price_per_kg=retail_price_per_kg,
        purchase_price=round(retail_price / 1.28, 2),
        unit=unit,
        image_url=image_url,
        fixed_weight=is_kjottdeig,
        adjustable_size=is_adjustable,
    )


# ── Homlagaarden (Chicken / Turkey) ─────────────────────────────────────────

PRODUCTS: list[Product] = [
    _p(
        "Hel kylling",
        "Homlagaarden",
        1.90,
        198.37,
        "kg",
        "https://storage.googleapis.com/dyrket3-api-prod-media/products/hom-10157/972d7926-8232-44f6-b0cb-39a953297a2a.JPG.666x500_q85_crop-%2C0_fillwhite_upscale.jpg",
    ),
    _p(
        "Kalkun bryst",
        "Homlagaarden",
        1.10,
        589.09,
        "kg",
        "https://storage.googleapis.com/dyrket3-api-prod-media/products/hom-10161/b9f14554-748b-4077-910b-016c2b43cd99.png.666x500_q85_crop-%2C0_fillwhite_upscale.png",
    ),
    _p(
        "Kalkun kjøttdeig",
        "Homlagaarden",
        0.42,
        290.48,
        "kg",
        "https://storage.googleapis.com/dyrket3-api-prod-media/products/hom-10147/06b0fe66-d02b-4d5a-b4ee-3a8ae0b297a0.png.666x500_q85_crop-%2C0_fillwhite_upscale.png",
    ),
    _p(
        "Kalkunlår",
        "Homlagaarden",
        0.80,
        286.25,
        "kg",
        "https://storage.googleapis.com/dyrket3-api-prod-media/products/hom-10174/275116a7-9bd7-4d3c-9d38-b7821481d756.png.666x500_q85_crop-%2C0_fillwhite_upscale.png",
    ),
    _p(
        "Kyllingbryst",
        "Homlagaarden",
        0.39,
        468.00,
        "kg",
        "https://storage.googleapis.com/dyrket3-api-prod-media/products/hom-10145/2c984a42-69c8-46e8-a371-a56a8664bc10.png.666x500_q85_crop-%2C0_fillwhite_upscale.png",
    ),
    _p(
        "Kyllinglår, filet",
        "Homlagaarden",
        0.5,
        438.00,
        "kg",
        "https://storage.googleapis.com/dyrket3-api-prod-media/products/hom-10173/72b76f53-9506-4af1-9b45-92920299a1e0.png.666x500_q85_crop-%2C0_fillwhite_upscale.png",
    ),
    _p(
        "Kylling kjøttdeig",
        "Homlagaarden",
        0.50,
        278.00,
        "kg",
        "https://storage.googleapis.com/dyrket3-api-prod-media/products/hom-10007/6a91bbf4-96a4-4332-b023-6239cf25821c.png.666x500_q85_crop-%2C0_fillwhite_upscale.png",
    ),
    # ── Kvarøy (Fish) ────────────────────────────────────────────────────
    _p(
        "Laks",
        "Kvarøy",
        0.34,
        532.00,
        "kg",
        "https://www.fourstarseafood.com/cdn/shop/files/Retail-Kvaroy-Salmon-Fillets3_600x.webp?v=1764346258",
    ),
    # ── Opaker (Beef) ───────────────────────────────────────────────────
    _p(
        "Burger Storfe",
        "Opaker",
        0.30,
        363.00,
        "kg",
        "https://scontent-arn2-1.xx.fbcdn.net/v/t39.30808-6/472487051_18379581892107582_6139578578617255856_n.jpg",
    ),
    _p("Entrecôte", "Opaker", 0.60, 727.00, "kg"),
    _p("Grytekjøtt", "Opaker", 0.95, 309.00, "kg"),
    _p("Høyrygg", "Opaker", 1.00, 329.00, "kg"),
    _p("Kjøttdeig Storfe", "Opaker", 0.40, 300.00, "kg"),
    _p("Mørbrad", "Opaker", 0.60, 539.00, "kg"),
    # ── Stølsvidda (Pork / Freshwater fish) ──────────────────────────────────
    # Products without f-pack weight: assigned reasonable defaults
    _p("Abborfilet", "Stølsvidda", 0.40, 240.00, "kg"),  # 0.4kg × 240 kr/kg
    _p(
        "Bacon",
        "Stølsvidda",
        0.40,
        346.00,
        "kg",
        "https://stolsvidda.com/media/catalog/product/cache/7a3236aaef95070a37b3ec2c1a9c2683/b/a/bacon_av_sideflesk_2nd.jpg",
    ),
    _p("Bakt knoke", "Stølsvidda", 0.80, 160.00, "kg"),  # heavier cut
    _p(
        "Koteletter",
        "Stølsvidda",
        0.50,
        240.00,
        "kg",
        "https://stolsvidda.com/media/catalog/product/cache/7a3236aaef95070a37b3ec2c1a9c2683/k/a/kamkoteletter2_2nd.jpg",
    ),
    _p(
        "Nakkekoteletter",
        "Stølsvidda",
        0.50,
        263.00,
        "kg",
        "https://stolsvidda.com/media/catalog/product/cache/7a3236aaef95070a37b3ec2c1a9c2683/n/a/nakkekoteletter_2nd.jpg",
    ),
    _p("Pancetta", "Stølsvidda", 0.30, 360.00, "kg"),
    _p("Pulled pork", "Stølsvidda", 0.50, 170.00, "kg"),
    _p("Sikfilet", "Stølsvidda", 0.40, 185.00, "kg"),
    _p(
        "Stek",
        "Stølsvidda",
        0.80,
        195.00,
        "kg",
        "https://stolsvidda.com/media/catalog/product/cache/7a3236aaef95070a37b3ec2c1a9c2683/s/k/skinkesteik_2nd.jpg",
    ),
    _p("Svine side", "Stølsvidda", 0.60, 230.00, "kg"),
    _p(
        "Ytrefilet",
        "Stølsvidda",
        0.50,
        293.00,
        "kg",
        "https://stolsvidda.com/media/catalog/product/cache/7a3236aaef95070a37b3ec2c1a9c2683/y/t/ytrefillet_2nd.jpg",
    ),
    _p("Kjøttdeig Svin", "Stølsvidda", 0.30, 228.00, "kg"),
]


_notion_cache: list[Product] | None = None


def get_products(use_notion: bool | None = None) -> list[Product]:
    """Return the full product catalog.

    If NOTION_API_KEY is set (or use_notion=True), fetches live data from Notion.
    Results are cached for the process lifetime.
    Falls back to the hardcoded PRODUCTS list on failure.
    """
    global _notion_cache

    should_fetch = (
        use_notion
        if use_notion is not None
        else bool(os.environ.get("NOTION_API_KEY"))
    )

    if should_fetch:
        if _notion_cache is not None:
            return _notion_cache
        try:
            from notion_sync import fetch_products_from_notion

            _notion_cache = fetch_products_from_notion()
            return _notion_cache
        except Exception as e:
            print(f"⚠️  Notion fetch failed, using hardcoded data: {e}")

    return PRODUCTS


def get_product_by_name(name: str) -> Product | None:
    """Look up a single product by name."""
    for p in get_products():
        if p.name == name:
            return p
    return None
