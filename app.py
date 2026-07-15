"""Yrja Financial Dashboard — box subscription simulator."""

import io
import os
import streamlit as st
import pandas as pd
import numpy as np
import requests
import random
import logging
from datetime import date, timedelta
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
from shopify_client import (
    ShopifyClient,
    VariantMetafields,
    has_shopify_credentials,
    Order,
)
from skio_client import (
    SkioClient,
    SkioOrder,
    has_skio_credentials,
)
from fulfillment.client import ShopifyClient as FulfillmentShopifyClient
from fulfillment.exporter import (
    build_query_filter,
    flatten_orders,
    flatten_orders_exploded,
)
from fulfillment.models import (
    Order as FulfillmentOrder,
    VariantMetadata,
    collect_variant_ids,
)
from fulfillment.pdf import generate_fulfillment_pdf

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

# ── Password gate ──────────────────────────────────────────────────────
_APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

if _APP_PASSWORD and not st.session_state.get("authenticated"):
    st.title("🔒 Yrja Finansdashboard")
    password = st.text_input("Passord", type="password")
    if st.button("Logg inn", type="primary"):
        if password == _APP_PASSWORD:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Feil passord.")
    st.stop()


st.title("📦 Yrja Finansdashboard")

# ── Constants ────────────────────────────────────────────────────────
SUBSCRIPTIONS = {
    "3 slots": {"slots": 3, "default_abbo_price": 1200},
    "4 slots": {"slots": 4, "default_abbo_price": 1600},
    "6 slots": {"slots": 6, "default_abbo_price": 2400},
    "8 slots": {"slots": 8, "default_abbo_price": 3200},
}
DEFAULT_SHIPPING_PRICE = 149

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
                "ODA pris/kg": _get_number(props, "ODA pris/kg"),
                "AMOI pris/kg": _get_number(props, "AMOI pris/kg "),
                "Shopify Variant ID": _get_number(props, "Shopify Variant ID"),
                "FID per Kolli": safe_float(
                    _get_number(props, "FID per Kolli"), "FID per Kolli"
                ),
                "Max kolli": safe_float(
                    _get_number(props, "Max kolli "), "Max kolli"
                ),
            }
        )

    df = pd.DataFrame(records)
    return df.sort_values(["Produsent", "Produktnavn"]).reset_index(drop=True)


# ── Shopify metafield enrichment ─────────────────────────────────────

_shopify_available = has_shopify_credentials()


@st.cache_data(ttl=300, show_spinner="Henter metafelter fra Shopify …")
def fetch_shopify_metafields() -> dict[str, dict]:
    """Fetch all product variant metafields from Shopify.

    Returns a serialisable dict for Streamlit caching.
    """
    if not _shopify_available:
        return {}
    client = ShopifyClient()
    try:
        raw = client.fetch_all_variant_metafields()
        # Key by numeric variant ID for exact matching with Notion
        result: dict[str, dict] = {}
        for title, vm in raw.items():
            entry = {
                "price_per_kg": vm.price_per_kg,
                "price_per_portion": vm.price_per_portion,
                "porsjoner": vm.porsjoner,
                "slot_antall_enheter": vm.slot_antall_enheter,
                "slot_fpack_kg": vm.slot_fpack_kg,
                "sku_name": vm.sku_name,
                "shopify_title": title,
                "variant_price": vm.price,
                "metafields": vm.metafields,
            }
            result[vm.variant_id] = entry
        return result
    except Exception as e:
        logger.warning("Shopify fetch failed: %s", e)
        return {}
    finally:
        client.close()


@st.cache_data(ttl=300, show_spinner="Henter ordrer fra Shopify …")
def fetch_shopify_orders(
    query: str | None = None, limit: int = 500,
) -> list[dict]:
    """Fetch Shopify orders as serialisable dicts for Streamlit caching."""
    if not _shopify_available:
        return []
    client = ShopifyClient()
    try:
        orders = client.fetch_recent_orders(
            limit=limit, query=query, include_customer=False,
        )
        return [
            {
                "order_id": o.order_id,
                "name": o.name,
                "created_at": o.created_at,
                "total_price": o.total_price,
                "line_items": [
                    {
                        "title": li.title,
                        "quantity": li.quantity,
                        "custom_attributes": li.custom_attributes,
                    }
                    for li in o.line_items
                ],
            }
            for o in orders
        ]
    except Exception as e:
        logger.warning("Shopify order fetch failed: %s", e)
        return []
    finally:
        client.close()


_skio_available = has_skio_credentials()


@st.cache_data(ttl=300, show_spinner="Henter ordrer fra Skio …")
def fetch_skio_orders(
    from_iso: str, to_iso: str, limit: int = 500,
) -> list[dict]:
    """Fetch Skio subscription orders as serialisable dicts for Streamlit caching.

    Returns an empty list if Skio isn't configured or the API call fails;
    callers should surface a friendly message based on ``_skio_available``.
    """
    if not _skio_available:
        return []
    try:
        client = SkioClient()
    except ValueError as e:
        logger.warning("Skio client init failed: %s", e)
        return []
    try:
        orders = client.fetch_orders(
            from_iso=from_iso, to_iso=to_iso, limit=limit,
        )
        return [
            {
                "order_id": o.order_id,
                "shopify_order_id": o.shopify_order_id,
                "platform_number": o.platform_number,
                "created_at": o.created_at,
                "line_items": [
                    {
                        "title": li.title,
                        "product_title": li.product_title,
                        "quantity": li.quantity,
                        "sku": li.sku,
                        "variant_id": li.variant_id,
                        "custom_attributes": li.custom_attributes,
                    }
                    for li in o.line_items
                ],
            }
            for o in orders
        ]
    except Exception as e:
        logger.warning("Skio order fetch failed: %s", e)
        return []
    finally:
        client.close()


def enrich_with_shopify(df: pd.DataFrame) -> pd.DataFrame:
    """Add Shopify metafield columns to the product DataFrame."""
    shopify_data = fetch_shopify_metafields()
    if not shopify_data:
        df["Pris/kg (utpris)"] = np.nan
        df["Pris/porsjon"] = np.nan
        df["Porsjoner"] = np.nan
        df["S: antall enheter"] = np.nan
        df["S: f-pack kg"] = np.nan
        df["SKU Name"] = ""
        df["Slot kg (Shopify)"] = np.nan
        return df

    pris_kg = []
    pris_porsjon = []
    porsjoner = []
    s_enheter = []
    s_fpack_kg = []
    sku_names = []
    for _, row in df.iterrows():
        # Match by Shopify Variant ID
        vid = row.get("Shopify Variant ID")
        vid_str = str(int(vid)) if pd.notna(vid) else ""
        match = shopify_data.get(vid_str)
        pris_kg.append(match["price_per_kg"] if match else None)
        pris_porsjon.append(match["price_per_portion"] if match else None)
        porsjoner.append(match["porsjoner"] if match else None)
        s_enheter.append(match["slot_antall_enheter"] if match else None)
        s_fpack_kg.append(match["slot_fpack_kg"] if match else None)
        sku_names.append(match["sku_name"] if match else "")

    df["Pris/kg (utpris)"] = pd.array(pris_kg, dtype=pd.Float64Dtype())
    df["Pris/porsjon"] = pd.array(pris_porsjon, dtype=pd.Float64Dtype())
    df["Porsjoner"] = pd.array(porsjoner, dtype=pd.Float64Dtype())
    df["S: antall enheter"] = pd.array(s_enheter, dtype=pd.Float64Dtype())
    df["S: f-pack kg"] = pd.array(s_fpack_kg, dtype=pd.Float64Dtype())
    df["SKU Name"] = sku_names

    # Yrja pris/kg is computed dynamically in the benchmark tab
    # Store slot kg for later use
    _enheter = df["S: antall enheter"].to_numpy(dtype="float64", na_value=np.nan)
    _fpack = df["S: f-pack kg"].to_numpy(dtype="float64", na_value=np.nan)
    df["Slot kg (Shopify)"] = _enheter * _fpack
    return df


# ── Sidebar ──────────────────────────────────────────────────────────

# Subscription prices
st.sidebar.header("Abonnementspriser (eks. frakt)")
prices = {}
for label, sub in SUBSCRIPTIONS.items():
    prices[label] = st.sidebar.number_input(
        f"{label} abonnement",
        value=sub["default_abbo_price"],
        step=50,
        min_value=0,
        key=f"price_{label}",
    )
shipping_price = st.sidebar.number_input(
    "Frakt per ordre (kr)",
    value=DEFAULT_SHIPPING_PRICE,
    step=10,
    min_value=0,
    key="price_shipping",
)
subscription_total_prices = {
    label: prices[label] + shipping_price for label in SUBSCRIPTIONS
}

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
if _shopify_available:
    st.sidebar.caption("✅ Shopify-tilkobling aktiv")
else:
    st.sidebar.caption(
        "⚠️ Shopify ikke konfigurert (sett SHOPIFY_SHOP_DOMAIN og SHOPIFY_ACCESS_TOKEN i .env)"
    )
if _skio_available:
    st.sidebar.caption("✅ Skio-tilkobling aktiv")
else:
    st.sidebar.caption(
        "⚠️ Skio ikke konfigurert (sett SKIO_API_TOKEN i .env)"
    )
if st.sidebar.button("🔄 Oppdater fra Notion & Shopify"):
    st.cache_data.clear()
    st.rerun()


# ── Helper: cost breakdown for a subscription ───────────────────────


def compute_cost_breakdown(
    price: float,
    varekostnad: float,
    total_fpacks: float,
    utpris_kg_total: float | None = None,
    pris_porsjon_total: float | None = None,
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

    result = {
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
    # Shopify pricing data (if available)
    if utpris_kg_total is not None:
        result["Utpris/kg totalt"] = utpris_kg_total
    if pris_porsjon_total is not None:
        result["Pris/porsjon totalt"] = pris_porsjon_total
    return result


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

    # Shopify pricing arrays (NaN-safe)
    utpris_kg_arr = df_sim["Slot utpris/kg (kr)"].values
    pris_porsjon_arr = df_sim["Slot pris/porsjon (kr)"].values
    slot_vekt_shopify_arr = df_sim["Slot vekt (Shopify)"].values
    slot_porsjoner_arr = df_sim["Slot porsjoner"].values

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

    # Shopify pricing totals per box (nansum to handle missing data)
    total_utpris_kg = np.nansum(utpris_kg_arr[sim], axis=1)
    total_pris_porsjon = np.nansum(pris_porsjon_arr[sim], axis=1)
    total_box_kg = np.nansum(slot_vekt_shopify_arr[sim], axis=1)
    total_porsjoner = np.nansum(slot_porsjoner_arr[sim], axis=1)

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

    metrics_dict = {
        "Innkjøpspris": total_cogs,
        "Dekningsbidrag": dekningsbidrag,
        "Lager, distribusjon & transaksjoner": ops_total,
        "MVA": np.full(n_sims, mva),
        "Driftsresultat": bunnlinje,
    }
    # Add Shopify pricing columns if any data is present
    if not np.all(np.isnan(utpris_kg_arr)):
        metrics_dict["Utpris/kg totalt"] = total_utpris_kg
    if not np.all(np.isnan(pris_porsjon_arr)):
        metrics_dict["Pris/porsjon totalt"] = total_pris_porsjon
    if not np.all(np.isnan(slot_vekt_shopify_arr)):
        metrics_dict["Boks vekt (kg)"] = total_box_kg
    if not np.all(np.isnan(slot_porsjoner_arr)):
        metrics_dict["Boks porsjoner"] = total_porsjoner

    return {
        "metrics": pd.DataFrame(metrics_dict),
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
        row_dict = {
            "Slot": slot_nr,
            "Produktnavn": row["Produktnavn"],
            "F-packs": int(row["SLOT: antall enheter"]),
            "Vekt (kg)": round(slot_vekt, 2),
            "Innpris (kr)": round(innpris_kg * slot_vekt, 2),
        }
        utpris = row.get("Pris/kg (utpris)")
        if pd.notna(utpris):
            row_dict["Utpris/kg (kr)"] = round(utpris * slot_vekt, 2)
        porsjon = row.get("Pris/porsjon")
        if pd.notna(porsjon):
            row_dict["Pris/porsjon (kr)"] = round(porsjon, 2)
        rows.append(row_dict)

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
df = enrich_with_shopify(df)

df_sim = df.dropna(
    subset=["Innpris (kr/kg)", "SLOT: tot slot vekt", "SLOT: antall enheter"]
).copy()
df_sim["Slot innpris (kr)"] = (
    df_sim["Innpris (kr/kg)"] * df_sim["SLOT: tot slot vekt"]
)
# Compute customer-facing slot prices if Shopify data is available
df_sim["Slot utpris/kg (kr)"] = (
    df_sim["Pris/kg (utpris)"] * df_sim["SLOT: tot slot vekt"]
)
df_sim["Slot pris/porsjon (kr)"] = df_sim["Pris/porsjon"]
# Shopify slot-level metrics for box weight and portions histograms
df_sim["Slot vekt (Shopify)"] = df_sim["S: antall enheter"] * df_sim["S: f-pack kg"]
df_sim["Slot porsjoner"] = df_sim["Porsjoner"]

if len(df_sim) > 0:
    avg_slot_cogs = df_sim["Slot innpris (kr)"].mean()
    avg_slot_fpacks = df_sim["SLOT: antall enheter"].mean()
else:
    avg_slot_cogs = 0.0
    avg_slot_fpacks = 0.0


# ══════════════════════════════════════════════════════════════════════
#  TABS
# ══════════════════════════════════════════════════════════════════════

(
    tab_dashboard,
    tab_analyse,
    tab_benchmark,
    tab_health,
    tab_influencer,
    tab_orders,
    tab_fulfillment,
) = st.tabs(
    [
        "📦 Dashboard",
        "📊 Unit Economics",
        "📈 Pris benchmarking",
        "🏥 Forretningshelse",
        "🤝 Influencer modelling",
        "🛒 Ordrestatus",
        "📦 Fulfillment",
    ]
)


# ── Tab 1: Dashboard ─────────────────────────────────────────────────

with tab_dashboard:
    st.subheader("Produktoversikt")
    st.dataframe(df, use_container_width=True, hide_index=True)
    _shopify_meta = fetch_shopify_metafields()
    _n_matched = df["Pris/kg (utpris)"].notna().sum()
    st.caption(
        f"{len(df)} produkter lastet fra Notion"
        + (f" · {_n_matched} med Shopify-prisdata" if _shopify_meta else "")
    )

    # ── Per-slot statistics for box-level aggregation ────────
    _portions_series = df["Porsjoner"].dropna()
    _portions_label = "Porsjoner"
    if len(_portions_series) == 0:
        _portions_series = df["SLOT: antall enheter"].dropna()
        _portions_label = "Antall enheter"
    _slot_wt_series = df["SLOT: tot slot vekt"].dropna()

    _has_portions = len(_portions_series) > 0
    _has_weight = len(_slot_wt_series) > 0
    _slot_port_mean = _portions_series.mean() if _has_portions else 0.0
    _slot_port_std = _portions_series.std() if _has_portions else 0.0
    _slot_wt_mean = _slot_wt_series.mean() if _has_weight else 0.0
    _slot_wt_std = _slot_wt_series.std() if _has_weight else 0.0

    if _shopify_meta:
        with st.expander("🔍 Shopify metafelter (debug)"):
            all_keys: set[str] = set()
            for title, data in _shopify_meta.items():
                all_keys.update(data.get("metafields", {}).keys())
            st.write("**Metafield-nøkler funnet:**", sorted(all_keys))
            sample_rows = []
            for title, data in list(_shopify_meta.items())[:10]:
                sample_rows.append(
                    {
                        "Produkt": title,
                        "Pris/kg": data["price_per_kg"],
                        "Pris/porsjon": data["price_per_portion"],
                        "Variant pris": data["variant_price"],
                        "Alle metafelter": str(data["metafields"]),
                    }
                )
            st.dataframe(
                pd.DataFrame(sample_rows),
                use_container_width=True,
                hide_index=True,
            )

    st.subheader("Abonnementer")
    cols = st.columns(len(SUBSCRIPTIONS))
    for i, (label, sub) in enumerate(SUBSCRIPTIONS.items()):
        with cols[i]:
            abbo_price = prices[label]
            price = subscription_total_prices[label]
            n_slots = sub["slots"]
            st.metric(label, f"{price:,} kr")
            st.caption(
                f"Abonnement {abbo_price:,.0f} kr + frakt {shipping_price:,.0f} kr"
            )
            st.caption(f"{n_slots} valgfrie produktslots")

            # Box-level portions & weight (sum of n_slots independent draws)
            if _has_portions:
                _box_port_mean = n_slots * _slot_port_mean
                _box_port_std = np.sqrt(n_slots) * _slot_port_std
                st.caption(
                    f"{_portions_label}: **{_box_port_mean:.1f}** ± {_box_port_std:.1f}"
                )
            if _has_weight:
                _box_wt_mean = n_slots * _slot_wt_mean
                _box_wt_std = np.sqrt(n_slots) * _slot_wt_std
                st.caption(
                    f"Vekt: **{_box_wt_mean:.2f}** ± {_box_wt_std:.2f} kg"
                )

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


# ── Tab 2: Unit Economics ────────────────────────────────────────────

# Chart metrics: (data_column, display_name)
CHART_METRICS = [
    ("Innkjøpspris", "Varekostnad"),
    ("Dekningsbidrag", "Dekningsbidrag"),
    ("Lager, distribusjon & transaksjoner", "Andre driftskostnader"),
    ("Driftsresultat", "Driftsresultat"),
]
# Shopify pricing chart metrics (conditionally shown when data is available)
SHOPIFY_CHART_METRICS = [
    ("Utpris/kg totalt", "Utpris/kg (totalt for boksen)"),
    ("Pris/porsjon totalt", "Pris/porsjon (totalt for boksen)"),
    ("Boks vekt (kg)", "Boks vekt (kg) — antall enheter × f-pack kg"),
    ("Boks porsjoner", "Boks porsjoner — sum porsjoner per slot"),
]

def _render_tab_analyse():
    if len(df_sim) == 0:
        st.warning(
            "Ingen produkter med komplett data (Innpris + SLOT-kolonner)."
        )
        return

    # ── Subscription selector ─────────────────────────────────
    sub_label = st.selectbox("Velg abonnement", list(SUBSCRIPTIONS.keys()))
    sub = SUBSCRIPTIONS[sub_label]
    n_slots = sub["slots"]
    price = subscription_total_prices[sub_label]
    st.caption(
        f"Pris: abonnement {prices[sub_label]:,.0f} kr + "
        f"frakt {shipping_price:,.0f} kr = {price:,.0f} kr"
    )

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
        return

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
                _bonus_fpacks = (
                    df_sim.loc[_bidx, "SLOT: antall enheter"] * _bfrac
                )
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
                df_sim.loc[idx, "Innpris (kr/kg)"]
                * df_sim.loc[idx, "SLOT: tot slot vekt"]
                for idx in _custom_picks
            )
            + _bonus_cogs
        )
        _c_fpacks = (
            sum(
                df_sim.loc[idx, "SLOT: antall enheter"] for idx in _custom_picks
            )
            + _bonus_fpacks
        )
        _c_rev = price / (1 + MVA_RATE)
        _c_ops = (
            lager_fast
            + lager_var * _c_fpacks
            + distribusjon
            + emballasje
            + price * (shopify_var_pct / 100)
            + shopify_fast
            + price * (skio_var_pct / 100)
            + skio_fast
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
                row_dict["Min boks"] = _fmt_pct(
                    custom_metrics[data_col] / revenue_ex_mva * 100
                )
            else:
                row_dict["Min boks"] = _fmt_kr(custom_metrics[data_col])
        summary_rows.append(row_dict)

    # Add Shopify pricing rows to summary if data is present
    for data_col, display_name in SHOPIFY_CHART_METRICS:
        if data_col in results_df.columns:
            vals = results_df[data_col]
            row_dict = {
                "Metrikk": display_name,
                "Snitt": _fmt_kr(vals.mean()),
                "Std.avvik": _fmt_kr(vals.std()),
                "Min": _fmt_kr(vals.min()),
                "Maks": _fmt_kr(vals.max()),
            }
            if _has_custom and data_col in custom_metrics:
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

    # ── Shopify pricing distributions ─────────────────────────────
    _has_shopify_charts = any(
        col in results_df.columns for col, _ in SHOPIFY_CHART_METRICS
    )
    if _has_shopify_charts:
        st.subheader("Prisfordelinger (Shopify)")
        for data_col, display_name in SHOPIFY_CHART_METRICS:
            if data_col not in results_df.columns:
                continue
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


with tab_analyse:
    _render_tab_analyse()


# ── Tab 3: Pris benchmarking

def _render_tab_benchmark():
    if not _shopify_available:
        st.warning("Shopify ikke konfigurert — sett SHOPIFY_SHOP_DOMAIN og SHOPIFY_ACCESS_TOKEN i .env")
        return

    # ── Yrja pricing controls ─────────────────────────────
    _DELIVERY_COST = float(shipping_price)
    _pc1, _pc2 = st.columns(2)
    _slot_price = _pc1.slider(
        "Pris per slot (kr)", min_value=200, max_value=600, value=400, step=10, key="bench_slot_price",
    )
    _pc2.metric("Leveringskostnad", f"{_DELIVERY_COST:,.0f} kr")

    # Show effective price per slot for each box size
    _eff_cols = st.columns(len(SUBSCRIPTIONS))
    for _i, (_lbl, _sub_info) in enumerate(SUBSCRIPTIONS.items()):
        _ns = _sub_info["slots"]
        _total = _slot_price * _ns + _DELIVERY_COST
        _eff_per_slot = _total / _ns
        _eff_cols[_i].metric(_lbl, f"{_eff_per_slot:,.0f} kr/slot", delta=f"totalt {_total:,.0f} kr")

    # Compute Yrja pris/kg dynamically based on slider
    # Effective price per slot = (slot_price * n_slots + delivery) / n_slots
    # For the product table we use a simple slot_price since delivery is box-level
    _slot_kg_col = df["Slot kg (Shopify)"].to_numpy(dtype="float64", na_value=np.nan)
    df["Yrja pris/kg"] = np.where(_slot_kg_col > 0, _slot_price / _slot_kg_col, np.nan)

    # Also update df_sim with the new Yrja pris/kg
    _sim_slot_kg = df_sim["Slot kg (Shopify)"].to_numpy(dtype="float64", na_value=np.nan) if "Slot kg (Shopify)" in df_sim.columns else df_sim["S: antall enheter"].to_numpy(dtype="float64", na_value=np.nan) * df_sim["S: f-pack kg"].to_numpy(dtype="float64", na_value=np.nan)
    df_sim["Yrja pris/kg"] = np.where(_sim_slot_kg > 0, _slot_price / _sim_slot_kg, np.nan)

    st.divider()

    # ── Section A: Product table from Shopify ─────────────────
    st.subheader("Produktoversikt")
    _bench_cols = [
        "Produktnavn", "SKU Name", "Porsjoner",
        "S: antall enheter", "S: f-pack kg",
        "Yrja pris/kg", "ODA pris/kg", "AMOI pris/kg",
    ]
    _bench_display = df[[c for c in _bench_cols if c in df.columns]].copy()

    # ── Section B: Filter ─────────────────────────────────────
    _fc1, _fc2 = st.columns(2)
    _filter_oda = _fc1.checkbox("Kun produkter med ODA pris/kg", value=False, key="bench_filter_oda")
    _filter_amoi = _fc2.checkbox("Kun produkter med AMOI pris/kg", value=False, key="bench_filter_amoi")

    _bench_filtered = _bench_display.copy()
    if _filter_oda and "ODA pris/kg" in _bench_filtered.columns:
        _bench_filtered = _bench_filtered[_bench_filtered["ODA pris/kg"].notna()]
    if _filter_amoi and "AMOI pris/kg" in _bench_filtered.columns:
        _bench_filtered = _bench_filtered[_bench_filtered["AMOI pris/kg"].notna()]

    st.dataframe(_bench_filtered, use_container_width=True, hide_index=True)
    st.caption(f"{len(_bench_filtered)} produkter vist")

    # ── Section C: Price/kg scatter chart ─────────────────────
    st.subheader("Pris/kg sammenligning")
    _scatter_df = _bench_filtered.dropna(subset=["Produktnavn"]).copy()
    _scatter_rows: list[dict] = []
    for _, _r in _scatter_df.iterrows():
        _name = _r["Produktnavn"]
        if pd.notna(_r.get("Yrja pris/kg")):
            _scatter_rows.append({"Produkt": _name, "kr/kg": _r["Yrja pris/kg"], "Kilde": "Yrja"})
        if pd.notna(_r.get("ODA pris/kg")):
            _scatter_rows.append({"Produkt": _name, "kr/kg": _r["ODA pris/kg"], "Kilde": "ODA"})
        if pd.notna(_r.get("AMOI pris/kg")):
            _scatter_rows.append({"Produkt": _name, "kr/kg": _r["AMOI pris/kg"], "Kilde": "AMOI"})

    if _scatter_rows:
        _scatter_plot_df = pd.DataFrame(_scatter_rows)
        _yrja_data = _scatter_plot_df[_scatter_plot_df["Kilde"] == "Yrja"]
        _competitor_data = _scatter_plot_df[_scatter_plot_df["Kilde"] != "Yrja"]

        fig = go.Figure()
        # Yrja as a line
        if len(_yrja_data) > 0:
            fig.add_trace(go.Scatter(
                x=_yrja_data["Produkt"],
                y=_yrja_data["kr/kg"],
                mode="lines+markers",
                name="Yrja",
                line=dict(color="#27ae60", width=2),
                marker=dict(size=8),
            ))
        # ODA as scatter
        _oda_data = _competitor_data[_competitor_data["Kilde"] == "ODA"]
        if len(_oda_data) > 0:
            fig.add_trace(go.Scatter(
                x=_oda_data["Produkt"],
                y=_oda_data["kr/kg"],
                mode="markers",
                name="ODA",
                marker=dict(color="#f1c40f", size=10),
            ))
        # AMOI as scatter
        _amoi_data = _competitor_data[_competitor_data["Kilde"] == "AMOI"]
        if len(_amoi_data) > 0:
            fig.add_trace(go.Scatter(
                x=_amoi_data["Produkt"],
                y=_amoi_data["kr/kg"],
                mode="markers",
                name="AMOI",
                marker=dict(color="#3498db", size=10),
            ))
        fig.update_layout(
            title="Pris per kg: Yrja vs. ODA vs. AMOI",
            height=500,
            xaxis_tickangle=-45,
            yaxis_title="kr/kg",
            margin=dict(b=120),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Ingen prisdata tilgjengelig for valgt filter.")

    # ── Section D: Filtered benchmark simulations ───────────────
    st.subheader("Bokssimulering — prisbenchmark")

    _b_sub_label = st.selectbox(
        "Velg abonnement", list(SUBSCRIPTIONS.keys()), key="bench_sub"
    )
    _b_sub = SUBSCRIPTIONS[_b_sub_label]
    _b_n_slots = _b_sub["slots"]
    _b_price = subscription_total_prices[_b_sub_label]
    _B_N_SIMS = 10_000

    # Build filtered product pools
    _yrja_col = df_sim["Yrja pris/kg"].to_numpy(dtype="float64", na_value=np.nan)
    _oda_col = df_sim["ODA pris/kg"].to_numpy(dtype="float64", na_value=np.nan) if "ODA pris/kg" in df_sim.columns else np.full(len(df_sim), np.nan)
    _amoi_col = df_sim["AMOI pris/kg"].to_numpy(dtype="float64", na_value=np.nan) if "AMOI pris/kg" in df_sim.columns else np.full(len(df_sim), np.nan)

    _has_yrja = ~np.isnan(_yrja_col)
    _has_oda = ~np.isnan(_oda_col)
    _has_amoi = ~np.isnan(_amoi_col)

    _pool_yrja_oda = df_sim[_has_yrja & _has_oda]
    _pool_yrja_amoi = df_sim[_has_yrja & _has_amoi]
    _pool_all_three = df_sim[_has_yrja & _has_oda & _has_amoi]

    st.caption(
        f"Produkter: {len(_pool_yrja_oda)} med Yrja+ODA, "
        f"{len(_pool_yrja_amoi)} med Yrja+AMOI, "
        f"{len(_pool_all_three)} med alle tre"
    )

    _b_run = st.button("🔬 Kjør benchmark-simulering", type="primary", key="bench_run")
    _b_key = f"bench_{_b_sub_label}"

    def _run_filtered_sim(pool: pd.DataFrame, n_slots: int, price: float, n_sims: int) -> dict | None:
        if len(pool) == 0:
            return None
        return run_simulation(pool, n_slots, {}, price, n_sims)

    if _b_run:
        st.session_state[_b_key] = {
            "yrja_oda": _run_filtered_sim(_pool_yrja_oda, _b_n_slots, _b_price, _B_N_SIMS),
            "yrja_amoi": _run_filtered_sim(_pool_yrja_amoi, _b_n_slots, _b_price, _B_N_SIMS),
            "all_three": _run_filtered_sim(_pool_all_three, _b_n_slots, _b_price, _B_N_SIMS),
        }

    if _b_key not in st.session_state:
        st.info("Klikk **Kjør benchmark-simulering** for å starte.")
        return

    _b_res = st.session_state[_b_key]

    # Effective Yrja total per box = slot_price * n_slots + delivery
    _yrja_box_total = _slot_price * _b_n_slots + _DELIVERY_COST
    _AMOI_DELIVERY = 169.0

    # ── Helper: compute box-level kr totals from picks ─────────
    def _compute_box_totals(pool: pd.DataFrame, picks: np.ndarray) -> dict:
        """Compute per-box Yrja/ODA/AMOI total kr (not kr/kg) for each simulated box."""
        _kg = pool["SLOT: tot slot vekt"].values
        _y = pool["Yrja pris/kg"].to_numpy(dtype="float64", na_value=np.nan)
        _o = pool["ODA pris/kg"].to_numpy(dtype="float64", na_value=np.nan) if "ODA pris/kg" in pool.columns else np.full(len(pool), np.nan)
        _a = pool["AMOI pris/kg"].to_numpy(dtype="float64", na_value=np.nan) if "AMOI pris/kg" in pool.columns else np.full(len(pool), np.nan)
        _i2l = {idx: i for i, idx in enumerate(pool.index)}
        _lp = np.vectorize(_i2l.get)(picks)
        _slot_kg = _kg[_lp]
        # Yrja total = fixed box price (slot_price * n_slots + delivery)
        _n_sims = picks.shape[0]
        _yrja_tot = np.full(_n_sims, _yrja_box_total)
        # Competitor totals = sum(pris/kg * kg) across slots
        _oda_tot = (_o[_lp] * _slot_kg).sum(axis=1)
        _amoi_tot = (_a[_lp] * _slot_kg).sum(axis=1) + _AMOI_DELIVERY
        return {
            "yrja_tot": _yrja_tot,
            "oda_tot": _oda_tot,
            "amoi_tot": _amoi_tot,
            "total_kg": _slot_kg.sum(axis=1),
        }

    # ── Section E: Discrepancy summary table ───────────────────
    st.subheader("Benchmark-resultater")

    _summary_rows = []
    for _label, _pool, _comp_key in [
        ("Yrja vs ODA", _pool_yrja_oda, "yrja_oda"),
        ("Yrja vs AMOI", _pool_yrja_amoi, "yrja_amoi"),
        ("Yrja vs ODA + AMOI", _pool_all_three, "all_three"),
    ]:
        _sim = _b_res.get(_comp_key)
        if _sim is None:
            continue
        _picks = _sim["picks"]
        _bt = _compute_box_totals(_pool, _picks)

        # Yrja vs ODA discrepancy (in kr per box)
        if "ODA" in _label or "alle" in _label.lower() or "ODA + AMOI" in _label:
            _diff_oda = _bt["yrja_tot"] - _bt["oda_tot"]
            _pct_oda = _diff_oda / _bt["oda_tot"] * 100
            _summary_rows.append({
                "Sammenligning": f"{_label} (ODA)",
                "Ant. produkter": len(_pool),
                "Snitt avvik (kr)": f"{_diff_oda.mean():+,.0f}",
                "Snitt avvik (%)": f"{_pct_oda.mean():+.1f}%",
                "Std.avvik (kr)": f"{_diff_oda.std():,.0f}",
                "Min (kr)": f"{_diff_oda.min():+,.0f}",
                "Maks (kr)": f"{_diff_oda.max():+,.0f}",
            })
        # Yrja vs AMOI discrepancy
        if "AMOI" in _label:
            _diff_amoi = _bt["yrja_tot"] - _bt["amoi_tot"]
            _pct_amoi = _diff_amoi / _bt["amoi_tot"] * 100
            _summary_rows.append({
                "Sammenligning": f"{_label} (AMOI)",
                "Ant. produkter": len(_pool),
                "Snitt avvik (kr)": f"{_diff_amoi.mean():+,.0f}",
                "Snitt avvik (%)": f"{_pct_amoi.mean():+.1f}%",
                "Std.avvik (kr)": f"{_diff_amoi.std():,.0f}",
                "Min (kr)": f"{_diff_amoi.min():+,.0f}",
                "Maks (kr)": f"{_diff_amoi.max():+,.0f}",
            })

    if _summary_rows:
        st.dataframe(pd.DataFrame(_summary_rows), use_container_width=True, hide_index=True)
        st.caption("Positivt avvik = Yrja er dyrere, negativt = Yrja er billigere")

    # ── Histogram: box total kr distributions ──────────────────
    st.subheader("Prisfordeling per boks (total kr)")
    _sim_all = _b_res.get("all_three")
    if _sim_all is not None:
        _bt_all = _compute_box_totals(_pool_all_three, _sim_all["picks"])
        _hist_fig = go.Figure()
        _hist_fig.add_trace(go.Histogram(
            x=_bt_all["yrja_tot"], name="Yrja",
            marker_color="#27ae60", opacity=0.6, nbinsx=60,
        ))
        _hist_fig.add_trace(go.Histogram(
            x=_bt_all["oda_tot"], name="ODA",
            marker_color="#f1c40f", opacity=0.6, nbinsx=60,
        ))
        _hist_fig.add_trace(go.Histogram(
            x=_bt_all["amoi_tot"], name="AMOI",
            marker_color="#3498db", opacity=0.6, nbinsx=60,
        ))
        _hist_fig.update_layout(
            barmode="overlay", height=400,
            xaxis_title="Total boks (kr)", yaxis_title="Antall bokser",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="center", x=0.5),
            margin=dict(t=40, b=30),
        )
        st.plotly_chart(_hist_fig, use_container_width=True)
    else:
        st.info("Ingen produkter med alle tre priskilder — kan ikke vise histogram.")

    # ── Box explorer (all-three pool) ─────────────────────────
    st.subheader("Utforsk enkeltbokser")
    if _sim_all is None or len(_pool_all_three) == 0:
        st.info("Ingen produkter med alle tre priskilder.")
        return

    _ex_picks = _sim_all["picks"]
    _ex_n = _ex_picks.shape[0]
    st.markdown(f"**{_ex_n:,}** bokser simulert med {len(_pool_all_three)} produkter (Yrja + ODA + AMOI)")

    _box_idx = st.number_input(
        "Boks nr.", min_value=1, max_value=_ex_n, value=1, step=1, key="bench_box_idx",
    )
    _bi = _box_idx - 1
    _box_picks = _ex_picks[_bi]
    # Yrja per-slot price WITHOUT delivery = slot_price
    _yrja_slot_only = _slot_price

    _box_rows = []
    for _slot_nr, _idx in enumerate(_box_picks, 1):
        _row = _pool_all_three.loc[_idx]
        _slot_kg = float(_row["SLOT: tot slot vekt"]) if pd.notna(_row["SLOT: tot slot vekt"]) else 0.0
        _oda_pkg = float(_row["ODA pris/kg"]) if pd.notna(_row.get("ODA pris/kg")) else 0.0
        _amoi_pkg = float(_row["AMOI pris/kg"]) if pd.notna(_row.get("AMOI pris/kg")) else 0.0
        _box_rows.append({
            "Slot": str(_slot_nr),
            "Produktnavn": _row["Produktnavn"],
            "kg": round(_slot_kg, 2),
            "Yrja (kr)": round(_yrja_slot_only, 0),
            "ODA (kr)": round(_oda_pkg * _slot_kg, 0),
            "AMOI (kr)": round(_amoi_pkg * _slot_kg, 0),
        })

    _box_detail_df = pd.DataFrame(_box_rows)
    _products_yrja = _slot_price * _b_n_slots
    _products_oda = _box_detail_df["ODA (kr)"].sum()
    _products_amoi = _box_detail_df["AMOI (kr)"].sum()
    _tot_kg = _box_detail_df["kg"].sum()
    _n = len(_box_rows)

    # Delivery rows + totals
    _extra = pd.DataFrame([
        {"Slot": "", "Produktnavn": "Levering", "kg": None,
         "Yrja (kr)": round(_DELIVERY_COST, 0), "ODA (kr)": None, "AMOI (kr)": round(_AMOI_DELIVERY, 0)},
        {"Slot": "", "Produktnavn": "TOTALT", "kg": round(_tot_kg, 2),
         "Yrja (kr)": round(_products_yrja + _DELIVERY_COST, 0),
         "ODA (kr)": round(_products_oda, 0),
         "AMOI (kr)": round(_products_amoi + _AMOI_DELIVERY, 0)},
        {"Slot": "", "Produktnavn": "SNITT per slot", "kg": round(_tot_kg / _n, 2),
         "Yrja (kr)": round((_products_yrja + _DELIVERY_COST) / _n, 0),
         "ODA (kr)": round(_products_oda / _n, 0),
         "AMOI (kr)": round((_products_amoi + _AMOI_DELIVERY) / _n, 0)},
    ])
    _box_detail_df = pd.concat([_box_detail_df, _extra], ignore_index=True)
    st.dataframe(_box_detail_df, use_container_width=True, hide_index=True)


with tab_benchmark:
    _render_tab_benchmark()


# ── Tab 4: Forretningshelse

def _render_tab_health():
    st.warning("🚧 **WORK IN PROGRESS** — Denne fanen er under utvikling og tallene er ikke verifisert.")

    if len(df_sim) == 0:
        st.warning(
            "Ingen produkter med komplett data (Innpris + SLOT-kolonner)."
        )
        return

    st.subheader("Forretningsmodell & vekstprognose")
    st.caption(
        "Juster parameterne for å se hvordan virksomheten utvikler seg over tid. "
        "Varekostnad og driftskostnader beregnes fra produktdata og sidebar-parametere."
    )

    # ── Input parameters ─────────────────────────────────────
    _h_col1, _h_col2, _h_col3 = st.columns(3)

    with _h_col1:
        st.markdown("**Vekst & churn**")
        _h_start_subs = st.number_input(
            "Startabonnenter",
            value=100,
            min_value=0,
            step=10,
            key="h_start_subs",
        )
        _h_monthly_growth = st.number_input(
            "Månedlig vekst nye kunder (%)",
            value=15.0,
            min_value=0.0,
            max_value=200.0,
            step=1.0,
            format="%.1f",
            key="h_growth",
            help="Nye abonnenter per måned som andel av nåværende base",
        )
        _h_churn = st.number_input(
            "Månedlig churn (%)",
            value=5.0,
            min_value=0.0,
            max_value=100.0,
            step=0.5,
            format="%.1f",
            key="h_churn",
            help="Andel abonnenter som avslutter per måned",
        )
        _h_months = st.slider(
            "Tidshorisont (måneder)",
            min_value=6,
            max_value=60,
            value=24,
            step=6,
            key="h_months",
        )

    with _h_col2:
        st.markdown("**Kundeanskaffelse & pris**")
        _h_cac = st.number_input(
            "CAC per kunde (kr)",
            value=500.0,
            min_value=0.0,
            step=50.0,
            format="%.0f",
            key="h_cac",
            help="Gjennomsnittlig kostnad for å skaffe én ny abonnent",
        )
        _h_deliveries = st.number_input(
            "Leveranser per måned",
            value=2.0,
            min_value=0.5,
            max_value=8.0,
            step=0.5,
            format="%.1f",
            key="h_deliveries",
            help="Antall boksleveranser per abonnent per måned",
        )
        _h_price_factor = st.number_input(
            "Prisjustering (%)",
            value=0.0,
            min_value=-50.0,
            max_value=100.0,
            step=5.0,
            format="%.0f",
            key="h_price_factor",
            help="Juster alle abonnementspriser relativt til sidebar-verdiene",
        )

    with _h_col3:
        st.markdown("**Abonnementsmiks (vekting)**")
        _h_mix_raw: dict[str, int] = {}
        _h_mix_default = max(1, int(round(100 / len(SUBSCRIPTIONS))))
        for _lbl, _sub_info in SUBSCRIPTIONS.items():
            _h_mix_raw[_lbl] = st.slider(
                f"{_lbl} ({_sub_info['slots']} slots)",
                min_value=0,
                max_value=100,
                value=_h_mix_default,
                key=f"h_mix_{_lbl}",
            )
        _h_mix_sum = sum(_h_mix_raw.values()) or 1
        _h_mix = {k: v / _h_mix_sum * 100 for k, v in _h_mix_raw.items()}
        st.caption(
            "Effektiv fordeling: "
            + " / ".join(f"{_h_mix[k]:.0f}%" for k in _h_mix)
        )

    # ── Compute weighted per-order economics ──────────────────
    _h_price_adj = 1 + _h_price_factor / 100
    _h_adj_abbo_prices = {
        lbl: prices[lbl] * _h_price_adj for lbl in SUBSCRIPTIONS
    }
    _h_adj_prices = {
        lbl: _h_adj_abbo_prices[lbl] + shipping_price
        for lbl in SUBSCRIPTIONS
    }

    _h_weighted_price = sum(
        _h_adj_prices[lbl] * (_h_mix[lbl] / 100) for lbl in SUBSCRIPTIONS
    )
    _h_weighted_cogs = sum(
        avg_slot_cogs * SUBSCRIPTIONS[lbl]["slots"] * (_h_mix[lbl] / 100)
        for lbl in SUBSCRIPTIONS
    )
    _h_weighted_fpacks = sum(
        avg_slot_fpacks * SUBSCRIPTIONS[lbl]["slots"] * (_h_mix[lbl] / 100)
        for lbl in SUBSCRIPTIONS
    )

    _h_rev_ex_mva = _h_weighted_price / (1 + MVA_RATE)
    _h_ops = (
        lager_fast
        + lager_var * _h_weighted_fpacks
        + distribusjon
        + emballasje
        + _h_weighted_price * (shopify_var_pct / 100)
        + shopify_fast
        + _h_weighted_price * (skio_var_pct / 100)
        + skio_fast
    )
    _h_margin_per_order = _h_rev_ex_mva - _h_weighted_cogs - _h_ops
    _h_margin_per_sub_month = _h_margin_per_order * _h_deliveries

    # LTV & CAC
    _h_churn_rate = _h_churn / 100
    _h_avg_lifetime = (1 / _h_churn_rate) if _h_churn_rate > 0 else float("inf")
    _h_ltv = (
        _h_margin_per_sub_month * _h_avg_lifetime
        if _h_churn_rate > 0
        else float("inf")
    )
    _h_ltv_cac = _h_ltv / _h_cac if _h_cac > 0 else float("inf")
    _h_payback = (
        _h_cac / _h_margin_per_sub_month
        if _h_margin_per_sub_month > 0
        else float("inf")
    )

    # ── Key metrics row ───────────────────────────────────────
    st.divider()
    _m_cols = st.columns(6)
    _m_cols[0].metric("Snitt pris/ordre", f"{_h_weighted_price:,.0f} kr")
    _m_cols[1].metric("Margin/ordre", f"{_h_margin_per_order:,.0f} kr")
    _m_cols[2].metric(
        "Margin/abb/mnd", f"{_h_margin_per_sub_month:,.0f} kr"
    )
    _m_cols[3].metric(
        "LTV", f"{_h_ltv:,.0f} kr" if _h_ltv < 1e8 else "∞"
    )
    _m_cols[4].metric(
        "LTV / CAC",
        f"{_h_ltv_cac:.1f}x" if _h_ltv_cac < 1e6 else "∞",
    )
    _m_cols[5].metric(
        "Payback",
        f"{_h_payback:.1f} mnd" if _h_payback < 1e6 else "N/A",
    )

    # ── Per-order waterfall ───────────────────────────────────
    with st.expander("💰 Økonomi per ordre (gjennomsnittlig boks)"):
        _wf_mva = _h_weighted_price - _h_rev_ex_mva
        _wf_fig = go.Figure()
        _wf_fig.add_trace(
            go.Waterfall(
                x=[
                    "Omsetning inkl. MVA",
                    "MVA (15%)",
                    "Omsetning eks. MVA",
                    "Varekostnad",
                    "Driftskostnader",
                    "Margin",
                ],
                y=[
                    _h_weighted_price,
                    -_wf_mva,
                    _h_rev_ex_mva,
                    -_h_weighted_cogs,
                    -_h_ops,
                    _h_margin_per_order,
                ],
                measure=[
                    "absolute",
                    "relative",
                    "total",
                    "relative",
                    "relative",
                    "total",
                ],
                connector=dict(line=dict(color="rgba(0,0,0,0)")),
                increasing=dict(marker=dict(color="#27ae60")),
                decreasing=dict(marker=dict(color="#e74c3c")),
                totals=dict(marker=dict(color="#3498db")),
                text=[
                    f"{_h_weighted_price:,.0f}",
                    f"-{_wf_mva:,.0f}",
                    f"{_h_rev_ex_mva:,.0f}",
                    f"-{_h_weighted_cogs:,.0f}",
                    f"-{_h_ops:,.0f}",
                    f"{_h_margin_per_order:,.0f}",
                ],
                textposition="outside",
            )
        )
        _wf_fig.update_layout(
            title="Økonomi per ordre",
            yaxis_title="kr",
            height=350,
            margin=dict(t=40, b=30),
            showlegend=False,
        )
        st.plotly_chart(_wf_fig, use_container_width=True)

    # ── Monthly projection ────────────────────────────────────
    st.divider()
    _h_growth_rate = _h_monthly_growth / 100
    _proj_rows: list[dict] = []
    _subs = float(_h_start_subs)
    _cumulative_pl = 0.0

    for _m in range(1, _h_months + 1):
        _new = _subs * _h_growth_rate
        _churned = _subs * _h_churn_rate
        _end_subs = _subs + _new - _churned
        _avg_subs = (_subs + _end_subs) / 2

        _orders = _avg_subs * _h_deliveries
        _m_revenue = _orders * _h_weighted_price
        _m_revenue_ex = _orders * _h_rev_ex_mva
        _m_cogs = _orders * _h_weighted_cogs
        _m_ops = _orders * _h_ops
        _m_cac_spend = _new * _h_cac
        _m_gross_margin = _m_revenue_ex - _m_cogs
        _m_operating = _m_gross_margin - _m_ops
        _m_net = _m_operating - _m_cac_spend
        _cumulative_pl += _m_net

        _proj_rows.append(
            {
                "Måned": _m,
                "Abonnenter": round(_end_subs),
                "Nye": round(_new),
                "Churnet": round(_churned),
                "Ordrer": round(_orders),
                "Omsetning inkl. MVA": round(_m_revenue),
                "Omsetning eks. MVA": round(_m_revenue_ex),
                "Varekostnad": round(_m_cogs),
                "Driftskostnader": round(_m_ops),
                "CAC-kostnad": round(_m_cac_spend),
                "Bruttoresultat": round(_m_gross_margin),
                "Driftsresultat": round(_m_operating),
                "Nettoresultat": round(_m_net),
                "Kumulativt resultat": round(_cumulative_pl),
            }
        )
        _subs = _end_subs

    _proj_df = pd.DataFrame(_proj_rows)

    # Break-even detection
    _be_month = None
    _cum_vals = _proj_df["Kumulativt resultat"].values
    for _i in range(1, len(_cum_vals)):
        if _cum_vals[_i - 1] < 0 and _cum_vals[_i] >= 0:
            _be_month = int(_proj_df.loc[_i, "Måned"])
            break

    # ── Charts row 1: subscribers & revenue ────────────────────
    st.subheader("Vekstprognose")
    if _be_month:
        st.success(f"📈 Break-even i måned {_be_month}")
    elif _cum_vals[-1] < 0:
        st.warning("⚠️ Ingen break-even innen tidshorisonten")

    _chart_cols = st.columns(2)

    with _chart_cols[0]:
        fig_subs = go.Figure()
        fig_subs.add_trace(
            go.Scatter(
                x=_proj_df["Måned"],
                y=_proj_df["Abonnenter"],
                mode="lines",
                name="Aktive abonnenter",
                line=dict(color="#27ae60", width=2.5),
                fill="tozeroy",
                fillcolor="rgba(39,174,96,0.1)",
            )
        )
        fig_subs.add_trace(
            go.Bar(
                x=_proj_df["Måned"],
                y=_proj_df["Nye"],
                name="Nye",
                marker_color="rgba(52,152,219,0.5)",
            )
        )
        fig_subs.add_trace(
            go.Bar(
                x=_proj_df["Måned"],
                y=-_proj_df["Churnet"],
                name="Churnet",
                marker_color="rgba(231,76,60,0.5)",
            )
        )
        fig_subs.update_layout(
            title="Abonnentutvikling",
            xaxis_title="Måned",
            yaxis_title="Antall",
            height=400,
            margin=dict(t=40, b=30),
            barmode="relative",
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="center",
                x=0.5,
            ),
        )
        st.plotly_chart(fig_subs, use_container_width=True)

    with _chart_cols[1]:
        fig_rev = go.Figure()
        fig_rev.add_trace(
            go.Scatter(
                x=_proj_df["Måned"],
                y=_proj_df["Omsetning eks. MVA"],
                mode="lines",
                name="Omsetning eks. MVA",
                line=dict(color="#27ae60", width=2),
            )
        )
        fig_rev.add_trace(
            go.Scatter(
                x=_proj_df["Måned"],
                y=_proj_df["Varekostnad"],
                mode="lines",
                name="Varekostnad",
                line=dict(color="#e74c3c", width=2),
            )
        )
        fig_rev.add_trace(
            go.Scatter(
                x=_proj_df["Måned"],
                y=_proj_df["Driftskostnader"],
                mode="lines",
                name="Driftskostnader",
                line=dict(color="#f39c12", width=2),
            )
        )
        fig_rev.add_trace(
            go.Scatter(
                x=_proj_df["Måned"],
                y=_proj_df["CAC-kostnad"],
                mode="lines",
                name="CAC-kostnad",
                line=dict(color="#9b59b6", width=2),
            )
        )
        fig_rev.update_layout(
            title="Inntekter vs. kostnader",
            xaxis_title="Måned",
            yaxis_title="kr",
            height=400,
            margin=dict(t=40, b=30),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="center",
                x=0.5,
            ),
        )
        st.plotly_chart(fig_rev, use_container_width=True)

    # ── Charts row 2: monthly P&L & cumulative ────────────────
    _pl_cols = st.columns(2)

    with _pl_cols[0]:
        _net_colors = [
            "#27ae60" if v >= 0 else "#e74c3c"
            for v in _proj_df["Nettoresultat"]
        ]
        fig_pl = go.Figure()
        fig_pl.add_trace(
            go.Bar(
                x=_proj_df["Måned"],
                y=_proj_df["Nettoresultat"],
                name="Nettoresultat",
                marker_color=_net_colors,
            )
        )
        fig_pl.add_hline(y=0, line_dash="dash", line_color="gray")
        fig_pl.update_layout(
            title="Månedlig nettoresultat (etter CAC)",
            xaxis_title="Måned",
            yaxis_title="kr",
            height=400,
            margin=dict(t=40, b=30),
            showlegend=False,
        )
        st.plotly_chart(fig_pl, use_container_width=True)

    with _pl_cols[1]:
        fig_cum = go.Figure()
        fig_cum.add_trace(
            go.Scatter(
                x=_proj_df["Måned"],
                y=_proj_df["Kumulativt resultat"],
                mode="lines",
                name="Kumulativt resultat",
                line=dict(color="#3498db", width=2.5),
                fill="tozeroy",
            )
        )
        if _be_month:
            fig_cum.add_vline(
                x=_be_month,
                line_dash="dash",
                line_color="#27ae60",
                annotation_text=f"Break-even (mnd {_be_month})",
                annotation_position="top",
            )
        fig_cum.add_hline(y=0, line_dash="dash", line_color="gray")
        fig_cum.update_layout(
            title="Kumulativt resultat",
            xaxis_title="Måned",
            yaxis_title="kr",
            height=400,
            margin=dict(t=40, b=30),
            showlegend=False,
        )
        st.plotly_chart(fig_cum, use_container_width=True)

    # ── Stacked cost breakdown over time ──────────────────────
    st.subheader("Kostnadsfordeling over tid")
    fig_stack = go.Figure()
    fig_stack.add_trace(
        go.Scatter(
            x=_proj_df["Måned"],
            y=_proj_df["Varekostnad"],
            mode="lines",
            name="Varekostnad",
            stackgroup="costs",
            line=dict(color="#e74c3c"),
        )
    )
    fig_stack.add_trace(
        go.Scatter(
            x=_proj_df["Måned"],
            y=_proj_df["Driftskostnader"],
            mode="lines",
            name="Driftskostnader",
            stackgroup="costs",
            line=dict(color="#f39c12"),
        )
    )
    fig_stack.add_trace(
        go.Scatter(
            x=_proj_df["Måned"],
            y=_proj_df["CAC-kostnad"],
            mode="lines",
            name="CAC-kostnad",
            stackgroup="costs",
            line=dict(color="#9b59b6"),
        )
    )
    fig_stack.add_trace(
        go.Scatter(
            x=_proj_df["Måned"],
            y=_proj_df["Omsetning eks. MVA"],
            mode="lines",
            name="Omsetning eks. MVA",
            line=dict(color="#27ae60", width=2.5, dash="dot"),
        )
    )
    fig_stack.update_layout(
        xaxis_title="Måned",
        yaxis_title="kr",
        height=400,
        margin=dict(t=40, b=30),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="center",
            x=0.5,
        ),
    )
    st.plotly_chart(fig_stack, use_container_width=True)

    # ── Year-end summary ──────────────────────────────────────
    st.subheader("Årssammendrag")
    _proj_df["År"] = ((_proj_df["Måned"] - 1) // 12) + 1
    _yearly = (
        _proj_df.groupby("År")
        .agg(
            {
                "Abonnenter": "last",
                "Nye": "sum",
                "Churnet": "sum",
                "Ordrer": "sum",
                "Omsetning inkl. MVA": "sum",
                "Omsetning eks. MVA": "sum",
                "Varekostnad": "sum",
                "Driftskostnader": "sum",
                "CAC-kostnad": "sum",
                "Nettoresultat": "sum",
                "Kumulativt resultat": "last",
            }
        )
        .reset_index()
    )
    _yearly_fmt = _yearly.copy()
    for _c in _yearly_fmt.columns:
        if _c != "År":
            _yearly_fmt[_c] = _yearly_fmt[_c].apply(lambda v: f"{v:,.0f}")
    st.dataframe(_yearly_fmt, use_container_width=True, hide_index=True)

    # ── Detailed monthly table ────────────────────────────────
    with st.expander("📊 Detaljert månedlig prognose"):
        _fmt_cols = [
            c
            for c in _proj_df.columns
            if c
            not in ("Måned", "Abonnenter", "Nye", "Churnet", "Ordrer", "År")
        ]
        _display_proj = _proj_df.drop(columns=["År"], errors="ignore").copy()
        for _c in _fmt_cols:
            _display_proj[_c] = _display_proj[_c].apply(
                lambda v: f"{v:,.0f}"
            )
        st.dataframe(
            _display_proj, use_container_width=True, hide_index=True
        )


with tab_health:
    _render_tab_health()


# ── Tab 5: Influencer modelling ─────────────────────────────────

def _render_tab_influencer():
    """Yearly revenue simulator for Yrja and influencer partners."""
    st.subheader("🤝 Influencer-samarbeid — årsmodellering")
    st.caption(
        "Juster parameterne for å simulere årlig inntjening for Yrja og for "
        "influenseren basert på unit economics-tallene fra resten av appen."
    )

    if len(df_sim) == 0:
        st.warning(
            "Ingen produkter med komplett data (Innpris + SLOT-kolonner) — "
            "kan ikke beregne unit economics."
        )
        return

    # ── Slider parameters ──────────────────────────────────────
    _subscription_totals = np.array(
        [subscription_total_prices[_lbl] for _lbl in SUBSCRIPTIONS],
        dtype=float,
    )
    _inf_order_min = int(np.floor(_subscription_totals.min()))
    _inf_order_max = int(np.ceil(_subscription_totals.max()))
    _inf_order_default = int(
        round(
            subscription_total_prices.get(
                "6 slots", float(_subscription_totals.mean())
            )
        )
    )
    _inf_order_default = min(
        max(_inf_order_default, _inf_order_min), _inf_order_max
    )
    _inf_reference = ", ".join(
        f"{int(round(subscription_total_prices[_lbl]))} = "
        f"{SUBSCRIPTIONS[_lbl]['slots']} slots"
        for _lbl in SUBSCRIPTIONS
    )
    _ic1, _ic2 = st.columns(2)

    with _ic1:
        _inf_count = st.slider(
            "Antall influensere",
            min_value=1,
            max_value=100,
            value=10,
            step=1,
            key="inf_count",
            help="Antall influensere Yrja samarbeider med",
        )
        _inf_customers = st.slider(
            "Snitt antall kunder per influenser",
            min_value=1,
            max_value=1000,
            value=50,
            step=1,
            key="inf_customers",
            help="Hvor mange kunder en gjennomsnittlig influenser tilfører",
        )
        _inf_order_size = st.slider(
            "Snitt ordrestørrelse (kr)",
            min_value=_inf_order_min,
            max_value=_inf_order_max,
            value=_inf_order_default,
            step=50,
            key="inf_order_size",
            help=(
                "Gjennomsnittlig ordreverdi inkl. MVA. "
                f"Referanse: {_inf_reference}"
            ),
        )

    with _ic2:
        _inf_monthly_buys = st.slider(
            "Snitt antall kjøp per måned",
            min_value=0.5,
            max_value=2.0,
            value=1.0,
            step=0.1,
            key="inf_monthly_buys",
            help="Hvor ofte en gjennomsnittskunde kjøper per måned",
        )
        _inf_orders_before_churn = st.slider(
            "Snitt antall ordre før churn",
            min_value=1,
            max_value=1000,
            value=12,
            step=1,
            key="inf_orders_churn",
            help="Hvor mange ordre en kunde gjør før de slutter (livstid)",
        )
        _inf_commission_pct = st.slider(
            "Influenser-kommisjon (% av ordreverdi)",
            min_value=5.0,
            max_value=10.0,
            value=7.5,
            step=0.1,
            format="%.1f",
            key="inf_commission_pct",
            help="Andel av ordreverdien som influenseren får",
        )

    # ── Derive box economics from order size ─────────────────────────
    # Map order size to effective slot count using linear interpolation
    # across the configured subscription price points.
    _slot_points = np.array(
        [SUBSCRIPTIONS[_lbl]["slots"] for _lbl in SUBSCRIPTIONS],
        dtype=float,
    )
    _price_points = np.array(
        [subscription_total_prices[_lbl] for _lbl in SUBSCRIPTIONS],
        dtype=float,
    )
    _sort = np.argsort(_price_points)
    _prices_sorted = _price_points[_sort]
    _slots_sorted = _slot_points[_sort]
    _unique_prices, _unique_idx = np.unique(_prices_sorted, return_index=True)
    _unique_slots = _slots_sorted[_unique_idx]
    if len(_unique_prices) == 1:
        _eff_slots = float(_unique_slots[0])
    else:
        _eff_slots = float(
            np.interp(_inf_order_size, _unique_prices, _unique_slots)
        )
    _box_cogs = avg_slot_cogs * _eff_slots
    _box_fpacks = avg_slot_fpacks * _eff_slots

    _bd = compute_cost_breakdown(
        float(_inf_order_size), _box_cogs, _box_fpacks
    )
    _profit_per_order_pre_commission = _bd["Driftsresultat"]
    _commission_per_order = _inf_order_size * _inf_commission_pct / 100
    _yrja_profit_per_order = (
        _profit_per_order_pre_commission - _commission_per_order
    )

    # ── Yearly aggregation ─────────────────────────────────────
    # Each customer makes min(monthly_buys * 12, orders_before_churn) orders
    # in their first year (capped by churn).
    _orders_per_customer_year = min(
        _inf_monthly_buys * 12, _inf_orders_before_churn
    )
    _total_customers = _inf_count * _inf_customers
    _total_orders_year = _total_customers * _orders_per_customer_year

    _yearly_gross_revenue = _total_orders_year * _inf_order_size
    _yearly_revenue_ex_mva = _total_orders_year * _bd["Omsetning eks. MVA"]
    _yearly_yrja_pre_commission = (
        _total_orders_year * _profit_per_order_pre_commission
    )
    _yearly_influencer_income = _total_orders_year * _commission_per_order
    _yearly_yrja_profit = (
        _yearly_yrja_pre_commission - _yearly_influencer_income
    )

    # Per-influencer income (gross commission)
    _influencer_income_each = (
        _yearly_influencer_income / _inf_count if _inf_count > 0 else 0.0
    )

    # ── Key metrics ─────────────────────────────────────────────
    st.divider()
    st.subheader("Årlige nøkkeltall")
    _m_cols = st.columns(4)
    _m_cols[0].metric(
        "Totalt antall kunder",
        f"{int(_total_customers):,}",
        help=f"{_inf_count} influensere × {_inf_customers} kunder",
    )
    _m_cols[1].metric(
        "Ordre per kunde / år",
        f"{_orders_per_customer_year:.1f}",
        help="min(månedlige kjøp × 12, ordre før churn)",
    )
    _m_cols[2].metric(
        "Totalt antall ordre / år",
        f"{int(round(_total_orders_year)):,}",
    )
    _m_cols[3].metric(
        "Brutto årsomsetning (inkl. MVA)",
        f"{_yearly_gross_revenue:,.0f} kr",
    )

    st.divider()
    st.subheader("Inntekt for Yrja vs. influensere")
    _r_cols = st.columns(3)
    _r_cols[0].metric(
        "Yrja — driftsresultat (etter influenser-kommisjon)",
        f"{_yearly_yrja_profit:,.0f} kr",
        delta=f"{_yearly_yrja_pre_commission - _yearly_yrja_profit:,.0f} kr utbetalt i kommisjon",
        delta_color="inverse",
    )
    _r_cols[1].metric(
        "Influensere — total kommisjon",
        f"{_yearly_influencer_income:,.0f} kr",
        delta=f"{_inf_commission_pct:.1f} % av ordreverdi",
        delta_color="off",
    )
    _r_cols[2].metric(
        "Inntekt per influenser",
        f"{_influencer_income_each:,.0f} kr",
        help=f"Årlig snittinntekt for hver av {_inf_count} influensere",
    )

    # ── Per-order economics breakdown ─────────────────────────────────
    st.divider()
    st.subheader("Unit economics per ordre")
    st.caption(
        f"Beregnet for en boks på {_inf_order_size:,} kr (≈ {_eff_slots:.1f} slots) "
        "med gjennomsnittlig varekostnad fra produktkatalogen."
    )

    _ue_rows = [
        ("Omsetning inkl. MVA", _bd["Omsetning inkl. MVA"], False),
        ("MVA (15 %)", _bd["MVA (15 %)"], True),
        ("Omsetning eks. MVA", _bd["Omsetning eks. MVA"], False),
        ("Varekostnad", _bd["Varekostnad"], True),
        ("Lager & distribusjon (fast)", _bd["Lager&dist fast"], True),
        ("Lager variabel (plukk)", _bd["Lager var. (plukk)"], True),
        ("Transaksjonsgebyrer", _bd["Transaksjonsgebyrer"], True),
        (
            "Driftsresultat (før kommisjon)",
            _profit_per_order_pre_commission,
            False,
        ),
        (
            f"Influenser-kommisjon ({_inf_commission_pct:.1f} %)",
            -_commission_per_order,
            True,
        ),
        ("Yrja netto per ordre", _yrja_profit_per_order, False),
    ]
    _ue_df = pd.DataFrame(
        [
            {"Post": _name, "Per ordre (kr)": f"{_v:,.0f}"}
            for _name, _v, _ in _ue_rows
        ]
    )
    _bold_rows = {
        "Driftsresultat (før kommisjon)",
        "Yrja netto per ordre",
        "Omsetning eks. MVA",
    }
    _cost_rows = {row[0] for row in _ue_rows if row[2]}
    _styled_ue = _ue_df.style.apply(
        lambda row: [
            ("font-weight: bold; " if row["Post"] in _bold_rows else "")
            + ("color: red; " if row["Post"] in _cost_rows else "")
            for _ in row
        ],
        axis=1,
    )
    _ue_cols = st.columns([3, 2])
    with _ue_cols[0]:
        st.dataframe(_styled_ue, use_container_width=True, hide_index=True)
    with _ue_cols[1]:
        # Stacked bar: revenue split into varekost / andre drift / commission / Yrja net
        _other_ops = (
            abs(_bd["Lager&dist fast"])
            + abs(_bd["Lager var. (plukk)"])
            + abs(_bd["Transaksjonsgebyrer"])
        )
        _split_fig = go.Figure()
        for _name, _val, _color in [
            ("Varekostnad", abs(_bd["Varekostnad"]), "#e74c3c"),
            ("Andre driftskostnader", _other_ops, "#f39c12"),
            ("Influenser-kommisjon", _commission_per_order, "#9b59b6"),
            ("Yrja netto", max(_yrja_profit_per_order, 0), "#27ae60"),
        ]:
            _split_fig.add_trace(
                go.Bar(
                    name=_name,
                    x=["Per ordre"],
                    y=[_val],
                    marker_color=_color,
                    text=[f"{int(round(_val)):,}"],
                    textposition="inside",
                    insidetextanchor="middle",
                )
            )
        _split_fig.update_layout(
            barmode="stack",
            showlegend=True,
            height=320,
            margin=dict(t=30, b=10, l=10, r=10),
            yaxis_title="kr",
            xaxis=dict(showticklabels=False),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="center",
                x=0.5,
            ),
        )
        st.plotly_chart(
            _split_fig, use_container_width=True, key="inf_split_chart"
        )

    # ── Yearly revenue chart ──────────────────────────────────────
    st.divider()
    st.subheader("Årlig fordeling")
    _rev_cols = st.columns(2)

    with _rev_cols[0]:
        _bar_fig = go.Figure()
        _bar_fig.add_trace(
            go.Bar(
                x=["Yrja", "Influensere (totalt)"],
                y=[_yearly_yrja_profit, _yearly_influencer_income],
                marker_color=["#27ae60", "#9b59b6"],
                text=[
                    f"{_yearly_yrja_profit:,.0f} kr",
                    f"{_yearly_influencer_income:,.0f} kr",
                ],
                textposition="outside",
            )
        )
        _bar_fig.update_layout(
            title="Årlig inntekt",
            yaxis_title="kr",
            height=400,
            margin=dict(t=40, b=30),
            showlegend=False,
        )
        st.plotly_chart(
            _bar_fig, use_container_width=True, key="inf_yearly_bar"
        )

    with _rev_cols[1]:
        # Sensitivity: yearly Yrja profit as a function of commission %
        _pct_range = np.linspace(5.0, 10.0, 26)
        _yrja_curve = []
        _inf_curve = []
        for _p in _pct_range:
            _c = _inf_order_size * _p / 100
            _yrja_curve.append(
                _total_orders_year
                * (_profit_per_order_pre_commission - _c)
            )
            _inf_curve.append(_total_orders_year * _c)

        _sens_fig = go.Figure()
        _sens_fig.add_trace(
            go.Scatter(
                x=_pct_range,
                y=_yrja_curve,
                mode="lines",
                name="Yrja netto",
                line=dict(color="#27ae60", width=2.5),
            )
        )
        _sens_fig.add_trace(
            go.Scatter(
                x=_pct_range,
                y=_inf_curve,
                mode="lines",
                name="Influensere totalt",
                line=dict(color="#9b59b6", width=2.5),
            )
        )
        _sens_fig.add_vline(
            x=_inf_commission_pct,
            line_dash="dash",
            line_color="gray",
            annotation_text=f"{_inf_commission_pct:.1f} %",
            annotation_position="top",
        )
        _sens_fig.update_layout(
            title="Årlig inntekt vs. kommisjonssats",
            xaxis_title="Kommisjon (%)",
            yaxis_title="kr",
            height=400,
            margin=dict(t=40, b=30),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="center",
                x=0.5,
            ),
        )
        st.plotly_chart(
            _sens_fig, use_container_width=True, key="inf_sensitivity"
        )

    # ── Summary table ───────────────────────────────────────────
    st.divider()
    _summary_rows = [
        {"Post": "Antall influensere", "Verdi": f"{_inf_count:,}"},
        {
            "Post": "Totalt antall kunder",
            "Verdi": f"{int(_total_customers):,}",
        },
        {
            "Post": "Ordre per kunde / år",
            "Verdi": f"{_orders_per_customer_year:.1f}",
        },
        {
            "Post": "Totalt antall ordre / år",
            "Verdi": f"{int(round(_total_orders_year)):,}",
        },
        {
            "Post": "Brutto årsomsetning (inkl. MVA)",
            "Verdi": f"{_yearly_gross_revenue:,.0f} kr",
        },
        {
            "Post": "Omsetning eks. MVA",
            "Verdi": f"{_yearly_revenue_ex_mva:,.0f} kr",
        },
        {
            "Post": "Yrja driftsresultat (før kommisjon)",
            "Verdi": f"{_yearly_yrja_pre_commission:,.0f} kr",
        },
        {
            "Post": "Influenser-kommisjon (totalt)",
            "Verdi": f"{_yearly_influencer_income:,.0f} kr",
        },
        {
            "Post": "Yrja netto driftsresultat (etter kommisjon)",
            "Verdi": f"{_yearly_yrja_profit:,.0f} kr",
        },
        {
            "Post": "Inntekt per influenser",
            "Verdi": f"{_influencer_income_each:,.0f} kr",
        },
    ]
    st.dataframe(
        pd.DataFrame(_summary_rows),
        use_container_width=True,
        hide_index=True,
    )


with tab_influencer:
    _render_tab_influencer()


# ── Tab 6: Ordrestatus

def _render_tab_orders():
    if not _shopify_available:
        st.warning("Shopify ikke konfigurert — kan ikke hente ordrer.")
        return

    st.subheader("Ordrestatus — solgt vs. kapasitet")
    st.caption(
        "Sammenligner antall bestilte enheter fra Shopify mot "
        "tilgjengelig kapasitet (FID per Kolli × Max kolli) fra Notion."
    )

    # ── Filters ────────────────────────────────────────────────
    _o_col1, _o_col2, _o_col3 = st.columns(3)
    with _o_col1:
        _o_from = st.date_input(
            "Fra dato",
            value=date.today() - timedelta(days=30),
            key="order_from",
        )
    with _o_col2:
        _o_to = st.date_input("Til dato", value=date.today(), key="order_to")
    with _o_col3:
        _o_limit = st.number_input(
            "Maks antall ordrer", value=500, min_value=10, step=50, key="order_limit",
        )

    # Build Shopify query filter from date range
    _o_query_parts: list[str] = ["tag_not:Test"]
    if _o_from:
        _o_query_parts.append(f"created_at:>={_o_from.isoformat()}")
    if _o_to:
        _o_query_parts.append(f"created_at:<={_o_to.isoformat()}")
    _o_query = " ".join(_o_query_parts)

    # ── Fetch orders ──────────────────────────────────────────
    _raw_orders = fetch_shopify_orders(query=_o_query, limit=_o_limit)

    if not _raw_orders:
        st.info("Ingen ordrer funnet for valgt periode.")
        return

    # ── Build Shopify title → Notion Produktnavn mapping ──────
    _shopify_meta = fetch_shopify_metafields()
    _vid_to_notion: dict[str, str] = {}
    for _, _row in df.iterrows():
        _vid = _row.get("Shopify Variant ID")
        if pd.notna(_vid):
            _vid_to_notion[str(int(_vid))] = _row["Produktnavn"]

    _title_to_notion: dict[str, str] = {}
    for _vid, _entry in _shopify_meta.items():
        _notion_name = _vid_to_notion.get(_vid)
        if _notion_name:
            _title_to_notion[_entry["shopify_title"]] = _notion_name

    # ── Aggregate line items by product ───────────────────────
    # Bundle products (Råvareboks/Yrjaboks) store the actual picks in
    # customAttributes with keys like "_pvgid://shopify/ProductVariant/ID".
    _BUNDLE_TITLES = {"Råvareboks", "Yrjaboks"}
    _EXCLUDED_TITLES = {"Testvare"}
    _PVGID_PREFIX = "_pvgid://shopify/ProductVariant/"

    _product_qty: dict[str, int] = {}
    for _order in _raw_orders:
        for _li in _order["line_items"]:
            if _li["title"] in _EXCLUDED_TITLES:
                continue

            attrs = _li.get("custom_attributes") or {}

            if _li["title"] in _BUNDLE_TITLES:
                # Parse bundle custom attributes for variant-level picks
                for _attr_key, _attr_val in attrs.items():
                    if _attr_key.startswith(_PVGID_PREFIX):
                        _vid = _attr_key[len(_PVGID_PREFIX):]
                        _qty = int(_attr_val) if _attr_val.isdigit() else 1
                        _notion_name = _vid_to_notion.get(_vid, f"Variant {_vid}")
                        _product_qty[_notion_name] = _product_qty.get(_notion_name, 0) + _qty
            else:
                # Regular line item (non-bundle)
                _mapped = _title_to_notion.get(_li["title"], _li["title"])
                _product_qty[_mapped] = _product_qty.get(_mapped, 0) + _li["quantity"]

    # ── Notion lookups (SLOT: antall enheter = antall SKU per slot) ──
    _sku_per_slot_map: dict[str, int] = {}
    for _, _row in df.iterrows():
        _sku = _row.get("SLOT: antall enheter")
        if pd.notna(_sku) and _sku > 0:
            _sku_per_slot_map[_row["Produktnavn"]] = int(_sku)

    # ── Build purchase-order DataFrame ──────────────────────
    _po_rows: list[dict] = []
    for _prod, _qty in _product_qty.items():
        _sku_per_slot = _sku_per_slot_map.get(_prod)
        _total_sku = _qty * _sku_per_slot if _sku_per_slot else None
        _match = df[df["Produktnavn"] == _prod]
        if len(_match) > 0:
            _r = _match.iloc[0]
            _po_rows.append({
                "Produsent": _r["Produsent"],
                "Produktnavn": _prod,
                "SKU Name": _r.get("SKU Name", ""),
                "Antall r_pakker bestilt": _qty,
                "Antall SKU per slot": _sku_per_slot,
                "Antall SKU bestilt": _total_sku,
            })
        else:
            _po_rows.append({
                "Produsent": "",
                "Produktnavn": _prod,
                "SKU Name": "",
                "Antall r_pakker bestilt": _qty,
                "Antall SKU per slot": _sku_per_slot,
                "Antall SKU bestilt": _total_sku,
            })
    _po_df = pd.DataFrame(_po_rows)
    # Sort by Produsent (blank/missing goes last); keep the column order as
    # declared above.
    if len(_po_df) > 0:
        _po_df = (
            _po_df.assign(
                _prod_sort=_po_df["Produsent"].map(
                    lambda v: (1, "") if (v == "" or pd.isna(v)) else (0, v)
                )
            )
            .sort_values(by=["_prod_sort", "Produktnavn"])
            .drop(columns="_prod_sort")
            .reset_index(drop=True)
        )

    # ── Join with product table (FID per Kolli, Max kolli) ────
    _capacity_map: dict[str, float] = {}
    for _, _row in df.iterrows():
        _fid = _row.get("FID per Kolli")
        _kolli = _row.get("Max kolli")
        if pd.notna(_fid) and pd.notna(_kolli) and _fid > 0 and _kolli > 0:
            _capacity_map[_row["Produktnavn"]] = _fid * _kolli

    _status_rows: list[dict] = []
    for _prod, _qty in sorted(_product_qty.items(), key=lambda x: -x[1]):
        _cap = _capacity_map.get(_prod)
        _sku_per_slot = _sku_per_slot_map.get(_prod)
        _total_sku = _qty * _sku_per_slot if _sku_per_slot else None
        # Utilization is SKU-ordered vs SKU-capacity so the colours and
        # thresholds reflect the actual warehouse-level sell-through.
        if _total_sku is not None and _cap:
            _pct = _total_sku / _cap * 100
        else:
            _pct = None
        if _pct is not None:
            if _pct >= 100:
                _status = "🔴 Utsolgt"
            elif _pct >= 80:
                _status = "🟡 Snart utsolgt"
            else:
                _status = "🟢 På lager"
        else:
            _status = "⚪ Ukjent kapasitet"
        _status_rows.append({
            "Produkt": _prod,
            "Antall slots bestilt": _qty,
            "Antall SKU per slot": _sku_per_slot,
            "Totalt SKU bestilt": _total_sku,
            "Kapasitet (SKU)": int(_cap) if _cap else None,
            "Utnyttelse (%)": round(_pct, 1) if _pct is not None else None,
            "Status": _status,
        })

    _status_df = pd.DataFrame(_status_rows)

    # ── Key metrics ───────────────────────────────────────────
    _total_orders = len(_raw_orders)
    _total_units = sum(_product_qty.values())
    _matched = sum(1 for r in _status_rows if r["Kapasitet (SKU)"] is not None)
    _sold_out = sum(1 for r in _status_rows if r["Status"] == "🔴 Utsolgt")

    _m_cols = st.columns(4)
    _m_cols[0].metric("Ordrer", f"{_total_orders:,}")
    _m_cols[1].metric("Enheter bestilt", f"{_total_units:,}")
    _m_cols[2].metric("Produkter matchet", f"{_matched} / {len(_status_rows)}")
    _m_cols[3].metric("Utsolgt", f"{_sold_out}")

    # ── Download purchase order as XLSX ────────────────────────
    if len(_po_df) > 0:
        _xlsx_buf = io.BytesIO()
        _po_df.to_excel(_xlsx_buf, index=False, engine="openpyxl")
        _xlsx_buf.seek(0)
        st.download_button(
            label="📥 Generer innkjøpsordre (Shopify)",
            data=_xlsx_buf,
            file_name="innkjopsordre_shopify.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="order_dl_shopify_xlsx",
        )

    # ── Table ─────────────────────────────────────────────────
    st.dataframe(_status_df, use_container_width=True, hide_index=True)

    # ── Bar chart: sell-through per product ────────────────────
    _chart_df = _status_df.dropna(subset=["Utnyttelse (%)"]).sort_values(
        "Utnyttelse (%)", ascending=True
    )
    if len(_chart_df) > 0:
        _bar_colors = [
            "#e74c3c" if p >= 100 else "#f39c12" if p >= 80 else "#27ae60"
            for p in _chart_df["Utnyttelse (%)"]
        ]
        _fig_bar = go.Figure()
        _fig_bar.add_trace(
            go.Bar(
                y=_chart_df["Produkt"],
                x=_chart_df["Utnyttelse (%)"],
                orientation="h",
                marker_color=_bar_colors,
                text=_chart_df["Utnyttelse (%)"].apply(lambda v: f"{v:.0f}%"),
                textposition="outside",
            )
        )
        _fig_bar.add_vline(x=100, line_dash="dash", line_color="red")
        _fig_bar.update_layout(
            title="Utnyttelse per produkt (Totalt SKU bestilt / Kapasitet SKU)",
            xaxis_title="Utnyttelse (%)",
            height=max(400, len(_chart_df) * 28),
            margin=dict(t=40, b=30, l=200),
            showlegend=False,
        )
        st.plotly_chart(_fig_bar, use_container_width=True)

    # ── Unmatched products ───────────────────────────────
    _unmatched = _status_df[_status_df["Kapasitet (SKU)"].isna()]
    if len(_unmatched) > 0:
        with st.expander(f"⚠️ {len(_unmatched)} produkter uten kapasitetsdata"):
            st.dataframe(
                _unmatched[["Produkt", "Antall slots bestilt"]],
                use_container_width=True,
                hide_index=True,
            )
            st.caption(
                "Disse produktene fra Shopify-ordrer ble ikke matchet mot "
                "Notion-produkter med FID per Kolli / Max kolli."
            )

    # ── Purchase-order table (Shopify) ──────────────────────────
    if len(_po_df) > 0:
        st.subheader("Innkjøpsordre — Shopify")
        st.caption(
            "Inventarbehov fra Shopify-ordrer (engangskjøp og abonnementsfaktureringer "
            "som har gått gjennom Shopify-kassen)."
        )
        st.dataframe(_po_df, use_container_width=True, hide_index=True)

    # ── Skio purchase-order table ───────────────────────────────
    st.divider()
    st.subheader("Innkjøpsordre — Skio")
    st.caption(
        "Inventarbehov fra Skio-abonnementsordrer i samme periode. "
        "Skilles fra Shopify-tabellen så innkjøp kan planlegges per kanal."
    )

    if not _skio_available:
        st.info(
            "⚠️ Skio ikke konfigurert — sett `SKIO_API_TOKEN` i `.env` for å hente "
            "abonnementsordrer fra Skio."
        )
    else:
        _skio_raw_orders = fetch_skio_orders(
            from_iso=_o_from.isoformat() if _o_from else "",
            to_iso=_o_to.isoformat() if _o_to else "",
            limit=int(_o_limit),
        )

        if not _skio_raw_orders:
            st.info("Ingen Skio-ordrer funnet for valgt periode.")
        else:
            # Aggregate Skio line items into a Notion-product → qty dict.
            # Bundle detection prefers explicit `_pvgid://` attrs (works even
            # when product titles vary), falling back to the bundle-title set.
            _skio_product_qty: dict[str, int] = {}
            for _order in _skio_raw_orders:
                for _li in _order["line_items"]:
                    _prod_title = (
                        _li.get("product_title")
                        or _li.get("title")
                        or ""
                    )
                    if _prod_title in _EXCLUDED_TITLES:
                        continue

                    _attrs = _li.get("custom_attributes") or {}
                    _has_pvgid = any(
                        str(k).startswith(_PVGID_PREFIX) for k in _attrs
                    )
                    _is_bundle = (
                        _prod_title in _BUNDLE_TITLES or _has_pvgid
                    )

                    if _is_bundle:
                        for _attr_key, _attr_val in _attrs.items():
                            if not str(_attr_key).startswith(_PVGID_PREFIX):
                                continue
                            _vid = str(_attr_key)[len(_PVGID_PREFIX):]
                            _qty = (
                                int(_attr_val)
                                if str(_attr_val).isdigit()
                                else 1
                            )
                            _notion_name = _vid_to_notion.get(
                                _vid, f"Variant {_vid}"
                            )
                            _skio_product_qty[_notion_name] = (
                                _skio_product_qty.get(_notion_name, 0) + _qty
                            )
                    else:
                        # Regular line item: match by variant ID first, then by
                        # Shopify product title (via _title_to_notion).
                        _li_vid = _li.get("variant_id")
                        _notion_name = None
                        if _li_vid:
                            _notion_name = _vid_to_notion.get(str(_li_vid))
                        if not _notion_name:
                            _notion_name = _title_to_notion.get(
                                _prod_title, _prod_title
                            )
                        _skio_product_qty[_notion_name] = (
                            _skio_product_qty.get(_notion_name, 0)
                            + int(_li.get("quantity") or 0)
                        )

            # Build the Skio purchase-order DataFrame (same columns as Shopify).
            _po_rows_skio: list[dict] = []
            for _prod, _qty in _skio_product_qty.items():
                _sku_per_slot = _sku_per_slot_map.get(_prod)
                _total_sku = _qty * _sku_per_slot if _sku_per_slot else None
                _match = df[df["Produktnavn"] == _prod]
                if len(_match) > 0:
                    _r = _match.iloc[0]
                    _po_rows_skio.append({
                        "Produsent": _r["Produsent"],
                        "Produktnavn": _prod,
                        "SKU Name": _r.get("SKU Name", ""),
                        "Antall r_pakker bestilt": _qty,
                        "Antall SKU per slot": _sku_per_slot,
                        "Antall SKU bestilt": _total_sku,
                    })
                else:
                    _po_rows_skio.append({
                        "Produsent": "",
                        "Produktnavn": _prod,
                        "SKU Name": "",
                        "Antall r_pakker bestilt": _qty,
                        "Antall SKU per slot": _sku_per_slot,
                        "Antall SKU bestilt": _total_sku,
                    })
            _po_df_skio = pd.DataFrame(_po_rows_skio)
            if len(_po_df_skio) > 0:
                _po_df_skio = (
                    _po_df_skio.assign(
                        _prod_sort=_po_df_skio["Produsent"].map(
                            lambda v: (1, "") if (v == "" or pd.isna(v)) else (0, v)
                        )
                    )
                    .sort_values(by=["_prod_sort", "Produktnavn"])
                    .drop(columns="_prod_sort")
                    .reset_index(drop=True)
                )

            # Skio metrics row.
            _skio_total_orders = len(_skio_raw_orders)
            _skio_total_units = sum(_skio_product_qty.values())
            _sm_cols = st.columns(2)
            _sm_cols[0].metric("Skio-ordrer", f"{_skio_total_orders:,}")
            _sm_cols[1].metric("Enheter bestilt (Skio)", f"{_skio_total_units:,}")

            if len(_po_df_skio) > 0:
                _xlsx_skio = io.BytesIO()
                _po_df_skio.to_excel(_xlsx_skio, index=False, engine="openpyxl")
                _xlsx_skio.seek(0)
                st.download_button(
                    label="📥 Generer innkjøpsordre (Skio)",
                    data=_xlsx_skio,
                    file_name="innkjopsordre_skio.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="order_dl_skio_xlsx",
                )
                st.dataframe(
                    _po_df_skio, use_container_width=True, hide_index=True,
                )


with tab_orders:
    _render_tab_orders()


# ── Tab 6: Fulfillment ───────────────────────────────────────────────────


@st.cache_data(ttl=300, show_spinner="Henter ordrer fra Shopify …")
def _fulfillment_load_orders(
    status_filter: str,
    since_date: str,
    until_date: str,
    exclude_tags_csv: str,
) -> list[dict]:
    """Fetch raw Shopify orders (typed-model shape) for the fulfillment tab.

    ``exclude_tags_csv`` is a comma-separated list of Shopify order tags that
    will be passed through the ``tag_not:`` filter (e.g. ``"Test"`` skips test
    orders like #1027 that shouldn't hit the warehouse).
    """
    shop = os.environ.get("SHOPIFY_SHOP_DOMAIN", "")
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    if not shop or not token:
        return []
    exclude_tags = [t.strip() for t in exclude_tags_csv.split(",") if t.strip()]
    client = FulfillmentShopifyClient(shop, token)
    try:
        qf = build_query_filter(
            status=status_filter if status_filter != "any" else None,
            since=since_date,
            until=until_date,
            exclude_tags=exclude_tags,
        )
        return client.fetch_orders(qf)
    finally:
        client.close()


@st.cache_data(ttl=600, show_spinner="Henter variant-metafelter fra Shopify …")
def _fulfillment_load_variant_metafields(
    variant_ids_csv: str,
) -> dict[str, dict]:
    """Fetch custom metafields for variants (CSV string used as cache key)."""
    if not variant_ids_csv:
        return {}
    shop = os.environ.get("SHOPIFY_SHOP_DOMAIN", "")
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    if not shop or not token:
        return {}
    variant_ids = variant_ids_csv.split(",")
    client = FulfillmentShopifyClient(shop, token)
    try:
        lookup = client.fetch_variant_metafields(variant_ids)
        return {
            vid: {
                "variant_id": vm.variant_id,
                "display_name": vm.display_name,
                "metafields": vm.metafields,
            }
            for vid, vm in lookup.items()
        }
    finally:
        client.close()


def _render_tab_fulfillment():
    if not _shopify_available:
        st.warning(
            "Shopify ikke konfigurert — sett SHOPIFY_SHOP_DOMAIN og "
            "SHOPIFY_ACCESS_TOKEN i .env."
        )
        return

    st.subheader("📦 Ordreeksport & Fulfillment")
    st.caption(
        "Henter Shopify-ordrer, genererer en plukkliste og produserer "
        "ordreetiketter (PDF) + CSV/Excel-eksport."
    )

    # ── Filters (in-page, so sidebar stays reserved for pricing) ──
    _fc1, _fc2, _fc3 = st.columns([1, 1, 1])
    with _fc1:
        _status = st.selectbox(
            "Fulfillment-status",
            ["unfulfilled", "partial", "fulfilled", "any"],
            index=0,
            key="ff_status",
        )
    with _fc2:
        _since = st.date_input(
            "Fra",
            value=date.today() - timedelta(days=30),
            key="ff_since",
        )
    with _fc3:
        _until = st.date_input("Til", value=date.today(), key="ff_until")

    _oc1, _oc2, _oc3 = st.columns([1, 1, 1])
    with _oc1:
        _explode = st.toggle(
            "Eksplodér bokser (plukkliste)", value=True, key="ff_explode"
        )
    with _oc2:
        _show_raw = st.toggle("Vis rådata", value=False, key="ff_show_raw")
    with _oc3:
        _include_internal = (
            st.toggle(
                "Inkluder __shopify-felt",
                value=False,
                key="ff_include_internal",
            )
            if _show_raw
            else False
        )

    _bc1, _bc2, _bc3 = st.columns([2, 2, 1])
    with _bc1:
        _batch_id = st.text_input(
            "Batch ID",
            placeholder="f.eks. 2026-W12",
            help=(
                "Filter/tag-grupper for fulfillment-kørsler. "
                "Plassholder — batch-tildeling er ikke koblet på ennå."
            ),
            key="ff_batch_id",
        )
    with _bc2:
        _exclude_tags = st.text_input(
            "Ekskluder tags",
            value="Test",
            placeholder="Test, Intern, …",
            help=(
                "Komma-separert liste med Shopify-ordretags som skal "
                "utelukkes (bruker `tag_not:` i Shopify-query). "
                "Standardverdien `Test` holder testordrer (f.eks. #1027) "
                "ute av plukklisten."
            ),
            key="ff_exclude_tags",
        )
    with _bc3:
        st.write("")  # vertical spacer to align the button with the input
        if st.button(
            "🔄 Hent ordrer",
            type="primary",
            use_container_width=True,
            key="ff_refresh",
        ):
            _fulfillment_load_orders.clear()
            _fulfillment_load_variant_metafields.clear()

    # ── Fetch orders ───────────────────────────────────────────
    _raw = _fulfillment_load_orders(
        _status, str(_since), str(_until), _exclude_tags
    )
    _orders = [FulfillmentOrder.from_graphql(o) for o in _raw]

    if not _orders:
        st.info("Ingen ordrer funnet for valgt filter.")
        return

    # ── Variant metafields for bundle enrichment ──────────────
    _variant_ids = collect_variant_ids(_orders)
    _raw_meta = _fulfillment_load_variant_metafields(",".join(_variant_ids))
    _variant_lookup: dict[str, VariantMetadata] = {
        vid: VariantMetadata(**data) for vid, data in _raw_meta.items()
    }

    # ── Build dataframe ────────────────────────────────────────
    if _explode:
        _df = flatten_orders_exploded(_orders, variant_lookup=_variant_lookup)
    else:
        _df = flatten_orders(
            _orders, include_shopify_internal=_include_internal
        )

    # ── Batch filtering (placeholder) ──────────────────────────
    if _batch_id:
        st.caption(f"Batch-filter aktivt: **{_batch_id}** (plassholder)")
        # TODO: filter _df by _batch_id when batch-assignment logic exists

    # ── Display ────────────────────────────────────────────────
    _m1, _m2, _m3 = st.columns(3)
    _m1.metric("Ordrer", len(_orders))
    _m2.metric("Rader", len(_df))
    _m3.metric(
        "Varianter m/ metafelter", f"{len(_variant_lookup)} / {len(_variant_ids)}"
    )

    st.subheader(f"{len(_df)} rader fra {len(_orders)} ordrer")

    if _explode and "order_number" in _df.columns:
        # Alternating background colors per order for easier scanning
        _order_ids = _df["order_number"].unique()
        _color_map = {oid: i % 2 for i, oid in enumerate(_order_ids)}

        def _highlight_orders(row):
            c = _color_map.get(row["order_number"], 0)
            bg = "background-color: #f0f2f6" if c == 1 else ""
            return [bg] * len(row)

        _styled = _df.style.apply(_highlight_orders, axis=1)
        st.dataframe(_styled, use_container_width=True, hide_index=True)
    else:
        st.dataframe(_df, use_container_width=True, hide_index=True)

    if _show_raw:
        _raw_df = flatten_orders(
            _orders, include_shopify_internal=_include_internal
        )
        with st.expander("Rå ordredata", expanded=True):
            st.dataframe(_raw_df, use_container_width=True, hide_index=True)

    # ── Export ────────────────────────────────────────────────
    st.divider()
    _col_pdf, _col_csv, _col_xlsx = st.columns(3)

    with _col_pdf:
        _pdf_bytes = generate_fulfillment_pdf(
            _orders, variant_lookup=_variant_lookup
        )
        st.download_button(
            "📦 Last ned PDF (ordreetiketter)",
            data=_pdf_bytes,
            file_name="ordreetiketter.pdf",
            mime="application/pdf",
            use_container_width=True,
            type="primary",
            key="ff_dl_pdf",
        )

    with _col_csv:
        _csv_data = _df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Last ned CSV",
            data=_csv_data,
            file_name="orders.csv",
            mime="text/csv",
            use_container_width=True,
            key="ff_dl_csv",
        )

    with _col_xlsx:
        _xlsx_buf = io.BytesIO()
        _df.to_excel(_xlsx_buf, index=False, engine="openpyxl")
        st.download_button(
            "⬇️ Last ned Excel",
            data=_xlsx_buf.getvalue(),
            file_name="orders.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key="ff_dl_xlsx",
        )


with tab_fulfillment:
    _render_tab_fulfillment()
