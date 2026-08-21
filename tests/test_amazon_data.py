"""Tests for combining the orders, details, and payments pages."""

from ynab_amazon_categorizer.amazon_data import AmazonData, merge_order
from ynab_amazon_categorizer.models import (
    AmazonCharge,
    Order,
    OrderItem,
    expand_items,
)

ORDER_ID = "114-1234567-1234567"


def _list_page_order() -> Order:
    """What the orders list page yields: total and a truncated item list."""
    return Order(
        order_id=ORDER_ID,
        total=157.90,
        date_str="August 13, 2026",
        items=["Caribou Coffee K-Cup Pods"],
        currency="$",
    )


def _details_page_order() -> Order:
    """What the details page yields: every item, with prices and tax."""
    return Order(
        order_id=ORDER_ID,
        total=157.90,
        date_str="August 13, 2026",
        items=["Caribou Coffee K-Cup Pods", "Large Ceramic Coffee Mug Set"],
        currency="$",
        detailed_items=[
            OrderItem("Caribou Coffee K-Cup Pods", 19.99),
            OrderItem("Large Ceramic Coffee Mug Set", 19.99),
        ],
        subtotal=149.83,
        tax=11.52,
    )


def _charge(amount: float, order_id: str = ORDER_ID) -> AmazonCharge:
    return AmazonCharge(
        amount=amount,
        date_str="August 15, 2026",
        order_id=order_id,
        payment_method="Prime Visa ****1234",
        currency="$",
    )


# --- merging -----------------------------------------------------------------


def test_details_page_wins_over_the_orders_list() -> None:
    merged = merge_order(_list_page_order(), _details_page_order())

    assert len(merged.items) == 2
    assert merged.has_item_prices
    assert merged.tax == 11.52


def test_merge_is_order_independent() -> None:
    """Pasting the details page first must give the same result."""
    forward = merge_order(_list_page_order(), _details_page_order())
    backward = merge_order(_details_page_order(), _list_page_order())

    assert forward == backward


def test_merge_keeps_a_value_only_the_poorer_source_had() -> None:
    """A details page whose summary could not be read still gets the total."""
    priced_but_totalless = Order(
        order_id=ORDER_ID,
        items=["Caribou Coffee K-Cup Pods"],
        detailed_items=[OrderItem("Caribou Coffee K-Cup Pods", 19.99)],
    )

    merged = merge_order(_list_page_order(), priced_but_totalless)

    assert merged.total == 157.90
    assert merged.currency == "$"
    assert merged.has_item_prices


# --- AmazonData --------------------------------------------------------------


def test_add_orders_merges_a_repeat_sighting() -> None:
    data = AmazonData()

    assert data.add_orders([_list_page_order()]) == (1, 0)
    assert data.add_orders([_details_page_order()]) == (0, 1)
    assert len(data.orders) == 1
    assert data.orders[0].has_item_prices


def test_add_charges_ignores_rows_already_held() -> None:
    data = AmazonData()

    assert data.add_charges([_charge(-42.68), _charge(-115.22)]) == 2
    assert data.add_charges([_charge(-42.68)]) == 0
    assert len(data.charges) == 2


def test_unknown_charge_order_ids_lists_orders_with_no_page() -> None:
    data = AmazonData(orders=[_list_page_order()])
    data.add_charges([_charge(-42.68), _charge(-16.42, "114-4567890-4567890")])

    assert data.unknown_charge_order_ids() == ["114-4567890-4567890"]


def test_unknown_charge_order_ids_empties_once_details_arrive() -> None:
    data = AmazonData()
    data.add_charges([_charge(-16.42, "114-4567890-4567890")])
    assert data.unknown_charge_order_ids() == ["114-4567890-4567890"]

    data.add_orders([Order(order_id="114-4567890-4567890", items=["Something"])])

    assert data.unknown_charge_order_ids() == []


def test_charges_for_order_returns_every_shipment_charge() -> None:
    data = AmazonData()
    data.add_charges([_charge(-42.68), _charge(-115.22), _charge(-9.99, "111-0-0")])

    assert len(data.charges_for_order(ORDER_ID)) == 2
    assert data.charges_for_order(None) == []


def test_empty_data_is_falsy() -> None:
    assert not AmazonData()
    assert AmazonData(orders=[_list_page_order()])
    assert AmazonData(charges=[_charge(-1.00)])


def test_from_orders_accepts_none() -> None:
    assert AmazonData.from_orders(None).orders == []


# --- item expansion ----------------------------------------------------------


def test_expand_items_repeats_a_name_per_unit() -> None:
    """Separate units stay separately splittable when they fit."""
    names, prices = expand_items(
        [OrderItem("Desk Organizer", 9.44, 3), OrderItem("Highlighters", 5.89)], 10
    )

    assert names == ["Desk Organizer"] * 3 + ["Highlighters"]
    assert prices == [9.44, 9.44, 9.44, 5.89]


def test_expand_items_keeps_distinct_items_over_repeated_units() -> None:
    """Dropping a distinct item hides a purchase; dropping a unit does not."""
    detailed = [OrderItem(f"Item {n}", float(n)) for n in range(1, 10)]
    detailed.insert(0, OrderItem("Bought Three Of These", 9.44, 3))

    names, prices = expand_items(detailed, 10)

    assert names.count("Bought Three Of These") == 1
    assert len(names) == 10
    assert len(set(names)) == 10  # every distinct line survived
    assert prices[0] == 9.44


def test_expand_items_truncates_when_distinct_items_alone_overflow() -> None:
    names, prices = expand_items(
        [OrderItem(f"Item {n}", float(n)) for n in range(1, 15)], 10
    )

    assert len(names) == len(prices) == 10


def test_expand_items_handles_no_items() -> None:
    assert expand_items([], 10) == ([], [])
