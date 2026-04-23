"""GraphQL query strings for the Shopify Admin API (fulfillment pipeline)."""

ORDERS_QUERY = """
query Orders($first: Int!, $after: String, $query: String) {
  orders(first: $first, after: $after, query: $query, sortKey: CREATED_AT, reverse: true) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      node {
        id
        name
        createdAt
        displayFinancialStatus
        displayFulfillmentStatus
        note
        email
        totalPriceSet {
          shopMoney {
            amount
            currencyCode
          }
        }
        customAttributes {
          key
          value
        }
        shippingAddress {
          name
          address1
          address2
          city
          zip
          provinceCode
          countryCode
          phone
        }
        billingAddress {
          name
          address1
          address2
          city
          zip
          provinceCode
          countryCode
          phone
        }
        lineItems(first: 50) {
          edges {
            node {
              id
              name
              quantity
              sku
              customAttributes {
                key
                value
              }
              originalUnitPriceSet {
                shopMoney {
                  amount
                  currencyCode
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

VARIANT_METAFIELDS_QUERY = """
query VariantMetafields($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      displayName
      metafields(first: 25, namespace: "custom") {
        edges {
          node {
            key
            value
            type
          }
        }
      }
    }
  }
}
"""
