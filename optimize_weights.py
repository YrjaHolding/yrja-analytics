"""
Yrja Slot Value Optimizer
=========================
Finds optimal package configurations so every product occupies a "slot"
with as similar a value as possible.

Algorithm (anchor-based iterative):
    1. Identify "critical products" — fixed-weight products where only one
       unit fits per slot (n_max = 1).  Their slot value is completely
       immovable.  Currently this is "Hel kylling" (1.90 kg × 198.37 kr/kg
       ≈ 377 kr).
    2. Iterate over each critical product as a potential anchor.
    3. For each anchor, compute the best n_units for every other fixed
       product and the ideal total weight for every free product.
    4. Pick the anchor that yields the lowest max deviation across all
       products.

If no critical product exists, falls back to a MILP solver (Pyomo + HiGHS).

Key concepts:
    - A slot can hold MULTIPLE units of a product (e.g. 3× Kjøttdeig)
    - Total slot weight must not exceed MAX_SLOT_WEIGHT
    - Stølsvidda/Opaker unit package weights are free variables
    - All other unit package weights are fixed by the producer
    - Slot value = n_units × unit_weight × price_per_kg

Usage:
    uv run python optimize_weights.py
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pyomo.environ as pyo
from products import get_products, Product


MAX_SLOT_WEIGHT = 2.5  # kg
MIN_SLOT_WEIGHT = 0.10  # minimum total slot weight (kg)


def _build_milp_model(
    products: list[Product] | None = None,
) -> pyo.ConcreteModel:
    """Build the Pyomo MILP model (fallback when no critical anchor exists)."""

    if products is None:
        all_products = get_products()
        products = [p for p in all_products if p.shopify_visible]

    fixed = [p for p in products if not p.adjustable_size]
    free = [p for p in products if p.adjustable_size]

    m = pyo.ConcreteModel("Yrja_Slot_Value_Optimizer")

    # ── Sets ─────────────────────────────────────────────────────────────
    m.FIXED = pyo.Set(initialize=[p.name for p in fixed])
    m.FREE = pyo.Set(initialize=[p.name for p in free])
    m.ALL = m.FIXED | m.FREE

    # ── Parameters ───────────────────────────────────────────────────────
    price_map = {p.name: p.retail_price_per_kg for p in products}
    m.price_per_kg = pyo.Param(m.ALL, initialize=price_map)

    unit_weight_map = {p.name: p.f_pack_weight_kg for p in fixed}
    m.unit_weight = pyo.Param(m.FIXED, initialize=unit_weight_map)

    # Max units per slot for fixed products (≥1, capped by 2kg limit)
    n_max_map = {
        p.name: max(1, math.floor(MAX_SLOT_WEIGHT / p.f_pack_weight_kg))
        for p in fixed
    }
    m.n_max = pyo.Param(m.FIXED, initialize=n_max_map)

    # ── Variables ────────────────────────────────────────────────────────

    # Fixed products: n[i] = number of units per slot (integer ≥ 1)
    m.n = pyo.Var(
        m.FIXED,
        domain=pyo.PositiveIntegers,
        bounds=lambda m, i: (1, m.n_max[i]),
    )

    # Free (Stølsvidda / Opaker) products: total slot weight (continuous)
    # These producers can adapt package sizes, so total weight is continuous.
    m.tw = pyo.Var(
        m.FREE,
        domain=pyo.NonNegativeReals,
        bounds=(MIN_SLOT_WEIGHT, MAX_SLOT_WEIGHT),
    )

    # Target value and max deviation
    m.t = pyo.Var(domain=pyo.Reals)
    m.d = pyo.Var(domain=pyo.NonNegativeReals)

    # ── Constraints ──────────────────────────────────────────────────────

    # Fixed products: slot_value = n[i] × unit_weight × price_per_kg
    def fixed_upper_rule(m, i):
        val = m.n[i] * m.unit_weight[i] * m.price_per_kg[i]
        return val - m.t <= m.d

    def fixed_lower_rule(m, i):
        val = m.n[i] * m.unit_weight[i] * m.price_per_kg[i]
        return m.t - val <= m.d

    m.fixed_upper = pyo.Constraint(m.FIXED, rule=fixed_upper_rule)
    m.fixed_lower = pyo.Constraint(m.FIXED, rule=fixed_lower_rule)

    # Free products (Stølsvidda + Opaker): slot_value = tw[i] × price_per_kg
    def free_upper_rule(m, i):
        val = m.tw[i] * m.price_per_kg[i]
        return val - m.t <= m.d

    def free_lower_rule(m, i):
        val = m.tw[i] * m.price_per_kg[i]
        return m.t - val <= m.d

    m.free_upper = pyo.Constraint(m.FREE, rule=free_upper_rule)
    m.free_lower = pyo.Constraint(m.FREE, rule=free_lower_rule)

    # ── Objective ────────────────────────────────────────────────────────
    m.obj = pyo.Objective(expr=m.d, sense=pyo.minimize)

    return m


def _suggest_unit_breakdown(total_weight: float) -> tuple[int, float]:
    """
    Given an optimal total slot weight, suggest a concrete (n_units, unit_weight)
    breakdown with a sensible per-package weight (0.25 – 1.2 kg).
    """
    UNIT_MIN, UNIT_MAX = 0.25, 1.2
    best_n, best_w = 1, total_weight
    best_score = float("inf")

    max_n = max(1, math.floor(total_weight / UNIT_MIN))
    for n in range(1, max_n + 1):
        w = total_weight / n
        if w < UNIT_MIN or w > UNIT_MAX:
            continue
        # Prefer unit weights that are "round" (close to 0.1 steps)
        rounded = round(w * 10) / 10  # snap to nearest 0.1
        score = abs(w - rounded) + abs(n * rounded - total_weight) * 0.1
        if score < best_score:
            best_score = score
            best_n = n
            best_w = round(w, 2)

    return best_n, best_w


@dataclass
class SlotResult:
    """Optimizer result for a single product slot."""

    name: str
    n_units: int
    unit_weight: float
    total_weight: float
    price_per_kg: float
    slot_value: float
    delta: float  # deviation from target


@dataclass
class OptimizerOutput:
    """Full optimizer output."""

    target: float
    max_deviation: float
    slots: dict[str, SlotResult]  # product_name → SlotResult

    @property
    def spread(self) -> float:
        """Actual spread: max slot value - min slot value."""
        values = [s.slot_value for s in self.slots.values()]
        return max(values) - min(values) if values else 0.0


# ── Anchor-based optimizer ────────────────────────────────────────────────────


def _n_max_for(p: Product) -> int:
    """Maximum number of f-packs that fit in one slot."""
    return max(1, math.floor(MAX_SLOT_WEIGHT / p.f_pack_weight_kg))


def _find_anchor_candidates(
    products: list[Product],
) -> list[tuple[Product, int, float]]:
    """Find critical products whose slot value is completely fixed.

    A product is critical when n_max = 1 — only one unit fits per slot,
    so its slot value cannot be adjusted.  This product sets the target.

    Returns list of (product, n_units=1, slot_value), sorted by slot value.
    """
    candidates = []
    for p in products:
        if p.adjustable_size:
            continue
        if _n_max_for(p) == 1:
            val = p.f_pack_weight_kg * p.retail_price_per_kg
            candidates.append((p, 1, val))
    return sorted(candidates, key=lambda x: x[2])


def _best_n_for_target(
    p: Product,
    anchor_value: float,
) -> tuple[int, float, float]:
    """For a fixed-weight product, find the best n_units under the anchor ceiling.

    Only considers n where slot_value ≤ anchor_value.  Among those, picks the
    n with the highest slot value (closest to anchor from below).

    Returns (n_units, slot_value, abs_deviation_from_anchor).
    """
    n_max = _n_max_for(p)
    best_n = 0
    best_val = 0.0

    for n in range(1, n_max + 1):
        val = n * p.f_pack_weight_kg * p.retail_price_per_kg
        if val <= anchor_value and val > best_val:
            best_n = n
            best_val = val

    # If no n fits under the ceiling (single unit already exceeds anchor),
    # fall back to n=1 as the least-bad option.
    if best_n == 0:
        best_n = 1
        best_val = p.f_pack_weight_kg * p.retail_price_per_kg

    return best_n, best_val, abs(best_val - anchor_value)


def _free_weight_for_target(
    p: Product,
    target: float,
    anchor_value: float,
) -> tuple[float, float, float]:
    """For a free-weight product, compute ideal total weight for target.

    The target is typically the minimum fixed-product slot value.
    The ceiling is anchor_value / price_per_kg (hard upper bound).

    Returns (total_weight, slot_value, abs_deviation_from_target).
    """
    max_tw = min(MAX_SLOT_WEIGHT, anchor_value / p.retail_price_per_kg)
    ideal_tw = target / p.retail_price_per_kg
    clamped_tw = min(max_tw, max(MIN_SLOT_WEIGHT, ideal_tw))
    val = clamped_tw * p.retail_price_per_kg
    dev = abs(val - target)
    return clamped_tw, val, dev


def _evaluate_anchor(
    products: list[Product],
    anchor_value: float,
) -> tuple[float, dict[str, SlotResult]]:
    """Compute optimal slot configs for all products given an anchor value.

    Two-phase approach:
      Phase 1 — Fixed products: pick best n_units ≤ anchor ceiling, as close
               to anchor as possible.  This establishes the fixed-product range.
      Phase 2 — Free products: target the minimum fixed-product slot value
               (favours Yrja) while staying within [MIN_SLOT_WEIGHT, anchor].

    Returns (max_deviation_among_fixed, slots_dict).
    """
    slots: dict[str, SlotResult] = {}

    # ── Phase 1: fixed products (ceiling-constrained, target anchor) ──────
    fixed_values: list[float] = []
    for p in products:
        if p.adjustable_size:
            continue
        n, val, dev = _best_n_for_target(p, anchor_value)
        slots[p.name] = SlotResult(
            name=p.name,
            n_units=n,
            unit_weight=p.f_pack_weight_kg,
            total_weight=n * p.f_pack_weight_kg,
            price_per_kg=p.retail_price_per_kg,
            slot_value=val,
            delta=val - anchor_value,
        )
        fixed_values.append(val)

    # The range established by fixed products
    min_fixed = min(fixed_values) if fixed_values else anchor_value
    max_dev = anchor_value - min_fixed  # worst-case deviation among fixed
    midpoint = (min_fixed + anchor_value) / 2

    # ── Phase 2: free products (target midpoint of fixed range) ───────────
    for p in products:
        if not p.adjustable_size:
            continue
        tw, val, dev = _free_weight_for_target(p, midpoint, anchor_value)
        n, unit_w = _suggest_unit_breakdown(tw)
        slots[p.name] = SlotResult(
            name=p.name,
            n_units=n,
            unit_weight=unit_w,
            total_weight=tw,
            price_per_kg=p.retail_price_per_kg,
            slot_value=val,
            delta=val - anchor_value,
        )

    return max_dev, slots


def _solve_milp(products: list[Product]) -> OptimizerOutput | None:
    """Fallback: solve using MILP when no critical anchor product exists."""
    m = _build_milp_model(products)
    solver = pyo.SolverFactory("appsi_highs")
    result = solver.solve(m, tee=False)

    if result.solver.termination_condition != pyo.TerminationCondition.optimal:
        return None

    target = pyo.value(m.t)
    max_dev = pyo.value(m.d)

    product_lookup = {p.name: p for p in products}
    slots: dict[str, SlotResult] = {}

    for name in m.FIXED:
        p = product_lookup[name]
        n = int(round(pyo.value(m.n[name])))
        total_w = n * p.f_pack_weight_kg
        val = total_w * p.retail_price_per_kg
        slots[name] = SlotResult(
            name=name,
            n_units=n,
            unit_weight=p.f_pack_weight_kg,
            total_weight=total_w,
            price_per_kg=p.retail_price_per_kg,
            slot_value=val,
            delta=val - target,
        )

    for name in m.FREE:
        p = product_lookup[name]
        total_w = pyo.value(m.tw[name])
        val = total_w * p.retail_price_per_kg
        n, unit_w = _suggest_unit_breakdown(total_w)
        slots[name] = SlotResult(
            name=name,
            n_units=n,
            unit_weight=unit_w,
            total_weight=total_w,
            price_per_kg=p.retail_price_per_kg,
            slot_value=val,
            delta=val - target,
        )

    return OptimizerOutput(target=target, max_deviation=max_dev, slots=slots)


def solve(products: list[Product] | None = None) -> OptimizerOutput | None:
    """Run the anchor-based iterative optimizer.

    1. Find all critical products (fixed-weight, n_max = 1).
    2. Iterate over each as a potential anchor — compute how well every
       other product can match its slot value.
    3. Pick the anchor that minimizes the overall max deviation.
    4. Falls back to MILP if no critical product exists.

    Args:
        products: Products to optimize. If None, uses get_products() filtered
                  to shopify_visible.

    Returns:
        OptimizerOutput with all slot results, or None if optimization fails.
    """
    if products is None:
        all_products = get_products()
        products = [p for p in all_products if p.shopify_visible]

    anchors = _find_anchor_candidates(products)

    if not anchors:
        # No immovable product — fall back to MILP for free-target optimization
        return _solve_milp(products)

    best_output: OptimizerOutput | None = None
    best_max_dev = float("inf")

    for _anchor_product, _anchor_n, anchor_value in anchors:
        max_dev, slots = _evaluate_anchor(products, anchor_value)

        if max_dev < best_max_dev:
            best_max_dev = max_dev
            best_output = OptimizerOutput(
                target=anchor_value,
                max_deviation=max_dev,
                slots=slots,
            )

    return best_output


def solve_and_report() -> OptimizerOutput | None:
    """Solve and print results."""

    output = solve()
    if output is None:
        print("⚠️  Optimizer failed to find a solution.")
        return None

    target = output.target
    max_dev = output.max_deviation

    print("=" * 78)
    print("  YRJA SLOT VALUE OPTIMIZER — RESULTS")
    print("=" * 78)
    print(f"  Target slot value (t):   {target:>10.2f} kr")
    print(f"  Max deviation (d):       {max_dev:>10.2f} kr")
    print(
        f"  Effective range:         {target - max_dev:.2f} – {target + max_dev:.2f} kr"
    )
    print(f"  Max slot weight:         {MAX_SLOT_WEIGHT:.1f} kg")

    # Identify anchor product(s)
    all_products = get_products()
    shopify_products = [p for p in all_products if p.shopify_visible]
    anchors = _find_anchor_candidates(shopify_products)
    if anchors:
        anchor_names = ", ".join(
            a[0].name for a in anchors if abs(a[2] - target) < 0.01
        )
        if anchor_names:
            print(f"  Anchor (critical):       {anchor_names}")
    print()

    product_lookup = {p.name: p for p in all_products}
    fixed_slots = []
    free_slots = []
    for name, slot in sorted(output.slots.items()):
        p = product_lookup.get(name)
        if p and p.adjustable_size:
            free_slots.append(slot)
        else:
            fixed_slots.append(slot)

    # ── Fixed products ───────────────────────────────────────────────────
    hdr = f"  {'Product':<22s} {'Units':>5s} {'Pkg wt':>7s} {'Tot wt':>7s} {'kr/kg':>7s} {'Slot val':>9s} {'Δ':>8s}"
    sep = "  " + "-" * 74

    print("  FIXED-WEIGHT PRODUCTS")
    print(sep)
    print(hdr)
    print(sep)

    values = []
    for slot in fixed_slots:
        values.append(slot.slot_value)
        print(
            f"  {slot.name:<22s}"
            f" {slot.n_units:>4d}×"
            f" {slot.unit_weight:>5.2f}kg"
            f" {slot.total_weight:>5.2f}kg"
            f" {slot.price_per_kg:>7.2f}"
            f" {slot.slot_value:>8.2f}"
            f" {slot.delta:>+8.2f}"
        )

    # ── Free (Stølsvidda + Opaker) products ──────────────────────────────
    print()
    print("  FREE-WEIGHT PRODUCTS (Stølsvidda + Opaker — weight optimized)")
    print(sep)
    hdr2 = f"  {'Product':<22s} {'Units':>5s} {'Pkg wt*':>7s} {'Tot wt':>7s} {'kr/kg':>7s} {'Slot val':>9s} {'Δ':>8s}"
    print(hdr2)
    print(sep)

    for slot in free_slots:
        values.append(slot.slot_value)
        print(
            f"  {slot.name:<22s}"
            f" {slot.n_units:>4d}×"
            f" {slot.unit_weight:>5.2f}kg"
            f" {slot.total_weight:>5.2f}kg"
            f" {slot.price_per_kg:>7.2f}"
            f" {slot.slot_value:>8.2f}"
            f" {slot.delta:>+8.2f}"
        )

    # ── Summary ──────────────────────────────────────────────────────────
    print()
    print("  " + "=" * 74)
    print(f"  Min slot value:  {min(values):>10.2f} kr")
    print(f"  Max slot value:  {max(values):>10.2f} kr")
    print(f"  Spread:          {max(values) - min(values):>10.2f} kr")
    print(f"  Mean:            {sum(values) / len(values):>10.2f} kr")
    print()
    print("  * = unit weight suggested by optimizer")

    return output


if __name__ == "__main__":
    solve_and_report()
