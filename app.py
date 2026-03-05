"""
Yrja Box Simulator — Streamlit frontend.

Run with:  uv run streamlit run app.py
"""

import streamlit as st
import pandas as pd
import plotly.express as px
from collections import Counter

from products import get_products, Product
from optimize_weights import solve, SlotResult
from simulator import (
    simulate_box,
    simulate_product_weights,
    find_extreme_boxes,
    value_spread_analysis,
    slot_retail,
    slot_cogs,
    slot_margin,
    slot_margin_pct,
)

# ── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Yrja Box Simulator", page_icon="🥩", layout="wide"
)
st.title("🥩 Yrja Box Simulator")
st.caption(
    "Simulate how customer box choices affect COGS, margins, and weight variation."
)


# ── Run optimizer at startup (cached) ────────────────────────────────────────

@st.cache_data(show_spinner="Optimaliserer slotverdier …")
def _run_optimizer():
    all_products = get_products()
    shopify_products = [p for p in all_products if p.shopify_visible]
    result = solve(shopify_products)
    return shopify_products, result


products, optimizer_output = _run_optimizer()

if optimizer_output is None:
    st.error("Optimizer feilet — kan ikke vise dashboard.")
    st.stop()

slot_configs: dict[str, SlotResult] = optimizer_output.slots
product_map = {p.name: p for p in products}
product_names = sorted(product_map.keys())

# ── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Box Configuration")

    n_slots = st.radio(
        "Box size (slots)", [6, 10, 15], index=1, horizontal=True
    )

    st.caption(
        f"Optimizer target: **{optimizer_output.target:,.0f} kr** per slot "
        f"(±{optimizer_output.max_deviation:,.0f} kr)"
    )

    st.subheader("Pick products")
    st.caption(
        f"Select {n_slots} products to fill the box. You can pick the same product multiple times."
    )

    selected_names: list[str] = []
    for i in range(n_slots):
        name = st.selectbox(
            f"Slot {i + 1}",
            product_names,
            index=i % len(product_names),
            key=f"slot_{i}",
        )
        selected_names.append(name)

    st.divider()
    n_simulations = st.slider(
        "Weight simulations", 500, 20_000, 5_000, step=500
    )

selected_products = [product_map[n] for n in selected_names]

# ── Tab layout ───────────────────────────────────────────────────────────────

tab_box, tab_weight, tab_extremes, tab_spread = st.tabs(
    [
        "📦 Your Box",
        "⚖️ Weight Simulation",
        "📊 Extreme Boxes",
        "🔀 Value Spread",
    ]
)

# ── Tab 1: Your Box ─────────────────────────────────────────────────────────

with tab_box:
    st.subheader("Box Contents")

    # Build per-product summary (aggregate duplicates)
    counts = Counter(selected_names)
    rows = []
    for name, qty in counts.items():
        p = product_map[name]
        slot = slot_configs[name]
        retail = slot_retail(slot)
        cogs = slot_cogs(slot, p)
        margin = retail - cogs
        margin_p = (margin / retail * 100) if retail else 0
        rows.append(
            {
                "Produkt": name,
                "Produsent": p.producer,
                "Antall slots": qty,
                "Enheter/slot": slot.n_units,
                "Vekt/enhet (kg)": round(slot.unit_weight, 2),
                "Tot vekt/slot (kg)": round(slot.total_weight, 2),
                "Utpris/slot (kr)": round(retail, 2),
                "COGS/slot (kr)": round(cogs, 2),
                "Margin/slot (kr)": round(margin, 2),
                "Margin %": round(margin_p, 1),
            }
        )

    df_box = pd.DataFrame(rows)
    st.dataframe(df_box, use_container_width=True, hide_index=True)

    # Totals
    total_retail = sum(
        slot_retail(slot_configs[n]) for n in selected_names
    )
    total_cogs = sum(
        slot_cogs(slot_configs[n], product_map[n]) for n in selected_names
    )
    total_margin = total_retail - total_cogs
    total_weight = sum(
        slot_configs[n].total_weight for n in selected_names
    )
    margin_pct = (total_margin / total_retail * 100) if total_retail else 0

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total utsalgspris", f"{total_retail:,.0f} kr")
    col2.metric("Total COGS", f"{total_cogs:,.0f} kr")
    col3.metric(
        "Marginal Earnings",
        f"{total_margin:,.0f} kr",
        delta=f"{margin_pct:.1f}%",
    )
    col4.metric("Nominell vekt", f"{total_weight:,.2f} kg")

# ── Tab 2: Weight Simulation ─────────────────────────────────────────────────

with tab_weight:
    st.subheader("Weight Variation per Product")
    st.caption(
        "Meat cuts vary naturally in size — the nominal weight is just a baseline. "
        "Actual weights are drawn from a distribution where ~15% of packages are "
        "lighter than nominal. **Kjøttdeig is the exception** — ground meat is "
        "packed to a fixed weight with no variation."
    )

    df_weights = simulate_product_weights(
        selected_products, slot_configs, n_simulations
    )

    st.dataframe(
        df_weights.style.format(
            {
                "unit_weight_kg": "{:.3f}",
                "nominal_total_kg": "{:.3f}",
                "mean_actual_kg": "{:.3f}",
                "std_kg": "{:.4f}",
                "min_kg": "{:.3f}",
                "max_kg": "{:.3f}",
                "pct_under_nominal": "{:.1f}%",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    # Box-level weight simulation
    st.subheader("Total Box Weight Distribution")
    df_sim = simulate_box(selected_products, slot_configs, n_simulations)

    fig_weight = px.histogram(
        df_sim,
        x="total_weight",
        nbins=60,
        title="Simulated Total Box Weight",
        labels={"total_weight": "Total weight (kg)"},
        color_discrete_sequence=["#2E86AB"],
    )
    nominal_total = sum(
        slot_configs[n].total_weight for n in selected_names
    )
    fig_weight.add_vline(
        x=nominal_total,
        line_dash="dash",
        line_color="red",
        annotation_text=f"Nominal: {nominal_total:.2f} kg",
    )
    st.plotly_chart(fig_weight, use_container_width=True)

    avg_pct_under = df_sim["pct_under_nominal"].mean()
    st.metric(
        "Avg % of packages under nominal weight (per box)",
        f"{avg_pct_under:.1f}%",
    )

# ── Tab 3: Extreme Boxes ────────────────────────────────────────────────────

with tab_extremes:
    st.subheader("Extreme Box Configurations")
    st.caption(
        f"Auto-generated boxes for {n_slots} slots showing worst-case, "
        "best-case, and balanced scenarios."
    )

    extremes = find_extreme_boxes(products, slot_configs, n_slots)

    def _render_extreme(label: str, key: str, emoji: str):
        data = extremes[key]
        st.markdown(f"### {emoji} {label}")
        col_a, col_b, col_c, col_d = st.columns(4)
        col_a.metric("Utsalgspris", f"{data['total_retail']:,.0f} kr")
        col_b.metric("COGS", f"{data['total_cogs']:,.0f} kr")
        col_c.metric(
            "Marginal Earnings", f"{data['marginal_earnings']:,.0f} kr"
        )
        col_d.metric("Margin %", f"{data['margin_pct']:.1f}%")

        product_counts = Counter(data["products"])
        items = [
            f"{name} ×{count}" if count > 1 else name
            for name, count in product_counts.items()
        ]
        st.write("**Produkter:** " + " · ".join(items))
        st.write(f"**Nominell vekt:** {data['total_weight_kg']:.2f} kg")
        st.divider()

    st.info(
        "💡 The optimizer equalizes slot *retail* values. Since innpris "
        "varies by product, margin % now differs between slots — "
        "that's where the interesting variation is."
    )

    _render_extreme("Billigst boks (laveste COGS)", "cheapest", "💰")
    _render_extreme("Dyrest boks (høyeste COGS)", "most_expensive", "💎")
    _render_extreme(
        f"Mest balansert (nærmest snitt {extremes['target_retail']:,.0f} kr)",
        "most_balanced",
        "⚖️",
    )

# ── Tab 4: Value Spread ─────────────────────────────────────────────────────

with tab_spread:
    st.subheader("Value Spread Across Random Boxes")
    st.caption(
        f"10,000 random {n_slots}-slot boxes sampled to show how COGS "
        "and marginal earnings vary when customers choose freely. "
        "(Retail is ~equalized by the optimizer.)"
    )

    df_spread = value_spread_analysis(
        products, slot_configs, n_slots, n_random_boxes=10_000
    )

    fig_cogs = px.histogram(
        df_spread,
        x="total_cogs",
        nbins=40,
        title="Distribution of Total COGS",
        labels={"total_cogs": "Total COGS (kr)"},
        color_discrete_sequence=["#E74C3C"],
    )
    st.plotly_chart(fig_cogs, use_container_width=True)

    fig_margin = px.histogram(
        df_spread,
        x="marginal_earnings",
        nbins=40,
        title="Distribution of Marginal Earnings",
        labels={"marginal_earnings": "Marginal earnings (kr)"},
        color_discrete_sequence=["#28A745"],
    )
    st.plotly_chart(fig_margin, use_container_width=True)

    col1, col2, col3 = st.columns(3)
    col1.metric("Avg Retail", f"{df_spread['total_retail'].mean():,.0f} kr")
    col2.metric("Avg COGS", f"{df_spread['total_cogs'].mean():,.0f} kr")
    col3.metric(
        "Avg Marginal Earnings",
        f"{df_spread['marginal_earnings'].mean():,.0f} kr",
    )

    st.subheader("Spread Statistics")
    spread_stats = df_spread.describe().T
    spread_stats.columns = [
        "Count",
        "Mean",
        "Std",
        "Min",
        "25%",
        "50%",
        "75%",
        "Max",
    ]
    st.dataframe(
        spread_stats.style.format("{:,.1f}"),
        use_container_width=True,
    )
