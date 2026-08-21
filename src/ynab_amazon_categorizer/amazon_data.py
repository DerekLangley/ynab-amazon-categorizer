"""Aggregation of the Amazon pages that describe a purchase.

No single Amazon page carries everything needed to categorize a YNAB
transaction:

- the **orders list** page has order totals and (paginated, price-less) items;
- an **order details** page has the complete item list with per-unit prices and
  the order's subtotal/tax breakdown;
- the **payments/transactions** page maps each individual card charge to the
  order it paid for.

A transaction that no order total matches is usually explained by the third
page — one order billed once per shipment, split with a gift card or reward
points, or refunded. This module holds whichever pages the user supplied and
merges repeat sightings of the same order so later, richer data wins.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from .models import AmazonCharge, Order

logger = logging.getLogger(__name__)


def _order_richness(order: Order) -> tuple[int, int, int]:
    """Sort key for how much an order tells us, richest last.

    Priced items only come from an order details page, which is also the only
    page that shows every item, so that signal dominates.
    """
    return (
        1 if order.has_item_prices else 0,
        len(order.detailed_items),
        len(order.items),
    )


def merge_order(existing: Order, incoming: Order) -> Order:
    """Combine two sightings of the same order, preferring the richer one.

    Field-by-field so that a details page's items and prices win while a value
    only the other page had (e.g. the orders list's total, when the details
    page's summary could not be read) is still kept.
    """
    primary, secondary = (
        (incoming, existing)
        if _order_richness(incoming) > _order_richness(existing)
        else (existing, incoming)
    )

    def pick(
        primary_value: float | None, secondary_value: float | None
    ) -> float | None:
        return primary_value if primary_value is not None else secondary_value

    return Order(
        order_id=primary.order_id or secondary.order_id,
        total=pick(primary.total, secondary.total),
        date_str=primary.date_str or secondary.date_str,
        items=primary.items or secondary.items,
        currency=primary.currency or secondary.currency,
        detailed_items=primary.detailed_items or secondary.detailed_items,
        subtotal=pick(primary.subtotal, secondary.subtotal),
        tax=pick(primary.tax, secondary.tax),
    )


@dataclass(slots=True)
class AmazonData:
    """Everything parsed from the Amazon pages provided for this run."""

    orders: list[Order] = field(default_factory=list)
    charges: list[AmazonCharge] = field(default_factory=list)

    @classmethod
    def from_orders(cls, orders: Iterable[Order] | None) -> "AmazonData":
        """Build from orders alone (the pre-existing orders-page-only flow)."""
        return cls(orders=list(orders or []))

    def __bool__(self) -> bool:
        """True when any Amazon data at all was provided this run."""
        return bool(self.orders or self.charges)

    def add_orders(self, incoming: Iterable[Order]) -> tuple[int, int]:
        """Add parsed orders, merging any already known. Returns (added, merged)."""
        added = merged = 0
        for order in incoming:
            existing_index = self._index_of(order.order_id)
            if existing_index is None:
                self.orders.append(order)
                added += 1
            else:
                self.orders[existing_index] = merge_order(
                    self.orders[existing_index], order
                )
                merged += 1
        return added, merged

    def add_charges(self, incoming: Iterable[AmazonCharge]) -> int:
        """Add parsed charges, ignoring rows already held. Returns how many were new."""
        known = {charge.key for charge in self.charges}
        added = 0
        for charge in incoming:
            if charge.key in known:
                continue
            known.add(charge.key)
            self.charges.append(charge)
            added += 1
        return added

    def order_by_id(self, order_id: str | None) -> Order | None:
        """The order with this ID, when it was parsed from one of the pages."""
        index = self._index_of(order_id)
        return self.orders[index] if index is not None else None

    def charges_for_order(self, order_id: str | None) -> list[AmazonCharge]:
        """Every charge row that named this order (one per shipment/tender)."""
        if not order_id:
            return []
        return [charge for charge in self.charges if charge.order_id == order_id]

    def unknown_charge_order_ids(self) -> list[str]:
        """Orders named by a charge but never seen on an orders or details page.

        These are exactly the transactions the tool can identify but not
        describe — the user can paste those order details pages to fill the
        gap. Ordered by first appearance so the prompt is stable.
        """
        missing: list[str] = []
        for charge in self.charges:
            order_id = charge.order_id
            if not order_id or order_id in missing:
                continue
            if self.order_by_id(order_id) is None:
                missing.append(order_id)
        return missing

    def _index_of(self, order_id: str | None) -> int | None:
        if not order_id:
            return None
        for index, order in enumerate(self.orders):
            if order.order_id == order_id:
                return index
        return None
