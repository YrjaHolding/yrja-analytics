"""
Yrja Box Simulator — Monte Carlo simulation engine.

Simulates weight variation per product slot (using optimizer configs)
and computes COGS / marginal earnings using innpris_per_kg.
"""

from collections import Counter

import numpy as np
import pandas as pd

from products import Product
from optimize_weights import SlotResult


# ── Weight variation model ───────────────────────────────────────────────────
# We want P(actual < nominal) ≈ 0.15.
# Using Normal(μ, σ) with σ = 0.05 × nominal:
#   Φ⁻¹(0.15) ≈ −1.036  →  μ = nominal × (1 + 1.036 × 0.05) ≈ nominal × 1.052

_SIGMA_FRAC = 0.05  # relative standard deviation (5% of nominal)
_SHIFT = 1.052  # mean = nominal × _SHIFT
_CLAMP_LO = 0.85  # minimum = 85% of nominal
_CLAMP_HI = 1.20  # maximum = 120% of nominal


def simulate_weights(
    nominal_kg: float,
    n_samples: int = 1000,
    rng: np.random.Generator | None = None,
    fixed: bool = False,
) -> np.ndarray:
    """
    Draw *n_samples* actual weights for a single unit package.

    If *fixed* is True (e.g. kjøttdeig), return the nominal weight with no
    variation — these products are packed to an exact weight.
    """
    if fixed:
        return np.full(n_samples, nominal_kg)
    if rng is None:
        rng = np.random.default_rng()
    mu = nominal_kg * _SHIFT
    sigma = nominal_kg * _SIGMA_FRAC
    samples = rng.normal(mu, sigma, size=n_samples)
    return np.clip(samples, nominal_kg * _CLAMP_LO, nominal_kg * _CLAMP_HI)


# ── Slot-level helpers ───────────────────────────────────────────────────────


def slot_retail(slot: SlotResult) -> float:
    """Retail value for a slot (utpris-based)."""
    return slot.slot_value


def slot_cogs(slot: SlotResult, product: Product) -> float:
    """COGS for a slot (innpris-based)."""
    return slot.total_weight * product.purchase_price_per_kg


def slot_margin(slot: SlotResult, product: Product) -> float:
    """Margin for a slot."""
    return slot_retail(slot) - slot_cogs(slot, product)


def slot_margin_pct(slot: SlotResult, product: Product) -> float:
    """Margin % for a slot."""
    ret = slot_retail(slot)
    return (slot_margin(slot, product) / ret * 100) if ret else 0.0


# ── Box simulation ───────────────────────────────────────────────────────────


def simulate_box(
    selected_products: list[Product],
    slot_configs: dict[str, SlotResult],
    n_simulations: int = 5000,
    seed: int | None = None,
) -> pd.DataFrame:
    """
    Run Monte Carlo weight simulations for a box.

    Each slot contains n_units packages of unit_weight (from optimizer).
    Simulates weight variation per package and aggregates.
    """
    rng = np.random.default_rng(seed)

    total_weight = np.zeros(n_simulations)
    total_under = 0
    total_packages = 0

    for prod in selected_products:
        slot = slot_configs.get(prod.name)
        if not slot:
            continue
        for _ in range(slot.n_units):
            weights = simulate_weights(slot.unit_weight, n_simulations, rng, fixed=prod.fixed_weight)
            total_weight += weights
            total_under += (weights < slot.unit_weight).sum()
            total_packages += n_simulations

    box_retail = sum(slot_retail(slot_configs[p.name]) for p in selected_products if p.name in slot_configs)
    box_cogs = sum(slot_cogs(slot_configs[p.name], p) for p in selected_products if p.name in slot_configs)

    pct_under = (total_under / total_packages * 100) if total_packages else 0

    return pd.DataFrame({
        "total_retail": box_retail,
        "total_cogs": box_cogs,
        "marginal_earnings": box_retail - box_cogs,
        "total_weight": total_weight,
        "pct_under_nominal": pct_under,
    })


# ── Per-product weight simulation detail ─────────────────────────────────────


def simulate_product_weights(
    selected_products: list[Product],
    slot_configs: dict[str, SlotResult],
    n_simulations: int = 5000,
    seed: int | None = None,
) -> pd.DataFrame:
    """Return per-slot weight statistics across simulations."""
    rng = np.random.default_rng(seed)
    rows = []
    for prod in selected_products:
        slot = slot_configs.get(prod.name)
        if not slot:
            continue
        slot_weights = np.zeros(n_simulations)
        for _ in range(slot.n_units):
            slot_weights += simulate_weights(
                slot.unit_weight, n_simulations, rng, fixed=prod.fixed_weight
            )
        nominal_total = slot.total_weight
        pct_under = (slot_weights < nominal_total).mean() * 100
        rows.append({
            "product": prod.name,
            "producer": prod.producer,
            "n_units": slot.n_units,
            "unit_weight_kg": slot.unit_weight,
            "nominal_total_kg": nominal_total,
            "mean_actual_kg": slot_weights.mean(),
            "std_kg": slot_weights.std(),
            "min_kg": slot_weights.min(),
            "max_kg": slot_weights.max(),
            "pct_under_nominal": pct_under,
        })
    return pd.DataFrame(rows)


# ── Extreme box finder ───────────────────────────────────────────────────────


def _box_stats(
    products: list[Product],
    slot_configs: dict[str, SlotResult],
) -> dict:
    """Compute box statistics using optimizer slot configs and innpris COGS."""
    total_retail = 0.0
    total_cogs_val = 0.0
    total_weight = 0.0
    for p in products:
        slot = slot_configs.get(p.name)
        if not slot:
            continue
        total_retail += slot_retail(slot)
        total_cogs_val += slot_cogs(slot, p)
        total_weight += slot.total_weight

    margin = total_retail - total_cogs_val
    return {
        "products": [p.name for p in products],
        "total_retail": round(total_retail, 2),
        "total_cogs": round(total_cogs_val, 2),
        "marginal_earnings": round(margin, 2),
        "margin_pct": round(margin / total_retail * 100, 1) if total_retail else 0,
        "total_weight_kg": round(total_weight, 2),
    }


def find_extreme_boxes(
    products: list[Product],
    slot_configs: dict[str, SlotResult],
    n_slots: int,
) -> dict:
    """
    Find cheapest, most expensive, and most-balanced boxes.
    Sorts by COGS since retail is ~equalized by optimizer.
    """
    available = [p for p in products if p.name in slot_configs]

    sorted_by_cogs = sorted(available, key=lambda p: slot_cogs(slot_configs[p.name], p))
    cheapest_box = _greedy_fill_diverse(sorted_by_cogs, n_slots)
    most_expensive_box = _greedy_fill_diverse(sorted_by_cogs[::-1], n_slots)

    avg_slot_retail = sum(slot_retail(slot_configs[p.name]) for p in available) / len(available)
    target_box_retail = avg_slot_retail * n_slots

    balanced_box = _find_closest_value_box(available, slot_configs, n_slots, target_box_retail)

    return {
        "cheapest": _box_stats(cheapest_box, slot_configs),
        "most_expensive": _box_stats(most_expensive_box, slot_configs),
        "most_balanced": _box_stats(balanced_box, slot_configs),
        "target_retail": round(target_box_retail, 2),
    }


def _greedy_fill_diverse(
    sorted_products: list[Product],
    n_slots: int,
) -> list[Product]:
    """Fill n_slots cycling through sorted products for diversity."""
    box: list[Product] = []
    for i in range(n_slots):
        box.append(sorted_products[i % len(sorted_products)])
    return box


def _find_closest_value_box(
    products: list[Product],
    slot_configs: dict[str, SlotResult],
    n_slots: int,
    target_retail: float,
    max_repeats: int = 2,
) -> list[Product]:
    """Greedy: build a box whose total retail is closest to *target_retail*."""
    box: list[Product] = []
    remaining_target = target_retail
    usage = Counter()

    for slot_idx in range(n_slots):
        slots_left = n_slots - slot_idx
        ideal_next = remaining_target / slots_left
        available = [p for p in products if usage[p.name] < max_repeats]
        if not available:
            available = products
        best = min(available, key=lambda p: abs(slot_retail(slot_configs[p.name]) - ideal_next))
        box.append(best)
        usage[best.name] += 1
        remaining_target -= slot_retail(slot_configs[best.name])

    return box


# ── Value spread analysis ────────────────────────────────────────────────────


def value_spread_analysis(
    products: list[Product],
    slot_configs: dict[str, SlotResult],
    n_slots: int,
    n_random_boxes: int = 10_000,
    seed: int | None = None,
) -> pd.DataFrame:
    """Sample random box configs and show how retail, COGS, and margin vary."""
    rng = np.random.default_rng(seed)
    available = [p for p in products if p.name in slot_configs]
    indices = np.arange(len(available))
    rows = []

    for _ in range(n_random_boxes):
        picks = rng.choice(indices, size=n_slots, replace=True)
        chosen = [available[i] for i in picks]
        total_retail = sum(slot_retail(slot_configs[p.name]) for p in chosen)
        total_cogs_val = sum(slot_cogs(slot_configs[p.name], p) for p in chosen)
        rows.append({
            "total_retail": total_retail,
            "total_cogs": total_cogs_val,
            "marginal_earnings": total_retail - total_cogs_val,
        })

    return pd.DataFrame(rows)
