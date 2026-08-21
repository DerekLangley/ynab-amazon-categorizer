"""Non-interactive memo enrichment policy."""

import logging
from collections.abc import Mapping, Sequence
from typing import Any

import requests

from .amazon_data import AmazonData
from .exceptions import YNABAPIError
from .memo_generator import MemoGenerator, build_batch_memo
from .models import format_currency_amount
from .payloads import build_memo_only_payload
from .transaction_matcher import TransactionMatcher
from .ynab_client import YNABClient

logger = logging.getLogger(__name__)


def process_batch(
    transactions: Sequence[Mapping[str, Any]],
    amazon_data: AmazonData | None,
    memo_generator: MemoGenerator,
    ynab_client: YNABClient,
    dry_run: bool = False,
) -> tuple[int, int, int]:
    """Auto-enrich confidently matched memos without changing categories."""
    matcher = TransactionMatcher()
    data = amazon_data if amazon_data is not None else AmazonData()
    used_order_ids: set[str] = set()
    used_charge_keys: set[tuple[str, str, str]] = set()
    enriched = skipped = failed = 0

    for transaction in transactions:
        amount_float = transaction["amount"] / 1000.0
        order = matcher.resolve_confident_order(
            amount_float,
            transaction["date"],
            data,
            used_order_ids,
            used_charge_keys,
        )
        if order is None:
            skipped += 1
            continue

        # A confident match belongs to this transaction even when enrichment is
        # unnecessary or impossible. Do not let a later same-amount transaction
        # reuse it merely because this transaction does not get updated. A
        # charge-based match retires only that charge row, since a
        # multi-shipment order rightly matches several transactions.
        if order.matched_charge is not None:
            used_charge_keys.add(order.matched_charge.key)
        elif order.order_id:
            used_order_ids.add(order.order_id)

        original_memo = transaction.get("memo")
        existing_memo = original_memo if isinstance(original_memo, str) else ""
        memo = build_batch_memo(
            order,
            memo_generator,
            existing_memo,
        )
        if memo is None:
            logger.info(
                "Skipping transaction %s because enrichment would truncate its memo.",
                transaction["id"],
            )
            skipped += 1
            continue

        if memo == existing_memo:
            logger.info(
                "Skipping transaction %s because its memo is already enriched.",
                transaction["id"],
            )
            skipped += 1
            continue

        payload = build_memo_only_payload(
            memo, bool(transaction.get("approved", False))
        )
        payee = transaction.get("payee_name", "N/A")
        summary = memo.splitlines()[0] if memo else ""
        amount_display = format_currency_amount(amount_float, order.currency)

        if dry_run:
            print(f"  [dry-run] would enrich {payee} {amount_display}: {summary}")
            enriched += 1
            continue

        try:
            ynab_client.update_transaction(transaction["id"], payload)
            print(f"  ✓ Enriched {payee} {amount_display}: {summary}")
            enriched += 1
        except (YNABAPIError, requests.exceptions.RequestException, OSError) as exc:
            logger.error("Failed to enrich transaction %s: %s", transaction["id"], exc)
            print(f"  ✗ Failed to enrich {payee} {amount_display}: {exc}")
            failed += 1

    return enriched, skipped, failed
