"""Memo generation functionality for Amazon order transactions."""

import re
from typing import Any

from .amazon_links import order_details_url
from .models import Order

# YNAB memo field maximum length (API rejects longer values)
YNAB_MEMO_MAX_LENGTH = 200


def sanitize_memo(memo: str, max_length: int = YNAB_MEMO_MAX_LENGTH) -> str:
    """Sanitize a memo string for YNAB API submission.

    - Strips control characters (except newlines)
    - Truncates to ``max_length``, preserving an Amazon order link at the
      end when possible.
    """
    if not memo:
        return ""

    # Strip control characters except \n and \r
    memo = re.sub(r"[\x00-\x09\x0b\x0c\x0e-\x1f]", "", memo)
    memo = memo.strip()

    if max_length <= 3:
        return memo[:max_length]

    if len(memo) <= max_length:
        return memo

    # Try to preserve the order link at the end
    link_match = re.search(r"(https://www\.\S+)$", memo)
    if link_match:
        link = link_match.group(1)
        available = max_length - len(link) - 4  # 4 for "\n..\n"
        if available > 10:
            return memo[:available].rstrip() + "\n..\n" + link
        if len(link) <= max_length:
            return link
    # Simple truncation with ellipsis
    return memo[: max_length - 3].rstrip() + "..."


def generate_split_summary_memo(order: Order) -> str:
    """Generate a compact summary of every parsed item in an order.

    When the transaction is only one charge against a larger order (a
    per-shipment bill, or a split payment), the items listed are the whole
    order's rather than this charge's — Amazon does not say which items each
    charge covered — so the summary says so instead of implying otherwise.
    """
    items = order.items
    if not items:
        return ""

    partial = order.is_partial_charge
    if len(items) == 1:
        return sanitize_memo(f"Part of: {items[0]}" if partial else items[0])

    header = f"{len(items)} Items" + (" (part of order)" if partial else "")
    summary = f"{header}:\n" + "\n".join(f"- {item}" for item in items)
    return sanitize_memo(summary)


def build_batch_memo(
    order: Order,
    memo_generator: "MemoGenerator",
    original_memo: str = "",
) -> str | None:
    """Build an idempotent enrichment without truncating an existing memo.

    When all order context cannot fit, retaining the existing memo plus the
    order link is preferred. ``None`` means enrichment would require losing
    existing content and the caller should skip the update.
    """
    items_text = generate_split_summary_memo(order) or "Amazon Purchase"
    order_link = memo_generator.generate_amazon_order_link(order.order_id)
    enrichment = f"{items_text}\n {order_link}" if order_link else items_text
    original = sanitize_memo(original_memo)

    if not original:
        return sanitize_memo(enrichment)

    idempotency_marker = order_link or items_text
    if idempotency_marker and idempotency_marker in original:
        return original

    candidate = f"{original}\n{enrichment}"
    if len(candidate) <= YNAB_MEMO_MAX_LENGTH:
        return sanitize_memo(candidate)

    if order_link:
        link_only_candidate = f"{original}\n{order_link}"
        if len(link_only_candidate) <= YNAB_MEMO_MAX_LENGTH:
            return sanitize_memo(link_only_candidate)

    return None


class MemoGenerator:
    """Handles memo generation for Amazon transactions."""

    def __init__(self, amazon_domain: str = "amazon.ca") -> None:
        self.amazon_domain = amazon_domain

    def generate_amazon_order_link(self, order_id: str | None) -> str | None:
        """Generate Amazon order details link"""
        return order_details_url(self.amazon_domain, order_id)

    def generate_enhanced_memo(
        self,
        original_memo: str,
        order_id: str | None,
        item_details: Any | None = None,
    ) -> str:
        """Generate enhanced memo with order information and item details"""
        memo_parts = []
        if original_memo:
            memo_parts.append(original_memo)

        if item_details:
            if isinstance(item_details, dict):
                title = item_details.get("title")
                quantity = item_details.get("quantity")
                price = item_details.get("price")

                details_str = ""
                if title:
                    details_str += str(title)
                if quantity and int(quantity) > 1:
                    details_str += f" (x{quantity})"
                if price:
                    details_str += f" - ${float(price):.2f}"

                if details_str:
                    memo_parts.append(details_str)
            elif isinstance(item_details, str):
                memo_parts.append(item_details)

        order_link = self.generate_amazon_order_link(order_id)
        if order_link:
            memo_parts.append(f"Amazon Order: {order_link}")

        raw = "\n\n".join(memo_parts) if memo_parts else ""
        return sanitize_memo(raw)
