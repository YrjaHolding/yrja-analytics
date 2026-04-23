"""Fulfillment utilities: Shopify order export, warehouse pick lists, and PDF labels.

Ported from yrja-fulfilment-analytics. These modules are kept isolated from the
existing analytics code (which has its own ShopifyClient tailored for order
status and variant metafield enrichment) because the fulfillment pipeline needs
typed order dataclasses with full shipping address + line-item custom attributes.
"""
