"""Generate delivery fulfillment PDF labels.

Each order gets a label with:
- OrdreID (top right) + incremental counter
- Full shipping address
- Items hierarchy: parent bundle → sub-products with SKU name and number_of_SKUs
"""

from __future__ import annotations

from io import BytesIO

from fpdf import FPDF

from .models import Order, VariantMetadata


class FulfillmentPDF(FPDF):
    """Custom PDF for Yrja delivery labels."""

    def __init__(self) -> None:
        super().__init__(orientation="P", unit="mm", format=(105, 148))
        self.set_auto_page_break(auto=True, margin=8)


def generate_fulfillment_pdf(
    orders: list[Order],
    variant_lookup: dict[str, VariantMetadata] | None = None,
) -> bytes:
    """Generate a PDF with one label per order.

    Returns the PDF as bytes.
    """
    import re

    lookup = variant_lookup or {}
    pdf = FulfillmentPDF()
    pdf.set_font("Helvetica")

    for idx, order in enumerate(orders, 1):
        has_bundle = any(li.is_bundle for li in order.line_items)

        pdf.add_page()

        # ── OrdreID (top right) ──────────────────────────────
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 6, f"Ordre {idx}", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, order.name, align="R", new_x="LMARGIN", new_y="NEXT")

        pdf.ln(2)

        # ── Delivery address ───────────────────────────────
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Leveringsadresse", new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "", 8)
        addr = order.shipping_address
        if addr.name:
            pdf.cell(0, 4, addr.name, new_x="LMARGIN", new_y="NEXT")
        if addr.address1:
            pdf.cell(0, 4, addr.address1, new_x="LMARGIN", new_y="NEXT")
        if addr.address2:
            pdf.cell(0, 4, addr.address2, new_x="LMARGIN", new_y="NEXT")
        zip_city = f"{addr.zip} {addr.city}".strip()
        if zip_city:
            pdf.cell(0, 4, zip_city, new_x="LMARGIN", new_y="NEXT")
        if addr.phone:
            pdf.cell(0, 4, f"Tlf: {addr.phone}", new_x="LMARGIN", new_y="NEXT")

        pdf.ln(3)

        # ── Items ────────────────────────────────────────
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Innhold", new_x="LMARGIN", new_y="NEXT")

        item_num = 0
        for li in order.line_items:
            if not li.is_bundle:
                if has_bundle:
                    continue  # skip duplicates
                item_num += 1
                pdf.set_font("Helvetica", "", 8)
                pdf.cell(0, 4, f"  {item_num}. {li.name}", new_x="LMARGIN", new_y="NEXT")
                continue

            # Parent bundle
            item_num += 1
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(
                0, 5,
                f"  {item_num}. {li.name}",
                new_x="LMARGIN", new_y="NEXT",
            )

            # Sub-products
            produkt_map: dict[int, str] = {}
            for attr in li.custom_attributes:
                pm = re.match(r"^Produkt (\d+)$", attr.key)
                if pm:
                    produkt_map[int(pm.group(1))] = attr.value

            sub_idx = 0
            sub_letter = ord("a")
            for attr in li.custom_attributes:
                if not attr.is_pvgid:
                    continue
                sub_idx += 1
                variant_id = attr.variant_id or ""
                qty = attr.quantity

                variant_meta = lookup.get(variant_id)
                sku_name = variant_meta.sku_name if variant_meta else produkt_map.get(sub_idx, variant_id)

                f_packs_per_unit = 0
                if variant_meta and variant_meta.slot_antall_enheter > 0:
                    f_packs_per_unit = variant_meta.slot_antall_enheter
                number_of_skus = qty * f_packs_per_unit if f_packs_per_unit else ""

                letter = chr(sub_letter + sub_idx - 1)

                pdf.set_font("Helvetica", "", 8)
                sku_line = f"      {item_num}{letter}. {sku_name}"
                if number_of_skus:
                    sku_line += f" - {number_of_skus} stk"
                pdf.cell(0, 4, sku_line, new_x="LMARGIN", new_y="NEXT")

        # ── Order note ───────────────────────────────────────
        if order.note:
            pdf.ln(2)
            pdf.set_font("Helvetica", "I", 7)
            pdf.multi_cell(0, 4, f"Notat: {order.note}")

    buf = BytesIO()
    pdf.output(buf)
    return buf.getvalue()
