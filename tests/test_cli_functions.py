"""Tests for extracted CLI helper functions."""

import dataclasses
from unittest.mock import Mock

import pytest

import ynab_amazon_categorizer.cli as cli_module
from ynab_amazon_categorizer.amazon_data import AmazonData
from ynab_amazon_categorizer.amazon_parser import AmazonParser, Order
from ynab_amazon_categorizer.batch import process_batch
from ynab_amazon_categorizer.cli import (
    _env_flag,
    _handle_categorize,
    _parse_args,
    _tax_rate_for_category,
    absorb_amazon_page,
    build_preview,
    compute_split_amount,
    display_matched_order,
    handle_split,
    main,
    print_config_summary,
    process_transaction,
    prompt_for_category_selection,
    resolve_memo,
)
from ynab_amazon_categorizer.config import Config
from ynab_amazon_categorizer.exceptions import YNABResponseError
from ynab_amazon_categorizer.memo_generator import (
    MemoGenerator,
    build_batch_memo,
    generate_split_summary_memo,
)
from ynab_amazon_categorizer.models import (
    AmazonCharge,
    OrderItem,
    SaveSubtransaction,
    expand_items,
)
from ynab_amazon_categorizer.payloads import (
    build_memo_only_payload,
    build_single_payload,
    build_split_payload,
)
from ynab_amazon_categorizer.transaction_matcher import (
    TransactionMatcher,
    mark_match_used,
)
from ynab_amazon_categorizer.transactions import (
    fetch_amazon_transactions,
    is_amazon_payee,
)

# --- build_preview tests ---


def test_build_preview_does_not_mutate() -> None:
    """Fix #1: build_preview uses deepcopy so the original payload is not mutated."""
    payload = {
        "id": "t1",
        "category_id": "cat1",
        "subtransactions": [
            {"amount": -5000, "category_id": "cat2", "memo": "item"},
        ],
    }
    category_id_map = {"cat1": "Groceries", "cat2": "Household"}

    preview = build_preview(payload, category_id_map)

    # Preview should have injected names
    assert preview["category_name"] == "Groceries"
    assert preview["subtransactions"][0]["category_name"] == "Household"

    # Original payload must NOT have category_name keys
    assert "category_name" not in payload
    assert "category_name" not in payload["subtransactions"][0]


def test_build_preview_adds_category_names() -> None:
    """Category names are resolved from the id map."""
    payload = {"category_id": "c1"}
    result = build_preview(payload, {"c1": "Fun Money"})
    assert result["category_name"] == "Fun Money"


def test_build_preview_unknown_category() -> None:
    """Unknown category IDs get a fallback label."""
    payload = {"category_id": "unknown_id"}
    result = build_preview(payload, {})
    assert result["category_name"] == "Unknown Category"


# --- compute_split_amount tests ---


def test_compute_split_amount_outflow() -> None:
    """Outflow (negative remaining) produces a negative result."""
    result = compute_split_amount(10.0, -20000)
    assert result == -10000


def test_compute_split_amount_inflow() -> None:
    """Fix #2: Inflow (positive remaining) produces a positive result."""
    result = compute_split_amount(10.0, 20000)
    assert result == 10000


def test_compute_split_amount_snap() -> None:
    """When the amount is within 1 milliunit of remaining, snap to exact remainder."""
    # 10.0 * 1000 = 10000, remaining is -10001 → difference is 1 → snap
    result = compute_split_amount(10.0, -10001)
    assert result == -10001


def test_compute_split_amount_exceeds() -> None:
    """Raises ValueError when amount exceeds the remaining balance."""
    with pytest.raises(ValueError, match="exceeds remaining"):
        compute_split_amount(25.0, -20000)


# --- build_single_payload tests ---


def test_build_single_payload() -> None:
    """Single-category updates include only fields intentionally changed."""
    result = build_single_payload("cat1", "test memo")

    assert result == {
        "category_id": "cat1",
        "memo": "test memo",
        "approved": True,
    }


def test_build_single_payload_sanitizes_long_memo() -> None:
    """Long memos are truncated via sanitize_memo."""
    long_memo = "A" * 300
    result = build_single_payload("cat1", long_memo)
    assert len(result["memo"]) <= 200


# --- build_split_payload tests ---


def test_build_split_payload() -> None:
    """Split updates include only the requested split fields."""
    subtransactions: list[SaveSubtransaction] = [
        {"amount": -10000, "category_id": "cat1", "memo": "item1"},
        {"amount": -5000, "category_id": "cat2", "memo": "item2"},
    ]
    result = build_split_payload(subtransactions, None, "original")

    assert result["category_id"] is None
    assert result["memo"] == "original"
    assert result["subtransactions"] == subtransactions
    assert result["approved"] is True
    assert set(result) == {"category_id", "memo", "approved", "subtransactions"}


def test_build_split_payload_with_order() -> None:
    """Split payload uses order items for summary memo."""
    order = Order()
    order.items = ["Widget A", "Widget B"]
    subtransactions: list[SaveSubtransaction] = [
        {"amount": -10000, "category_id": "cat1", "memo": "item1"}
    ]

    result = build_split_payload(subtransactions, order, "original")
    assert "Widget A" in result["memo"]
    assert "Widget B" in result["memo"]


def test_resolve_memo_keeps_all_matched_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single-category matched orders keep all parsed items in the suggested memo."""
    order = Order()
    order.order_id = "702-1234567-7654321"
    order.items = ["Widget A", "Widget B"]

    monkeypatch.setattr("ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: "")

    result = resolve_memo(order, "", MemoGenerator("amazon.com"))

    assert "Widget A" in result
    assert "Widget B" in result
    assert "702-1234567-7654321" in result


# --- print_config_summary tests ---


def test_print_config_summary_masks_secrets(capsys: pytest.CaptureFixture[str]) -> None:
    """Fix #6: No API key or full budget ID appears in output."""
    config = Config(
        api_key="dummy-api-key-for-testing",
        budget_id="abcd-efgh-ijkl-mnop",
        account_id=None,
    )
    print_config_summary(config)

    captured = capsys.readouterr().out

    # Must NOT contain the full API key or budget ID
    assert "dummy-api-key-for-testing" not in captured
    assert "abcd-efgh-ijkl-mnop" not in captured

    # Should show masked info
    assert "API Key: configured" in captured
    assert "mnop" in captured  # last 4 of budget_id
    assert "All accounts" in captured


def test_print_config_summary_with_account(capsys: pytest.CaptureFixture[str]) -> None:
    """Shows 'Account ID: configured' when account is set."""
    config = Config(api_key="key", budget_id="budget", account_id="acct123")
    print_config_summary(config)
    captured = capsys.readouterr().out
    assert "Account ID: configured" in captured


# --- fetch_amazon_transactions tests ---


@pytest.mark.parametrize(
    "payee_name", ["Amazon.com", "AMZN Mktp CA", "AMZ*Marketplace", "amazon.ca"]
)
def test_is_amazon_payee_accepts_vendor_markers(payee_name: str) -> None:
    assert is_amazon_payee(payee_name) is True


@pytest.mark.parametrize(
    "payee_name", ["Ramzi Market", "Glamzone", "Amazing Store", ""]
)
def test_is_amazon_payee_rejects_substring_false_positives(payee_name: str) -> None:
    assert is_amazon_payee(payee_name) is False


def test_fetch_amazon_transactions_filters_correctly() -> None:
    """Verify that fetch_amazon_transactions filters to uncategorized Amazon transactions."""
    mock_client = Mock()
    mock_client.get_data.return_value = {
        "transactions": [
            {
                "id": "t1",
                "account_id": "a1",
                "date": "2025-01-01",
                "payee_name": "Amazon.com",
                "category_id": None,
                "cleared": "uncleared",
                "amount": -5000,
                "transfer_account_id": None,
                "subtransactions": [],
                "import_id": "imp1",
            },
            {
                "id": "t2",
                "account_id": "a1",
                "date": "2025-01-02",
                "payee_name": "Grocery Store",
                "category_id": None,
                "cleared": "uncleared",
                "amount": -3000,
                "transfer_account_id": None,
                "subtransactions": [],
                "import_id": "imp2",
            },
            {
                "id": "t3",
                "account_id": "a1",
                "date": "2025-01-03",
                "payee_name": "AMZN Mktp US",
                "category_id": "cat1",  # already categorized
                "cleared": "uncleared",
                "amount": -2000,
                "transfer_account_id": None,
                "subtransactions": [],
                "import_id": "imp3",
            },
        ]
    }
    config = Config(api_key="key", budget_id="budget", account_id=None)

    result = fetch_amazon_transactions(mock_client, config)

    assert len(result) == 1
    assert result[0]["id"] == "t1"


def test_fetch_amazon_transactions_empty_response() -> None:
    """Returns an empty list when the API returns an empty transaction list."""
    mock_client = Mock()
    mock_client.get_data.return_value = {"transactions": []}
    config = Config(api_key="key", budget_id="budget", account_id=None)

    result = fetch_amazon_transactions(mock_client, config)

    assert result == []


@pytest.mark.parametrize("response", [None, {}, {"transactions": "not-a-list"}])
def test_fetch_amazon_transactions_rejects_malformed_collection(
    response: object,
) -> None:
    """A malformed collection cannot masquerade as no matching transactions."""
    mock_client = Mock()
    mock_client.get_data.return_value = response
    config = Config(api_key="key", budget_id="budget", account_id=None)

    with pytest.raises(YNABResponseError, match="transactions collection"):
        fetch_amazon_transactions(mock_client, config)


def test_fetch_amazon_transactions_rejects_malformed_item() -> None:
    """Required transaction fields are validated with item context."""
    mock_client = Mock()
    mock_client.get_data.return_value = {
        "transactions": [{"id": "t1", "payee_name": "Amazon", "amount": "5.00"}]
    }
    config = Config(api_key="key", budget_id="budget", account_id=None)

    with pytest.raises(YNABResponseError, match="transaction at index 0"):
        fetch_amazon_transactions(mock_client, config)


def test_fetch_amazon_transactions_with_account_id() -> None:
    """Uses account-specific endpoint when account_id is set."""
    mock_client = Mock()
    mock_client.get_data.return_value = {"transactions": []}
    config = Config(api_key="key", budget_id="budget", account_id="acct123")

    fetch_amazon_transactions(mock_client, config)

    mock_client.get_data.assert_called_once_with(
        "/budgets/budget/accounts/acct123/transactions"
    )


def test_fetch_amazon_transactions_includes_manual() -> None:
    """Manual transactions (no import_id) are now included."""
    mock_client = Mock()
    mock_client.get_data.return_value = {
        "transactions": [
            {
                "id": "t1",
                "account_id": "a1",
                "date": "2025-01-01",
                "payee_name": "Amazon.com",
                "category_id": None,
                "cleared": "uncleared",
                "amount": -5000,
                "transfer_account_id": None,
                "subtransactions": [],
                # No import_id — manual transaction
            },
        ]
    }
    config = Config(api_key="key", budget_id="budget", account_id=None)

    result = fetch_amazon_transactions(mock_client, config)

    assert len(result) == 1
    assert result[0]["id"] == "t1"


def test_fetch_amazon_transactions_optionally_includes_reconciled() -> None:
    """Reconciled Amazon transactions are returned only when requested."""
    mock_client = Mock()
    mock_client.get_data.return_value = {
        "transactions": [
            {
                "id": "t1",
                "account_id": "a1",
                "date": "2025-01-01",
                "payee_name": "Amazon.com",
                "category_id": None,
                "cleared": "reconciled",
                "amount": -5000,
                "transfer_account_id": None,
                "subtransactions": [],
            }
        ]
    }
    config = Config(api_key="key", budget_id="budget", account_id=None)

    assert fetch_amazon_transactions(mock_client, config) == []
    result = fetch_amazon_transactions(mock_client, config, include_reconciled=True)

    assert [transaction["id"] for transaction in result] == ["t1"]


# --- process_transaction display tests ---


def test_prompt_for_orders_displays_parsed_currency_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The parser's currency survives through the first CLI order summary."""
    pages = [
        """
        Order placed January 15, 2025
        Total £14.99
        Order # 702-1234567-7654321
        International Product Name With Enough Words To Parse
        """,
        "",  # an empty submit ends the paste loop
    ]
    monkeypatch.setattr(
        cli_module,
        "get_multiline_input_with_custom_submit",
        lambda _prompt: pages.pop(0),
    )

    amazon_data = cli_module.prompt_for_amazon_data()

    assert amazon_data.orders
    assert amazon_data.orders[0].currency == "£"
    captured = capsys.readouterr().out
    assert "Order 702-1234567-7654321: £14.99" in captured
    assert "Order 702-1234567-7654321: $14.99" not in captured


def test_process_transaction_displays_inflow_amount_without_negating(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Accepted inflows display with their actual positive sign."""
    transaction = {
        "id": "t1",
        "date": "2025-01-15",
        "payee_name": "Amazon",
        "amount": 10000,
        "memo": "",
    }
    responses = iter(["y", "s"])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: next(responses)
    )

    result = process_transaction(
        transaction,
        0,
        1,
        None,
        MemoGenerator(),
        Mock(),
        Mock(),
        {},
        {},
    )

    captured = capsys.readouterr().out
    assert result is True
    assert "Found inflow transaction: Amazon $10.00" in captured
    assert "Amount: 10.00" in captured


def test_process_transaction_uses_matched_order_currency_for_inflow(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A matched non-dollar refund is not presented as a dollar transaction."""
    transaction = {
        "id": "t1",
        "date": "2025-01-15",
        "payee_name": "Amazon",
        "amount": 10000,
        "memo": "",
    }
    order = Order(
        order_id="702-1234567-7654321",
        total=10.00,
        date_str="January 15, 2025",
        currency="£",
    )
    responses = iter(["y", "s"])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: next(responses)
    )

    result = process_transaction(
        transaction,
        0,
        1,
        AmazonData.from_orders([order]),
        MemoGenerator(),
        Mock(),
        Mock(),
        {},
        {},
    )

    assert result is True
    captured = capsys.readouterr().out
    assert "Found inflow transaction: Amazon £10.00" in captured
    assert "Found inflow transaction: Amazon $10.00" not in captured


# --- generate_split_summary_memo tests ---


def test_generate_split_summary_memo_single_item() -> None:
    """Single-item order returns item directly."""
    order = Order()
    order.items = ["Widget X"]
    assert generate_split_summary_memo(order) == "Widget X"


def test_generate_split_summary_memo_multiple_items() -> None:
    """Multiple items returns formatted list."""
    order = Order()
    order.items = ["Widget A", "Widget B"]
    result = generate_split_summary_memo(order)
    assert result == "2 Items:\n- Widget A\n- Widget B"


def test_generate_split_summary_memo_no_items() -> None:
    """Order with no items returns empty string."""
    order = Order()
    order.items = []
    assert generate_split_summary_memo(order) == ""


# --- display_matched_order tests ---


def test_display_matched_order_with_order_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Display order details from an Order object."""
    order = Order()
    order.order_id = "702-1234567-7654321"
    order.total = 42.99
    order.date_str = "January 15, 2025"
    order.items = ["Test Product"]

    memo_gen = MemoGenerator("amazon.com")
    display_matched_order(order, memo_gen)

    captured = capsys.readouterr().out
    assert "702-1234567-7654321" in captured
    assert "42.99" in captured
    assert "Test Product" in captured


def test_display_matched_order_uses_parsed_currency(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Matched totals retain their Amazon currency prefix during verification."""
    order = Order(
        order_id="702-1234567-7654321",
        total=42.99,
        date_str="January 15, 2025",
        items=["Test Product"],
        currency="£",
    )

    display_matched_order(order, MemoGenerator("amazon.co.uk"))

    captured = capsys.readouterr().out
    assert "Total: £42.99" in captured
    assert "Total: $42.99" not in captured


# --- handle_split tests ---


def _split_order() -> Order:
    order = Order()
    order.order_id = "702-1234567-7654321"
    order.total = 20.00
    order.date_str = "January 1, 2024"
    order.items = ["Widget A", "Widget B"]
    return order


def test_handle_split_two_even_splits(monkeypatch: pytest.MonkeyPatch) -> None:
    """A two-item order splits cleanly into two equal subtransactions.

    Uses the '=' exact-amount prefix so this test verifies split *mechanics*
    (categories, memos, amount bookkeeping) independent of the base-price/tax
    calculation added later — entering a bare "10" would now be treated as a
    pre-tax base price with tax added on top, not an exact $10.00.
    """
    order = _split_order()
    categories = iter([("cat1", "Cat One"), ("cat2", "Cat Two")])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt_for_category_selection",
        lambda *a, **k: next(categories),
    )
    # split-1 amount (exact, no tax), split-1 "use suggested?", split-2 amount
    # (default = remaining as-is), split-2 memo
    responses = iter(["=10", "", "", ""])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: next(responses)
    )

    result = handle_split(
        {"amount": -20000}, order, MemoGenerator("amazon.com"), Mock(), {}
    )

    assert result is not None
    assert len(result) == 2
    assert result[0]["amount"] == -10000
    assert result[1]["amount"] == -10000
    assert result[0]["category_id"] == "cat1"
    assert result[1]["category_id"] == "cat2"
    assert "Widget A" in str(result[0]["memo"])
    assert "Widget B" in str(result[1]["memo"])


def test_handle_split_cancel_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backing out of category selection cancels the whole split."""
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt_for_category_selection",
        lambda *a, **k: (None, None),
    )

    result = handle_split(
        {"amount": -20000}, _split_order(), MemoGenerator(), Mock(), {}
    )

    assert result is None


# --- _parse_args / dry-run tests ---


def test_parse_args_dry_run_flag() -> None:
    """--dry-run sets the dry_run attribute."""
    assert _parse_args(["--dry-run"]).dry_run is True
    assert _parse_args([]).dry_run is False


def test_handle_categorize_dry_run_does_not_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In dry-run mode the preview is shown but no API update is sent."""
    transaction = {
        "id": "t1",
        "account_id": "a1",
        "date": "2025-01-15",
        "amount": -15000,
    }
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt_for_category_selection",
        lambda *a, **k: ("cat1", "Cat One"),
    )
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.get_multiline_input_with_custom_submit",
        lambda *a, **k: "",
    )
    # "Split this transaction?" -> n, "Enter item details manually?" -> n
    responses = iter(["n", "n"])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: next(responses)
    )
    ynab_client = Mock()

    result = _handle_categorize(
        transaction,
        None,
        "",
        MemoGenerator(),
        ynab_client,
        Mock(),
        {},
        {},
        dry_run=True,
    )

    assert result == "done"
    ynab_client.update_transaction.assert_not_called()


# --- process_transaction order-consumption tests ---


def _amount_matched_order() -> Order:
    order = Order()
    order.order_id = "702-CONSUME-0000000"
    order.total = 20.00
    order.date_str = "January 1, 2024"
    order.items = ["Widget A"]
    return order


def test_process_transaction_marks_order_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successfully categorized matched order is added to used_order_ids."""
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._handle_categorize", lambda *a, **k: "done"
    )
    monkeypatch.setattr("ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: "c")
    used: set[str] = set()
    transaction = {
        "id": "t1",
        "date": "2024-01-01",
        "payee_name": "Amazon",
        "amount": -20000,
        "memo": "",
    }

    result = process_transaction(
        transaction,
        0,
        1,
        AmazonData.from_orders([_amount_matched_order()]),
        MemoGenerator(),
        Mock(),
        Mock(),
        {},
        {},
        used,
        False,
    )

    assert result is True
    assert "702-CONSUME-0000000" in used


def test_process_transaction_dry_run_does_not_mark_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In dry-run mode a matched order is NOT marked as consumed."""
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._handle_categorize", lambda *a, **k: "done"
    )
    monkeypatch.setattr("ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: "c")
    used: set[str] = set()
    transaction = {
        "id": "t1",
        "date": "2024-01-01",
        "payee_name": "Amazon",
        "amount": -20000,
        "memo": "",
    }

    result = process_transaction(
        transaction,
        0,
        1,
        AmazonData.from_orders([_amount_matched_order()]),
        MemoGenerator(),
        Mock(),
        Mock(),
        {},
        {},
        used,
        True,
    )

    assert result is True
    assert used == set()


# --- batch mode tests ---


def test_parse_args_batch_flag() -> None:
    """--batch sets the batch attribute; flags are independent."""
    assert _parse_args(["--batch"]).batch is True
    assert _parse_args([]).batch is False
    args = _parse_args(["--batch", "--dry-run"])
    assert args.batch is True and args.dry_run is True


@pytest.mark.parametrize("approved", [False, True])
def test_build_memo_only_payload_contains_only_intentional_fields(
    approved: bool,
) -> None:
    """Memo updates preserve approval without resending unrelated fields."""
    result = build_memo_only_payload("Widget A\n https://example/order", approved)

    assert result == {
        "memo": "Widget A\n https://example/order",
        "approved": approved,
    }


def test_build_batch_memo_preserves_existing_memo() -> None:
    """Batch enrichment appends order context without losing an existing memo."""
    order = _batch_order()

    result = build_batch_memo(order, MemoGenerator(), "KEEP THIS NOTE")

    assert result is not None
    assert result.startswith("KEEP THIS NOTE")
    assert "Widget A" in result
    assert order.order_id is not None
    assert order.order_id in result


def test_build_batch_memo_is_idempotent() -> None:
    """Rebuilding a generated memo does not duplicate its order context."""
    order = _batch_order()
    first = build_batch_memo(order, MemoGenerator())
    assert first is not None

    second = build_batch_memo(order, MemoGenerator(), first)

    assert second == first


def test_build_batch_memo_refuses_to_truncate_existing_memo() -> None:
    """An existing memo is never shortened merely to add enrichment."""
    order = _batch_order()
    existing = "X" * 195

    assert build_batch_memo(order, MemoGenerator(), existing) is None


def _batch_txn(txn_id: str, amount: int) -> dict:
    return {
        "id": txn_id,
        "account_id": "a1",
        "date": "2024-01-01",
        "amount": amount,
        "payee_name": "Amazon",
        "category_id": None,
        "approved": False,
        "memo": "",
    }


def _batch_order(order_id: str = "702-1234567-7654321") -> Order:
    order = Order()
    order.order_id = order_id
    order.total = 20.00
    order.date_str = "January 1, 2024"
    order.items = ["Widget A"]
    return order


def test_process_batch_enriches_confident_match() -> None:
    """A confident match gets a memo-only update; category stays None."""
    client = Mock()
    enriched, skipped, failed = process_batch(
        [_batch_txn("t1", -20000)],
        AmazonData.from_orders([_batch_order()]),
        MemoGenerator("amazon.com"),
        client,
    )

    assert (enriched, skipped, failed) == (1, 0, 0)
    client.update_transaction.assert_called_once()
    txn_id, payload = client.update_transaction.call_args[0]
    assert txn_id == "t1"
    assert "Widget A" in payload["memo"]
    assert set(payload) == {"memo", "approved"}


def test_process_batch_displays_matched_order_currency(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Batch transaction verification uses the matched order's currency."""
    order = _batch_order()
    order.currency = "€"

    result = process_batch(
        [_batch_txn("t1", -20000)],
        AmazonData.from_orders([order]),
        MemoGenerator(),
        Mock(),
        dry_run=True,
    )

    assert result == (1, 0, 0)
    captured = capsys.readouterr().out
    assert "Amazon -€20.00" in captured
    assert "Amazon -$20.00" not in captured


def test_process_batch_skips_ambiguous() -> None:
    """Two same-amount orders are ambiguous, so nothing is enriched."""
    client = Mock()
    orders = [_batch_order("702-AAAAAAA-0000000"), _batch_order("702-BBBBBBB-0000000")]
    enriched, skipped, failed = process_batch(
        [_batch_txn("t1", -20000)],
        AmazonData.from_orders(orders),
        MemoGenerator(),
        client,
    )

    assert (enriched, skipped, failed) == (0, 1, 0)
    client.update_transaction.assert_not_called()


def test_process_batch_dry_run_no_api_call() -> None:
    """Dry-run counts the enrichment but sends nothing to YNAB."""
    client = Mock()
    enriched, skipped, failed = process_batch(
        [_batch_txn("t1", -20000)],
        AmazonData.from_orders([_batch_order()]),
        MemoGenerator(),
        client,
        True,
    )

    assert enriched == 1
    client.update_transaction.assert_not_called()


def test_process_batch_counts_failure() -> None:
    """A failed update is counted, not raised."""
    from ynab_amazon_categorizer.exceptions import YNABAPIError

    client = Mock()
    client.update_transaction.side_effect = YNABAPIError("boom", status_code=500)
    enriched, skipped, failed = process_batch(
        [_batch_txn("t1", -20000)],
        AmazonData.from_orders([_batch_order()]),
        MemoGenerator(),
        client,
    )

    assert (enriched, skipped, failed) == (0, 0, 1)


# --- prompt_for_category_selection: double-Enter to cancel ---


def test_category_selection_single_empty_enter_reprompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single blank Enter does not cancel — it re-prompts, and a category
    typed afterward is still accepted."""
    responses = iter(["", "cat one"])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt", lambda *a, **k: next(responses)
    )
    completer = Mock()
    completer.category_list = [("Cat One", "cat1")]

    result = prompt_for_category_selection(completer, {"cat one": "cat1"})

    assert result == ("cat1", "Cat One")


def test_category_selection_two_empty_enters_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive blank Enters cancel (returns None, None)."""
    responses = iter(["", ""])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt", lambda *a, **k: next(responses)
    )

    result = prompt_for_category_selection(Mock(), {})

    assert result == (None, None)


def test_category_selection_b_cancels_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typing 'b' cancels on the first try — no double-confirmation needed
    for an explicit 'go back' command, unlike a blank Enter."""
    monkeypatch.setattr("ynab_amazon_categorizer.cli.prompt", lambda *a, **k: "b")

    result = prompt_for_category_selection(Mock(), {})

    assert result == (None, None)


def test_category_selection_empty_streak_resets_on_typed_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid (non-empty) entry between two blank Enters resets the
    streak — it takes two *consecutive* blanks to cancel, not two total."""
    responses = iter(["", "not-a-real-category", "", "cat one"])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt", lambda *a, **k: next(responses)
    )
    completer = Mock()
    completer.category_list = [("Cat One", "cat1")]

    result = prompt_for_category_selection(completer, {"cat one": "cat1"})

    assert result == ("cat1", "Cat One")


# --- main(): Ctrl+C handling ---


def test_main_wraps_keyboard_interrupt_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A KeyboardInterrupt from the workflow becomes exit code 130."""
    monkeypatch.setattr(cli_module, "_run", Mock(side_effect=KeyboardInterrupt))

    assert main([]) == 130
    captured = capsys.readouterr().out
    assert "Operation cancelled" in captured


def test_main_does_not_swallow_normal_quit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal sys.exit(0) from the 'q' quit path is not KeyboardInterrupt
    and must propagate through main() unmodified."""
    monkeypatch.setattr(cli_module, "_run", Mock(side_effect=SystemExit(0)))

    with pytest.raises(SystemExit) as exc_info:
        main([])

    assert exc_info.value.code == 0


# --- _handle_categorize: OSError handling on update ---


def test_handle_categorize_catches_oserror_on_update(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A raw OSError during the API update (e.g. the TLS/cert failure seen in
    practice — requests raises this directly, not as a RequestException) is
    caught and reported like other API errors, instead of crashing the whole
    session and losing the in-progress categorization."""
    transaction = {
        "id": "t1",
        "account_id": "a1",
        "date": "2025-01-15",
        "amount": -15000,
    }
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt_for_category_selection",
        lambda *a, **k: ("cat1", "Cat One"),
    )
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.get_multiline_input_with_custom_submit",
        lambda *a, **k: "",
    )
    # "Split this transaction?" -> n, "Enter item details manually?" -> n,
    # "Confirm update?" -> y
    responses = iter(["n", "n", "y"])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: next(responses)
    )
    ynab_client = Mock()
    ynab_client.update_transaction.side_effect = OSError(
        "Could not find a suitable TLS CA certificate bundle, invalid path: x"
    )

    result = _handle_categorize(
        transaction,
        None,
        "",
        MemoGenerator(),
        ynab_client,
        Mock(),
        {},
        {},
        dry_run=False,
    )

    assert result == "continue"
    captured = capsys.readouterr().out
    assert "Update failed" in captured
    assert "TLS CA certificate" in captured


# --- handle_split: base-price + auto tax calculation ---


def test_handle_split_applies_default_tax_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare number entered for a split amount is treated as a pre-tax base
    price; the default 9% tax rate is computed and added automatically."""
    order = _split_order()
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt_for_category_selection",
        lambda *a, **k: ("cat1", "Household"),
    )
    # base price "10" (+9% tax = $10.90, matching the transaction exactly),
    # then "use suggested memo?" -> y
    responses = iter(["10", ""])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: next(responses)
    )

    result = handle_split(
        {"amount": -10900}, order, MemoGenerator("amazon.com"), Mock(), {}
    )

    assert result is not None
    assert len(result) == 1
    assert result[0]["amount"] == -10900


def test_handle_split_applies_grocery_tax_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A category name containing 'grocery'/'groceries' uses the reduced
    4.5% rate instead of the 9% default."""
    order = _split_order()
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt_for_category_selection",
        lambda *a, **k: ("cat1", "Food: Groceries"),
    )
    # base price "10" (+4.5% tax = $10.45, matching the transaction exactly)
    responses = iter(["10", ""])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: next(responses)
    )

    result = handle_split(
        {"amount": -10450}, order, MemoGenerator("amazon.com"), Mock(), {}
    )

    assert result is not None
    assert result[0]["amount"] == -10450


def test_handle_split_exact_override_bypasses_tax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefixing the amount with '=' enters an exact total, with no tax
    calculation applied — for tax-exempt items, gift cards, etc."""
    order = _split_order()
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt_for_category_selection",
        lambda *a, **k: ("cat1", "Household"),
    )
    responses = iter(["=12.34", ""])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: next(responses)
    )

    result = handle_split(
        {"amount": -12340}, order, MemoGenerator("amazon.com"), Mock(), {}
    )

    assert result is not None
    assert result[0]["amount"] == -12340


def test_handle_split_blank_uses_remaining_balance_as_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank entry uses the full remaining balance as-is (no tax added) —
    e.g. for a final catch-all split."""
    order = _split_order()
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt_for_category_selection",
        lambda *a, **k: ("cat1", "Household"),
    )
    responses = iter(["", ""])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: next(responses)
    )

    result = handle_split(
        {"amount": -20000}, order, MemoGenerator("amazon.com"), Mock(), {}
    )

    assert result is not None
    assert result[0]["amount"] == -20000


# --- Tax rate: env var overrides ---


def test_tax_rate_default_and_grocery_categories() -> None:
    """Default 9% rate applies normally; a grocery-keyword category name
    uses the reduced 4.5% rate."""
    assert _tax_rate_for_category("Household: Supplies") == 0.09
    assert _tax_rate_for_category("Food: Groceries") == 0.045
    assert _tax_rate_for_category("Groceries") == 0.045
    assert _tax_rate_for_category(None) == 0.09


def test_tax_rate_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """YNAB_DEFAULT_TAX_RATE / YNAB_GROCERY_TAX_RATE override the built-in
    defaults, read at call-time (so values loaded from .env after this
    module is imported are still picked up)."""
    monkeypatch.setenv("YNAB_DEFAULT_TAX_RATE", "0.0825")
    monkeypatch.setenv("YNAB_GROCERY_TAX_RATE", "0.02")

    assert _tax_rate_for_category("Household: Supplies") == 0.0825
    assert _tax_rate_for_category("Groceries") == 0.02


def test_tax_rate_env_override_invalid_value_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric env override is ignored (with a warning), falling back
    to the built-in default rather than crashing."""
    monkeypatch.setenv("YNAB_DEFAULT_TAX_RATE", "not-a-number")

    assert _tax_rate_for_category("Household: Supplies") == 0.09


# --- _env_flag / YNAB_SKIP_SPLIT_PROMPT_SINGLE_ITEM ---


def test_env_flag_recognizes_common_truthy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_env_flag accepts 1/true/yes/y case-insensitively; anything else, or
    unset, is falsy."""
    for value in ["1", "true", "TRUE", "yes", "y", "Y"]:
        monkeypatch.setenv("_TEST_FLAG", value)
        assert _env_flag("_TEST_FLAG") is True
    for value in ["0", "false", "no", "", "maybe"]:
        monkeypatch.setenv("_TEST_FLAG", value)
        assert _env_flag("_TEST_FLAG") is False
    monkeypatch.delenv("_TEST_FLAG", raising=False)
    assert _env_flag("_TEST_FLAG") is False
    assert _env_flag("_TEST_FLAG", default=True) is True


def test_skip_split_prompt_single_item_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With YNAB_SKIP_SPLIT_PROMPT_SINGLE_ITEM set and nothing to split
    (matching_order is None here), the 'Split this transaction?' prompt is
    skipped entirely — only one _prompt_line response is needed, not two."""
    transaction = {
        "id": "t1",
        "account_id": "a1",
        "date": "2025-01-15",
        "amount": -15000,
    }
    monkeypatch.setenv("YNAB_SKIP_SPLIT_PROMPT_SINGLE_ITEM", "true")
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt_for_category_selection",
        lambda *a, **k: ("cat1", "Cat One"),
    )
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.get_multiline_input_with_custom_submit",
        lambda *a, **k: "",
    )
    # Only "Enter item details manually?" -> n; no split-decision response.
    responses = iter(["n"])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: next(responses)
    )

    result = _handle_categorize(
        transaction, None, "", MemoGenerator(), Mock(), Mock(), {}, {}, dry_run=True
    )

    assert result == "done"


def test_split_prompt_still_asked_without_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the env var set, the split prompt is still asked even when
    there's nothing to split — confirms the skip is opt-in, not a silent
    default-behavior change."""
    transaction = {
        "id": "t1",
        "account_id": "a1",
        "date": "2025-01-15",
        "amount": -15000,
    }
    monkeypatch.delenv("YNAB_SKIP_SPLIT_PROMPT_SINGLE_ITEM", raising=False)
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.prompt_for_category_selection",
        lambda *a, **k: ("cat1", "Cat One"),
    )
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli.get_multiline_input_with_custom_submit",
        lambda *a, **k: "",
    )
    # Both responses required: "Split this transaction?" -> n, then
    # "Enter item details manually?" -> n.
    responses = iter(["n", "n"])
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: next(responses)
    )

    result = _handle_categorize(
        transaction, None, "", MemoGenerator(), Mock(), Mock(), {}, {}, dry_run=True
    )

    assert result == "done"


# --- process_transaction: auto-skip when order data doesn't match ---


def test_process_transaction_auto_skips_unmatched_when_orders_provided(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When order data WAS provided this run but nothing matches this
    specific transaction's amount, it's skipped automatically (no prompt)
    rather than asking the user to categorize blind — and the skip is
    counted in stats for the end-of-run summary."""
    transaction = {
        "id": "t1",
        "date": "2024-01-01",
        "payee_name": "Amazon",
        "amount": -12340,  # does not match the $20.00 order below
        "memo": "",
    }
    stats: dict[str, int] = {}

    result = process_transaction(
        transaction,
        0,
        1,
        AmazonData.from_orders([_amount_matched_order()]),
        MemoGenerator(),
        Mock(),
        Mock(),
        {},
        {},
        set(),
        False,
        stats,
    )

    captured = capsys.readouterr().out
    assert result is True
    assert "No matching order found" in captured
    assert stats["auto_skipped_no_match"] == 1


def test_process_transaction_no_orders_provided_still_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no order data was provided at all this run (parsed_orders is
    None), the normal action prompt still fires — auto-skip only applies
    when order data exists but doesn't cover this specific transaction."""
    transaction = {
        "id": "t1",
        "date": "2024-01-01",
        "payee_name": "Amazon",
        "amount": -12340,
        "memo": "",
    }
    responses = iter(["s"])  # skip via the normal action prompt
    monkeypatch.setattr(
        "ynab_amazon_categorizer.cli._prompt_line", lambda _prompt: next(responses)
    )

    result = process_transaction(
        transaction, 0, 1, None, MemoGenerator(), Mock(), Mock(), {}, {}
    )

    assert result is True


def test_process_batch_preserves_existing_memo() -> None:
    """The batch update sent to YNAB retains pre-existing memo content."""
    client = Mock()
    transaction = _batch_txn("t1", -20000)
    transaction["memo"] = "Imported reference 123"

    result = process_batch(
        [transaction],
        AmazonData.from_orders([_batch_order()]),
        MemoGenerator(),
        client,
    )

    assert result == (1, 0, 0)
    payload = client.update_transaction.call_args.args[1]
    assert payload["memo"].startswith("Imported reference 123")
    assert set(payload) == {"memo", "approved"}
    assert payload["approved"] is False


def test_process_batch_skips_already_enriched_memo_and_consumes_order() -> None:
    """An idempotent rerun sends no update or reuses the matched order."""
    client = Mock()
    order = _batch_order()
    transaction = _batch_txn("t1", -20000)
    transaction["memo"] = build_batch_memo(order, MemoGenerator())

    result = process_batch(
        [transaction, _batch_txn("t2", -20000)],
        AmazonData.from_orders([order]),
        MemoGenerator(),
        client,
    )

    assert result == (0, 2, 0)
    client.update_transaction.assert_not_called()


def test_process_batch_skips_when_existing_memo_cannot_be_preserved() -> None:
    """Batch mode skips rather than truncating a nearly-full existing memo."""
    client = Mock()
    transaction = _batch_txn("t1", -20000)
    transaction["memo"] = "X" * 195

    result = process_batch(
        [transaction],
        AmazonData.from_orders([_batch_order()]),
        MemoGenerator(),
        client,
    )

    assert result == (0, 1, 0)
    client.update_transaction.assert_not_called()


def test_process_batch_oversized_memo_consumes_matched_order() -> None:
    """A too-long memo cannot make its order available to a later transaction."""
    client = Mock()
    transaction = _batch_txn("t1", -20000)
    transaction["memo"] = "X" * 195

    result = process_batch(
        [transaction, _batch_txn("t2", -20000)],
        AmazonData.from_orders([_batch_order()]),
        MemoGenerator(),
        client,
    )

    assert result == (0, 2, 0)
    client.update_transaction.assert_not_called()


def test_main_batch_dry_run_smoke(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The top-level batch dry-run completes without sending an update."""
    config = Config("secret", "budget")
    client = Mock()
    client.get_categories.return_value = (
        [("Needs: Household", "cat1")],
        {"needs: household": "cat1"},
        {"cat1": "Needs: Household"},
    )
    transaction = _batch_txn("t1", -20000)

    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(cli_module, "YNABClient", lambda *_args: client)
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "y")
    monkeypatch.setattr(
        cli_module,
        "prompt_for_amazon_data",
        lambda *_args, **_kwargs: AmazonData.from_orders([_batch_order()]),
    )
    monkeypatch.setattr(
        cli_module,
        "fetch_amazon_transactions",
        lambda *_args, **_kwargs: [transaction],
    )

    exit_code = cli_module.main(["--batch", "--dry-run"])

    client.update_transaction.assert_not_called()
    assert exit_code == 0
    assert "Batch complete: 1 enriched" in capsys.readouterr().out


def test_main_exits_before_order_prompt_when_no_transactions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With zero uncategorized Amazon transactions, the run ends right after
    the transaction fetch — before fetching categories and, most importantly,
    before asking the user to paste Amazon order data there is nothing to
    match against."""
    config = Config("secret", "budget")
    client = Mock()
    client.get_categories.side_effect = AssertionError(
        "categories must not be fetched when there are no transactions"
    )

    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(cli_module, "YNABClient", lambda *_args: client)
    monkeypatch.setattr(
        cli_module,
        "_prompt_line",
        Mock(side_effect=AssertionError("no prompt should fire without transactions")),
    )
    monkeypatch.setattr(
        cli_module, "fetch_amazon_transactions", lambda *_args, **_kwargs: []
    )

    exit_code = cli_module.main([])

    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "nothing to do" in captured
    assert "Amazon Orders Data" not in captured


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt(), EOFError()])
def test_main_handles_terminal_interruption(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interrupt: BaseException,
) -> None:
    """Ctrl+C and EOF exit cleanly without exposing a traceback."""
    config = Config("secret", "budget")
    client = Mock()
    client.get_categories.return_value = (
        [("Needs: Household", "cat1")],
        {"needs: household": "cat1"},
        {"cat1": "Needs: Household"},
    )

    monkeypatch.setattr(Config, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(cli_module, "YNABClient", lambda *_args: client)
    # Transactions are fetched before the first prompt now; one must exist
    # for the run to reach the interactive prompts at all.
    monkeypatch.setattr(
        cli_module,
        "fetch_amazon_transactions",
        lambda *_args, **_kwargs: [_batch_txn("t1", -20000)],
    )

    def interrupt_prompt(_message: str) -> str:
        raise interrupt

    monkeypatch.setattr(cli_module, "_prompt_line", interrupt_prompt)
    # The paste box is the first prompt now; submit empty so the run reaches
    # the per-transaction prompts this test interrupts.
    monkeypatch.setattr(
        cli_module, "get_multiline_input_with_custom_submit", lambda _prompt: ""
    )

    exit_code = cli_module.main([])

    assert exit_code == 130
    assert "Operation cancelled" in capsys.readouterr().out


# --- charge-aware matching through the CLI ---------------------------------


def _charge_txn(
    txn_id: str = "t1", amount: int = -115220, date: str = "2026-08-16"
) -> dict:
    return {
        "id": txn_id,
        "account_id": "a1",
        "date": date,
        "amount": amount,
        "payee_name": "Amazon",
        "category_id": None,
        "approved": False,
        "memo": "",
    }


def _shipment_data() -> AmazonData:
    """One $157.90 order billed as two separate card charges."""
    detailed_items = [
        OrderItem("Caribou Coffee K-Cup Pods", 19.99),
        OrderItem("Large Ceramic Coffee Mug Set", 22.69),
    ]
    items, item_prices = expand_items(detailed_items, 10)
    order = Order(
        order_id="114-1234567-1234567",
        total=157.90,
        date_str="August 13, 2026",
        items=items,
        currency="$",
        detailed_items=detailed_items,
        tax=11.52,
        item_prices=item_prices,
    )
    charges = [
        AmazonCharge(
            amount=-115.22,
            date_str="August 15, 2026",
            order_id="114-1234567-1234567",
            payment_method="Prime Visa ****1234",
            currency="$",
        ),
        AmazonCharge(
            amount=-42.68,
            date_str="August 15, 2026",
            order_id="114-1234567-1234567",
            payment_method="Prime Visa ****1234",
            currency="$",
        ),
    ]
    return AmazonData(orders=[order], charges=charges)


def test_display_matched_order_flags_a_partial_charge(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A per-shipment charge must not read as if it covered the whole order."""
    matcher = TransactionMatcher()
    order = matcher.resolve_order(-115.22, "2026-08-16", _shipment_data())

    assert order is not None
    display_matched_order(order, MemoGenerator("amazon.com"))

    captured = capsys.readouterr().out
    assert "Charge: -$115.22" in captured
    assert "Prime Visa ****1234" in captured
    assert "covers PART of the order" in captured
    assert "Total: $157.90" in captured


def test_display_matched_order_shows_item_prices(
    capsys: pytest.CaptureFixture[str],
) -> None:
    order = _shipment_data().orders[0]

    display_matched_order(order, MemoGenerator("amazon.com"))

    captured = capsys.readouterr().out
    assert "Caribou Coffee K-Cup Pods — $19.99" in captured
    assert "Order tax: $11.52" in captured


def test_display_matched_order_labels_a_refund(
    capsys: pytest.CaptureFixture[str],
) -> None:
    order = Order(
        order_id="114-3456789-3456789",
        currency="$",
        matched_charge=AmazonCharge(
            amount=19.36,
            date_str="August 8, 2026",
            order_id="114-3456789-3456789",
            is_refund=True,
            currency="$",
        ),
    )

    display_matched_order(order, MemoGenerator("amazon.com"))

    captured = capsys.readouterr().out
    assert "Refund: $19.36" in captured
    assert "Items: none parsed" in captured


def test_process_transaction_matches_a_charge_the_orders_page_cannot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The regression this feature exists for: -115.22 vs a $157.90 order."""
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "s")
    stats: dict[str, int] = {}

    result = process_transaction(
        _charge_txn(),
        0,
        1,
        _shipment_data(),
        MemoGenerator("amazon.com"),
        Mock(),
        Mock(),
        {},
        {},
        set(),
        False,
        stats,
    )

    captured = capsys.readouterr().out
    assert result is True
    assert "MATCHED ORDER FOUND" in captured
    assert "114-1234567-1234567" in captured
    assert stats.get("auto_skipped_no_match", 0) == 0


def test_mark_match_used_retires_the_charge_not_the_order() -> None:
    """Retiring the order would strand the order's other shipment charge."""
    matcher = TransactionMatcher()
    data = _shipment_data()
    used_orders: set[str] = set()
    used_charges: set[tuple[str, str, str]] = set()

    first = matcher.resolve_order(
        -115.22, "2026-08-16", data, used_orders, used_charges
    )
    assert first is not None
    mark_match_used(first, used_orders, used_charges)

    assert used_orders == set()
    assert len(used_charges) == 1

    second = matcher.resolve_order(
        -42.68, "2026-08-16", data, used_orders, used_charges
    )
    assert second is not None
    assert second.matched_charge is not None
    assert second.matched_charge.amount == -42.68


def test_mark_match_used_retires_the_order_for_a_total_match() -> None:
    used_orders: set[str] = set()
    used_charges: set[tuple[str, str, str]] = set()

    mark_match_used(_batch_order(), used_orders, used_charges)

    assert used_orders == {"702-1234567-7654321"}
    assert used_charges == set()


def test_process_batch_enriches_a_partial_shipment_charge() -> None:
    """Batch mode picks up the charge-only matches too."""
    client = Mock()

    enriched, skipped, failed = process_batch(
        [_charge_txn()],
        _shipment_data(),
        MemoGenerator("amazon.com"),
        client,
    )

    assert (enriched, skipped, failed) == (1, 0, 0)
    _txn_id, payload = client.update_transaction.call_args[0]
    assert "114-1234567-1234567" in payload["memo"]
    assert "part of order" in payload["memo"]


def test_process_batch_enriches_both_charges_of_one_order() -> None:
    """Both transactions of a two-shipment order get enriched, not just one."""
    client = Mock()

    enriched, skipped, failed = process_batch(
        [_charge_txn("t1", -115220), _charge_txn("t2", -42680)],
        _shipment_data(),
        MemoGenerator("amazon.com"),
        client,
    )

    assert (enriched, skipped, failed) == (2, 0, 0)
    assert client.update_transaction.call_count == 2


def test_process_batch_enriches_a_charge_for_an_unknown_order() -> None:
    """An order link alone is still worth writing when no page described it."""
    client = Mock()
    data = AmazonData(
        charges=[
            AmazonCharge(
                amount=-16.42,
                date_str="August 7, 2026",
                order_id="114-4567890-4567890",
                currency="$",
            )
        ]
    )

    enriched, _skipped, _failed = process_batch(
        [_charge_txn("t1", -16420, "2026-08-09")],
        data,
        MemoGenerator("amazon.com"),
        client,
    )

    assert enriched == 1
    _txn_id, payload = client.update_transaction.call_args[0]
    assert "114-4567890-4567890" in payload["memo"]


def test_prompt_for_order_details_fills_in_a_missing_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pasting one details page turns a bare order ID into a real item list."""
    data = AmazonData(
        charges=[
            AmazonCharge(
                amount=-16.42,
                date_str="August 7, 2026",
                order_id="114-4567890-4567890",
                currency="$",
            )
        ]
    )
    details = """
Order placed August 7, 2026  Order # 114-4567890-4567890
Order Summary
Grand Total:
$16.42
A Perfectly Ordinary Product Name Here
A Perfectly Ordinary Product Name Here
Sold by: Amazon.com
$15.19
"""
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "y")
    monkeypatch.setattr(
        cli_module, "get_multiline_input_with_custom_submit", lambda _prompt: details
    )

    order = cli_module.prompt_for_order_details(
        data, "114-4567890-4567890", MemoGenerator("amazon.com")
    )

    assert order is not None
    assert order.items == ["A Perfectly Ordinary Product Name Here"]
    assert data.unknown_charge_order_ids() == []


def test_prompt_for_order_details_declined_leaves_data_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = AmazonData(charges=[AmazonCharge(amount=-16.42, order_id="114-0-0")])
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "n")

    assert cli_module.prompt_for_order_details(data, "114-0-0", MemoGenerator()) is None
    assert data.orders == []


def test_prompt_for_order_details_rejects_a_page_for_another_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Pasting the wrong order's page must not be silently attached."""
    data = AmazonData()
    other = """
Order placed August 7, 2026  Order # 114-9999999-9999999
Order Summary
Grand Total:
$16.42
A Perfectly Ordinary Product Name Here
A Perfectly Ordinary Product Name Here
Sold by: Amazon.com
$15.19
"""
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "y")
    monkeypatch.setattr(
        cli_module, "get_multiline_input_with_custom_submit", lambda _prompt: other
    )

    result = cli_module.prompt_for_order_details(
        data, "114-4567890-4567890", MemoGenerator("amazon.com")
    )

    assert result is None
    assert "not 114-4567890-4567890" in capsys.readouterr().out


def test_absorb_amazon_page_routes_each_page_kind() -> None:
    data = AmazonData()
    parser = AmazonParser()

    assert (
        absorb_amazon_page(
            data,
            """
ORDER PLACED
August 13, 2026
TOTAL
$5.18
ORDER # 114-8901234-8901234
 Amazon Basics Low-Odor Dry Erase Whiteboard Markers, 4-Pack
""",
            parser,
        )
        == "orders"
    )
    assert (
        absorb_amazon_page(
            data,
            """
August 15, 2026
Prime Visa ****1234-$5.18
Order #114-8901234-8901234
AMZN Mktp US
Prime Visa ****1234-$42.68
Order #114-1234567-1234567
AMZN Mktp US
""",
            parser,
        )
        == "transactions"
    )
    assert absorb_amazon_page(data, "unrelated notes", parser) == "unknown"

    assert len(data.orders) == 1
    assert len(data.charges) == 2


def test_generate_split_summary_memo_marks_a_partial_charge() -> None:
    matcher = TransactionMatcher()
    order = matcher.resolve_order(-115.22, "2026-08-16", _shipment_data())

    assert order is not None
    assert "part of order" in generate_split_summary_memo(order)


def test_generate_split_summary_memo_unmarked_for_a_whole_order() -> None:
    assert "part of" not in generate_split_summary_memo(_shipment_data().orders[0])


# --- coverage reporting in the paste loop -----------------------------------


def test_prompt_for_amazon_data_reports_coverage_after_each_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fetching transactions first lets the paste loop name what is missing."""
    transactions_page = """
August 15, 2026
Prime Visa ****1234-$115.22
Order #114-1234567-1234567
AMZN Mktp US
Prime Visa ****1234-$16.42
Order #114-4567890-4567890
Amazon.com
"""
    pages = [transactions_page, ""]
    monkeypatch.setattr(
        cli_module,
        "get_multiline_input_with_custom_submit",
        lambda _prompt: pages.pop(0),
    )
    pending = [_charge_txn("t1", -115220), _charge_txn("t2", -16420, "2026-08-16")]

    cli_module.prompt_for_amazon_data(pending, MemoGenerator("amazon.com"))

    captured = capsys.readouterr().out
    assert "Coverage: 0 of 2 transaction(s) matched with item details." in captured
    assert "2 matched an order with no item data." in captured
    # The orders worth fetching are named, with a clickable link.
    assert "orderID=114-1234567-1234567" in captured
    assert "orderID=114-4567890-4567890" in captured


def test_prompt_for_amazon_data_suggests_the_transactions_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unmatched transaction with no charges on hand points at page 3."""
    orders_page = """
ORDER PLACED
August 13, 2026
TOTAL
$5.18
ORDER # 114-8901234-8901234
 Amazon Basics Low-Odor Dry Erase Whiteboard Markers, 4-Pack
"""
    pages = [orders_page, ""]
    monkeypatch.setattr(
        cli_module,
        "get_multiline_input_with_custom_submit",
        lambda _prompt: pages.pop(0),
    )

    cli_module.prompt_for_amazon_data(
        [_charge_txn("t1", -115220)], MemoGenerator("amazon.com")
    )

    captured = capsys.readouterr().out
    assert "1 with no matching order yet." in captured
    assert "Paste the Your Transactions page" in captured


def test_prompt_for_amazon_data_confirms_full_coverage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    orders_page = """
ORDER PLACED
August 13, 2026
TOTAL
$115.22
ORDER # 114-8901234-8901234
 Amazon Basics Low-Odor Dry Erase Whiteboard Markers, 4-Pack
"""
    pages = [orders_page, ""]
    monkeypatch.setattr(
        cli_module,
        "get_multiline_input_with_custom_submit",
        lambda _prompt: pages.pop(0),
    )
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "")

    cli_module.prompt_for_amazon_data(
        [_charge_txn("t1", -115220)], MemoGenerator("amazon.com")
    )

    captured = capsys.readouterr().out
    assert "Coverage: 1 of 1 transaction(s) matched with item details." in captured
    assert "Every transaction has an order and its items." in captured


def test_prompt_for_amazon_data_stays_quiet_without_transactions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Callers that pass no transactions get the old, unannotated flow."""
    pages = ["unrelated notes", ""]
    monkeypatch.setattr(
        cli_module,
        "get_multiline_input_with_custom_submit",
        lambda _prompt: pages.pop(0),
    )

    cli_module.prompt_for_amazon_data()

    assert "Coverage:" not in capsys.readouterr().out


def test_prompt_for_amazon_data_stops_asking_once_covered(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Full coverage must end the loop instead of presenting another paste box."""
    orders_page = """
ORDER PLACED
August 13, 2026
TOTAL
$115.22
ORDER # 114-8901234-8901234
 Amazon Basics Low-Odor Dry Erase Whiteboard Markers, 4-Pack
"""
    pages = [orders_page]  # a second read would raise IndexError
    asked: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "get_multiline_input_with_custom_submit",
        lambda _prompt: pages.pop(0),
    )
    monkeypatch.setattr(
        cli_module, "_prompt_line", lambda message: (asked.append(message), "")[1]
    )

    cli_module.prompt_for_amazon_data(
        [_charge_txn("t1", -115220)], MemoGenerator("amazon.com")
    )

    assert any("Paste more pages anyway?" in message for message in asked)
    assert "Paste page 2" not in capsys.readouterr().out


def test_prompt_for_amazon_data_keeps_going_when_asked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Answering yes reopens the paste box for optional extra pages."""
    orders_page = """
ORDER PLACED
August 13, 2026
TOTAL
$115.22
ORDER # 114-8901234-8901234
 Amazon Basics Low-Odor Dry Erase Whiteboard Markers, 4-Pack
"""
    pages = [orders_page, ""]
    monkeypatch.setattr(
        cli_module,
        "get_multiline_input_with_custom_submit",
        lambda _prompt: pages.pop(0),
    )
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "y")

    cli_module.prompt_for_amazon_data(
        [_charge_txn("t1", -115220)], MemoGenerator("amazon.com")
    )

    assert "Paste page 2" in capsys.readouterr().out


def test_prompt_for_amazon_data_flags_missing_item_prices(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covered-but-unpriced is called out, since it still affects splitting."""
    orders_page = """
ORDER PLACED
August 13, 2026
TOTAL
$115.22
ORDER # 114-8901234-8901234
 Amazon Basics Low-Odor Dry Erase Whiteboard Markers, 4-Pack
 BIC Brite Liner Highlighters, Chisel Tip, 12-Count Pack, Assorted Colors
"""
    pages = [orders_page]
    monkeypatch.setattr(
        cli_module,
        "get_multiline_input_with_custom_submit",
        lambda _prompt: pages.pop(0),
    )
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "")

    cli_module.prompt_for_amazon_data(
        [_charge_txn("t1", -115220)], MemoGenerator("amazon.com")
    )

    captured = capsys.readouterr().out
    assert "have item names but no prices" in captured
    assert "Nothing further is needed." not in captured


def test_amazon_data_prompt_links_use_the_configured_storefront(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every link offered must point at the user's own Amazon domain."""
    pages = [""]
    monkeypatch.setattr(
        cli_module,
        "get_multiline_input_with_custom_submit",
        lambda _prompt: pages.pop(0),
    )

    cli_module.prompt_for_amazon_data([], MemoGenerator("amazon.co.uk"))

    captured = capsys.readouterr().out
    assert "https://www.amazon.co.uk/your-orders/orders" in captured
    assert "https://www.amazon.co.uk/cpe/yourpayments/transactions" in captured
    assert "amazon.com/" not in captured


def test_unmatched_coverage_links_the_transactions_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unmatched transaction gets a clickable route to the fix."""
    orders_page = """
ORDER PLACED
August 13, 2026
TOTAL
$5.18
ORDER # 114-8901234-8901234
 Amazon Basics Low-Odor Dry Erase Whiteboard Markers, 4-Pack
"""
    pages = [orders_page, ""]
    monkeypatch.setattr(
        cli_module,
        "get_multiline_input_with_custom_submit",
        lambda _prompt: pages.pop(0),
    )

    cli_module.prompt_for_amazon_data(
        [_charge_txn("t1", -115220)], MemoGenerator("amazon.com")
    )

    captured = capsys.readouterr().out
    assert "1 with no matching order yet." in captured
    assert "https://www.amazon.com/cpe/yourpayments/transactions" in captured


def test_process_transaction_links_transactions_page_when_unmatched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The auto-skip says which page would have resolved the transaction."""
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "s")

    process_transaction(
        _charge_txn("t1", -999990),
        0,
        1,
        AmazonData.from_orders([_batch_order()]),
        MemoGenerator("amazon.ca"),
        Mock(),
        Mock(),
        {},
        {},
        set(),
        False,
        {},
    )

    captured = capsys.readouterr().out
    assert "No matching order found" in captured
    assert "https://www.amazon.ca/cpe/yourpayments/transactions" in captured


def test_price_gap_prompt_links_the_orders_to_fetch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A count alone leaves the user hunting; link the order pages instead."""
    orders_page = """
ORDER PLACED
August 13, 2026
TOTAL
$115.22
ORDER # 114-8901234-8901234
 Amazon Basics Low-Odor Dry Erase Whiteboard Markers, 4-Pack
 BIC Brite Liner Highlighters, Chisel Tip, 12-Count Pack, Assorted Colors
"""
    pages = [orders_page]
    monkeypatch.setattr(
        cli_module,
        "get_multiline_input_with_custom_submit",
        lambda _prompt: pages.pop(0),
    )
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "")

    cli_module.prompt_for_amazon_data(
        [_charge_txn("t1", -115220)], MemoGenerator("amazon.ca")
    )

    captured = capsys.readouterr().out
    assert "have item names but no prices" in captured
    assert (
        "https://www.amazon.ca/gp/your-account/order-details"
        "?ie=UTF8&orderID=114-8901234-8901234" in captured
    )


# --- prices offered at the split, not up front -------------------------------


def _unpriced_multi_item_order() -> Order:
    """What the orders list page yields: item names, no prices."""
    return Order(
        order_id="114-8901234-8901234",
        total=20.59,
        date_str="August 13, 2026",
        items=["Widget A", "Widget B"],
        currency="$",
    )


def test_offer_prices_for_split_asks_only_when_prices_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "_prompt_line",
        lambda message: (asked.append(message), "n")[1],
    )

    order = _unpriced_multi_item_order()
    cli_module._offer_prices_for_split(order, AmazonData(), MemoGenerator())

    assert asked, "an unpriced order should prompt for its details page"


def test_offer_prices_for_split_stays_quiet_when_prices_are_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to gain, so nothing to ask."""

    def fail(_message: str) -> str:
        raise AssertionError("must not prompt when item prices are known")

    monkeypatch.setattr(cli_module, "_prompt_line", fail)
    priced = _shipment_data().orders[0]

    assert (
        cli_module._offer_prices_for_split(priced, AmazonData(), MemoGenerator())
        is priced
    )


def test_offer_prices_for_split_keeps_the_matched_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fetched details must not drop which charge this transaction is."""
    details = """
Order placed August 13, 2026  Order # 114-8901234-8901234
Order Summary
Grand Total:
$20.59
A Perfectly Ordinary Product Name Here
A Perfectly Ordinary Product Name Here
Sold by: Amazon.com
$18.99
"""
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "y")
    monkeypatch.setattr(
        cli_module, "get_multiline_input_with_custom_submit", lambda _prompt: details
    )
    charge = AmazonCharge(
        amount=-4.98, order_id="114-8901234-8901234", date_str="August 9, 2026"
    )
    order = dataclasses.replace(_unpriced_multi_item_order(), matched_charge=charge)
    data = AmazonData()

    improved = cli_module._offer_prices_for_split(order, data, MemoGenerator())

    assert improved is not None
    assert improved.has_item_prices
    assert improved.matched_charge is charge


def test_offer_prices_for_split_survives_a_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "n")
    order = _unpriced_multi_item_order()

    assert (
        cli_module._offer_prices_for_split(order, AmazonData(), MemoGenerator())
        is order
    )


def test_single_item_order_never_prompts_for_prices_up_front(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reported case: one item, so a price would change nothing."""
    orders_page = """
ORDER PLACED
August 13, 2026
TOTAL
$115.22
ORDER # 114-8901234-8901234
 Viva Naturals Omega 3 Fish Oil Supplement, 120 Pescatarian-Friendly Softgels
"""
    pages = [orders_page]
    monkeypatch.setattr(
        cli_module,
        "get_multiline_input_with_custom_submit",
        lambda _prompt: pages.pop(0),
    )
    monkeypatch.setattr(cli_module, "_prompt_line", lambda _message: "")

    cli_module.prompt_for_amazon_data(
        [_charge_txn("t1", -115220)], MemoGenerator("amazon.com")
    )

    captured = capsys.readouterr().out
    assert "have item names but no prices" not in captured
    assert "Nothing further is needed." in captured
