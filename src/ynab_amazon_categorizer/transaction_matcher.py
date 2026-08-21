"""Transaction matching functionality."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .amazon_data import AmazonData
from .amazon_parser import Order
from .models import AmazonCharge


def _parse_transaction_date(date_str: str) -> datetime | None:
    """Parse transaction date in YYYY-MM-DD format."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _parse_order_date(date_str: str | None) -> datetime | None:
    """Parse order date in 'Month DD, YYYY' format."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%B %d, %Y")
    except (ValueError, TypeError):
        return None


class TransactionMatcher:
    """Matches Amazon orders with YNAB transactions."""

    def __init__(self) -> None:
        pass

    def find_matching_order(
        self,
        transaction_amount: float,
        transaction_date: str,
        parsed_orders: Sequence[Order],
        used_order_ids: set[str] | None = None,
        max_date_diff_days: int = 14,
    ) -> Order | None:
        """Find the best matching order for a transaction.

        Matching requires an exact amount match (within 1 cent) and, when both
        dates are parseable, a date within ``max_date_diff_days``.
        Ties are broken by date proximity, then by order ID for determinism.

        Orders whose ``order_id`` appears in ``used_order_ids`` are skipped so a
        single order is not matched to multiple transactions of the same amount.
        """
        if not parsed_orders:
            return None

        transaction_amount_abs = abs(transaction_amount)
        trans_date = _parse_transaction_date(transaction_date)

        best_match: Order | None = None
        best_score = 0
        best_date_diff: int | None = None
        best_order_id: str = ""

        for order in parsed_orders:
            if order.total is None:
                continue

            if (
                used_order_ids
                and order.order_id is not None
                and order.order_id in used_order_ids
            ):
                continue

            amount_diff = abs(order.total - transaction_amount_abs)
            if amount_diff >= 0.01:
                continue

            score = 100
            date_diff: int | None = None

            # Check date proximity
            if trans_date:
                order_date = _parse_order_date(order.date_str)
                if order_date:
                    date_diff = abs((trans_date - order_date).days)
                    if date_diff > max_date_diff_days:
                        continue
                    if date_diff <= 1:  # Same or next day
                        score += 30
                    elif date_diff <= 3:  # Within 3 days
                        score += 15
                    elif date_diff <= 7:  # Within a week
                        score += 5

            order_id = order.order_id or ""

            # Deterministic tie-breaking: score > date_diff (lower wins) > order_id
            is_better = False
            if score > best_score:
                is_better = True
            elif score == best_score:
                # Tie on score: prefer closer date
                if date_diff is not None and (
                    best_date_diff is None or date_diff < best_date_diff
                ):
                    is_better = True
                elif date_diff == best_date_diff:
                    # Tie on date too: use order_id as stable key
                    if order_id < best_order_id:
                        is_better = True

            if is_better:
                best_score = score
                best_match = order
                best_date_diff = date_diff
                best_order_id = order_id

        return best_match

    def find_confident_match(
        self,
        transaction_amount: float,
        transaction_date: str,
        parsed_orders: Sequence[Order],
        used_order_ids: set[str] | None = None,
        max_date_diff_days: int = 7,
    ) -> Order | None:
        """Return an order only when the match is unambiguous (for batch use).

        Unlike ``find_matching_order``, this requires *exactly one* unused order
        matching the amount (within 1 cent). If that order has a parseable date
        it must be within ``max_date_diff_days`` of the transaction. Any
        ambiguity (zero or multiple amount matches, or a far-off date) returns
        ``None`` so batch mode never auto-applies a guess.
        """
        amount_abs = abs(transaction_amount)
        trans_date = _parse_transaction_date(transaction_date)

        candidates: list[Order] = []
        for order in parsed_orders:
            if order.total is None:
                continue
            if (
                used_order_ids
                and order.order_id is not None
                and order.order_id in used_order_ids
            ):
                continue
            if abs(order.total - amount_abs) >= 0.01:
                continue
            candidates.append(order)

        if len(candidates) != 1:
            return None

        order = candidates[0]
        if trans_date:
            order_date = _parse_order_date(order.date_str)
            if order_date:
                if abs((trans_date - order_date).days) > max_date_diff_days:
                    return None
        return order

    # --- Charge-level matching (Amazon payments/transactions page) --------

    def find_matching_charge(
        self,
        transaction_amount: float,
        transaction_date: str,
        charges: Sequence[AmazonCharge],
        used_charge_keys: set[tuple[str, str, str]] | None = None,
        max_date_diff_days: int = 14,
    ) -> AmazonCharge | None:
        """Find the charge row that produced this YNAB transaction.

        Charges are what actually hit the card, so this matches transactions an
        order total never can: one order billed once per shipment, an order
        partly paid by gift card or points, or a refund. Direction must agree
        (an inflow only matches a refund) on top of the same exact-amount and
        date-proximity rules used for orders.

        ``used_charge_keys`` tracks charges already consumed. It is keyed by
        charge rather than by order because one order legitimately produces
        several transactions.
        """
        candidates = self._charge_candidates(
            transaction_amount,
            transaction_date,
            charges,
            used_charge_keys,
            max_date_diff_days,
        )
        if not candidates:
            return None
        # Closest date first, then a stable key so repeated runs agree.
        candidates.sort(
            key=lambda pair: (
                pair[1] if pair[1] is not None else max_date_diff_days + 1,
                pair[0].order_id or "",
            )
        )
        return candidates[0][0]

    def find_confident_charge(
        self,
        transaction_amount: float,
        transaction_date: str,
        charges: Sequence[AmazonCharge],
        used_charge_keys: set[tuple[str, str, str]] | None = None,
        max_date_diff_days: int = 7,
    ) -> AmazonCharge | None:
        """Return a charge only when exactly one is plausible (for batch use)."""
        candidates = self._charge_candidates(
            transaction_amount,
            transaction_date,
            charges,
            used_charge_keys,
            max_date_diff_days,
        )
        if len(candidates) != 1:
            return None
        return candidates[0][0]

    def _charge_candidates(
        self,
        transaction_amount: float,
        transaction_date: str,
        charges: Sequence[AmazonCharge],
        used_charge_keys: set[tuple[str, str, str]] | None,
        max_date_diff_days: int,
    ) -> list[tuple[AmazonCharge, int | None]]:
        """Charges matching this transaction, paired with their date distance."""
        trans_date = _parse_transaction_date(transaction_date)
        transaction_is_inflow = transaction_amount > 0
        matches: list[tuple[AmazonCharge, int | None]] = []

        for charge in charges:
            if charge.amount is None or not charge.order_id:
                continue
            if used_charge_keys and charge.key in used_charge_keys:
                continue
            if (charge.amount > 0) != transaction_is_inflow:
                continue
            if abs(abs(charge.amount) - abs(transaction_amount)) >= 0.01:
                continue

            date_diff: int | None = None
            if trans_date:
                charge_date = _parse_order_date(charge.date_str)
                if charge_date:
                    date_diff = abs((trans_date - charge_date).days)
                    if date_diff > max_date_diff_days:
                        continue
            matches.append((charge, date_diff))

        return matches

    # --- Combined resolution ---------------------------------------------

    def resolve_order(
        self,
        transaction_amount: float,
        transaction_date: str,
        amazon_data: AmazonData,
        used_order_ids: set[str] | None = None,
        used_charge_keys: set[tuple[str, str, str]] | None = None,
    ) -> Order | None:
        """Best available order context for a transaction, charges first.

        A charge match names its order outright, so it is trusted over an
        amount match against order totals. When the named order was never
        parsed, a stub carrying just the ID is returned: the order link and the
        charge details are still worth showing, and the CLI can offer to take
        that order's details page.

        The returned order is a copy with ``matched_charge`` set, so the caller
        can tell a whole-order match from one shipment of a larger order
        without mutating the shared parsed data.
        """
        charge = self.find_matching_charge(
            transaction_amount,
            transaction_date,
            amazon_data.charges,
            used_charge_keys,
        )
        if charge is not None:
            return _order_for_charge(charge, amazon_data)

        return self.find_matching_order(
            transaction_amount,
            transaction_date,
            amazon_data.orders,
            used_order_ids,
        )

    def resolve_confident_order(
        self,
        transaction_amount: float,
        transaction_date: str,
        amazon_data: AmazonData,
        used_order_ids: set[str] | None = None,
        used_charge_keys: set[tuple[str, str, str]] | None = None,
    ) -> Order | None:
        """Unambiguous order context for batch mode, charges first.

        A charge naming an order we have no item data for is not enough to
        enrich a memo beyond the order link, but the link alone is still a real
        improvement, so it is returned like any other match.
        """
        charge = self.find_confident_charge(
            transaction_amount,
            transaction_date,
            amazon_data.charges,
            used_charge_keys,
        )
        if charge is not None:
            return _order_for_charge(charge, amazon_data)

        return self.find_confident_match(
            transaction_amount,
            transaction_date,
            amazon_data.orders,
            used_order_ids,
        )


def _order_for_charge(charge: AmazonCharge, amazon_data: AmazonData) -> Order:
    """Attach a matched charge to its order, stubbing one in when unknown."""
    order = amazon_data.order_by_id(charge.order_id)
    if order is None:
        return Order(
            order_id=charge.order_id,
            date_str=charge.date_str,
            currency=charge.currency,
            matched_charge=charge,
        )
    return replace(order, matched_charge=charge)


def mark_match_used(
    matching_order: Order,
    used_order_ids: set[str] | None,
    used_charge_keys: set[tuple[str, str, str]] | None,
) -> None:
    """Record a match as consumed so a later transaction cannot reuse it.

    A charge-based match consumes only that charge row: a multi-shipment order
    produces several charges and therefore several transactions, so retiring
    the whole order would strand the rest of them.
    """
    charge = matching_order.matched_charge
    if charge is not None:
        if used_charge_keys is not None:
            used_charge_keys.add(charge.key)
        return
    if used_order_ids is not None and matching_order.order_id is not None:
        used_order_ids.add(matching_order.order_id)


@dataclass(slots=True)
class CoverageSummary:
    """How much of a transaction set the Amazon data on hand can describe."""

    total: int = 0
    described: int = 0
    without_items: int = 0
    without_prices: int = 0
    unmatched: int = 0
    orders_needing_details: list[str] = field(default_factory=list)
    # Multi-item orders with no per-item prices. Single-item orders are
    # excluded: there is nothing to split, so a price would change nothing.
    orders_needing_prices: list[str] = field(default_factory=list)

    @property
    def matched(self) -> int:
        """Transactions resolved to an order, with or without item data."""
        return self.described + self.without_items

    @property
    def is_complete(self) -> bool:
        """True when every transaction has an order *and* its items.

        Note this does not imply every item has a *price*: an order matched
        from the orders list page has names only. ``without_prices`` counts
        the ones where that costs something — see ``orders_needing_prices``.
        """
        return self.total > 0 and self.described == self.total


def summarize_coverage(
    transactions: Sequence[Mapping[str, Any]], amazon_data: AmazonData
) -> CoverageSummary:
    """Report what the pasted pages can and cannot explain, before categorizing.

    Run while collecting pages, this turns a blind paste loop into a checklist:
    it names the exact orders whose details pages are still worth fetching,
    rather than letting the user discover the gaps one transaction at a time.

    Matches are consumed as the survey goes, exactly as the real run consumes
    them, so two same-amount transactions cannot both claim one charge and
    inflate the count.
    """
    matcher = TransactionMatcher()
    summary = CoverageSummary(total=len(transactions))
    used_order_ids: set[str] = set()
    used_charge_keys: set[tuple[str, str, str]] = set()

    for transaction in transactions:
        order = matcher.resolve_order(
            transaction["amount"] / 1000.0,
            transaction["date"],
            amazon_data,
            used_order_ids,
            used_charge_keys,
        )
        if order is None:
            summary.unmatched += 1
            continue

        mark_match_used(order, used_order_ids, used_charge_keys)
        if order.items:
            summary.described += 1
            # Prices only ever matter for splitting, and only a multi-item
            # order can be split, so a single-item order is already complete.
            if not order.has_item_prices and len(order.items) > 1:
                summary.without_prices += 1
                if (
                    order.order_id
                    and order.order_id not in summary.orders_needing_prices
                ):
                    summary.orders_needing_prices.append(order.order_id)
            continue

        summary.without_items += 1
        if order.order_id and order.order_id not in summary.orders_needing_details:
            summary.orders_needing_details.append(order.order_id)

    return summary
