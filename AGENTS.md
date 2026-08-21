# AGENTS.md

Repository instructions for coding agents working on this project.
`CLAUDE.md` may point here for compatibility.

## Project Overview

This is a Python CLI tool that matches Amazon orders to YNAB transactions with item-level memo enrichment and guided categorization.

## Setup and Validation Commands

```bash
# Install project + development dependencies
uv sync --extra dev

# Run tests
python -X utf8 -m pytest tests/ -v

# Run tests with coverage
python -X utf8 -m pytest tests/ --cov=src --cov-report=html

# Type checking (portable, repo-local)
uv run --extra dev ty check src tests

# Formatting / lint
uv run --extra dev ruff format src tests
uv run --extra dev ruff check src tests --fix

# Run locally
python -X utf8 -m ynab_amazon_categorizer
python -X utf8 src/ynab_amazon_categorizer/cli.py
```

## Architecture

Current modules:
- `src/ynab_amazon_categorizer/cli.py` - main CLI entry point and interactive flow.
- `src/ynab_amazon_categorizer/amazon_parser.py` - parsing for all three Amazon pages (orders list, order details, payments/transactions) plus page-type detection.
- `src/ynab_amazon_categorizer/amazon_data.py` - `AmazonData` aggregate that merges orders/details/charges from however many pages were pasted.
- `src/ynab_amazon_categorizer/transaction_matcher.py` - amount/date matching against order totals and against individual charges.
- `src/ynab_amazon_categorizer/memo_generator.py` - memo and order-link generation.
- `src/ynab_amazon_categorizer/ynab_client.py` - YNAB API communication.
- `src/ynab_amazon_categorizer/config.py` - environment config loading/validation.
- `src/ynab_amazon_categorizer/models.py` - typed domain and YNAB payload models.
- `src/ynab_amazon_categorizer/payloads.py` - minimal YNAB update construction.
- `src/ynab_amazon_categorizer/transactions.py` - transaction validation and Amazon-payee filtering.
- `src/ynab_amazon_categorizer/batch.py` - non-interactive matching and memo enrichment policy.

Design principles:
- Keep modules single-purpose and composable.
- Keep `cli.py` as orchestration; move business logic into focused modules.
- Prefer typed interfaces and predictable return values for API/parsing layers.
- Add tests for behavior changes before refactoring or extending logic.

Data flow:
1. User pastes any of three Amazon pages, in any order: the orders list, an
   order details page, and/or the payments/transactions page.
2. `detect_page_kind` routes each paste to its parser; `AmazonData` merges the
   results, with order-details data winning over the orders list.
3. Tool fetches uncategorized YNAB transactions *before* prompting, so the
   paste loop can report coverage and name the orders still missing details
   (`summarize_coverage`).
4. Matcher resolves each transaction to an order, charges first.
5. CLI guides category updates and split transactions.
6. Tool updates YNAB memos/categories via API.

Why three pages:
- The orders list has order *totals*; YNAB records what hit the *card*. They
  differ whenever an order ships in several packages, is partly paid by gift
  card or reward points, or is refunded — which is why amount-only matching
  against order totals leaves those transactions unmatched.
- The payments/transactions page states the order ID for each charge outright,
  so it resolves exactly those cases.
- The order details page carries the full item list with per-unit prices and
  the tax breakdown; the orders list paginates items inside each order card and
  never shows prices.

Matching and memo behavior:
- A charge match names its order outright, so it is preferred over an
  amount match against order totals; order totals remain the fallback.
- `used_order_ids` and `used_charge_keys` are tracked separately: one order
  legitimately produces several charges, hence several transactions.
  `mark_match_used` is the single consumption policy, shared by the interactive
  flow and the coverage survey so their counts cannot drift apart.
- A charge covering only part of its order is flagged as partial in display and
  memo text, since Amazon does not say which items that shipment covered.
- Transaction matching prioritizes amount match with date proximity heuristics.
- Memo generation should include item context and an order link when available.
- Missing/partial order data should degrade gracefully rather than crash updates.

## Configuration Requirements

Create a `.env` with:

```env
YNAB_API_KEY=your_api_key_here
YNAB_BUDGET_ID=your_budget_id_here
YNAB_ACCOUNT_ID=none
```

`YNAB_ACCOUNT_ID` is optional (`none` means all accounts).

## Dependencies

- `requests` - YNAB API calls
- `prompt_toolkit` - interactive CLI UX
- `python-dotenv` - `.env` loading

## Security Notes

- Never commit real API keys or `.env` contents.
- Do not print full secrets in logs, tests, or screenshots.
- When sharing examples, use placeholder credentials.

## Agent Notes

- On Windows, prefer `python -X utf8` to avoid emoji/category encoding issues.
- Focus processing on likely Amazon payees (`amazon`, `amzn`, `amz`).
- Add or update tests when behavior changes (parser, matcher, memo generation, API payloads).
- Never commit real Amazon page copies or fixtures built from them: they carry names,
  addresses, card last-4 digits, and real order IDs. Test fixtures must use synthetic
  names and order IDs (see `tests/test_amazon_pages.py`).
