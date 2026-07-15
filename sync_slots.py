"""Sync optimizer results to Notion SLOT columns.

Fetches product data from the Prisoversikt database, runs the slot value
optimizer, and writes results (SLOT: antall enheter, SLOT: f-pack kg,
SLOT: tot slot vekt) back to Notion for all Shopify-public products.

Usage:
    # One-shot: run once and exit
    uv run python sync_slots.py

    # Watch mode: poll every 30s and re-run on changes
    uv run python sync_slots.py --watch

    # Custom poll interval (seconds)
    uv run python sync_slots.py --watch --interval 10
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from notion_sync import fetch_rows_from_notion, write_slot_results, NotionRow
from optimize_weights import solve


def _apply_forced_units(results: dict, rows: list[NotionRow]) -> None:
    """Override n_units with Espen's values for products with Fri fpack vekt = 0."""
    for r in rows:
        p = r.product
        if p.espen_n_units is not None and p.name in results:
            slot = results[p.name]
            slot["n_units"] = p.espen_n_units
            slot["total_weight"] = p.espen_n_units * p.f_pack_weight_kg


def _fingerprint(rows: list[NotionRow]) -> str:
    """Hash the pricing/weight fields that affect optimization.

    If this changes, the optimizer needs to re-run.
    """
    data = []
    for r in rows:
        p = r.product
        data.append({
            "name": p.name,
            "utpris": p.retail_price_per_kg,
            "f_pack": p.f_pack_weight_kg,
            "shopify": p.shopify_visible,
            "adjustable": p.adjustable_size,
            "espen_n_units": p.espen_n_units,
        })
    data.sort(key=lambda d: d["name"])
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()[:16]


def run_sync(quiet: bool = False) -> bool:
    """Fetch → optimize → write back. Returns True if successful."""
    if not quiet:
        print("📥  Fetching products from Notion...")
    rows = fetch_rows_from_notion()
    # Only sync products marked "Shopify public = Ja"
    shopify_rows = [r for r in rows if r.product.shopify_visible]
    shopify_products = [r.product for r in shopify_rows]

    if not shopify_products:
        print("⚠️  No Shopify-public products found.")
        return False

    if not quiet:
        print(f"    {len(rows)} products fetched, {len(shopify_products)} Shopify-public")

    if not quiet:
        print("⚙️  Running optimizer...")
    output = solve(shopify_products)
    if output is None:
        print("❌  Optimizer failed to find optimal solution.")
        return False

    if not quiet:
        print(f"    Target: {output.target:.2f} kr | Spread: {output.spread:.2f} kr")

    # Build results dict for write-back
    results = {
        name: {
            "n_units": s.n_units,
            "unit_weight": s.unit_weight,
            "total_weight": s.total_weight,
            "price_per_kg": s.price_per_kg,
        }
        for name, s in output.slots.items()
    }

    _apply_forced_units(results, shopify_rows)

    if not quiet:
        print("📤  Writing SLOT columns to Notion...")
    updated = write_slot_results(results, shopify_rows)
    if not quiet:
        print(f"    ✅ Updated {updated} products")

    return True


def watch(interval: int = 30) -> None:
    """Poll Notion and re-sync when product data changes."""
    print(f"👀  Watching for changes (polling every {interval}s). Ctrl+C to stop.\n")

    last_fingerprint: str | None = None

    while True:
        try:
            rows = fetch_rows_from_notion()
            fp = _fingerprint(rows)

            if fp != last_fingerprint:
                if last_fingerprint is not None:
                    print(f"\n🔄  Change detected (fingerprint {fp})")
                else:
                    print(f"🔄  Initial sync (fingerprint {fp})")

                # Only sync products marked "Shopify public = Ja"
                shopify_rows = [r for r in rows if r.product.shopify_visible]
                shopify_products = [r.product for r in shopify_rows]

                if not shopify_products:
                    print("⚠️  No Shopify-public products.")
                else:
                    output = solve(shopify_products)
                    if output is None:
                        print("❌  Optimizer failed.")
                    else:
                        results = {
                            name: {
                                "n_units": s.n_units,
                                "unit_weight": s.unit_weight,
                                "total_weight": s.total_weight,
                                "price_per_kg": s.price_per_kg,
                            }
                            for name, s in output.slots.items()
                        }
                        _apply_forced_units(results, shopify_rows)
                        updated = write_slot_results(results, shopify_rows)
                        print(
                            f"    ✅ {updated} products updated | "
                            f"Target: {output.target:.2f} kr | "
                            f"Spread: {output.spread:.2f} kr"
                        )

                last_fingerprint = fp

        except KeyboardInterrupt:
            print("\n👋  Stopped watching.")
            break
        except Exception as e:
            print(f"⚠️  Error: {e}")

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n👋  Stopped watching.")
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync optimizer results to Notion SLOT columns.")
    parser.add_argument("--watch", action="store_true", help="Poll for changes and re-sync automatically")
    parser.add_argument("--interval", type=int, default=30, help="Poll interval in seconds (default: 30)")
    args = parser.parse_args()

    if args.watch:
        watch(interval=args.interval)
    else:
        success = run_sync()
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
