"""Typed domain models used across parsing, matching, and YNAB updates."""

from dataclasses import dataclass, field
from datetime import date
from typing import NotRequired, TypedDict, cast


@dataclass(slots=True)
class AmazonCharge:
    """One row from Amazon's payments/transactions page.

    The orders page reports what an *order* cost; this reports what actually
    hit a card. The two differ whenever an order ships in several packages,
    is split across payment methods (gift card, points), or is refunded — the
    cases where amount-only matching against order totals finds nothing.

    ``amount`` keeps its sign: negative for a charge, positive for a refund,
    matching YNAB's outflow/inflow convention.
    """

    amount: float | None = None
    date_str: str | None = None
    order_id: str | None = None
    payment_method: str | None = None
    merchant: str | None = None
    is_refund: bool = False
    currency: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        """Stable identity for de-duplication and 'already consumed' tracking.

        Two rows with the same order, amount, and date are the same charge even
        if the page was pasted twice; a single order legitimately produces
        several distinct rows, so the order ID alone is not enough.
        """
        amount = f"{self.amount:.2f}" if self.amount is not None else ""
        return (self.order_id or "", amount, self.date_str or "")


@dataclass(slots=True)
class OrderItem:
    """One line item with the per-unit price shown on an order details page.

    ``price`` is per unit, so a line's contribution to the order subtotal is
    ``price * quantity``. Both are optional because the orders *list* page
    shows item names without prices.
    """

    name: str
    price: float | None = None
    quantity: int = 1

    @property
    def line_total(self) -> float | None:
        """Total for this line (unit price x quantity), or None without a price."""
        if self.price is None:
            return None
        return round(self.price * self.quantity, 2)


@dataclass(slots=True)
class Order:
    """A parsed Amazon order.

    Optional scalar fields allow partially parsed orders to remain useful for
    matching while making their shape explicit and easy to construct in tests.

    ``items`` stays the canonical list of item names that memo generation and
    splitting consume. ``detailed_items`` is populated only from an order
    details page and adds per-unit prices and quantities on top;
    ``item_prices`` runs parallel to ``items`` so a split can look up the price
    of the entry it is on by index.
    """

    order_id: str | None = None
    total: float | None = None
    date_str: str | None = None
    items: list[str] = field(default_factory=list)
    currency: str | None = None
    detailed_items: list[OrderItem] = field(default_factory=list)
    subtotal: float | None = None
    tax: float | None = None
    matched_charge: "AmazonCharge | None" = None
    item_prices: list[float | None] = field(default_factory=list)

    @property
    def has_item_prices(self) -> bool:
        """True when at least one item carries a price from an order details page."""
        return any(item.price is not None for item in self.detailed_items)

    @property
    def is_partial_charge(self) -> bool:
        """True when this order was matched through a charge covering only part of it.

        A multi-shipment order is billed once per shipment, so the matched
        charge is smaller than the order total. Splitting and memo text should
        say so rather than implying the whole order is on this transaction.
        """
        charge = self.matched_charge
        if charge is None or charge.amount is None or self.total is None:
            return False
        return abs(abs(charge.amount) - abs(self.total)) >= 0.01

    def item_price(self, index: int) -> float | None:
        """Per-unit price for the ``index``-th entry of ``items``, when known."""
        if 0 <= index < len(self.item_prices):
            return self.item_prices[index]
        return None

    def items_total(self) -> float | None:
        """Sum of every priced line, or None when no prices are known."""
        if not self.has_item_prices:
            return None
        return round(
            sum(item.line_total or 0.0 for item in self.detailed_items),
            2,
        )


def expand_items(
    detailed_items: list[OrderItem], max_items: int
) -> tuple[list[str], list[float | None]]:
    """Flatten priced line items into the ``items``/``item_prices`` pair.

    A name is repeated once per unit so multiple units of one product can be
    split into separate categories. When that would overflow ``max_items``
    while the distinct lines themselves fit, one entry per line is kept
    instead: dropping a distinct item hides a purchase outright, whereas
    dropping a repeated unit only hides its multiplicity, which
    ``detailed_items`` still records.
    """
    total_units = sum(max(item.quantity, 1) for item in detailed_items)
    expand = total_units <= max_items or len(detailed_items) > max_items

    names: list[str] = []
    prices: list[float | None] = []
    for item in detailed_items:
        units = max(item.quantity, 1) if expand else 1
        for _ in range(units):
            if len(names) >= max_items:
                return names, prices
            names.append(item.name)
            prices.append(item.price)
    return names, prices


def format_currency_amount(amount: float | None, currency: str | None = None) -> str:
    """Format an amount with its parsed currency, defaulting legacy orders to dollars."""
    if amount is None:
        return "N/A"
    sign = "-" if amount < 0 else ""
    return f"{sign}{currency or '$'}{abs(amount):.2f}"


class YNABTransaction(TypedDict):
    """YNAB transaction fields consumed by this application."""

    id: str
    account_id: str
    date: str
    amount: int
    payee_id: NotRequired[str | None]
    payee_name: NotRequired[str | None]
    category_id: NotRequired[str | None]
    memo: NotRequired[str | None]
    cleared: NotRequired[str | None]
    approved: NotRequired[bool]
    flag_color: NotRequired[str | None]
    import_id: NotRequired[str | None]
    transfer_account_id: NotRequired[str | None]
    subtransactions: NotRequired[list[object]]


class SaveSubtransaction(TypedDict):
    """Fields accepted when creating a YNAB split subtransaction."""

    amount: int
    category_id: str
    memo: str | None


class TransactionUpdate(TypedDict, total=False):
    """Minimal set of fields intentionally changed in a YNAB update."""

    category_id: str | None
    memo: str
    approved: bool
    subtransactions: list[SaveSubtransaction]


def validate_ynab_transaction(value: object) -> YNABTransaction:
    """Validate the YNAB fields required by filtering and processing."""
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    raw = cast(dict[str, object], value)

    for field_name in ("id", "account_id", "date"):
        field_value = raw.get(field_name)
        if not isinstance(field_value, str) or not field_value:
            raise ValueError(f"{field_name} must be a non-empty string")

    try:
        date.fromisoformat(cast(str, raw["date"]))
    except ValueError as exc:
        raise ValueError("date must use ISO YYYY-MM-DD format") from exc

    amount = raw.get("amount")
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise ValueError("amount must be an integer number of milliunits")

    nullable_strings = (
        "payee_id",
        "payee_name",
        "category_id",
        "memo",
        "cleared",
        "flag_color",
        "import_id",
        "transfer_account_id",
    )
    for field_name in nullable_strings:
        field_value = raw.get(field_name)
        if field_value is not None and not isinstance(field_value, str):
            raise ValueError(f"{field_name} must be a string or null")

    approved = raw.get("approved")
    if approved is not None and not isinstance(approved, bool):
        raise ValueError("approved must be a boolean")

    subtransactions = raw.get("subtransactions")
    if subtransactions is not None and not isinstance(subtransactions, list):
        raise ValueError("subtransactions must be a list")

    return cast(YNABTransaction, raw)
