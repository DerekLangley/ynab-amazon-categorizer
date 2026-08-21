import argparse
import copy
import dataclasses
import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import requests
from prompt_toolkit import prompt
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent

from . import __version__
from .amazon_data import AmazonData
from .amazon_parser import AmazonParser, Order, PageKind, detect_page_kind
from .batch import process_batch
from .config import Config
from .exceptions import ConfigurationError, YNABAPIError
from .memo_generator import (
    MemoGenerator,
    generate_split_summary_memo,
    sanitize_memo,
)
from .models import SaveSubtransaction, TransactionUpdate, format_currency_amount
from .payloads import (
    build_single_payload,
    build_split_payload,
)
from .tax import tax_rate_for_category as _tax_rate_for_category
from .transaction_matcher import (
    CoverageSummary,
    TransactionMatcher,
    mark_match_used,
    summarize_coverage,
)
from .transactions import fetch_amazon_transactions
from .ynab_client import YNABClient

logger = logging.getLogger(__name__)


def _env_flag(var_name: str, default: bool = False) -> bool:
    """Read a boolean flag from the environment (e.g. a .env file).

    Truthy values: ``1``, ``true``, ``yes``, ``y`` (case-insensitive).
    Read at call-time so values loaded from ``.env`` during startup are used.
    """
    raw = os.getenv(var_name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y")


def _print_amazon_data_instructions() -> None:
    """Explain which Amazon pages help and what each one contributes."""
    print("\n--- Amazon Data Entry ---")
    print("Paste any of these pages; the tool works out which is which.")
    print("  1. Your Orders          - order totals and item names")
    print("     https://www.amazon.com/your-orders/orders")
    print("  2. Order details        - every item with its price, plus tax")
    print("     (open an order and choose 'View order details')")
    print("  3. Your Transactions    - which card charge paid for which order")
    print("     https://www.amazon.com/cpe/yourpayments/transactions")
    print(
        "\nPage 3 is what resolves transactions the orders page alone cannot: an\n"
        "order billed once per shipment, one split with a gift card or points,\n"
        "or a refund. Page 2 fills in items the orders page truncates."
    )


def absorb_amazon_page(
    amazon_data: AmazonData, page_text: str, parser: AmazonParser
) -> PageKind:
    """Parse one pasted page into ``amazon_data`` and report what it was.

    Returns the detected page kind so the caller can tell the user what landed
    (and say something useful when nothing did).
    """
    kind = detect_page_kind(page_text)

    if kind == "transactions":
        added = amazon_data.add_charges(parser.parse_transactions_page(page_text))
        print(f"✓ Transactions page: {added} new charge(s) linked to orders.")
    elif kind == "details":
        orders = parser.parse_order_details_page(page_text)
        added, merged = amazon_data.add_orders(orders)
        priced = sum(1 for order in orders if order.has_item_prices)
        print(
            f"✓ Order details: {len(orders)} order(s) "
            f"({added} new, {merged} updated, {priced} with item prices)."
        )
    elif kind == "orders":
        orders = parser.parse_orders_page(page_text)
        added, merged = amazon_data.add_orders(orders)
        print(f"✓ Orders page: {len(orders)} order(s) ({added} new, {merged} updated).")
    else:
        print(
            "⚠ Could not tell which Amazon page that was, so nothing was read.\n"
            "  Copy the whole page (Select All) from one of the three URLs above."
        )

    return kind


# How many order IDs to name before collapsing the rest into a count.
MAX_LISTED_ORDER_IDS = 8


def _print_coverage(summary: CoverageSummary, memo_generator: MemoGenerator) -> None:
    """Report which pending transactions the pasted pages can already explain.

    Because transactions are fetched before this prompt, the tool knows exactly
    which orders are still worth fetching and can name them, instead of leaving
    the user to discover the gaps one transaction at a time.
    """
    if not summary.total:
        return

    print(
        f"\n  Coverage: {summary.described} of {summary.total} "
        "transaction(s) matched with item details."
    )

    if summary.unmatched:
        print(f"    • {summary.unmatched} with no matching order yet.")
    if summary.without_items:
        print(f"    • {summary.without_items} matched an order with no item data.")

    for order_id in summary.orders_needing_details[:MAX_LISTED_ORDER_IDS]:
        link = memo_generator.generate_amazon_order_link(order_id)
        print(f"        {link or order_id}")
    remaining = len(summary.orders_needing_details) - MAX_LISTED_ORDER_IDS
    if remaining > 0:
        print(f"        ... and {remaining} more order(s)")


def _confirm_more_pages(summary: CoverageSummary) -> bool:
    """Ask whether to keep pasting once every transaction is already covered.

    Without this the loop kept presenting a paste box after there was nothing
    left to collect. Answering is one keystroke, and the default ends the loop,
    so the common case is just Enter.
    """
    print("  ✓ Every transaction has an order and its items.")
    if summary.without_prices:
        # Complete coverage is not the same as complete *pricing*: an order
        # matched from the orders list page has item names but no prices.
        print(
            f"  {summary.without_prices} transaction(s) have item names but no "
            "prices — their\n    order details pages would make splits exact. "
            "Optional."
        )
    else:
        print("  Nothing further is needed.")

    answer = _prompt_line("Paste more pages anyway? (y/n, default n): ")
    return answer.strip().lower() == "y"


def _print_coverage_advice(summary: CoverageSummary, amazon_data: AmazonData) -> None:
    """Suggest the page most likely to close the remaining gap."""
    if summary.is_complete:
        return
    if summary.unmatched and not amazon_data.charges:
        print(
            "  → Paste the Your Transactions page: it maps each card charge to "
            "its\n    order, which is what unmatched transactions usually need."
        )
    if summary.orders_needing_details:
        print(
            "  → Paste the order details page for the order(s) listed above to "
            "get\n    their items and prices."
        )


def _print_amazon_data_summary(amazon_data: AmazonData) -> None:
    """Summarize what was gathered."""
    if not amazon_data:
        print("\nNo Amazon data provided.")
        return

    print(
        f"\n✓ Amazon data: {len(amazon_data.orders)} order(s), "
        f"{len(amazon_data.charges)} charge(s)."
    )
    for order in amazon_data.orders[:3]:
        print(
            f"  - Order {order.order_id}: "
            f"{format_currency_amount(order.total, order.currency)} on "
            f"{order.date_str} ({len(order.items)} items)"
        )
    if len(amazon_data.orders) > 3:
        print(f"  ... and {len(amazon_data.orders) - 3} more orders")


def prompt_for_amazon_data(
    transactions: Sequence[Mapping[str, Any]] | None = None,
    memo_generator: MemoGenerator | None = None,
) -> AmazonData:
    """Collect any number of Amazon pages, in any order, into one dataset.

    When the pending ``transactions`` are supplied, each paste is followed by a
    coverage report naming the orders still missing item data, so the user can
    fetch exactly those pages before moving on.
    """
    _print_amazon_data_instructions()

    parser = AmazonParser()
    amazon_data = AmazonData()
    pending = list(transactions or [])
    links = memo_generator or MemoGenerator()
    page_number = 1

    while True:
        print(f"\nPaste page {page_number} (or submit empty / 'done' to continue):")
        page_text = get_multiline_input_with_custom_submit("Paste here: ")

        if page_text is None or page_text.strip().lower() in ("", "done", "skip"):
            break

        absorb_amazon_page(amazon_data, page_text, parser)
        page_number += 1

        if pending:
            summary = summarize_coverage(pending, amazon_data)
            _print_coverage(summary, links)
            _print_coverage_advice(summary, amazon_data)
            if summary.is_complete and not _confirm_more_pages(summary):
                break

    _print_amazon_data_summary(amazon_data)
    if pending and amazon_data:
        _print_coverage(summarize_coverage(pending, amazon_data), links)
    return amazon_data


def prompt_for_order_details(
    amazon_data: AmazonData, order_id: str, memo_generator: MemoGenerator
) -> Order | None:
    """Offer to take the details page for an order we only know by ID.

    Reached when a charge identifies the order behind a transaction but no
    pasted page described it. Pasting that one page turns "some Amazon order"
    into a real item list, which is the whole point of categorizing.
    """
    order_link = memo_generator.generate_amazon_order_link(order_id)
    print(f"  This charge belongs to order {order_id}, but no item data was provided.")
    if order_link:
        print(f"  Details page: {order_link}")

    answer = _prompt_line("Paste that order's details page now? (y/n, default n): ")
    if answer.strip().lower() != "y":
        return None

    print("Paste the order details page:")
    page_text = get_multiline_input_with_custom_submit("Paste here: ")
    if not page_text or not page_text.strip():
        print("  Nothing pasted.")
        return None

    parser = AmazonParser()
    orders = parser.parse_order_details_page(page_text)
    if not orders:
        print("  No order details could be read from that text.")
        return None

    amazon_data.add_orders(orders)
    resolved = amazon_data.order_by_id(order_id)
    if resolved is None:
        parsed_ids = ", ".join(order.order_id or "?" for order in orders)
        print(f"  That page was for {parsed_ids}, not {order_id}.")
        return None

    print(f"  ✓ Loaded {len(resolved.items)} item(s) for {order_id}.")
    return resolved


def get_multiline_input_with_custom_submit(
    prompt_message: str = "Enter multiline text: ",
) -> str | None:
    """Get multiline input with Ctrl+J to submit"""
    kb = KeyBindings()

    @kb.add("escape", "enter")  # Binds Alt+Enter to submit
    def _(event: KeyPressEvent) -> None:
        """When Alt+Enter is pressed, accept the current buffer's text."""
        event.app.exit(result=event.app.current_buffer.text)

    print("Press Enter for a new line.")
    print("Submit by pressing Alt+Enter.")
    print("Press Ctrl+C to cancel.")

    try:
        user_input = prompt(prompt_message, multiline=True, key_bindings=kb)
        return user_input
    except EOFError:
        print("\nInput cancelled (EOF).")
        return None
    except KeyboardInterrupt:
        print("\nInput cancelled (KeyboardInterrupt).")
        return None


def _prompt_line(message: str) -> str:
    """Read one line of input via prompt_toolkit for consistent UX.

    Used in place of the builtin input function so every prompt in the tool goes
    through prompt_toolkit (uniform rendering and key handling). Mirrors builtin
    input semantics: returns the entered text and lets ``EOFError`` /
    ``KeyboardInterrupt`` propagate to the caller, so existing ``.strip()`` /
    ``.lower()`` chains on the result keep working.
    """
    return prompt(message)


def _prompt_quantity() -> int | None:
    while True:
        qty_input = _prompt_line(
            "Enter quantity (optional, press Enter to skip): "
        ).strip()
        if not qty_input:
            return None
        try:
            quantity = int(qty_input)
            if quantity > 0:
                return quantity
            print("Quantity must be positive.")
        except ValueError:
            print("Please enter a valid number.")


def _prompt_price() -> float | None:
    while True:
        price_input = _prompt_line(
            "Enter item price (optional, press Enter to skip): "
        ).strip()
        if not price_input:
            return None
        try:
            price = float(price_input.replace("$", "").replace(",", ""))
            if price >= 0:
                return price
            print("Price must be non-negative.")
        except ValueError:
            print("Please enter a valid price (e.g., 29.99).")


def prompt_for_item_details() -> dict[str, str | int | float | list[str] | None] | None:
    """Prompt user to enter item details manually"""
    print("\n--- Manual Item Details Entry ---")

    item_details: dict[str, str | int | float | list[str] | None] = {}

    # Get item title/description
    title = _prompt_line("Enter item title/description (optional): ").strip()
    if title:
        item_details["title"] = title

    # Get quantity
    quantity = _prompt_quantity()
    if quantity is not None:
        item_details["quantity"] = quantity

    # Get price per item
    price = _prompt_price()
    if price is not None:
        item_details["price"] = price

    return item_details if item_details else None


# --- Extracted Helper Functions ---


def print_config_summary(config: Config) -> None:
    """Print configuration summary without exposing secrets."""
    print(f"ynab-amazon-categorizer v{__version__}")
    print("✓ Configuration loaded successfully")
    print("✓ API Key: configured")
    if config.budget_id and len(config.budget_id) >= 4:
        print(f"✓ Budget ID: ...{config.budget_id[-4:]}")
    else:
        print("✓ Budget ID: configured")
    if config.account_id:
        print("✓ Account ID: configured")
    else:
        print("✓ All accounts")


def build_preview(
    payload: Mapping[str, object], category_id_map: dict[str, str]
) -> dict[str, Any]:
    """Build a preview dict from payload with category names injected.

    Uses deep copy to avoid mutating the original payload.
    """
    preview_dict: dict[str, Any] = copy.deepcopy(dict(payload))
    category_id = preview_dict.get("category_id")
    if isinstance(category_id, str):
        category_name = category_id_map.get(category_id, "Unknown Category")
        preview_dict["category_name"] = category_name
    subtransactions_value = preview_dict.get("subtransactions")
    if isinstance(subtransactions_value, list):
        for subtrans in subtransactions_value:
            if not isinstance(subtrans, dict):
                continue
            subtrans_category_id = subtrans.get("category_id")
            if isinstance(subtrans_category_id, str):
                cat_name = category_id_map.get(subtrans_category_id, "Unknown Category")
                subtrans["category_name"] = cat_name
    return preview_dict


def compute_split_amount(amount_float: float, remaining_milliunits: int) -> int:
    """Convert a positive user-entered amount to signed milliunits matching the parent.

    The sign of the result matches ``remaining_milliunits`` (negative for outflows,
    positive for inflows/refunds).

    Raises ``ValueError`` if the amount exceeds the remaining balance.
    """
    split_amount_milliunits = int(round(amount_float * 1000))

    if split_amount_milliunits > abs(remaining_milliunits) + 1:
        raise ValueError(
            f"Amount exceeds remaining. Max {abs(remaining_milliunits / 1000.0):.2f}"
        )

    # Apply sign to match parent transaction direction
    if remaining_milliunits < 0:
        split_amount_milliunits = -abs(split_amount_milliunits)
    else:
        split_amount_milliunits = abs(split_amount_milliunits)

    # Snap to exact remainder when within 1 milliunit
    if abs(abs(split_amount_milliunits) - abs(remaining_milliunits)) <= 1:
        split_amount_milliunits = remaining_milliunits

    return split_amount_milliunits


class CategoryCompleter(Completer):
    def __init__(self, category_list: list[tuple[str, str]]) -> None:
        self.categories = [name for name, _id in category_list]
        self.category_list = category_list

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text_before_cursor = document.text_before_cursor.lower()
        if text_before_cursor:
            for category_name in self.categories:
                if text_before_cursor in category_name.lower():
                    yield Completion(
                        category_name, start_position=-len(text_before_cursor)
                    )


def prompt_for_category_selection(
    category_completer: CategoryCompleter, name_to_id_map: dict[str, str]
) -> tuple[str | None, str | None]:
    history_file = os.path.join(os.path.expanduser("~"), ".ynab_amazon_cat_history")
    history = FileHistory(history_file)
    empty_streak = 0
    while True:
        try:
            user_input = prompt(
                "Enter category name (Tab to complete, Enter to confirm, "
                "empty+Enter twice or 'b' to go back): ",
                completer=category_completer,
                history=history,
                reserve_space_for_menu=5,
            ).strip()
            if not user_input:
                empty_streak += 1
                if empty_streak >= 2:
                    return None, None
                print(
                    "Press Enter again with nothing typed to go back, "
                    "or start typing a category name."
                )
                continue
            empty_streak = 0
            if user_input.lower() == "b":
                return None, None
            input_lower = user_input.lower()
            if input_lower in name_to_id_map:
                selected_id = name_to_id_map[input_lower]
                selected_display_name = ""
                for name, cat_id in category_completer.category_list:
                    if cat_id == selected_id:
                        selected_display_name = name
                        break
                print(f"Selected: {selected_display_name}")
                return selected_id, selected_display_name
            else:
                print(
                    f"Error: '{user_input}' is not a recognized category. Please use Tab completion or try again."
                )
        except EOFError:
            print("\nOperation cancelled by user (EOF).")
            return None, None
        except KeyboardInterrupt:
            print("\nOperation cancelled by user (KeyboardInterrupt).")
            return None, None


# --- Extracted per-transaction functions ---


def display_matched_order(matching_order: Order, memo_generator: MemoGenerator) -> None:
    """Display matched order details to the user."""
    charge = matching_order.matched_charge
    print("\n  🎯 MATCHED ORDER FOUND:")
    print(f"     Order ID: {matching_order.order_id}")
    print(
        f"     Total: "
        f"{format_currency_amount(matching_order.total, matching_order.currency)}"
    )
    print(
        f"     Date: {matching_order.date_str if matching_order.date_str is not None else 'N/A'}"
    )
    if charge is not None:
        payment = f" via {charge.payment_method}" if charge.payment_method else ""
        label = "Refund" if charge.is_refund else "Charge"
        print(
            f"     {label}: "
            f"{format_currency_amount(charge.amount, charge.currency)}"
            f" on {charge.date_str or 'N/A'}{payment}"
        )
        if matching_order.is_partial_charge:
            # The order is billed per shipment (or partly paid another way), so
            # this transaction covers only some of the items listed below.
            print(
                "     ⚠ This charge covers PART of the order — "
                "the items below are the whole order."
            )
    order_link = memo_generator.generate_amazon_order_link(matching_order.order_id)
    print(f"     Order Link: {order_link}")
    if matching_order.tax is not None:
        print(
            f"     Order tax: "
            f"{format_currency_amount(matching_order.tax, matching_order.currency)}"
        )
    if matching_order.items:
        print("     Items:")
        for index, item in enumerate(matching_order.items):
            price = matching_order.item_price(index)
            price_text = (
                f" — {format_currency_amount(price, matching_order.currency)}"
                if price is not None
                else ""
            )
            print(f"       - {item}{price_text}")
    else:
        print("     Items: none parsed for this order")
    print()


def _get_item_details(
    matching_order: Order | None,
) -> dict[str, str | int | float | list[str] | None] | None:
    if matching_order:
        print("Using matched order data for memo generation...")
        return {
            "order_id": matching_order.order_id or "",
            "items": matching_order.items,
            "total": matching_order.total,
            "date": matching_order.date_str,
        }

    # Ask if user wants to enter item details manually
    manual_entry = _prompt_line(
        "No order match found. Enter item details manually? (y/n, default n): "
    ).lower()
    if manual_entry == "y":
        return prompt_for_item_details()
    return None


def _build_suggested_memo(
    item_details: dict[str, str | int | float | list[str] | None] | None,
    matching_order: Order | None,
    original_memo: str,
    memo_generator: MemoGenerator,
) -> str:
    if not item_details:
        return original_memo

    if isinstance(item_details, dict) and "items" in item_details:
        # Auto-matched order data - format as: Item Name\n Order Link
        items_text = (
            generate_split_summary_memo(matching_order) if matching_order else ""
        ) or "Amazon Purchase"
        order_id_value = item_details["order_id"]
        order_link = memo_generator.generate_amazon_order_link(
            order_id_value if isinstance(order_id_value, str) else None
        )
        return f"{items_text}\n {order_link}" if order_link else items_text

    # Manual item details
    return memo_generator.generate_enhanced_memo(original_memo, None, item_details)


def _prompt_memo_confirmation(suggested_memo: str, original_memo: str) -> str:
    if suggested_memo and suggested_memo != original_memo:
        print("\nSuggested memo:")
        print(f"'{suggested_memo}'")
        use_suggested = _prompt_line("Use suggested memo? (y/n, default y): ").lower()
        if use_suggested != "n":
            return sanitize_memo(suggested_memo)
        print("Enter custom memo (multiline):")
        memo_input = get_multiline_input_with_custom_submit("> ")
        return sanitize_memo(memo_input.strip()) if memo_input else ""

    print("Enter optional memo (multiline):")
    memo_input = get_multiline_input_with_custom_submit("> ")
    return sanitize_memo(memo_input.strip()) if memo_input else ""


def resolve_memo(
    matching_order: Order | None,
    original_memo: str,
    memo_generator: MemoGenerator,
) -> str:
    """Determine the memo for a single-category transaction.

    Uses matched order data when available, otherwise prompts for manual entry.
    Returns the final memo string (already sanitized).
    """
    item_details = _get_item_details(matching_order)
    enhanced_memo = _build_suggested_memo(
        item_details, matching_order, original_memo, memo_generator
    )
    return _prompt_memo_confirmation(enhanced_memo, original_memo)


def handle_split(
    transaction: Mapping[str, Any],
    matching_order: Order | None,
    memo_generator: MemoGenerator,
    category_completer: CategoryCompleter,
    category_name_map: dict[str, str],
) -> list[SaveSubtransaction] | None:
    """Handle split transaction flow.

    Returns list of subtransaction dicts, or None if cancelled.
    """
    print("\n--- Splitting Transaction ---")
    subtransactions: list[SaveSubtransaction] = []
    amount_milliunits = transaction["amount"]
    remaining_milliunits = amount_milliunits
    split_count = 1

    while remaining_milliunits != 0:
        print(
            f"\nSplit {split_count}: Amount remaining: {abs(remaining_milliunits / 1000.0):.2f}"
        )

        # Show which item this split is for if we have matched order data
        items: list[str] = matching_order.items if matching_order else []
        item_price: float | None = None

        if items:
            if split_count <= len(items):
                item_price = (
                    matching_order.item_price(split_count - 1)
                    if matching_order
                    else None
                )
                price_text = f"  ({item_price:.2f})" if item_price is not None else ""
                print(f"Item {split_count}: {items[split_count - 1]}{price_text}")
            else:
                print("Additional split for remaining items")

        print(f"Enter category name for split {split_count}:")
        category_id, category_name = prompt_for_category_selection(
            category_completer, category_name_map
        )
        if category_id is None:  # User backed out
            print("Cancelling split process.")
            return None

        # Get amount for this split: enter the pre-tax base item price and
        # let the tool add sales tax automatically (rate chosen by category
        # via _tax_rate_for_category). Blank uses the full remaining balance
        # as-is (e.g. for a final catch-all split); '=' prefix enters an
        # exact charged total with no tax math applied; 'i' uses the item's
        # price from the order details page, when one was provided.
        tax_rate = _tax_rate_for_category(category_name)
        tax_pct = tax_rate * 100
        while True:
            try:
                max_amount = abs(remaining_milliunits / 1000.0)
                max_base = max_amount / (1 + tax_rate) if tax_rate else max_amount
                item_hint = (
                    f", 'i' = item price {item_price:.2f}"
                    if item_price is not None
                    else ""
                )
                base_str = _prompt_line(
                    f"Enter base price for '{category_name}' ({tax_pct:g}% tax, "
                    f"max base ~{max_base:.2f}, blank = remaining "
                    f"{max_amount:.2f} as-is{item_hint}): "
                ).strip()

                if base_str.lower() == "i" and item_price is not None:
                    base_str = f"{item_price:.2f}"

                if not base_str:
                    split_amount_float = max_amount
                elif base_str.startswith("="):
                    split_amount_float = float(
                        base_str[1:].replace("$", "").replace(",", "")
                    )
                    if split_amount_float <= 0:
                        print("Amount must be positive.")
                        continue
                else:
                    base_amount = float(base_str.replace("$", "").replace(",", ""))
                    if base_amount <= 0:
                        print("Amount must be positive.")
                        continue
                    tax_amount = round(base_amount * tax_rate, 2)
                    split_amount_float = round(base_amount + tax_amount, 2)
                    print(
                        f"  Base: ${base_amount:.2f}  +  Tax ({tax_pct:g}%): "
                        f"${tax_amount:.2f}  =  Total: ${split_amount_float:.2f}"
                    )

                split_amount_milliunits = compute_split_amount(
                    split_amount_float, remaining_milliunits
                )
                if split_amount_milliunits == remaining_milliunits:
                    print("Amount covers remaining balance.")
                break  # Amount valid
            except ValueError as e:
                print(str(e) if str(e) != str(e).lower() else "Invalid amount.")

        # --- ENHANCED SPLIT MEMO INPUT ---
        split_memo = _resolve_split_memo(
            matching_order, memo_generator, category_name, split_count
        )
        # --- END ENHANCED SPLIT MEMO INPUT ---

        subtransactions.append(
            {
                "amount": split_amount_milliunits,
                "category_id": category_id,
                "memo": sanitize_memo(split_memo) if split_memo else None,
            }
        )

        remaining_milliunits -= split_amount_milliunits
        split_count += 1

        if abs(remaining_milliunits) <= 1:  # Handle tiny remainder
            print("Remaining amount negligible.")
            if subtransactions:
                print(
                    f"Adjusting last split amount by {remaining_milliunits} milliunits."
                )
                subtransactions[-1]["amount"] += remaining_milliunits
            remaining_milliunits = 0  # Force complete

    if remaining_milliunits == 0 and subtransactions:
        return subtransactions
    return None


def _get_suggested_split_memo(
    matching_order: Order | None,
    memo_generator: MemoGenerator,
    split_count: int,
) -> str:
    if matching_order:
        print("Using matched order data for split memo...")
        items = matching_order.items
        order_id = matching_order.order_id

        if split_count <= len(items):
            items_text = items[split_count - 1]
            order_link = memo_generator.generate_amazon_order_link(order_id)
            return f"{items_text}\n {order_link}" if order_link else items_text
        return "Additional item"

    manual_entry = _prompt_line(
        "Enter item details for this split? (y/n, default n): "
    ).lower()
    if manual_entry == "y":
        item_details = prompt_for_item_details()
        if item_details:
            return memo_generator.generate_enhanced_memo("", None, item_details)
    return ""


def _prompt_split_memo_confirmation(
    suggested_split_memo: str, category_name: str | None
) -> str:
    if suggested_split_memo:
        print(f"Suggested memo for '{category_name}' split:")
        print(f"'{suggested_split_memo}'")
        use_suggested = _prompt_line("Use suggested memo? (y/n, default y): ").lower()
        if use_suggested != "n":
            return suggested_split_memo
        print(f"Enter custom memo for '{category_name}' split (multiline):")
        split_memo = get_multiline_input_with_custom_submit("> ")
        return split_memo.strip() if split_memo else ""

    print(f"Enter optional memo for '{category_name}' split (multiline):")
    split_memo = get_multiline_input_with_custom_submit("> ")
    return split_memo.strip() if split_memo else ""


def _resolve_split_memo(
    matching_order: Order | None,
    memo_generator: MemoGenerator,
    category_name: str | None,
    split_count: int,
) -> str:
    """Resolve memo for a single split within a split transaction."""
    suggested_split_memo = _get_suggested_split_memo(
        matching_order, memo_generator, split_count
    )
    return _prompt_split_memo_confirmation(suggested_split_memo, category_name)


def process_transaction(
    transaction: Mapping[str, Any],
    index: int,
    total: int,
    amazon_data: AmazonData | None,
    memo_generator: MemoGenerator,
    ynab_client: YNABClient,
    category_completer: CategoryCompleter,
    category_name_map: dict[str, str],
    category_id_map: dict[str, str],
    used_order_ids: set[str] | None = None,
    dry_run: bool = False,
    stats: dict[str, int] | None = None,
    used_charge_keys: set[tuple[str, str, str]] | None = None,
) -> bool:
    """Process a single transaction through the interactive flow.

    Returns True if processed/skipped, False if user quit.

    ``used_order_ids`` accumulates the order IDs already applied to a
    transaction so the matcher does not reuse one order for several
    same-amount transactions. ``used_charge_keys`` does the same for charge
    rows from the payments page; it is tracked separately because one order
    legitimately produces several charges, and so several transactions. When
    ``dry_run`` is True no changes are sent to YNAB and matched orders are not
    marked as used. ``stats``, if given, is used to accumulate run-level
    counters (currently just ``auto_skipped_no_match``) for a summary printed
    at the end of the run.
    """
    transaction_id = transaction["id"]
    date = transaction["date"]
    payee = transaction.get("payee_name", "N/A")
    amount_milliunits = transaction["amount"]
    amount_float = amount_milliunits / 1000.0
    original_memo = transaction.get("memo", "")

    matching_order: Order | None = None
    if amazon_data:
        transaction_matcher = TransactionMatcher()
        matching_order = transaction_matcher.resolve_order(
            amount_float, date, amazon_data, used_order_ids, used_charge_keys
        )

    if amount_milliunits > 0:
        currency = matching_order.currency if matching_order else None
        print(
            f"Found inflow transaction: {payee} "
            f"{format_currency_amount(amount_float, currency)}"
        )
        process_inflow = _prompt_line(
            "Process this inflow (refund/credit)? (y/n, default n): "
        ).lower()
        if process_inflow != "y":
            print("Skipping inflow transaction.")
            return True

    print(f"\n--- Processing Transaction {index + 1}/{total} ---")
    print(f"  ID:   {transaction_id}")
    print(f"  Date: {date}")
    print(f"  Payee: {payee}")
    amount_display = (
        format_currency_amount(amount_float, matching_order.currency)
        if matching_order
        else f"{amount_float:.2f}"
    )
    print(f"  Amount: {amount_display}")
    if transaction.get("cleared") == "reconciled":
        print(
            "  Status: 🔒 reconciled (category edits do not affect the reconciled balance)"
        )
    if original_memo:
        print(f"  Original Memo: {original_memo}")

    # Try to find matching order from parsed data and show it
    if amazon_data:
        if matching_order:
            # A charge can name an order none of the pasted pages described.
            # That is worth one more prompt: its details page is the
            # difference between an order link and a real item list.
            if (
                not matching_order.items
                and matching_order.matched_charge is not None
                and matching_order.order_id
            ):
                filled = prompt_for_order_details(
                    amazon_data, matching_order.order_id, memo_generator
                )
                if filled is not None:
                    matching_order = dataclasses.replace(
                        filled, matched_charge=matching_order.matched_charge
                    )
            display_matched_order(matching_order, memo_generator)
        else:
            # Amazon data was provided for this run, but nothing matched this
            # specific transaction (by amount/date). There's no data to help
            # categorize it, so skip the prompt instead of asking blind.
            # (When amazon_data itself is empty/None — i.e. nothing was
            # provided at all this run — we fall through to the normal
            # action loop below, which still offers manual item entry.)
            print(
                "  ⚠ No matching order found in parsed Amazon data — "
                "skipping (nothing to categorize from)."
            )
            if stats is not None:
                stats["auto_skipped_no_match"] = (
                    stats.get("auto_skipped_no_match", 0) + 1
                )
            return True

    while True:  # Action loop (c, s, q)
        action = _prompt_line(
            "Action? (c = categorize/split, s = skip, q = quit, default c): "
        ).lower()
        if not action:
            action = "c"
        if action == "q":
            print("Quitting.")
            return False
        elif action == "s":
            print("Skipping.")
            return True
        elif action == "c":
            result = _handle_categorize(
                transaction,
                matching_order,
                original_memo,
                memo_generator,
                ynab_client,
                category_completer,
                category_name_map,
                category_id_map,
                dry_run,
            )
            if result == "done":
                # Mark what was consumed so it is not reused for a later
                # transaction of the same amount. Skip in dry-run because
                # nothing was actually applied.
                if not dry_run and matching_order is not None:
                    mark_match_used(matching_order, used_order_ids, used_charge_keys)
                return True
            # result == "continue" means back to action prompt
            continue
        else:
            print("Invalid action. Choose 'c', 's', or 'q'.")


def _handle_categorize(
    transaction: Mapping[str, Any],
    matching_order: Order | None,
    original_memo: str,
    memo_generator: MemoGenerator,
    ynab_client: YNABClient,
    category_completer: CategoryCompleter,
    category_name_map: dict[str, str],
    category_id_map: dict[str, str],
    dry_run: bool = False,
) -> str:
    """Handle the categorize action for a transaction.

    Returns "done" if the transaction was successfully updated (or split completed),
    or "continue" to go back to the action prompt.

    When ``dry_run`` is True the preview is shown but no update is sent to YNAB.
    """
    transaction_id = transaction["id"]
    updated_payload_dict: TransactionUpdate | None = None

    # Check if we should offer splitting
    should_offer_split = bool(
        matching_order and matching_order.items and len(matching_order.items) > 1
    )

    if should_offer_split:
        print("There is more than one item in this transaction.")
        split_decision = _prompt_line(
            "Split this transaction? (y/n, default n): "
        ).lower()
    elif _env_flag("YNAB_SKIP_SPLIT_PROMPT_SINGLE_ITEM"):
        # Only one item (or no matched order) — nothing to split, and the
        # user has opted via .env to skip asking about it every time.
        split_decision = "n"
    else:
        split_decision = _prompt_line(
            "Split this transaction? (y/n, default n): "
        ).lower()

    if split_decision != "y":
        # --- SINGLE CATEGORY ---
        print("Enter category name for the transaction:")
        category_id, _category_name = prompt_for_category_selection(
            category_completer, category_name_map
        )
        if category_id is None:
            return "continue"

        memo_input = resolve_memo(matching_order, original_memo, memo_generator)

        updated_payload_dict = build_single_payload(
            category_id, memo_input if memo_input else original_memo
        )
    else:
        # --- SPLITTING ---
        subtransactions = handle_split(
            transaction,
            matching_order,
            memo_generator,
            category_completer,
            category_name_map,
        )
        if subtransactions:
            updated_payload_dict = build_split_payload(
                subtransactions, matching_order, original_memo
            )
        else:
            print("Splitting cancelled. No changes will be made.")

    # --- Confirmation and API Call ---
    if updated_payload_dict:
        print("\n--- Preview Update ---")
        preview_dict = build_preview(updated_payload_dict, category_id_map)
        print(json.dumps(preview_dict, indent=2, ensure_ascii=False))
        if dry_run:
            print("[dry-run] No changes were sent to YNAB.")
            return "done"
        confirm = _prompt_line("Confirm update? (y/n, default y): ").lower()
        if not confirm:
            confirm = "y"
        if confirm == "y":
            try:
                ynab_client.update_transaction(transaction_id, updated_payload_dict)
                print("Update successful.")
                return "done"
            except (
                YNABAPIError,
                requests.exceptions.RequestException,
                OSError,
            ) as exc:
                logger.error("Failed to update transaction %s: %s", transaction_id, exc)
                print(f"Update failed: {exc}")
                return "continue"
        else:
            print("Update cancelled.")
            return "continue"

    return "continue"


# --- Main Script Logic ---


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="ynab-amazon-categorizer",
        description=(
            "Match Amazon orders to YNAB transactions with item-level memos "
            "and guided categorization."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview updates without sending any changes to YNAB.",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help=(
            "Non-interactive: auto-set memos (items + order link) for "
            "confidently matched transactions and leave categories unchanged. "
            "Combine with --dry-run to preview."
        ),
    )
    parser.add_argument(
        "--include-reconciled",
        action="store_true",
        help=(
            "Also surface uncategorized Amazon transactions that are already "
            "reconciled (excluded by default). Category edits don't affect "
            "the reconciled balance. Combine with --dry-run to just print the "
            "suggested categories for manual entry without writing to YNAB."
        ),
    )
    return parser.parse_args(argv)


def _run(argv: list[str] | None = None) -> int:
    """Run the CLI workflow and return a process exit code."""
    args = _parse_args(argv)
    dry_run = args.dry_run
    include_reconciled = args.include_reconciled

    logging.basicConfig(level=logging.INFO)

    if dry_run:
        print("*** DRY RUN: no changes will be sent to YNAB. ***")
    if include_reconciled:
        print("*** Including already-reconciled transactions. ***")
    if _env_flag("YNAB_SKIP_SPLIT_PROMPT_SINGLE_ITEM"):
        print("*** Skipping split prompt for single-item transactions. ***")

    # Load configuration using extracted Config class
    try:
        config = Config.from_env()
        print_config_summary(config)
    except ConfigurationError as e:
        logger.error("Configuration error: %s", e)
        print("Please set environment variables or create a .env file.")
        print("See README.md for setup instructions.")
        return 1

    # Initialize YNAB client
    ynab_client = YNABClient(config.api_key, config.budget_id)
    memo_generator = MemoGenerator(config.amazon_domain)

    # Fetch transactions FIRST so the user isn't asked to paste Amazon order
    # data (or wait on a category fetch) when there is nothing to match it to.
    print("\nFetching transactions...")
    try:
        transactions_to_process = fetch_amazon_transactions(
            ynab_client, config, include_reconciled=include_reconciled
        )
    except (
        YNABAPIError,
        requests.exceptions.RequestException,
        OSError,
    ) as exc:
        logger.error("Failed to fetch transactions: %s", exc)
        print(f"Could not fetch transactions: {exc}")
        return 1

    if not transactions_to_process:
        print("\nNo uncategorized Amazon transactions found — nothing to do. ✓")
        return 0

    reconciled_count = sum(
        1 for t in transactions_to_process if t.get("cleared") == "reconciled"
    )
    print(
        f"\nFound {len(transactions_to_process)} uncategorized Amazon transaction(s) needing attention."
    )
    if reconciled_count:
        print(f"  ({reconciled_count} of these are already reconciled 🔒)")

    print("\nFetching categories...")
    try:
        categories_list, category_name_map, category_id_map = (
            ynab_client.get_categories()
        )
    except (
        YNABAPIError,
        requests.exceptions.RequestException,
        OSError,
    ) as exc:
        logger.error("Failed to fetch categories: %s", exc)
        print(f"Could not fetch categories: {exc}")
        return 1

    if not categories_list:
        print("Exiting due to category fetch error or no usable categories found.")
        return 1

    category_completer_instance = CategoryCompleter(categories_list)
    print(f"\nFound {len(categories_list)} usable categories. Completion enabled.")

    # Ask user if they want to provide Amazon page data for automatic item detection
    print("\n--- Optional: Amazon Data ---")
    print(
        "You can paste your Amazon orders, order details, and transactions pages\n"
        "to match YNAB transactions to orders and their items."
    )
    provide_orders = _prompt_line(
        "Would you like to provide Amazon data? (y/n, default y): "
    ).lower()
    if not provide_orders:
        provide_orders = "y"

    amazon_data: AmazonData | None = None
    if provide_orders == "y":
        amazon_data = prompt_for_amazon_data(transactions_to_process, memo_generator)
        if not amazon_data:
            print("No usable Amazon data found in provided text.")

    # --- Batch Mode (non-interactive memo enrichment) ---
    if args.batch:
        print("\n--- Batch: auto-enriching memos for confident matches ---")
        enriched, skipped, failed = process_batch(
            transactions_to_process,
            amazon_data,
            memo_generator,
            ynab_client,
            dry_run,
        )
        print(
            f"\nBatch complete: {enriched} enriched, {skipped} skipped "
            f"(no/ambiguous match), {failed} failed."
        )
        return 0

    # --- Process Transactions (Main Loop) ---
    used_order_ids: set[str] = set()
    used_charge_keys: set[tuple[str, str, str]] = set()
    stats: dict[str, int] = {}
    for i, t in enumerate(transactions_to_process):
        should_continue = process_transaction(
            t,
            i,
            len(transactions_to_process),
            amazon_data,
            memo_generator,
            ynab_client,
            category_completer_instance,
            category_name_map,
            category_id_map,
            used_order_ids,
            dry_run,
            stats,
            used_charge_keys,
        )
        if not should_continue:
            return 0

    # End of processing loop
    print("\nFinished processing transactions.")
    auto_skipped = stats.get("auto_skipped_no_match", 0)
    if auto_skipped:
        print(
            f"  ({auto_skipped} auto-skipped: no matching order data for that "
            "transaction)"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI with clean handling for terminal cancellation."""
    try:
        return _run(argv)
    except (EOFError, KeyboardInterrupt):
        print("\nOperation cancelled. No further changes were made.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
