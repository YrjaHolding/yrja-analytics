"""One-off Gordon order send for ad-hoc customers not present in Shopify.

This intentionally bypasses the Shopify pull because these orders don't exist
in Shopify yet (Skio integration is pending). The payload is hand-built from
the customer info + a per-customer article list.

Run dry-run first (prints JSON, no Gordon call):

    uv run python gordon_oneoff_20260526.py --dry-run

Real send:

    uv run python gordon_oneoff_20260526.py

Both default to the AWS-fronted staging host; override with --base-url.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from gordon_client import GordonAPIError, GordonClient

log = logging.getLogger("gordon_oneoff")

# ── Constants shared by both orders ──────────────────────────────────────

DELIVERY_DATE = "2026-05-26"
TIME_WINDOW = "16:00 - 22:00"
DELIVERY_GROUP = "Yrja"
INVENTORY_TYPE = "frozen"

# ── Per-customer article lists ───────────────────────────────────────────
# SKU names are the canonical "SKU name" (third column from the source
# table) — these are what Yrja's pickers see in Gordon Last Mile.
# Lines with qty 0 or blank in the source table are intentionally omitted.

OLE_ARTICLES: list[dict[str, Any]] = [
    {"name": "Elgkarbonade",         "quantity": 1},
    {"name": "Burger 2 pk storfe øko", "quantity": 1},
    {"name": "Høyrygg Storfe",       "quantity": 1},
    {"name": "Mørbrad Storfe øko",   "quantity": 1},
    {"name": "Indrefilet Storfe øko", "quantity": 2},
]

JORGEN_ARTICLES: list[dict[str, Any]] = [
    {"name": "Kyllingvinger",                  "quantity": 1},
    {"name": "Portions Club Pack 6x151 365",   "quantity": 1},  # was "Laks"
    {"name": "Burger 2 pk storfe øko",         "quantity": 1},
    {"name": "Mørbrad Storfe øko",             "quantity": 1},
    {"name": "Indrefilet Storfe øko",          "quantity": 1},
    {"name": "Kjøttdeig Storfe øko",           "quantity": 1},
    {"name": "Pancetta",                       "quantity": 1},
    {"name": "Bacon",                          "quantity": 1},
]


def _build(
    external_ref: str,
    customer_name: str,
    address: str,
    zip_code: str,
    city: str,
    email: str,
    mobile: str,
    articles: list[dict[str, Any]],
    inventory_name: str,
) -> dict[str, Any]:
    return {
        "external_ref": external_ref,
        "customer-name": customer_name,
        "address": address,
        "zip": zip_code,
        "city": city,
        "deliverydate": DELIVERY_DATE,
        "time-window": TIME_WINDOW,
        "email": email,
        "mobile": mobile,
        "country_code": "NO",
        "deliverygroup": DELIVERY_GROUP,
        "inventory": [
            {
                "name": inventory_name,
                "quantity": 1,
                "type": INVENTORY_TYPE,
                "articles": articles,
            }
        ],
    }


def build_payloads() -> list[dict[str, Any]]:
    return [
        _build(
            # Sequential after the last real Shopify-derived ref (10001054).
            external_ref="10001056",
            customer_name="Ole Sæter",
            address="Nobels Gate 21",
            zip_code="0268",
            city="Oslo",
            email="ole.saeter@hotmail.com",
            mobile="+4793000577",
            articles=OLE_ARTICLES,
            inventory_name="Råvareboks 6 ",
        ),
        _build(
            external_ref="10001057",
            customer_name="Jorgen Iversen",
            address="Parkveien 38A",
            zip_code="1405",
            city="Langhus",
            email="jorgen@reviohealth.com",
            mobile="+4741402740",
            articles=JORGEN_ARTICLES,
            inventory_name="Råvareboks 8 ",
        ),
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Send the 2026-05-26 ad-hoc Gordon orders.")
    p.add_argument(
        "--base-url",
        default="https://backend.aws.gordondelivery.com",
        help="Gordon base URL (defaults to the AWS-fronted staging host).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print payloads but make no network call to Gordon.",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    payloads = build_payloads()

    if args.dry_run:
        print(json.dumps(payloads, indent=2, ensure_ascii=False))
        return

    with GordonClient(
        base_url=args.base_url,
        delivery_group=DELIVERY_GROUP,
    ) as client:
        log.info("Sending %d ad-hoc orders to Gordon (%s)", len(payloads), client.base_url)
        try:
            resp = client.create_orders_bulk(payloads)
            log.info("Response: %r", resp)
        except GordonAPIError as e:
            log.error("Gordon API error %d: %r", e.status_code, e.body)
            sys.exit(1)


if __name__ == "__main__":
    main()
