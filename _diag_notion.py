"""Diagnose why some Shopify-public products may be missing from the fetch."""

import os
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

headers = {
    "Authorization": f"Bearer {os.environ['NOTION_API_KEY']}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
DB = "30db4d1b-8dbf-80f9-b805-e9bcd12e6192"

# Get database schema
r = requests.get(f"https://api.notion.com/v1/databases/{DB}", headers=headers)
r.raise_for_status()
schema = r.json()
props = schema.get("properties", {})

print("=== Shopify public schema ===")
sp = props.get("Shopify public", {})
print("type:", sp.get("type"))
print("config:", sp.get(sp.get("type"), {}))
print()

# Query all rows
raw = []
cursor = None
while True:
    body = {} if not cursor else {"start_cursor": cursor}
    r = requests.post(
        f"https://api.notion.com/v1/databases/{DB}/query",
        headers=headers,
        json=body,
    )
    r.raise_for_status()
    d = r.json()
    raw.extend(d.get("results", []))
    if not d.get("has_more"):
        break
    cursor = d.get("next_cursor")

print(f"Total rows: {len(raw)}")
print()

print("=== Per-row diagnosis ===")
for row in raw:
    p = row.get("properties", {})
    name = "".join(
        t.get("plain_text", "") for t in p.get("Produktnavn", {}).get("title", [])
    ).strip()
    producer = "".join(
        t.get("plain_text", "") for t in p.get("Produsent", {}).get("rich_text", [])
    ).strip()
    utpris = p.get("Utpris", {}).get("number")
    enhetspris = p.get("Enhetspris", {}).get("number")

    sp_raw = p.get("Shopify public", {})
    sp_type = sp_raw.get("type")
    if sp_type == "select":
        sp_val = (sp_raw.get("select") or {}).get("name")
    elif sp_type == "checkbox":
        sp_val = sp_raw.get("checkbox")
    elif sp_type == "status":
        sp_val = (sp_raw.get("status") or {}).get("name")
    elif sp_type == "multi_select":
        sp_val = [o.get("name") for o in sp_raw.get("multi_select", [])]
    else:
        sp_val = f"<{sp_type}>"

    dropped = ""
    if not name or not producer:
        dropped = " [DROPPED: missing name/producer]"
    elif (utpris or enhetspris or 0) <= 0:
        dropped = " [DROPPED: missing utpris]"

    print(
        f"  shopify={sp_val!r:20s} utpris={utpris} enhetspris={enhetspris} "
        f"name={name!r:32s} producer={producer!r:15s}{dropped}"
    )
