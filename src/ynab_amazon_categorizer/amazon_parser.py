"""Amazon order parsing functionality."""

import logging
import re
from datetime import datetime
from typing import Literal

from .models import AmazonCharge, Order, OrderItem, expand_items

logger = logging.getLogger(__name__)

# Maximum items to extract per order (keeps memos manageable)
MAX_ITEMS_PER_ORDER = 10

ORDER_START_LABEL = r"(?:Order placed|Subscription charged on|Digital order placed)"
ORDER_DATE_PATTERN = r"(?:[A-Za-z]+ \d{1,2}, \d{4}|\d{1,2} [A-Za-z]+ \d{4})"
CURRENCY_PREFIX_PATTERN = r"(?:C(?:A|DN)\$|US\$|[$£€])"
ORDER_ID_PATTERN = r"(?:(?:\d{3}|D\d{2})-\d{7}-\d{7})"

ORDER_HEADER_PATTERN = re.compile(
    rf"{ORDER_START_LABEL}\s*"
    rf"(?P<date>{ORDER_DATE_PATTERN})\s*"
    rf"Total\s*(?P<currency>{CURRENCY_PREFIX_PATTERN})\s*"
    rf"(?P<total>[0-9][0-9,]*(?:\.[0-9]{{1,2}})?)\s*"
    rf".*?Order #\s*(?P<order_id>{ORDER_ID_PATTERN})",
    re.DOTALL | re.IGNORECASE,
)

ORDER_CONTENT_BOUNDARY_PATTERN = re.compile(
    rf"^\s*{ORDER_START_LABEL}\b",
    re.IGNORECASE | re.MULTILINE,
)

ORDER_TAIL_SENTINEL_PATTERN = re.compile(
    r"^\s*(?:"
    r"[←<]?\s*Previous\b.*|"
    r"Next set of slides\b.*|"
    r"Next\s*[→>]?\s*$|"
    r"Sponsored\s*$|"
    rf"Learn more[ \t]*(?:\r?\n)[ \t]*{CURRENCY_PREFIX_PATTERN}\s*\d|"
    r"Top .+ For You\s*$|"
    r"Customers who (?:viewed|bought)\b.*|"
    r"Continue series you\b.*|"
    r"Your Browsing History\b.*|"
    r"Back to top\b.*|"
    r"Get to Know Us\s*$|"
    r"Make Money with Us\s*$|"
    r"Amazon Payment Products\s*$|"
    r"Let Us Help You\s*$"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

AMOUNT_PATTERN = r"[0-9][0-9,]*(?:\.[0-9]{1,2})?"

# --- Payments/transactions page ---------------------------------------------
# Rows look like "Prime Visa ****1234-$42.68" / "Amazon Gift Card-$15.61" /
# "Prime Visa ****1234+$19.36": the payment method and the signed amount are
# copied as one line with no separator, followed by "Order #..." (or
# "Refund: Order #...") and then the merchant descriptor.
CHARGE_AMOUNT_PATTERN = re.compile(
    rf"^\s*(?P<method>.*?)\s*(?P<sign>[-+])\s*"
    rf"(?P<currency>{CURRENCY_PREFIX_PATTERN})\s*(?P<amount>{AMOUNT_PATTERN})\s*$"
)
CHARGE_ORDER_PATTERN = re.compile(
    rf"^\s*(?P<refund>Refund:)?\s*Order\s*#\s*(?P<order_id>{ORDER_ID_PATTERN})\s*$",
    re.IGNORECASE,
)
DATE_HEADING_PATTERN = re.compile(rf"^\s*(?P<date>{ORDER_DATE_PATTERN})\s*$")
# Lines between the "Order #" row and the next charge that are page furniture
# rather than the merchant descriptor.
CHARGE_STATUS_PATTERN = re.compile(
    r"^\s*(?:Completed|In Progress|Pending|Processing|Transactions|"
    r"Refunded|Cancell?ed)\s*$",
    re.IGNORECASE,
)
# How many lines after the amount to keep looking for its "Order #" row before
# giving up, so an extra blank/decoration line does not drop a real charge.
MAX_LINES_AMOUNT_TO_ORDER = 3

# --- Order details page ------------------------------------------------------
ORDER_DETAILS_HEADER_PATTERN = re.compile(
    rf"{ORDER_START_LABEL}\s*:?\s*(?P<date>{ORDER_DATE_PATTERN})"
    rf".{{0,200}}?Order\s*#\s*(?P<order_id>{ORDER_ID_PATTERN})",
    re.DOTALL | re.IGNORECASE,
)
ORDER_DETAILS_URL_PATTERN = re.compile(
    rf"order-details\?[^\s]*orderID=(?P<order_id>{ORDER_ID_PATTERN})",
    re.IGNORECASE,
)
SOLD_BY_PATTERN = re.compile(r"^\s*Sold by:", re.IGNORECASE)
PRICE_LINE_PATTERN = re.compile(
    rf"^\s*(?P<currency>{CURRENCY_PREFIX_PATTERN})\s*(?P<amount>{AMOUNT_PATTERN})\s*$"
)

# Order-summary rows. Each label may be followed by its amount on the same line
# or on the next one, depending on how the page was copied.
SUMMARY_LABELS: dict[str, tuple[str, ...]] = {
    "subtotal": (r"Item\(s\) Subtotal", r"Items? Subtotal", r"Subtotal"),
    "tax": (
        r"Estimated tax to be collected",
        r"Estimated tax",
        r"Tax Collected",
        r"Tax",
    ),
    "total": (r"Grand Total", r"Order Total", r"Total for this Order"),
}

# Sanity cap for a detected quantity badge (see _deduplicate_and_badge_filter).
# Above this, a trailing number is more likely a coincidental part of the
# title (e.g. a model number) than a genuine "you bought N of these" badge.
MAX_REASONABLE_BADGE_QTY = 12

# How many charge rows a page needs before it is called a transactions page on
# structure alone (i.e. without its URL). One coincidental amount/order pair is
# not enough; a real payments page always lists many.
MIN_CHARGE_ROWS_FOR_DETECTION = 2

PageKind = Literal["orders", "details", "transactions", "unknown"]


def detect_page_kind(text: str) -> PageKind:
    """Identify which Amazon page a pasted blob came from.

    Lets the tool accept the orders list, an order details page, and the
    payments/transactions page through one prompt instead of asking the user
    to declare which is which. Checks run most-distinctive first: the
    transactions page has no order headers at all, and a details page has no
    "Total" beside its order header, so the three are mutually exclusive in
    practice.
    """
    if not text.strip():
        return "unknown"

    normalized = _normalize_markdown_text(text)

    if "/yourpayments/transactions" in normalized.lower():
        return "transactions"

    if ORDER_DETAILS_URL_PATTERN.search(normalized):
        return "details"

    if ORDER_HEADER_PATTERN.search(normalized):
        return "orders"

    charge_rows = 0
    lines = normalized.split("\n")
    for index, line in enumerate(lines):
        if not CHARGE_AMOUNT_PATTERN.match(line):
            continue
        if any(
            CHARGE_ORDER_PATTERN.match(following)
            for following in lines[index + 1 : index + 1 + MAX_LINES_AMOUNT_TO_ORDER]
        ):
            charge_rows += 1
    if charge_rows >= MIN_CHARGE_ROWS_FOR_DETECTION:
        return "transactions"

    if ORDER_DETAILS_HEADER_PATTERN.search(normalized) and (
        SOLD_BY_PATTERN.search(normalized)
        or re.search(r"^\s*Order Summary\s*$", normalized, re.IGNORECASE | re.MULTILINE)
        or re.search(r"^\s*Grand Total", normalized, re.IGNORECASE | re.MULTILINE)
    ):
        return "details"

    return "unknown"


def _normalize_markdown_text(text: str) -> str:
    """Strip markdown-link and bullet-marker syntax from a full page of text.

    Some order-history copies (e.g. from a markdown-rendering copy tool)
    wrap every line as "* [Visible Text](https://...)". Left as-is, this
    breaks the order-header regex below (a "* " bullet in front of "TOTAL"
    or "ORDER #" stops the \\s*-only gaps in that pattern from matching
    through it) as well as per-item extraction (skip_patterns are anchored
    to the start of the line, so a leading "* [" hides "Buy it again",
    "View", etc., and the raw URL would otherwise get glued onto extracted
    item text). Applied once up front so every downstream check — the order
    header, cancelled-order detection, and item extraction — sees plain text.
    """
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1 ", text)  # [text](url) -> text
    text = re.sub(r"(?m)^[ \t]*[-•*]\s*", "", text)  # leading bullet markers
    return text


# Amazon sometimes emits an image's alt-text line and the product-link text
# for the *same* item as two separate lines that are reworded/reordered
# rather than character-for-character identical (e.g. "Soft Pink" vs.
# "Soft Fashion Pink", clauses in a different order). A plain string-equality
# or quantity-badge check won't catch that, so near-duplicates are detected
# by token overlap (order-independent) instead. Threshold picked from real
# examples: true alt-text/title pairs for the same item score ~0.85-0.95;
# genuinely different items (even same brand) score well under 0.3.
NEAR_DUPLICATE_JACCARD_THRESHOLD = 0.7

# Real alt-text/title pairs can score as low as ~0.6 (well within range of a
# genuinely different same-brand item's title, e.g. two different sizes of
# the same listing) — text similarity alone can't cleanly separate the two
# cases. But there's a reliable structural marker: in real order-history
# copies, an item's image alt-text line has a leading space, and the
# following (unindented) title-link line for the *same* item never does.
# When that pattern is present, this much lower floor is enough — it's only
# there to rule out two unrelated lines that coincidentally landed adjacent.
ADJACENT_DUPLICATE_JACCARD_FLOOR = 0.3


def _item_token_set(text: str) -> set[str]:
    """Lowercased alphanumeric tokens of an item line, for similarity checks."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _differs_only_numerically(a: str, b: str) -> bool:
    """True if the only tokens that differ between two lines are pure numbers.

    e.g. "...Bourbon, 38" vs. "...Bourbon, 36" — that's the "same listing,
    different size/quantity/model number" case, a real separate line item,
    not a reworded repeat of the same one, regardless of how similar the
    rest of the text is or whether the two lines are structurally adjacent.
    """
    tokens_a, tokens_b = _item_token_set(a), _item_token_set(b)
    differing_tokens = tokens_a.symmetric_difference(tokens_b)
    return bool(differing_tokens) and all(t.isdigit() for t in differing_tokens)


def _differs_by_single_word_substitution(a: str, b: str) -> bool:
    """True if the two lines differ by exactly one token swapped for another.

    e.g. "...24 oz, Black" vs. "...24 oz, White" — a color/size *word* variant
    of the same listing, i.e. a real separate line item, the word analogue of
    _differs_only_numerically. Restricted to exactly one unique token per side
    because genuine reworded alt-text/title pairs for a single item differ by
    several tokens on each side (real example: an alt/title pair with 4 vs. 7
    unique tokens), and a looser rule would wrongly split those. A prefix
    relationship between the two tokens ("Toy"/"Toys", "Color"/"Colorful")
    is treated as a spelling/plural rewording of the same item, not a variant.
    """
    tokens_a, tokens_b = _item_token_set(a), _item_token_set(b)
    only_a, only_b = tokens_a - tokens_b, tokens_b - tokens_a
    if len(only_a) != 1 or len(only_b) != 1:
        return False
    (token_a,), (token_b,) = only_a, only_b
    return not (token_a.startswith(token_b) or token_b.startswith(token_a))


def _token_overlap(a: str, b: str) -> float:
    """Jaccard similarity of two lines' token sets. 0 if either has no tokens."""
    tokens_a, tokens_b = _item_token_set(a), _item_token_set(b)
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _is_duplicate_item_pair(
    prev_item: str, prev_had_leading_space: bool, item: str, had_leading_space: bool
) -> bool:
    """True if `item` is a reworded repeat of the immediately preceding kept item.

    Prefers the structural signal (leading-space alt-text line immediately
    followed by a non-indented title line) when present, since it reliably
    separates true duplicates from genuinely different same-brand items in a
    way text similarity alone cannot (real examples overlap: a true
    duplicate can score lower than a real distinct-size variant). Falls back
    to the plain similarity check otherwise. Numeric-only and single-word
    substitutions are size/color variants of the same listing — genuinely
    separate items — and are never treated as duplicates.
    """
    if _differs_only_numerically(prev_item, item):
        return False
    if _differs_by_single_word_substitution(prev_item, item):
        return False
    if prev_had_leading_space and not had_leading_space:
        return _token_overlap(prev_item, item) >= ADJACENT_DUPLICATE_JACCARD_FLOOR
    return _token_overlap(prev_item, item) >= NEAR_DUPLICATE_JACCARD_THRESHOLD


class AmazonParser:
    """Parses Amazon order data from order history pages."""

    def _remove_cancelled_orders(self, text: str) -> str:
        """Remove cancelled order blocks so their items don't bleed into adjacent orders."""
        parts = re.split(
            rf"(?=^\s*{ORDER_START_LABEL}\b)",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        kept = []
        for part in parts:
            if (
                re.match(rf"\s*{ORDER_START_LABEL}", part, re.IGNORECASE)
                and "your order was cancelled" in part.lower()
            ):
                continue
            kept.append(part)
        return "".join(kept)

    def _normalize_order_date(self, date_str: str) -> str:
        """Normalize supported English date layouts for the matcher."""
        for date_format in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
            try:
                parsed = datetime.strptime(date_str, date_format)
                return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"
            except ValueError:
                continue
        return date_str

    def parse_orders_page(self, orders_text: str) -> list[Order]:
        """Parse Amazon orders page text to extract order information.

        Orders are kept even when item extraction fails (partial orders)
        so that amount/date matching can still work.
        """
        if not orders_text.strip():
            return []

        orders_text = _normalize_markdown_text(orders_text)

        orders_text = self._remove_cancelled_orders(orders_text)

        orders = []

        order_matches = list(ORDER_HEADER_PATTERN.finditer(orders_text))

        for idx, match in enumerate(order_matches):
            order_date = self._normalize_order_date(match.group("date").strip())
            order_total = float(match.group("total").replace(",", ""))
            order_currency = match.group("currency")
            order_id = match.group("order_id")

            # Find the content after this order until the next order-like block or end
            start_pos = match.end()
            if idx + 1 < len(order_matches):
                end_pos = order_matches[idx + 1].start()
            else:
                end_pos = len(orders_text)
            end_pos = self._find_order_content_end(orders_text, start_pos, end_pos)
            order_content = orders_text[start_pos:end_pos]

            # Extract items from the order content
            items = self.extract_items_from_content(order_content)

            # Always keep the order even without items (partial order)
            order = Order(
                order_id=order_id,
                total=order_total,
                date_str=order_date,
                items=items,
                currency=order_currency,
            )

            if not items:
                logger.info(
                    "Order %s parsed without items (amount=%.2f). "
                    "It can still match by amount/date.",
                    order_id,
                    order_total,
                )

            orders.append(order)

        return orders

    def _find_order_content_end(
        self, orders_text: str, start_pos: int, default_end: int
    ) -> int:
        """Find the earliest unparsed order-like boundary before the default end."""
        boundary = ORDER_CONTENT_BOUNDARY_PATTERN.search(
            orders_text, start_pos, default_end
        )
        if boundary:
            return boundary.start()
        return default_end

    def _trim_footer(self, order_content: str) -> str:
        """Trim at the earliest footer/recommendation-carousel sentinel.

        A full page copy includes real order content first, then Amazon's
        "recommended for you" carousels, "continue reading" carousels,
        browsing history, and the site-wide footer nav — all of which are
        long, mixed-case, multi-word lines that would otherwise pass the
        product-name heuristics below. Cutting at the *first* sentinel found
        (rather than only the copyright line at the very end) removes all of
        that in one go, since none of it is genuine order content.
        """
        footer_sentinel = ORDER_TAIL_SENTINEL_PATTERN.search(order_content)
        if not footer_sentinel:
            footer_sentinel = re.search(
                r"©\s*\d{4}|To move between items",
                order_content,
                re.IGNORECASE,
            )
        if footer_sentinel:
            return order_content[: footer_sentinel.start()]
        return order_content

    def _get_valid_cleaned_item(self, line: str) -> str | None:
        """Check if a line matches product name criteria and return the cleaned string, or None."""
        line = line.strip()
        if not line or len(line) < 15:
            return None

        # Normalize markdown link formatting before any other checks. Some
        # order-history copies (e.g. from a markdown-rendering copy tool)
        # wrap every line as "* [Visible Text](https://...)". Left as-is,
        # the leading "* [" defeats the line-start-anchored skip_patterns
        # below (e.g. "* [Buy it again](...)" no longer starts with "Buy it
        # again"), and the raw URL would otherwise get glued onto extracted
        # item text.
        line = re.sub(r"^[-•*]\s*", "", line)  # leading bullet marker
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1 ", line)  # [text](url) -> text
        line = re.sub(r"\s+", " ", line).strip()
        if not line or len(line) < 15:
            return None

        # Skip common UI elements and delivery status lines
        skip_patterns = [
            r"^(Buy it again|Track package|View|Return|Write|Get|Share|Leave|Ask)\b",
            r"^(Delivered|Arriving|Now arriving|Auto-delivered|Package was)",
            r"^(Your package|Your order|Your item|Your shipment|Your refund"
            r"|Your replacement)\b",
            r"^(Return items:|Return or replace|Refund issued|Refund:|Returned"
            r"|Return started)",
            r"^(Subscribe & Save|Subscribe now|Skip this delivery|Deliver every"
            r"|Change delivery|Manage subscription|Edit delivery|Set up now)",
            r"^\d+\.?\d* out of \d+ stars",
            r"^FREE|^Today by|^Get it|^List:|^Was:|^Limited-time deal",
            r"^\$\d+\.\d+|\(\$\d+\.\d+",
            r"^\d+ sustainability features?$",
            r"^(Ship to|Order #|View order|Invoice)",
        ]

        if any(re.match(pattern, line, re.IGNORECASE) for pattern in skip_patterns):
            return None

        # All-caps lines (e.g. shouty UI labels) are skipped too, but this
        # check must stay case-sensitive — matched under the shared
        # re.IGNORECASE above, "[A-Z]" would also match lowercase letters and
        # wrongly reject any ordinary product title made up of only letters
        # and spaces (no digits/punctuation).
        if re.match(r"^[A-Z\s]+$", line):
            return None

        # Look for product names - they usually contain specific patterns
        has_product_pattern = (
            any(
                re.search(rf"\b{re.escape(word)}\b", line, re.IGNORECASE)
                for word in [
                    "pack",
                    "count",
                    "size",
                    "oz",
                    "ml",
                    "lbs",
                    "kg",
                    "inch",
                    "cm",
                ]
            )
            or re.search(
                r"[A-Z][a-z].*[A-Z]", line
            )  # Mixed case indicating product names
            or len(line.split()) >= 5
        )  # Long descriptive lines

        if not has_product_pattern:
            return None

        # Whitespace/bullet normalization already happened above.
        cleaned_line = line

        # Skip if it looks like navigation or common elements. Word-boundary
        # matched (not plain substring) so single words like "cart" or
        # "prime" don't false-positive inside real product words — "cart"
        # is a substring of "Carton"/"Cartridge", "prime" of "Primer",
        # "orders" of "Recorders"/"Borders", etc.
        skip_words = [
            "account",
            "orders",
            "cart",
            "search",
            "hello",
            "browse",
            "prime",
            "shipping",
            "mastercard",
            "your brand",
            "registry & gift",
            "attract and engage",
            "interest-based",
        ]
        if any(
            re.search(rf"\b{re.escape(word)}\b", cleaned_line, re.IGNORECASE)
            for word in skip_words
        ):
            return None

        return cleaned_line

    def _deduplicate_and_badge_filter(
        self, candidates: list[tuple[str, bool]]
    ) -> list[str]:
        """Resolve quantity-badge duplicates and drop (near-)duplicate lines.

        ``candidates`` pairs each cleaned line with whether its *raw* source
        line had leading whitespace — see _is_duplicate_item_pair for why
        that matters. Keeps up to MAX_ITEMS_PER_ORDER entries.
        """
        # Amazon shows "Product Name <qty>" and "Product Name" on adjacent lines when
        # qty > 1. Rather than collapsing that pair to a single entry (which would
        # hide the fact that 2+ units were bought and make it impossible to split
        # them into separate line items later), the bare name is repeated once per
        # unit — capped at MAX_REASONABLE_BADGE_QTY so a coincidental trailing
        # number that isn't really a quantity (e.g. part of a model number)
        # doesn't blow up the item list.
        candidate_texts = {text for text, _ in candidates}
        seen: set[str] = set()
        unique_items: list[str] = []
        last_kept_had_leading_space = False

        for item, had_leading_space in candidates:
            badge_match = re.search(r"\s+(\d+)$", item)
            stripped = item[: badge_match.start()] if badge_match else item
            is_badge = bool(
                badge_match and stripped != item and stripped in candidate_texts
            )
            normalized = stripped if is_badge else item

            if normalized in seen or len(normalized) <= 15:
                continue
            # Skip a reworded/reordered repeat of the item we *just* kept
            # (e.g. an image alt-text line immediately followed by the
            # product-link text for the same item) rather than treating it
            # as a second, different item.
            if unique_items and _is_duplicate_item_pair(
                unique_items[-1],
                last_kept_had_leading_space,
                normalized,
                had_leading_space,
            ):
                continue

            seen.add(normalized)
            qty = int(badge_match.group(1)) if is_badge and badge_match else 1
            if qty < 1 or qty > MAX_REASONABLE_BADGE_QTY:
                qty = 1
            for _ in range(qty):
                if len(unique_items) >= MAX_ITEMS_PER_ORDER:
                    break
                unique_items.append(normalized)
            last_kept_had_leading_space = had_leading_space
            if len(unique_items) >= MAX_ITEMS_PER_ORDER:
                break
        return unique_items

    def extract_items_from_content(self, order_content: str) -> list[str]:
        """Extract item names from order content."""
        order_content = self._trim_footer(order_content)

        candidates: list[tuple[str, bool]] = []
        for line in order_content.split("\n"):
            had_leading_space = line[:1].isspace() if line else False
            cleaned = self._get_valid_cleaned_item(line)
            if cleaned:
                candidates.append((cleaned, had_leading_space))

        return self._deduplicate_and_badge_filter(candidates)

    # --- Payments/transactions page ------------------------------------

    def parse_transactions_page(self, transactions_text: str) -> list[AmazonCharge]:
        """Parse Amazon's payments/transactions page into individual charges.

        This is the page that tells us which *card charge* belongs to which
        order. The orders page only reports order totals, so a transaction can
        go unmatched whenever the charge and the order total differ — a
        multi-shipment order billed per package, an order partly paid with a
        gift card or reward points, or a refund. Each row here carries the
        order ID outright, which removes the guesswork.

        Rows without an order ID (e.g. gift card reloads) are ignored: without
        an order there is nothing to enrich a YNAB transaction with.
        """
        if not transactions_text.strip():
            return []

        lines = _normalize_markdown_text(transactions_text).split("\n")
        charges: list[AmazonCharge] = []
        seen: set[tuple[str, str, str]] = set()
        current_date: str | None = None

        index = 0
        while index < len(lines):
            line = lines[index]

            date_heading = DATE_HEADING_PATTERN.match(line)
            if date_heading:
                current_date = self._normalize_order_date(
                    date_heading.group("date").strip()
                )
                index += 1
                continue

            amount_match = CHARGE_AMOUNT_PATTERN.match(line)
            if not amount_match:
                index += 1
                continue

            order_line_index = self._find_charge_order_line(lines, index + 1)
            if order_line_index is None:
                index += 1
                continue

            order_match = CHARGE_ORDER_PATTERN.match(lines[order_line_index])
            assert order_match is not None  # guaranteed by _find_charge_order_line

            amount = float(amount_match.group("amount").replace(",", ""))
            if amount_match.group("sign") == "-":
                amount = -amount
            method = amount_match.group("method").strip() or None
            is_refund = bool(order_match.group("refund")) or amount > 0

            charge = AmazonCharge(
                amount=amount,
                date_str=current_date,
                order_id=order_match.group("order_id"),
                payment_method=method,
                merchant=self._charge_merchant(lines, order_line_index + 1),
                is_refund=is_refund,
                currency=amount_match.group("currency"),
            )
            if charge.key not in seen:
                seen.add(charge.key)
                charges.append(charge)

            index = order_line_index + 1

        if not charges:
            logger.info("No charges could be parsed from the transactions page text.")
        return charges

    def _find_charge_order_line(self, lines: list[str], start: int) -> int | None:
        """Index of the "Order #" row belonging to the charge amount before it."""
        checked = 0
        for index in range(start, len(lines)):
            if not lines[index].strip():
                continue
            if CHARGE_ORDER_PATTERN.match(lines[index]):
                return index
            checked += 1
            if checked >= MAX_LINES_AMOUNT_TO_ORDER:
                return None
        return None

    def _charge_merchant(self, lines: list[str], start: int) -> str | None:
        """Merchant descriptor following a charge's "Order #" row, when present."""
        for index in range(start, min(start + 3, len(lines))):
            candidate = lines[index].strip()
            if not candidate:
                continue
            if (
                CHARGE_AMOUNT_PATTERN.match(candidate)
                or CHARGE_ORDER_PATTERN.match(candidate)
                or DATE_HEADING_PATTERN.match(candidate)
                or CHARGE_STATUS_PATTERN.match(candidate)
            ):
                return None
            return candidate
        return None

    # --- Order details page ---------------------------------------------

    def parse_order_details_page(self, details_text: str) -> list[Order]:
        """Parse one or more Amazon order *details* pages.

        The orders list page paginates items inside each order card, so a large
        order shows only some of its items and never shows per-item prices. The
        details page has the full list with prices plus the order summary
        (subtotal/tax/grand total), which is what makes an informed split
        possible. Several pages may be pasted together; each is parsed
        separately.
        """
        if not details_text.strip():
            return []

        text = _normalize_markdown_text(details_text)
        if not self._looks_like_order_details(text):
            return []

        orders: list[Order] = []
        headers = list(ORDER_DETAILS_HEADER_PATTERN.finditer(text))
        if not headers:
            order = self._parse_single_order_details(text, None)
            return [order] if order else []

        for position, header in enumerate(headers):
            start = header.start()
            end = (
                headers[position + 1].start()
                if position + 1 < len(headers)
                else len(text)
            )
            # The URL line sits above the header, so search the whole page for
            # it when this is the only order on it.
            url_scope = text if len(headers) == 1 else text[start:end]
            order = self._parse_single_order_details(text[start:end], header, url_scope)
            if order:
                orders.append(order)

        return orders

    def _looks_like_order_details(self, text: str) -> bool:
        """Guard against handing an orders-list or unrelated page to this parser."""
        return bool(
            ORDER_DETAILS_URL_PATTERN.search(text)
            or SOLD_BY_PATTERN.search(text)
            or re.search(r"^\s*Order Summary\s*$", text, re.IGNORECASE | re.MULTILINE)
            or re.search(r"^\s*Grand Total", text, re.IGNORECASE | re.MULTILINE)
        )

    def _parse_single_order_details(
        self,
        section: str,
        header: re.Match[str] | None,
        url_scope: str | None = None,
    ) -> Order | None:
        """Build one Order from a single order-details page section."""
        order_id: str | None = None
        date_str: str | None = None
        if header:
            order_id = header.group("order_id")
            date_str = self._normalize_order_date(header.group("date").strip())
        if not order_id:
            url_match = ORDER_DETAILS_URL_PATTERN.search(url_scope or section)
            if url_match:
                order_id = url_match.group("order_id")
        if not order_id:
            logger.info("Skipping an order-details section with no order ID.")
            return None

        subtotal, _ = self._find_summary_amount(section, SUMMARY_LABELS["subtotal"])
        tax, _ = self._find_summary_amount(section, SUMMARY_LABELS["tax"])
        total, currency = self._find_summary_amount(section, SUMMARY_LABELS["total"])

        detailed_items = self._extract_detailed_items(section)
        items, item_prices = expand_items(detailed_items, MAX_ITEMS_PER_ORDER)

        if not items:
            # No "Sold by:" anchors (e.g. a digital or grocery order): fall back
            # to name-only extraction so the order is still useful for memos.
            items = self.extract_items_from_content(section)
            detailed_items = [OrderItem(name=name) for name in items]
            item_prices = [None] * len(items)

        return Order(
            order_id=order_id,
            total=total,
            date_str=date_str,
            items=items,
            currency=currency,
            detailed_items=detailed_items,
            subtotal=subtotal,
            tax=tax,
            item_prices=item_prices,
        )

    def _find_summary_amount(
        self, section: str, labels: tuple[str, ...]
    ) -> tuple[float | None, str | None]:
        """First matching order-summary amount, trying labels most-specific first.

        The amount may sit on the label's line or on the next one depending on
        how the page was copied, so both layouts are accepted.
        """
        for label in labels:
            match = re.search(
                rf"^[ \t]*{label}[ \t]*:?[ \t]*(?:\r?\n)?[ \t]*"
                rf"(?P<sign>-)?(?P<currency>{CURRENCY_PREFIX_PATTERN})[ \t]*"
                rf"(?P<amount>{AMOUNT_PATTERN})[ \t]*$",
                section,
                re.IGNORECASE | re.MULTILINE,
            )
            if match:
                amount = float(match.group("amount").replace(",", ""))
                if match.group("sign"):
                    amount = -amount
                return amount, match.group("currency")
        return None, None

    def _extract_detailed_items(self, section: str) -> list[OrderItem]:
        """Extract items with per-unit prices, anchored on each "Sold by:" row.

        Every purchased line on a details page is followed by "Sold by: <seller>"
        and then its price, which is a far more reliable anchor than the
        heuristics needed for the orders list page — and it also excludes the
        cart preview and recommendation carousels that surround the real order.
        """
        lines = self._trim_footer(section).split("\n")
        anchors = [
            index for index, line in enumerate(lines) if SOLD_BY_PATTERN.match(line)
        ]
        if not anchors:
            return []

        items: list[OrderItem] = []
        # Skip the address/payment/summary block above the first shipment so its
        # mixed-case lines (recipient name, card blurb) cannot pass for a title.
        title_start = self._items_region_start(lines)
        for position, anchor in enumerate(anchors):
            name, quantity = self._details_title(lines[title_start:anchor])
            next_anchor = (
                anchors[position + 1] if position + 1 < len(anchors) else len(lines)
            )
            price = self._first_price(lines, anchor + 1, next_anchor)
            if name:
                items.append(OrderItem(name=name, price=price, quantity=quantity))
            title_start = anchor + 1
        return items

    def _items_region_start(self, lines: list[str]) -> int:
        """Index just past the order-summary block, or 0 when it is absent."""
        for index in range(len(lines) - 1, -1, -1):
            if re.match(
                r"^\s*(?:Grand Total|Order Total|Total for this Order)\b",
                lines[index],
                re.IGNORECASE,
            ):
                # Skip the label and, when present, the amount on the next line.
                nxt = index + 1
                if nxt < len(lines) and PRICE_LINE_PATTERN.match(lines[nxt]):
                    return nxt + 1
                return nxt
        return 0

    def _details_title(self, region: list[str]) -> tuple[str, int]:
        """Title and quantity for the item whose "Sold by:" row follows ``region``.

        Amazon repeats each title twice — the image's alt text, then the product
        link — and stamps the quantity onto the alt-text copy ("...Black3"). The
        link copy is the clean one, so the *last* candidate wins, and an earlier
        candidate that is the same text plus trailing digits supplies the
        quantity.
        """
        candidates = [
            cleaned
            for cleaned in (self._get_valid_cleaned_item(line) for line in region)
            if cleaned
        ]
        if not candidates:
            return "", 1

        name = candidates[-1]
        quantity = 1
        for candidate in candidates[:-1]:
            badge = re.match(rf"^{re.escape(name)}\s*(\d+)$", candidate)
            if badge:
                parsed = int(badge.group(1))
                if 1 <= parsed <= MAX_REASONABLE_BADGE_QTY:
                    quantity = parsed
        return name, quantity

    def _first_price(self, lines: list[str], start: int, end: int) -> float | None:
        """First standalone price line in ``lines[start:end]``."""
        for index in range(start, min(end, len(lines))):
            price_match = PRICE_LINE_PATTERN.match(lines[index])
            if price_match:
                return float(price_match.group("amount").replace(",", ""))
        return None
