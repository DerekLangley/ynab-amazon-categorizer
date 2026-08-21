"""Tests for storefront-aware Amazon page URLs."""

from ynab_amazon_categorizer.amazon_links import (
    order_details_url,
    orders_page_url,
    transactions_page_url,
)
from ynab_amazon_categorizer.memo_generator import MemoGenerator


def test_urls_follow_the_configured_storefront() -> None:
    """A .com link is useless to someone whose orders live on .ca."""
    assert orders_page_url("amazon.ca") == "https://www.amazon.ca/your-orders/orders"
    assert (
        transactions_page_url("amazon.co.uk")
        == "https://www.amazon.co.uk/cpe/yourpayments/transactions"
    )
    assert "amazon.de" in (order_details_url("amazon.de", "114-1234567-1234567") or "")


def test_order_details_url_needs_an_order_id() -> None:
    assert order_details_url("amazon.com", None) is None
    assert order_details_url("amazon.com", "") is None


def test_memo_links_use_the_same_builder() -> None:
    """One definition of the order-link format, shared by memos and prompts."""
    generator = MemoGenerator("amazon.co.uk")

    assert generator.generate_amazon_order_link(
        "114-1234567-1234567"
    ) == order_details_url("amazon.co.uk", "114-1234567-1234567")
