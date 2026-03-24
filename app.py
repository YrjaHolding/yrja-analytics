"""Yrja Financial Dashboard — box subscription simulator."""

import streamlit as st
import pandas as pd
import numpy as np
import requests
import random
import logging
from math import comb

import plotly.express as px
import plotly.graph_objects as go

from notion_sync import (
    DB_QUERY_URL,
    _get_notion_headers,
    _get_title,
    _get_text,
    _get_number,
    _normalize_name,
)

logger = logging.getLogger(__name__)


def safe_float(value, field_name: str = "") -> float | None:
    """Convert a value to float, handling comma decimals. Logs a warning on conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(" ", "")
        if "," in cleaned and "." not in cleaned:
            cleaned = cleaned.replace(",", ".")
            logger.warning(
                "Comma→dot conversion for %s: %r → %s",
                field_name,
                value,
                cleaned,
            )
        elif "," in cleaned and "." in cleaned:
            # e.g. "1.234,56" → "1234.56"
            cleaned = cleaned.replace(".", "").replace(",", ".")
            logger.warning(
                "Comma→dot conversion for %s: %r → %s",
                field_name,
                value,
                cleaned,
            )
        try:
            return float(cleaned)
        except ValueError:
            logger.warning(
                "Could not convert %s value %r to float", field_name, value
            )
            return None
    return None


# ── Page config ────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Yrja Finansdashboard", page_icon="📦", layout="wide"
)


def _check_password() -> bool:
    """Show a login form and return True if the password is correct."""
    if st.session_state.get("authenticated"):
        return True

    st.title("📦 Yrja Finansdashboard")
    pwd = st.text_input("Passord", type="password", key="pwd_input")
    if st.button("Logg inn"):
        if pwd == st.secrets["password"]:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Feil passord")
    st.stop()
    return False


_check_password()
st.title("📦 Yrja Finansdashboard")

# ── Constants ────────────────────────────────────────────────────────
SUBSCRIPTIONS = {
    "4 slots": {"slots": 4, "default_price": 1749},
    "6 slots": {"slots": 6, "default_price": 2549},
    "8 slots": {"slots": 8, "default_price": 3349},
}

MVA_RATE = 0.15  # 15 % food MVA


# ── Data loading ─────────────────────────────────────────────────────


@st.cache_data(ttl=120, show_spinner="Henter data fra Notion …")
def fetch_product_table() -> pd.DataFrame:
    """Fetch the Notion product table with SLOT + Innpris columns."""
    headers = _get_notion_headers()
    raw_rows: list[dict] = []
    cursor: str | None = None

    while True:
        body: dict = {}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(
            DB_QUERY_URL, headers=headers, json=body, timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        raw_rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    records = []
    for row in raw_rows:
        props = row.get("properties", {})
        name = _get_title(props, "Produktnavn")
        producer = _get_text(props, "Produsent")
        if not name or not producer:
            continue
        name = _normalize_name(name, producer)

        records.append(
            {
                "Produktnavn": name,
                "Produsent": producer,
                "Innpris (kr/kg)": safe_float(
                    _get_number(props, "Innpris"), "Innpris"
                ),
                "SLOT: f-pack kg": safe_float(
                    _get_number(props, "SLOT: f-pack kg"), "SLOT: f-pack kg"
                ),
                "SLOT: antall enheter": safe_float(
                    _get_number(props, "SLOT: antall enheter"),
                    "SLOT: antall enheter",
                ),
                "SLOT: tot slot vekt": safe_float(
                    _get_number(props, "SLOT: tot slot vekt"),
                    "SLOT: tot slot vekt",
                ),
            }
        )

    df = pd.DataFrame(records)
    return df.sort_values(["Produsent", "Produktnavn"]).reset_index(drop=True)


# ── Sidebar ──────────────────────────────────────────────────────────

# Subscription prices
st.sidebar.header("Abonnementspriser")
prices = {}
for label, sub in SUBSCRIPTIONS.items():
    prices[label] = st.sidebar.number_input(
        f"{label}",
        value=sub["default_price"],
        step=50,
        min_value=0,
        key=f"price_{label}",
    )

# Operations parameters
st.sidebar.divider()
st.sidebar.header("Driftsparametere")

st.sidebar.caption("Lager (Storage)")
lager_fast = st.sidebar.number_input(
    "Fast kostnad per ordre (kr)",
    value=33.0,
    step=1.0,
    format="%.1f",
    key="lager_fast",
)
lager_var = st.sidebar.number_input(
    "Variabel kostnad per f-pack (kr)",
    value=5.0,
    step=0.5,
    format="%.1f",
    key="lager_var",
)

st.sidebar.caption("Distribusjon")
distribusjon = st.sidebar.number_input(
    "Distribusjon per ordre (kr)",
    value=150.0,
    step=5.0,
    format="%.1f",
    key="distribusjon",
)

st.sidebar.caption("Emballasje (Packaging)")
emballasje = st.sidebar.number_input(
    "Emballasje per ordre (kr)",
    value=7.0,
    step=1.0,
    format="%.1f",
    key="emballasje",
)

st.sidebar.caption("Shopify Payments")
shopify_var_pct = st.sidebar.number_input(
    "Variabelt ledd (%)",
    value=1.90,
    step=0.10,
    format="%.2f",
    key="shopify_var",
)
shopify_fast = st.sidebar.number_input(
    "Fast ledd (kr)",
    value=2.00,
    step=0.50,
    format="%.2f",
    key="shopify_fast",
)

st.sidebar.caption("Skio")
skio_var_pct = st.sidebar.number_input(
    "Variabelt ledd per abb (%)",
    value=1.00,
    step=0.10,
    format="%.2f",
    key="skio_var",
)
skio_fast = st.sidebar.number_input(
    "Fast ledd per abb (kr)",
    value=1.92,
    step=0.50,
    format="%.2f",
    key="skio_fast",
)

st.sidebar.divider()
if st.sidebar.button("🔄 Oppdater fra Notion"):
    st.cache_data.clear()
    st.rerun()


# ── Helper: cost breakdown for a subscription ───────────────────────


def compute_cost_breakdown(
    price: float, varekostnad: float, total_fpacks: float
) -> dict:
    """Compute per-order cost breakdown for a subscription tier."""
    revenue_ex_mva = price / (1 + MVA_RATE)
    mva_amount = price - revenue_ex_mva

    lager_dist_fast = lager_fast + distribusjon + emballasje
    lager_var_plukk = lager_var * total_fpacks
    shopify_cost = price * (shopify_var_pct / 100) + shopify_fast
    skio_cost = price * (skio_var_pct / 100) + skio_fast
    transaksjonsgebyrer = shopify_cost + skio_cost

    dekningsbidrag = revenue_ex_mva - varekostnad
    dek_omsetn_pct = (
        (dekningsbidrag / revenue_ex_mva * 100) if revenue_ex_mva else 0.0
    )
    total_ops = lager_dist_fast + lager_var_plukk + transaksjonsgebyrer
    driftsresultat = dekningsbidrag - total_ops
    drifts_omsetn_pct = (
        (driftsresultat / revenue_ex_mva * 100) if revenue_ex_mva else 0.0
    )

    return {
        "Omsetning inkl. MVA": price,
        "MVA (15 %)": -mva_amount,
        "Omsetning eks. MVA": revenue_ex_mva,
        "Varekostnad": -varekostnad,
        "Dekningsbidrag": dekningsbidrag,
        "Dekningsbidrag/Omsetn": dek_omsetn_pct,
        "Lager&dist fast": -lager_dist_fast,
        "Lager var. (plukk)": -lager_var_plukk,
        "Transaksjonsgebyrer": -transaksjonsgebyrer,
        "Driftsresultat": driftsresultat,
        "Driftsresultat/Omsetn": drifts_omsetn_pct,
    }


def run_simulation(
    df_sim: pd.DataFrame,
    n_slots: int,
    locked_slots: dict[int, int],
    price: float,
    n_sims: int,
) -> dict:
    """Vectorised Monte Carlo simulation of box configurations.

    Args:
        df_sim: Products eligible for simulation.
        n_slots: Number of product slots in the subscription.
        locked_slots: {slot_index: df_sim row index} for locked slots.
        price: Subscription price incl. MVA.
        n_sims: Number of random boxes to generate.

    Returns:
        Dict with:
          'metrics': DataFrame (COGS, ops, MVA, Dekningsbidrag per sim),
          'picks':   np.ndarray (n_sims, n_slots) of df_sim index values.
    """
    # Pre-extract numpy arrays for speed
    innpris = df_sim["Innpris (kr/kg)"].values
    vekt = df_sim["SLOT: tot slot vekt"].values
    fpacks_arr = df_sim["SLOT: antall enheter"].values
    slot_cost = innpris * vekt

    # Map df_sim.index → local 0..n-1 positions
    idx_to_local = {idx: i for i, idx in enumerate(df_sim.index)}
    local_to_idx = df_sim.index.values
    n_products = len(df_sim)

    # Build (n_sims, n_slots) matrix of local product indices
    sim = np.empty((n_sims, n_slots), dtype=int)
    for s in range(n_slots):
        if s in locked_slots:
            sim[:, s] = idx_to_local[locked_slots[s]]
        else:
            sim[:, s] = np.random.randint(0, n_products, size=n_sims)

    # Aggregate per-box totals via fancy indexing
    total_cogs = slot_cost[sim].sum(axis=1)
    total_fpacks = fpacks_arr[sim].sum(axis=1)

    # Fixed revenue / payment components
    revenue_ex_mva = price / (1 + MVA_RATE)
    mva = price - revenue_ex_mva
    shopify = price * (shopify_var_pct / 100) + shopify_fast
    skio = price * (skio_var_pct / 100) + skio_fast

    # Ops cost (variable part depends on f-packs)
    ops_total = (
        lager_fast
        + lager_var * total_fpacks
        + distribusjon
        + emballasje
        + shopify
        + skio
    )

    dekningsbidrag = revenue_ex_mva - total_cogs
    bunnlinje = dekningsbidrag - ops_total

    # Convert local indices back to df_sim index values
    picks_matrix = local_to_idx[sim]

    return {
        "metrics": pd.DataFrame(
            {
                "Innkjøpspris": total_cogs,
                "Dekningsbidrag": dekningsbidrag,
                "Lager, distribusjon & transaksjoner": ops_total,
                "MVA": np.full(n_sims, mva),
                "Driftsresultat": bunnlinje,
            }
        ),
        "picks": picks_matrix,
    }


# Styling constants for cost breakdown tables
PCT_BD_ITEMS = {"Dekningsbidrag/Omsetn", "Driftsresultat/Omsetn"}
BOLD_BD_ITEMS = {"Dekningsbidrag"}
COST_BD_ITEMS = {
    "MVA (15 %)",
    "Varekostnad",
    "Lager&dist fast",
    "Lager var. (plukk)",
    "Transaksjonsgebyrer",
}

# Consistent colors for breakdown charts
BREAKDOWN_COLORS = {
    "Varekostnad": "#e74c3c",
    "Andre driftskostnader": "#f39c12",
    "Driftsresultat": "#27ae60",
}


def make_breakdown_chart(
    varekost: float,
    andre_drift: float,
    driftsresultat: float,
    height: int = 280,
) -> go.Figure:
    """Vertical stacked bar: Omsetning = Varekostnad + Andre drift + Driftsresultat."""
    fig = go.Figure()
    for name, val in [
        ("Varekostnad", varekost),
        ("Andre driftskostnader", andre_drift),
        ("Driftsresultat", driftsresultat),
    ]:
        fig.add_trace(
            go.Bar(
                name=name,
                x=[""],
                y=[val],
                marker_color=BREAKDOWN_COLORS[name],
                text=[f"{int(round(val)):,}"],
                textposition="inside",
                insidetextanchor="middle",
            )
        )
    fig.update_layout(
        barmode="stack",
        showlegend=True,
        height=height,
        margin=dict(t=30, b=10, l=10, r=10),
        yaxis_title="kr",
        xaxis=dict(showticklabels=False),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5
        ),
    )
    return fig


def _bd_chart_values(bd: dict) -> tuple[float, float, float]:
    """Extract (varekost, andre_drift, driftsresultat) from a cost breakdown dict."""
    varekost = abs(bd["Varekostnad"])
    andre = (
        abs(bd["Lager&dist fast"])
        + abs(bd["Lager var. (plukk)"])
        + abs(bd["Transaksjonsgebyrer"])
    )
    return varekost, andre, bd["Driftsresultat"]


def find_example_indices(series: pd.Series) -> dict[str, int]:
    """Return simulation indices for highest, nearest-to-mean, and lowest."""
    return {
        "Høyest": int(series.idxmax()),
        "Snitt": int((series - series.mean()).abs().idxmin()),
        "Lavest": int(series.idxmin()),
    }


def render_box(
    df_sim: pd.DataFrame, picks: np.ndarray, price: float, label: str
):
    """Display a single box: slot table + dekningsbidrag summary."""
    rows = []
    for slot_nr, idx in enumerate(picks, 1):
        row = df_sim.loc[idx]
        slot_vekt = row["SLOT: tot slot vekt"]
        innpris_kg = row["Innpris (kr/kg)"]
        rows.append(
            {
                "Slot": slot_nr,
                "Produktnavn": row["Produktnavn"],
                "F-packs": int(row["SLOT: antall enheter"]),
                "Vekt (kg)": round(slot_vekt, 2),
                "Innpris (kr)": round(innpris_kg * slot_vekt, 2),
            }
        )

    box_df = pd.DataFrame(rows)
    tot_fpacks = box_df["F-packs"].sum()
    tot_vekt = box_df["Vekt (kg)"].sum()
    tot_innpris = box_df["Innpris (kr)"].sum()

    subtotal = pd.DataFrame(
        [
            {
                "Slot": None,
                "Produktnavn": "TOTALT",
                "F-packs": int(tot_fpacks),
                "Vekt (kg)": round(tot_vekt, 2),
                "Innpris (kr)": round(tot_innpris, 2),
            }
        ]
    )
    box_df = pd.concat([box_df, subtotal], ignore_index=True)
    box_df["Slot"] = box_df["Slot"].apply(
        lambda x: "" if pd.isna(x) else str(int(x))
    )

    n_slots = len(picks)
    avg_cogs = avg_slot_cogs * n_slots
    avg_fpacks = avg_slot_fpacks * n_slots

    bd_box = compute_cost_breakdown(price, tot_innpris, tot_fpacks)
    bd_avg = compute_cost_breakdown(price, avg_cogs, avg_fpacks)

    st.markdown(f"**{label}**")
    st.dataframe(box_df, use_container_width=True, hide_index=True)

    dr_val = bd_box["Driftsresultat"]
    rev_ex = bd_box["Omsetning eks. MVA"]
    margin_pct = (dr_val / rev_ex * 100) if rev_ex else 0.0
    dr_diff = dr_val - bd_avg["Driftsresultat"]
    st.markdown(
        f"**Driftsresultat: {dr_val:,.0f} kr "
        f"({margin_pct:.1f} %, {dr_diff:+,.0f} kr vs. snitt)**"
    )

    with st.expander("Kostnadsfordeling"):
        bd_rows = []
        for metric in bd_box:
            if metric == "Driftsresultat":
                continue
            sim_val = bd_box[metric]
            a_val = bd_avg[metric]
            is_pct = metric in PCT_BD_ITEMS
            if is_pct:
                sim_str = f"{sim_val:.1f} %"
                avg_str = f"{a_val:.1f} %"
            else:
                sim_str = f"{int(round(sim_val)):,}"
                avg_str = f"{int(round(a_val)):,}"
            deviation = (
                ((sim_val - a_val) / abs(a_val) * 100)
                if abs(a_val) > 0.01
                else 0.0
            )
            bd_rows.append(
                {
                    "": metric,
                    "Boks": sim_str,
                    "Gjennomsnitt": avg_str,
                    "Avvik (%)": f"{int(round(deviation))}",
                }
            )
        bd_df = pd.DataFrame(bd_rows)
        styled = bd_df.style.apply(
            lambda row: [
                ("font-weight: bold; " if row[""] in BOLD_BD_ITEMS else "")
                + ("color: red; " if row[""] in COST_BD_ITEMS else "")
                for _ in row
            ],
            axis=1,
        )
        _tc, _ta, _td = _bd_chart_values(bd_box)
        _ec = st.columns([3, 1])
        with _ec[0]:
            st.dataframe(styled, use_container_width=True, hide_index=True)
        with _ec[1]:
            st.plotly_chart(
                make_breakdown_chart(_tc, _ta, _td, height=260),
                use_container_width=True,
                key=f"bd_chart_box_{label}",
            )


# ── Shared data

df = fetch_product_table()

df_sim = df.dropna(
    subset=["Innpris (kr/kg)", "SLOT: tot slot vekt", "SLOT: antall enheter"]
).copy()
df_sim["Slot innpris (kr)"] = (
    df_sim["Innpris (kr/kg)"] * df_sim["SLOT: tot slot vekt"]
)

if len(df_sim) > 0:
    avg_slot_cogs = df_sim["Slot innpris (kr)"].mean()
    avg_slot_fpacks = df_sim["SLOT: antall enheter"].mean()
else:
    avg_slot_cogs = 0.0
    avg_slot_fpacks = 0.0


# ══════════════════════════════════════════════════════════════════════
#  TABS
# ══════════════════════════════════════════════════════════════════════

tab_dashboard, tab_analyse = st.tabs(["📦 Dashboard", "📊 Analyse"])


# ── Tab 1: Dashboard ─────────────────────────────────────────────────

with tab_dashboard:
    st.subheader("Produktoversikt")
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(f"{len(df)} produkter lastet fra Notion")

    st.subheader("Abonnementer")
    cols = st.columns(3)
    for i, (label, sub) in enumerate(SUBSCRIPTIONS.items()):
        with cols[i]:
            price = prices[label]
            n_slots = sub["slots"]
            st.metric(label, f"{price:,} kr")
            st.caption(f"{n_slots} valgfrie produktslots")

            n_combos = comb(len(df) + n_slots - 1, n_slots)
            st.write(f"_{n_combos:,} mulige kombinasjoner_")

            # ── Simulate single box ───────────────────────────
            sim_key = f"sim_{label}"
            if st.button("🎲 Simuler boks", key=f"btn_{label}"):
                if len(df_sim) > 0:
                    picks = random.choices(df_sim.index.tolist(), k=n_slots)
                    st.session_state[sim_key] = picks

            box_cogs = avg_slot_cogs * n_slots
            box_fpacks = avg_slot_fpacks * n_slots

            if sim_key in st.session_state and len(df_sim) > 0:
                picks = st.session_state[sim_key]
                rows = []
                for slot_nr, idx in enumerate(picks, 1):
                    row = df_sim.loc[idx]
                    slot_vekt = row["SLOT: tot slot vekt"]
                    innpris_kg = row["Innpris (kr/kg)"]
                    slot_innpris = innpris_kg * slot_vekt
                    fpacks = row["SLOT: antall enheter"]
                    rows.append(
                        {
                            "Slot": slot_nr,
                            "Produktnavn": row["Produktnavn"],
                            "Antall f-packs": int(fpacks),
                            "Slot vekt (kg)": round(slot_vekt, 2),
                            "Slot innpris (kr)": round(slot_innpris, 2),
                        }
                    )

                sim_df = pd.DataFrame(rows)
                total_fpacks_sim = sim_df["Antall f-packs"].sum()
                total_vekt = sim_df["Slot vekt (kg)"].sum()
                total_innpris = sim_df["Slot innpris (kr)"].sum()

                box_cogs = total_innpris
                box_fpacks = total_fpacks_sim

                subtotal = pd.DataFrame(
                    [
                        {
                            "Slot": None,
                            "Produktnavn": "TOTALT",
                            "Antall f-packs": int(total_fpacks_sim),
                            "Slot vekt (kg)": round(total_vekt, 2),
                            "Slot innpris (kr)": round(total_innpris, 2),
                        }
                    ]
                )
                sim_df = pd.concat([sim_df, subtotal], ignore_index=True)
                sim_df["Slot"] = sim_df["Slot"].apply(
                    lambda x: "" if pd.isna(x) else str(int(x))
                )
                st.dataframe(sim_df, use_container_width=True, hide_index=True)

            # ── Cost breakdown table ──────────────────────────
            avg_cogs = avg_slot_cogs * n_slots
            avg_fpacks = avg_slot_fpacks * n_slots

            bd_sim = compute_cost_breakdown(price, box_cogs, box_fpacks)
            bd_avg = compute_cost_breakdown(price, avg_cogs, avg_fpacks)

            bd_rows = []
            for metric in bd_sim:
                if metric == "Driftsresultat":
                    continue
                sim_val = bd_sim[metric]
                avg_val = bd_avg[metric]
                is_pct = metric in PCT_BD_ITEMS
                if is_pct:
                    sim_str = f"{sim_val:.1f} %"
                    avg_str = f"{avg_val:.1f} %"
                else:
                    sim_str = f"{int(round(sim_val)):,}"
                    avg_str = f"{int(round(avg_val)):,}"
                deviation = (
                    ((sim_val - avg_val) / abs(avg_val) * 100)
                    if abs(avg_val) > 0.01
                    else 0.0
                )
                bd_rows.append(
                    {
                        "": metric,
                        "Simulert boks": sim_str,
                        "Gjennomsnitt": avg_str,
                        "Avvik (%)": f"{int(round(deviation))}",
                    }
                )

            with st.expander("Kostnadsfordeling"):
                bd_df = pd.DataFrame(bd_rows)
                styled = bd_df.style.apply(
                    lambda row: [
                        (
                            "font-weight: bold; "
                            if row[""] in BOLD_BD_ITEMS
                            else ""
                        )
                        + ("color: red; " if row[""] in COST_BD_ITEMS else "")
                        for _ in row
                    ],
                    axis=1,
                )
                _tc, _ta, _td = _bd_chart_values(bd_sim)
                _ec = st.columns([3, 1])
                with _ec[0]:
                    st.dataframe(
                        styled,
                        use_container_width=True,
                        hide_index=True,
                    )
                with _ec[1]:
                    st.plotly_chart(
                        make_breakdown_chart(_tc, _ta, _td, height=260),
                        use_container_width=True,
                        key=f"bd_chart_dash_{label}",
                    )
                dr_sim = bd_sim["Driftsresultat"]
                dr_avg = bd_avg["Driftsresultat"]
                rev_ex = bd_sim["Omsetning eks. MVA"]
                margin_pct = (dr_sim / rev_ex * 100) if rev_ex else 0.0
                dr_diff = dr_sim - dr_avg
                st.markdown(
                    f"**Driftsresultat: {dr_sim:,.0f} kr "
                    f"({margin_pct:.1f} % av omsetning eks. MVA, "
                    f"{dr_diff:+,.0f} kr vs. snitt)**"
                )


# ── Tab 2: Analyse ───────────────────────────────────────────────────

# Chart metrics: (data_column, display_name)
CHART_METRICS = [
    ("Innkjøpspris", "Varekostnad"),
    ("Dekningsbidrag", "Dekningsbidrag"),
    ("Lager, distribusjon & transaksjoner", "Andre driftskostnader"),
    ("Driftsresultat", "Driftsresultat"),
]

with tab_analyse:
    if len(df_sim) == 0:
        st.warning(
            "Ingen produkter med komplett data (Innpris + SLOT-kolonner)."
        )
        st.stop()

    # ── Subscription selector ─────────────────────────────────
    sub_label = st.selectbox("Velg abonnement", list(SUBSCRIPTIONS.keys()))
    sub = SUBSCRIPTIONS[sub_label]
    n_slots = sub["slots"]
    price = prices[sub_label]

    # ── Slot locking ──────────────────────────────────────────
    st.subheader("Slotlåsing")
    product_names = sorted(df_sim["Produktnavn"].tolist())
    options = ["🎲 Tilfeldig"] + product_names

    locked_slots: dict[int, int] = {}
    lock_cols = st.columns(min(n_slots, 4))
    for slot_idx in range(n_slots):
        with lock_cols[slot_idx % len(lock_cols)]:
            choice = st.selectbox(
                f"Slot {slot_idx + 1}",
                options=options,
                key=f"lock_{sub_label}_{slot_idx}",
            )
            if choice != "🎲 Tilfeldig":
                match = df_sim[df_sim["Produktnavn"] == choice]
                if len(match) > 0:
                    locked_slots[slot_idx] = match.index[0]

    # ── Combination count ─────────────────────────────────────
    n_free = n_slots - len(locked_slots)
    n_combos = comb(len(df_sim) + n_free - 1, n_free) if n_free > 0 else 1
    st.write(f"**{n_combos:,} mulige kombinasjoner**")

    # ── Simulation controls ───────────────────────────────────
    sim_col1, sim_col2 = st.columns([1, 2])
    with sim_col1:
        n_sims = st.number_input(
            "Antall simuleringer",
            value=10_000,
            step=1_000,
            min_value=100,
            key="n_sims",
        )
    with sim_col2:
        st.write("")  # spacer
        run_btn = st.button("🔬 Kjør simulering", type="primary")

    results_key = f"analyse_{sub_label}"
    if run_btn:
        result = run_simulation(df_sim, n_slots, locked_slots, price, n_sims)
        st.session_state[results_key] = result

    # ── Results ───────────────────────────────────────────────
    if results_key not in st.session_state:
        st.info("Velg slotlåsing og klikk **Kjør simulering**.")
        st.stop()

    result = st.session_state[results_key]
    results_df: pd.DataFrame = result["metrics"]
    picks_matrix: np.ndarray = result["picks"]

    # ── Custom box comparison ─────────────────────────────────
    with st.expander("📦 Min boks — sammenlign mot simuleringen"):
        _custom_options = ["—"] + product_names
        _custom_picks: list[int | None] = [None] * n_slots
        _cc = st.columns(min(n_slots, 4))
        for _si in range(n_slots):
            with _cc[_si % len(_cc)]:
                _ch = st.selectbox(
                    f"Slot {_si + 1}",
                    options=_custom_options,
                    key=f"custom_{sub_label}_{_si}",
                )
                if _ch != "—":
                    _m = df_sim[df_sim["Produktnavn"] == _ch]
                    if len(_m) > 0:
                        _custom_picks[_si] = _m.index[0]

        # Bonus / upsell (optional fractional product)
        st.divider()
        st.caption("🎁 Bonus / upsell (valgfritt)")
        _bonus_cols = st.columns([2, 1])
        with _bonus_cols[0]:
            _bonus_choice = st.selectbox(
                "Bonusprodukt",
                options=["Ingen"] + product_names,
                key=f"bonus_{sub_label}",
            )
        with _bonus_cols[1]:
            _bonus_pct = st.number_input(
                "Andel (%)",
                value=50,
                min_value=1,
                max_value=100,
                step=5,
                key=f"bonus_pct_{sub_label}",
            )
        # Show bonus cost preview
        _bonus_cogs = 0.0
        _bonus_fpacks = 0.0
        if _bonus_choice != "Ingen":
            _bm = df_sim[df_sim["Produktnavn"] == _bonus_choice]
            if len(_bm) > 0:
                _bidx = _bm.index[0]
                _bfrac = _bonus_pct / 100
                _bonus_cogs = (
                    df_sim.loc[_bidx, "Innpris (kr/kg)"]
                    * df_sim.loc[_bidx, "SLOT: tot slot vekt"]
                    * _bfrac
                )
                _bonus_fpacks = df_sim.loc[_bidx, "SLOT: antall enheter"] * _bfrac
                st.caption(
                    f"Bonus: {_bonus_pct}% av {_bonus_choice} "
                    f"= {_bonus_cogs:,.1f} kr varekost, "
                    f"{_bonus_fpacks:.1f} slots"
                )

    # Compute custom box metrics when all slots are filled
    custom_metrics: dict[str, float] | None = None
    if all(p is not None for p in _custom_picks):
        _c_cogs = (
            sum(
                df_sim.loc[idx, "Innpris (kr/kg)"] * df_sim.loc[idx, "SLOT: tot slot vekt"]
                for idx in _custom_picks
            )
            + _bonus_cogs
        )
        _c_fpacks = (
            sum(df_sim.loc[idx, "SLOT: antall enheter"] for idx in _custom_picks)
            + _bonus_fpacks
        )
        _c_rev = price / (1 + MVA_RATE)
        _c_ops = (
            lager_fast
            + lager_var * _c_fpacks
            + distribusjon
            + emballasje
            + price * (shopify_var_pct / 100) + shopify_fast
            + price * (skio_var_pct / 100) + skio_fast
        )
        _c_dek = _c_rev - _c_cogs
        _c_drift = _c_dek - _c_ops
        custom_metrics = {
            "Innkjøpspris": _c_cogs,
            "Dekningsbidrag": _c_dek,
            "Lager, distribusjon & transaksjoner": _c_ops,
            "Driftsresultat": _c_drift,
        }

    # Summary table
    st.subheader("Oppsummering per ordre")
    revenue_ex_mva = price / (1 + MVA_RATE)

    def _fmt_kr(v):
        return f"{int(round(v)):,}"

    def _fmt_pct(v):
        return f"{v:.1f} %"

    # (data_col, display_name, is_pct) — for pct rows data_col is the source metric
    _SUMMARY_DEFS = [
        ("Innkjøpspris", "Varekostnad", False),
        ("Dekningsbidrag", "Dekningsbidrag", False),
        ("Dekningsbidrag", "Dekningsbidrag/Omsetn", True),
        ("Lager, distribusjon & transaksjoner", "Andre driftskostnader", False),
        ("Driftsresultat", "Driftsresultat", False),
        ("Driftsresultat", "Driftsresultat/Omsetn", True),
    ]

    _has_custom = custom_metrics is not None

    _rev_row: dict = {
        "Metrikk": "Omsetning eks. MVA",
        "Snitt": _fmt_kr(revenue_ex_mva),
        "Std.avvik": "-",
        "Min": _fmt_kr(revenue_ex_mva),
        "Maks": _fmt_kr(revenue_ex_mva),
    }
    if _has_custom:
        _rev_row["Min boks"] = _fmt_kr(revenue_ex_mva)
    summary_rows = [_rev_row]

    for data_col, display_name, is_pct in _SUMMARY_DEFS:
        if is_pct:
            vals = results_df[data_col] / revenue_ex_mva * 100
            fmt = _fmt_pct
        else:
            vals = results_df[data_col]
            fmt = _fmt_kr
        row_dict: dict = {
            "Metrikk": display_name,
            "Snitt": fmt(vals.mean()),
            "Std.avvik": fmt(vals.std()),
            "Min": fmt(vals.min()),
            "Maks": fmt(vals.max()),
        }
        if _has_custom:
            if is_pct:
                row_dict["Min boks"] = _fmt_pct(custom_metrics[data_col] / revenue_ex_mva * 100)
            else:
                row_dict["Min boks"] = _fmt_kr(custom_metrics[data_col])
        summary_rows.append(row_dict)

    _COST_SUMMARY = {"Varekostnad", "Andre driftskostnader"}
    _BOLD_SUMMARY = {"Dekningsbidrag", "Driftsresultat"}

    summary_df = pd.DataFrame(summary_rows)
    styled = summary_df.style.apply(
        lambda row: [
            ("font-weight: bold; " if row["Metrikk"] in _BOLD_SUMMARY else "")
            + ("color: red; " if row["Metrikk"] in _COST_SUMMARY else "")
            for _ in row
        ],
        axis=1,
    )
    _avg_vk = results_df["Innkjøpspris"].mean()
    _avg_ad = results_df["Lager, distribusjon & transaksjoner"].mean()
    _avg_dr = results_df["Driftsresultat"].mean()
    _sc = st.columns([3, 1])
    with _sc[0]:
        st.dataframe(
            styled,
            use_container_width=True,
            hide_index=True,
        )
    with _sc[1]:
        st.plotly_chart(
            make_breakdown_chart(_avg_vk, _avg_ad, _avg_dr),
            use_container_width=True,
            key="bd_chart_summary",
        )

    # Derived percentage columns (% av Omsetning eks. MVA)
    revenue_ex = price / (1 + MVA_RATE)
    for data_col, display_name in CHART_METRICS:
        pct_col = f"{display_name} (%)"
        results_df[pct_col] = results_df[data_col] / revenue_ex * 100

    # Charts: left = absolute (kr), right = % av Omsetning eks. MVA
    st.subheader("Fordelinger")
    for data_col, display_name in CHART_METRICS:
        pct_col = f"{display_name} (%)"
        chart_cols = st.columns(2)
        with chart_cols[0]:
            fig = px.histogram(
                results_df,
                x=data_col,
                nbins=60,
                title=display_name,
                labels={data_col: "kr"},
            )
            if custom_metrics and data_col in custom_metrics:
                fig.add_vline(
                    x=custom_metrics[data_col],
                    line_dash="dash",
                    line_color="red",
                    line_width=2,
                    annotation_text="Min boks",
                    annotation_position="top",
                    annotation_font_size=10,
                )
            fig.update_layout(
                showlegend=False,
                height=320,
                margin=dict(t=40, b=30),
            )
            st.plotly_chart(fig, use_container_width=True)
        with chart_cols[1]:
            fig = px.histogram(
                results_df,
                x=pct_col,
                nbins=60,
                title=f"{display_name} (% av oms. eks. MVA)",
                labels={pct_col: "%"},
            )
            if custom_metrics and data_col in custom_metrics:
                fig.add_vline(
                    x=custom_metrics[data_col] / revenue_ex * 100,
                    line_dash="dash",
                    line_color="red",
                    line_width=2,
                    annotation_text="Min boks",
                    annotation_position="top",
                    annotation_font_size=10,
                )
            fig.update_layout(
                showlegend=False,
                height=320,
                margin=dict(t=40, b=30),
            )
            st.plotly_chart(fig, use_container_width=True)

    # ── Example boxes ─────────────────────────────────────────
    EXAMPLE_METRICS = [
        ("Innkjøpspris", "Varekostnad"),
        ("Dekningsbidrag", "Dekningsbidrag"),
        ("Driftsresultat", "Driftsresultat"),
    ]

    st.subheader("Eksempelbokser")
    for metric_col, display_name in EXAMPLE_METRICS:
        st.markdown(f"#### {display_name}")
        indices = find_example_indices(results_df[metric_col])
        ex_cols = st.columns(3)
        for j, (variant, sim_idx) in enumerate(indices.items()):
            with ex_cols[j]:
                value = results_df.loc[sim_idx, metric_col]
                render_box(
                    df_sim,
                    picks_matrix[sim_idx],
                    price,
                    f"{variant}: {value:,.0f} kr",
                )
