"""URLs for the Amazon pages this tool reads.

Kept in one place so every prompt, warning, and memo points at the user's
configured storefront (``AMAZON_DOMAIN``) rather than a hardcoded one — a
`.com` link is useless, and confusing, to someone whose orders live on `.ca`.
"""


def order_details_url(domain: str, order_id: str | None) -> str | None:
    """Link to one order's details page, or None without an order ID."""
    if not order_id:
        return None
    return (
        f"https://www.{domain}/gp/your-account/order-details?ie=UTF8&orderID={order_id}"
    )


def orders_page_url(domain: str) -> str:
    """Link to the orders list page."""
    return f"https://www.{domain}/your-orders/orders"


def transactions_page_url(domain: str) -> str:
    """Link to the payments/transactions page.

    This is the page that maps each card charge to its order, so it is what an
    unmatched transaction almost always needs.
    """
    return f"https://www.{domain}/cpe/yourpayments/transactions"
