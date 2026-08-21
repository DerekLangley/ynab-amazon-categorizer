"""Tests for transaction matching functionality."""

from dataclasses import replace

import pytest

from ynab_amazon_categorizer.amazon_data import AmazonData
from ynab_amazon_categorizer.amazon_parser import Order
from ynab_amazon_categorizer.models import AmazonCharge, OrderItem
from ynab_amazon_categorizer.transaction_matcher import (
    TransactionMatcher,
    mark_match_used,
    summarize_coverage,
)


def _make_order(
    order_id: str = "702-0000000-0000000",
    total: float | None = 10.00,
    date_str: str | None = "January 1, 2024",
    items: list[str] | None = None,
) -> Order:
    """Helper to create Order objects for tests."""
    order = Order()
    order.order_id = order_id
    order.total = total
    order.date_str = date_str
    order.items = items or ["Test Item"]
    return order


def test_find_matching_order_exact_amount_match() -> None:
    """Test finding order with exact amount match."""
    matcher = TransactionMatcher()
    order = _make_order(
        order_id="702-8237239-1234567",
        total=57.57,
        date_str="July 31, 2024",
    )

    result = matcher.find_matching_order(57.57, "2024-07-31", [order])

    assert result is not None
    assert result.order_id == "702-8237239-1234567"
    assert result.total == 57.57


def test_find_matching_order_no_match() -> None:
    """Test finding order when no orders match criteria."""
    matcher = TransactionMatcher()
    order = _make_order(total=57.57, date_str="July 31, 2024")

    result = matcher.find_matching_order(100.00, "2024-07-31", [order])

    assert result is None


def test_find_matching_order_rejects_stale_exact_amount_match() -> None:
    """An old same-amount order is not presented as a current match."""
    matcher = TransactionMatcher()
    order = _make_order(total=57.57, date_str="January 1, 2024")

    result = matcher.find_matching_order(57.57, "2024-03-01", [order])

    assert result is None


def test_find_matching_order_close_amount_no_match() -> None:
    """Test close amount does not match when exact matching is required."""
    matcher = TransactionMatcher()
    order = _make_order(total=57.57, date_str="July 31, 2024")

    result = matcher.find_matching_order(57.07, "2024-07-31", [order])

    assert result is None


def test_find_matching_order_with_order_objects() -> None:
    """Test matching works with Order objects."""
    matcher = TransactionMatcher()
    order = _make_order(
        order_id="702-1111111-2222222",
        total=25.99,
        date_str="August 5, 2024",
        items=["Widget A"],
    )

    result = matcher.find_matching_order(25.99, "2024-08-05", [order])

    assert result is not None
    assert isinstance(result, Order)
    assert result.order_id == "702-1111111-2222222"


def test_find_matching_order_none_date_on_order() -> None:
    """Test matching when order has None date — should still match on amount."""
    matcher = TransactionMatcher()
    order = _make_order(total=10.00, date_str=None)

    result = matcher.find_matching_order(10.00, "2024-01-01", [order])

    assert result is not None
    assert result.total == 10.00


def test_find_matching_order_unparseable_transaction_date() -> None:
    """Test matching when transaction date can't be parsed."""
    matcher = TransactionMatcher()
    order = _make_order(total=30.00, date_str="March 1, 2024")

    result = matcher.find_matching_order(30.00, "not-a-date", [order])

    assert result is not None
    assert result.total == 30.00


def test_find_matching_order_date_proximity_scoring() -> None:
    """Test that closer date gets higher score and wins tie-break."""
    matcher = TransactionMatcher()

    order_far = _make_order(
        order_id="702-FAR0000-0000000",
        total=50.00,
        date_str="January 10, 2024",
        items=["Far Item"],
    )
    order_close = _make_order(
        order_id="702-CLOSE00-0000000",
        total=50.00,
        date_str="January 1, 2024",
        items=["Close Item"],
    )

    # Transaction on Jan 1 — should prefer the same-day order
    result = matcher.find_matching_order(50.00, "2024-01-01", [order_far, order_close])

    assert result is not None
    assert result.order_id == "702-CLOSE00-0000000"


def test_find_matching_order_empty_list() -> None:
    """Test with empty parsed orders list."""
    matcher = TransactionMatcher()
    result = matcher.find_matching_order(10.00, "2024-01-01", [])
    assert result is None


def test_find_matching_order_none_total_skipped() -> None:
    """Test that orders with None total are skipped."""
    matcher = TransactionMatcher()
    order = _make_order(total=None)

    result = matcher.find_matching_order(10.00, "2024-01-01", [order])
    assert result is None


def test_find_matching_order_deterministic_tie_break() -> None:
    """When scores and dates are identical, order_id breaks the tie deterministically."""
    matcher = TransactionMatcher()

    order_a = _make_order(order_id="702-AAAAAAA-0000000", total=50.00)
    order_b = _make_order(order_id="702-BBBBBBB-0000000", total=50.00)

    # Regardless of input order, smallest order_id wins
    result1 = matcher.find_matching_order(50.00, "2024-01-01", [order_b, order_a])
    result2 = matcher.find_matching_order(50.00, "2024-01-01", [order_a, order_b])

    assert result1 is not None and result2 is not None
    assert result1.order_id == result2.order_id == "702-AAAAAAA-0000000"


def test_find_matching_order_negative_amount() -> None:
    """Negative transaction amounts are matched via absolute value."""
    matcher = TransactionMatcher()
    order = _make_order(total=25.00)

    result = matcher.find_matching_order(-25.00, "2024-01-01", [order])
    assert result is not None
    assert result.total == 25.00


def test_find_matching_order_skips_used_order() -> None:
    """Orders already applied to a transaction are skipped via used_order_ids."""
    matcher = TransactionMatcher()
    order = _make_order(order_id="702-USED000-0000000", total=50.00)

    # Without exclusion it matches.
    assert matcher.find_matching_order(50.00, "2024-01-01", [order]) is not None

    # Once marked used, the same order is not returned again.
    result = matcher.find_matching_order(
        50.00, "2024-01-01", [order], used_order_ids={"702-USED000-0000000"}
    )
    assert result is None


def test_find_matching_order_used_falls_through_to_next() -> None:
    """When the best order is used, a second same-amount order is matched instead."""
    matcher = TransactionMatcher()
    order_a = _make_order(order_id="702-AAAAAAA-0000000", total=50.00)
    order_b = _make_order(order_id="702-BBBBBBB-0000000", total=50.00)

    result = matcher.find_matching_order(
        50.00,
        "2024-01-01",
        [order_a, order_b],
        used_order_ids={"702-AAAAAAA-0000000"},
    )
    assert result is not None
    assert result.order_id == "702-BBBBBBB-0000000"


@pytest.mark.parametrize(
    "trans_date,order_date,expected_bonus",
    [
        ("2024-01-01", "January 1, 2024", 30),  # same day
        ("2024-01-02", "January 1, 2024", 30),  # next day
        ("2024-01-04", "January 1, 2024", 15),  # within 3 days
        ("2024-01-08", "January 1, 2024", 5),  # within 7 days
        ("2024-01-15", "January 1, 2024", 0),  # beyond 7 days
    ],
)
def test_date_proximity_scoring_tiers(
    trans_date: str, order_date: str, expected_bonus: int
) -> None:
    """Verify each date proximity tier independently."""
    matcher = TransactionMatcher()

    # Use two orders: one matching on date, one with no date (score=100)
    order_with_date = _make_order(
        order_id="702-DATED00-0000000", total=10.00, date_str=order_date
    )
    order_no_date = _make_order(
        order_id="702-NODATE0-0000000", total=10.00, date_str=None
    )

    result = matcher.find_matching_order(
        10.00, trans_date, [order_no_date, order_with_date]
    )

    assert result is not None
    if expected_bonus > 0:
        # The dated order should win because 100+bonus > 100
        assert result.order_id == "702-DATED00-0000000"
    else:
        # No bonus, so tie-break by order_id (alphabetically)
        # "702-DATED00-0000000" < "702-NODATE0-0000000"
        assert result.order_id == "702-DATED00-0000000"


# --- find_confident_match (batch) tests ---


def test_find_confident_match_unique() -> None:
    """A single amount match with a close date is confident."""
    matcher = TransactionMatcher()
    order = _make_order(total=50.00, date_str="January 1, 2024")
    result = matcher.find_confident_match(50.00, "2024-01-01", [order])
    assert result is not None
    assert result.total == 50.00


def test_find_confident_match_ambiguous_returns_none() -> None:
    """Two orders with the same amount are ambiguous — no confident match."""
    matcher = TransactionMatcher()
    o1 = _make_order(order_id="702-AAAAAAA-0000000", total=50.00)
    o2 = _make_order(order_id="702-BBBBBBB-0000000", total=50.00)
    assert matcher.find_confident_match(50.00, "2024-01-01", [o1, o2]) is None


def test_find_confident_match_no_match() -> None:
    """No amount match returns None."""
    matcher = TransactionMatcher()
    order = _make_order(total=50.00)
    assert matcher.find_confident_match(99.99, "2024-01-01", [order]) is None


def test_find_confident_match_far_date_returns_none() -> None:
    """A unique amount match with a far-off date is not confident."""
    matcher = TransactionMatcher()
    order = _make_order(total=50.00, date_str="January 1, 2024")
    assert matcher.find_confident_match(50.00, "2024-01-31", [order]) is None


def test_find_confident_match_no_order_date_allowed() -> None:
    """A unique amount match with no order date still counts (uniqueness is strong)."""
    matcher = TransactionMatcher()
    order = _make_order(total=50.00, date_str=None)
    result = matcher.find_confident_match(50.00, "2024-01-01", [order])
    assert result is not None


def test_find_confident_match_excludes_used() -> None:
    """The only candidate being already used yields no confident match."""
    matcher = TransactionMatcher()
    order = _make_order(order_id="702-USED000-0000000", total=50.00)
    assert (
        matcher.find_confident_match(
            50.00, "2024-01-01", [order], used_order_ids={"702-USED000-0000000"}
        )
        is None
    )


# --- charge matching (Amazon payments page) ---------------------------------


def _make_charge(
    amount: float,
    order_id: str = "114-1234567-1234567",
    date_str: str | None = "August 15, 2026",
    is_refund: bool = False,
) -> AmazonCharge:
    """Helper to create AmazonCharge rows for tests."""
    return AmazonCharge(
        amount=amount,
        date_str=date_str,
        order_id=order_id,
        payment_method="Prime Visa ****1234",
        is_refund=is_refund,
        currency="$",
    )


def test_find_matching_charge_matches_a_partial_shipment_charge() -> None:
    """The core gap: a charge that is only part of the order's total."""
    matcher = TransactionMatcher()
    charges = [_make_charge(-115.22), _make_charge(-42.68)]

    result = matcher.find_matching_charge(-115.22, "2026-08-16", charges)

    assert result is not None
    assert result.amount == -115.22


def test_find_matching_charge_requires_matching_direction() -> None:
    """An inflow must not match an outflow charge of the same magnitude."""
    matcher = TransactionMatcher()

    assert (
        matcher.find_matching_charge(19.36, "2026-08-09", [_make_charge(-19.36)])
        is None
    )


def test_find_matching_charge_matches_a_refund_to_an_inflow() -> None:
    matcher = TransactionMatcher()
    refund = _make_charge(
        19.36, "114-3456789-3456789", "August 8, 2026", is_refund=True
    )

    result = matcher.find_matching_charge(19.36, "2026-08-09", [refund])

    assert result is refund


def test_find_matching_charge_respects_the_date_window() -> None:
    matcher = TransactionMatcher()
    charges = [_make_charge(-42.68, date_str="January 1, 2026")]

    assert matcher.find_matching_charge(-42.68, "2026-08-16", charges) is None


def test_find_matching_charge_skips_already_used_charges() -> None:
    """Two same-amount charges feed two transactions, one each."""
    matcher = TransactionMatcher()
    first, second = (
        _make_charge(-20.00, "114-1111111-1111111"),
        _make_charge(-20.00, "114-2222222-2222222"),
    )
    charges = [first, second]

    used: set[tuple[str, str, str]] = {first.key}
    result = matcher.find_matching_charge(-20.00, "2026-08-16", charges, used)

    assert result is second


def test_find_matching_charge_ignores_rows_without_an_order() -> None:
    matcher = TransactionMatcher()
    orphan = _make_charge(-42.68)
    orphan.order_id = None

    assert matcher.find_matching_charge(-42.68, "2026-08-16", [orphan]) is None


def test_find_confident_charge_rejects_two_candidates() -> None:
    matcher = TransactionMatcher()
    charges = [
        _make_charge(-20.00, "114-1111111-1111111"),
        _make_charge(-20.00, "114-2222222-2222222"),
    ]

    assert matcher.find_confident_charge(-20.00, "2026-08-16", charges) is None


# --- combined resolution -----------------------------------------------------


def test_resolve_order_prefers_a_charge_over_an_order_total() -> None:
    """The charge names its order outright, so it beats an amount coincidence."""
    matcher = TransactionMatcher()
    coincidence = _make_order(order_id="702-9999999-9999999", total=42.68)
    real = _make_order(order_id="114-1234567-1234567", total=157.90)
    data = AmazonData(orders=[coincidence, real], charges=[_make_charge(-42.68)])

    result = matcher.resolve_order(-42.68, "2026-08-16", data)

    assert result is not None
    assert result.order_id == "114-1234567-1234567"
    assert result.matched_charge is not None
    assert result.is_partial_charge


def test_resolve_order_falls_back_to_order_totals() -> None:
    matcher = TransactionMatcher()
    order = _make_order(
        order_id="114-9012345-9012345", total=16.34, date_str="August 5, 2026"
    )
    data = AmazonData(orders=[order], charges=[_make_charge(-42.68)])

    result = matcher.resolve_order(-16.34, "2026-08-09", data)

    assert result is not None
    assert result.order_id == "114-9012345-9012345"
    assert result.matched_charge is None


def test_resolve_order_stubs_an_order_known_only_from_a_charge() -> None:
    """A charge for an order we never saw still yields its ID and link."""
    matcher = TransactionMatcher()
    charge = _make_charge(-16.42, "114-4567890-4567890", "August 7, 2026")
    data = AmazonData(charges=[charge])

    result = matcher.resolve_order(-16.42, "2026-08-09", data)

    assert result is not None
    assert result.order_id == "114-4567890-4567890"
    assert result.items == []
    assert result.total is None
    assert result.matched_charge is charge


def test_resolve_order_does_not_mutate_the_stored_order() -> None:
    """Per-transaction charge context must not leak into shared parsed data."""
    matcher = TransactionMatcher()
    order = _make_order(order_id="114-1234567-1234567", total=157.90)
    data = AmazonData(orders=[order], charges=[_make_charge(-42.68)])

    result = matcher.resolve_order(-42.68, "2026-08-16", data)

    assert result is not None and result.matched_charge is not None
    assert order.matched_charge is None


def test_resolve_order_matches_both_charges_of_one_order() -> None:
    """A two-shipment order fills two transactions, not one."""
    matcher = TransactionMatcher()
    order = _make_order(
        order_id="114-1234567-1234567", total=157.90, date_str="August 13, 2026"
    )
    first, second = _make_charge(-115.22), _make_charge(-42.68)
    data = AmazonData(orders=[order], charges=[first, second])
    used_orders: set[str] = set()
    used_charges: set[tuple[str, str, str]] = set()

    one = matcher.resolve_order(-115.22, "2026-08-16", data, used_orders, used_charges)
    assert one is not None and one.matched_charge is not None
    used_charges.add(one.matched_charge.key)

    two = matcher.resolve_order(-42.68, "2026-08-16", data, used_orders, used_charges)

    assert two is not None and two.matched_charge is not None
    assert two.matched_charge.amount == -42.68


def test_is_partial_charge_is_false_for_a_whole_order_charge() -> None:
    matcher = TransactionMatcher()
    order = _make_order(order_id="114-9012345-9012345", total=16.34)
    data = AmazonData(
        orders=[order], charges=[_make_charge(-16.34, "114-9012345-9012345")]
    )

    result = matcher.resolve_order(-16.34, "2026-08-16", data)

    assert result is not None
    assert not result.is_partial_charge


def test_resolve_confident_order_skips_an_ambiguous_charge() -> None:
    matcher = TransactionMatcher()
    charges = [
        _make_charge(-20.00, "114-1111111-1111111"),
        _make_charge(-20.00, "114-2222222-2222222"),
    ]
    data = AmazonData(charges=charges)

    assert matcher.resolve_confident_order(-20.00, "2026-08-16", data) is None


# --- coverage summary --------------------------------------------------------


def _txn(amount: float, date: str = "2026-08-16") -> dict[str, object]:
    return {"id": "t", "date": date, "amount": int(round(amount * 1000))}


def test_summarize_coverage_counts_an_empty_dataset_as_unmatched() -> None:
    summary = summarize_coverage([_txn(-42.68), _txn(-115.22)], AmazonData())

    assert (summary.total, summary.described, summary.unmatched) == (2, 0, 2)
    assert not summary.is_complete


def test_summarize_coverage_counts_described_transactions() -> None:
    order = _make_order(
        order_id="114-1234567-1234567", total=157.90, date_str="August 13, 2026"
    )
    data = AmazonData(orders=[order], charges=[_make_charge(-42.68)])

    summary = summarize_coverage([_txn(-42.68)], data)

    assert summary.described == 1
    assert summary.orders_needing_details == []
    assert summary.is_complete


def test_summarize_coverage_names_orders_missing_item_data() -> None:
    """The whole point: say which details pages are still worth fetching."""
    data = AmazonData(
        charges=[
            _make_charge(-16.42, "114-4567890-4567890", "August 7, 2026"),
            _make_charge(-10.20, "111-5678901-5678901", "August 8, 2026"),
        ]
    )

    summary = summarize_coverage(
        [_txn(-16.42, "2026-08-09"), _txn(-10.20, "2026-08-09")], data
    )

    assert summary.without_items == 2
    assert summary.described == 0
    assert summary.orders_needing_details == [
        "114-4567890-4567890",
        "111-5678901-5678901",
    ]


def test_summarize_coverage_lists_an_order_once_for_several_charges() -> None:
    order_id = "114-1234567-1234567"
    data = AmazonData(charges=[_make_charge(-115.22), _make_charge(-42.68)])

    summary = summarize_coverage([_txn(-115.22), _txn(-42.68)], data)

    assert summary.without_items == 2
    assert summary.orders_needing_details == [order_id]


def test_summarize_coverage_does_not_let_two_transactions_share_one_charge() -> None:
    """Consuming matches keeps the count honest for same-amount transactions."""
    data = AmazonData(charges=[_make_charge(-42.68)])

    summary = summarize_coverage([_txn(-42.68), _txn(-42.68)], data)

    assert summary.matched == 1
    assert summary.unmatched == 1


def test_summarize_coverage_ignores_orders_with_no_pending_transaction() -> None:
    """Only orders behind a real transaction are worth asking the user for."""
    data = AmazonData(
        charges=[
            _make_charge(-16.42, "114-4567890-4567890", "August 7, 2026"),
            _make_charge(-99.99, "114-0000000-0000000", "August 7, 2026"),
        ]
    )

    summary = summarize_coverage([_txn(-16.42, "2026-08-09")], data)

    assert summary.orders_needing_details == ["114-4567890-4567890"]


def test_mark_match_used_is_the_shared_consumption_policy() -> None:
    order = _make_order(order_id="114-1234567-1234567", total=157.90)
    charged = replace(order, matched_charge=_make_charge(-42.68))
    used_orders: set[str] = set()
    used_charges: set[tuple[str, str, str]] = set()

    mark_match_used(charged, used_orders, used_charges)
    assert used_orders == set() and len(used_charges) == 1

    mark_match_used(order, used_orders, used_charges)
    assert used_orders == {"114-1234567-1234567"}


def test_summarize_coverage_counts_covered_orders_without_prices() -> None:
    """Complete coverage is not the same as complete pricing."""
    order = _make_order(
        order_id="114-8901234-8901234", total=42.68, date_str="August 13, 2026"
    )
    summary = summarize_coverage([_txn(-42.68)], AmazonData(orders=[order]))

    assert summary.is_complete
    assert summary.without_prices == 1


def test_summarize_coverage_reports_no_missing_prices_for_priced_orders() -> None:
    order = _make_order(
        order_id="114-8901234-8901234", total=42.68, date_str="August 13, 2026"
    )
    order.detailed_items = [OrderItem("Widget A", 42.68)]
    order.item_prices = [42.68]

    summary = summarize_coverage([_txn(-42.68)], AmazonData(orders=[order]))

    assert summary.is_complete
    assert summary.without_prices == 0


def test_summarize_coverage_names_orders_needing_prices() -> None:
    """Knowing the count is not enough — the user needs the order to go fetch."""
    order = _make_order(
        order_id="114-8901234-8901234", total=42.68, date_str="August 13, 2026"
    )
    summary = summarize_coverage([_txn(-42.68)], AmazonData(orders=[order]))

    assert summary.orders_needing_prices == ["114-8901234-8901234"]


def test_summarize_coverage_lists_a_price_gap_once_per_order() -> None:
    """Two charges against one order are one page to fetch, not two."""
    order = _make_order(
        order_id="114-1234567-1234567", total=157.90, date_str="August 13, 2026"
    )
    order.items = ["Widget A", "Widget B"]
    data = AmazonData(
        orders=[order], charges=[_make_charge(-115.22), _make_charge(-42.68)]
    )

    summary = summarize_coverage([_txn(-115.22), _txn(-42.68)], data)

    assert summary.without_prices == 2
    assert summary.orders_needing_prices == ["114-1234567-1234567"]


def test_summarize_coverage_omits_priced_orders_from_the_price_gap() -> None:
    order = _make_order(
        order_id="114-8901234-8901234", total=42.68, date_str="August 13, 2026"
    )
    order.detailed_items = [OrderItem("Widget A", 42.68)]
    order.item_prices = [42.68]

    summary = summarize_coverage([_txn(-42.68)], AmazonData(orders=[order]))

    assert summary.orders_needing_prices == []
