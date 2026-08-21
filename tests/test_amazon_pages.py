"""Tests for the payments and order-details pages, and page detection."""

from ynab_amazon_categorizer.amazon_parser import AmazonParser, detect_page_kind

TRANSACTIONS_PAGE = """
  Overview  Wallet  Transactions  Settings
Your Account > Your Payments > Transactions
Transactions
Completed
August 15, 2026
Prime Visa ****1234-$42.68
Order #114-1234567-1234567
AMZN Mktp US
Prime Visa ****1234-$115.22
Order #114-1234567-1234567
AMZN Mktp US
August 9, 2026
Amazon Gift Card-$15.61
Order #114-2345678-2345678
Amazon.com
Prime Visa ****1234+$19.36
Refund: Order #114-3456789-3456789
Amazon.com
"""

ORDER_DETAILS_PAGE = """
https://www.amazon.com/your-orders/order-details?orderID=114-1234567-1234567&ref=ppx
Your Account>Your Orders>Order Details
Order Details
Order placed August 13, 2026  Order # 114-1234567-1234567
Ship to
Sample Recipient
Order Summary
Item(s) Subtotal:
$46.87
Shipping & Handling:
$0.00
Total before tax:
$46.87
Estimated tax to be collected:
$3.86
Grand Total:
$50.73
Delivered August 16
Your package was left near the front door or porch.
Caribou Coffee Caribou Blend, Keurig Single-Serve K-Cup Pods, Medium Roast, 32 Count
Caribou Coffee Caribou Blend, Keurig Single-Serve K-Cup Pods, Medium Roast, 32 Count
Sold by: Amazon.com
Return items: Eligible through September 15, 2026
$19.99
Buy it again
 Amazon Basics Mesh Desk Organizer with Pen Holder and Office Caddy Storage, Black3
Amazon Basics Mesh Desk Organizer with Pen Holder and Office Caddy Storage, Black
Sold by: Amazon.com
Return or replace items: Eligible through September 15, 2026
$8.96
Buy it again
Continue series you've started
Calamity: The Reckoners, Book 3
$14.88 or 1 credit
"""

ORDERS_PAGE = """
ORDER PLACED
August 13, 2026
TOTAL
$50.73
SHIP TO
Sample Recipient
ORDER # 114-1234567-1234567
Delivered August 16
 Caribou Coffee Caribou Blend, Keurig Single-Serve K-Cup Pods, Medium Roast, 32 Count
Buy it again
"""


# --- transactions (payments) page ------------------------------------------


def test_parse_transactions_page_extracts_every_charge() -> None:
    charges = AmazonParser().parse_transactions_page(TRANSACTIONS_PAGE)

    assert [charge.amount for charge in charges] == [-42.68, -115.22, -15.61, 19.36]
    assert [charge.order_id for charge in charges] == [
        "114-1234567-1234567",
        "114-1234567-1234567",
        "114-2345678-2345678",
        "114-3456789-3456789",
    ]


def test_parse_transactions_page_keeps_one_order_per_shipment_charge() -> None:
    """The case the orders page cannot explain: one order, two card charges."""
    charges = AmazonParser().parse_transactions_page(TRANSACTIONS_PAGE)

    same_order = [
        charge for charge in charges if charge.order_id == "114-1234567-1234567"
    ]
    assert len(same_order) == 2
    assert sum(charge.amount or 0 for charge in same_order) == -157.90


def test_parse_transactions_page_carries_date_payment_method_and_merchant() -> None:
    charges = AmazonParser().parse_transactions_page(TRANSACTIONS_PAGE)

    assert charges[0].date_str == "August 15, 2026"
    assert charges[0].payment_method == "Prime Visa ****1234"
    assert charges[0].merchant == "AMZN Mktp US"
    assert charges[2].payment_method == "Amazon Gift Card"


def test_parse_transactions_page_marks_refunds() -> None:
    charges = AmazonParser().parse_transactions_page(TRANSACTIONS_PAGE)

    refunds = [charge for charge in charges if charge.is_refund]
    assert len(refunds) == 1
    assert refunds[0].amount == 19.36


def test_parse_transactions_page_ignores_amounts_without_an_order() -> None:
    """A cart preview or gift-card reload has no order to enrich anything with."""
    text = """
Subtotal
$20.81
 303 Aerospace Protectant for Vinyl, Plastic & Rubber, 10 oz
$12.99
August 9, 2026
Prime Visa ****1234-$4.98
Order #114-2345678-2345678
Amazon.com
"""
    charges = AmazonParser().parse_transactions_page(text)

    assert len(charges) == 1
    assert charges[0].amount == -4.98


def test_parse_transactions_page_deduplicates_repeated_pastes() -> None:
    parser = AmazonParser()

    once = parser.parse_transactions_page(TRANSACTIONS_PAGE)
    twice = parser.parse_transactions_page(TRANSACTIONS_PAGE + TRANSACTIONS_PAGE)

    assert len(twice) == len(once)


def test_parse_transactions_page_handles_empty_input() -> None:
    assert AmazonParser().parse_transactions_page("   ") == []


# --- order details page ------------------------------------------------------


def test_parse_order_details_page_reads_header_and_summary() -> None:
    orders = AmazonParser().parse_order_details_page(ORDER_DETAILS_PAGE)

    assert len(orders) == 1
    order = orders[0]
    assert order.order_id == "114-1234567-1234567"
    assert order.date_str == "August 13, 2026"
    assert order.subtotal == 46.87
    assert order.tax == 3.86
    assert order.total == 50.73
    assert order.currency == "$"


def test_parse_order_details_page_reads_item_prices() -> None:
    order = AmazonParser().parse_order_details_page(ORDER_DETAILS_PAGE)[0]

    assert order.has_item_prices
    assert [item.price for item in order.detailed_items] == [19.99, 8.96]


def test_parse_order_details_page_reads_quantity_badge() -> None:
    """A glued quantity ("...Black3") means three units at the unit price."""
    order = AmazonParser().parse_order_details_page(ORDER_DETAILS_PAGE)[0]

    organizer = order.detailed_items[1]
    assert organizer.quantity == 3
    assert organizer.name.endswith("Black")
    assert organizer.line_total == 26.88


def test_parse_order_details_page_item_prices_reconcile_with_subtotal() -> None:
    order = AmazonParser().parse_order_details_page(ORDER_DETAILS_PAGE)[0]

    assert order.items_total() == order.subtotal


def test_parse_order_details_page_expands_items_per_unit() -> None:
    order = AmazonParser().parse_order_details_page(ORDER_DETAILS_PAGE)[0]

    assert len(order.items) == 4  # one coffee + three organizers
    assert order.item_price(0) == 19.99
    assert order.item_price(3) == 8.96
    assert order.item_price(9) is None


def test_parse_order_details_page_excludes_recommendation_carousel() -> None:
    order = AmazonParser().parse_order_details_page(ORDER_DETAILS_PAGE)[0]

    assert not any("Calamity" in item for item in order.items)


def test_parse_order_details_page_accepts_inline_summary_amounts() -> None:
    """Some copies keep each summary label and its amount on one line."""
    text = """
Order placed March 2, 2026  Order # 114-1111111-2222222
Order Summary
Item(s) Subtotal: $10.00
Estimated tax to be collected: $0.83
Grand Total: $10.83
Some Product Name With Plenty Of Words
Some Product Name With Plenty Of Words
Sold by: Amazon.com
$10.00
"""
    order = AmazonParser().parse_order_details_page(text)[0]

    assert (order.subtotal, order.tax, order.total) == (10.00, 0.83, 10.83)


def test_parse_order_details_page_parses_several_pages_at_once() -> None:
    text = ORDER_DETAILS_PAGE + ORDER_DETAILS_PAGE.replace(
        "114-1234567-1234567", "114-0000000-0000001"
    )

    orders = AmazonParser().parse_order_details_page(text)

    assert [order.order_id for order in orders] == [
        "114-1234567-1234567",
        "114-0000000-0000001",
    ]


def test_parse_order_details_page_rejects_a_non_details_page() -> None:
    """Handing it the orders list must yield nothing rather than junk orders."""
    assert AmazonParser().parse_order_details_page(ORDERS_PAGE) == []


def test_parse_order_details_page_falls_back_when_no_seller_rows() -> None:
    """Grocery and digital orders have no "Sold by:" rows; names still matter."""
    text = """
https://www.amazon.com/your-orders/order-details?orderID=113-7890123-7890123
Order placed August 13, 2026  Order # 113-7890123-7890123
Order Summary
Grand Total:
$64.98
Purchased at Whole Foods Market
 Kodiak Cakes Cinnamon French Toast Sticks, 14.5 OZ
 Late July Blue Corn Organic Tortilla Chips, 10.1 Oz Bag
"""
    order = AmazonParser().parse_order_details_page(text)[0]

    assert order.total == 64.98
    assert any("Kodiak Cakes" in item for item in order.items)
    assert not order.has_item_prices


def test_parse_order_details_page_handles_empty_input() -> None:
    assert AmazonParser().parse_order_details_page("   ") == []


# --- page detection ----------------------------------------------------------


def test_detect_page_kind_identifies_each_page() -> None:
    assert detect_page_kind(TRANSACTIONS_PAGE) == "transactions"
    assert detect_page_kind(ORDER_DETAILS_PAGE) == "details"
    assert detect_page_kind(ORDERS_PAGE) == "orders"


def test_detect_page_kind_identifies_details_page_without_its_url() -> None:
    without_url = ORDER_DETAILS_PAGE.split("\n", 2)[2]

    assert detect_page_kind(without_url) == "details"


def test_detect_page_kind_returns_unknown_for_unrelated_text() -> None:
    assert detect_page_kind("") == "unknown"
    assert detect_page_kind("just some notes I pasted by mistake") == "unknown"
