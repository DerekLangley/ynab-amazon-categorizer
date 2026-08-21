# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Amazon **payments/transactions page** support: pasting
  `amazon.com/cpe/yourpayments/transactions` maps each card charge to its order
  ID, which resolves transactions the orders page alone cannot match — an order
  billed once per shipment, one split across a gift card or reward points, and
  refunds
- Amazon **order details page** support: the complete item list with per-unit
  prices, quantities, and the order's subtotal/tax, filling in items the orders
  list page truncates behind its in-card pagination
- One paste prompt now accepts any of the three pages, in any order, as many
  times as you like — the page type is detected automatically and repeat
  sightings of an order are merged, with details-page data winning
- When a charge names an order none of the pasted pages described, the tool
  offers to take that order's details page right then
- Matched-order display shows the specific charge (amount, date, payment
  method), per-item prices, and order tax
- Split entry accepts `i` to use the matched item's price from the order
  details page as the base amount
- Batch mode enriches charge-matched transactions too, including both
  transactions of a two-shipment order
- Because transactions are now fetched first, the paste prompt reports coverage
  after every page — how many transactions it can fully describe, and a linked
  list of the exact orders whose details pages are still worth fetching — plus
  a hint at which page will close the remaining gap
- The paste loop ends once every transaction is covered instead of asking for
  another page, and says when covered orders still lack item prices so extra
  details pages remain an informed choice
- Every prompt that reports missing information now links the page that would
  supply it — the payments page for unmatched transactions, and each order's
  details page for orders with no items

### Fixed

- Page links use the configured `AMAZON_DOMAIN` instead of a hardcoded
  `amazon.com`, which was wrong for the default `amazon.ca` and every other
  non-US storefront

### Changed

- Transactions are fetched before the Amazon data prompt, and the tool exits
  immediately when there is nothing to process — no more pasting pages against
  zero transactions
- A charge that covers only part of its order is flagged in the display and in
  generated memos ("part of order"), since Amazon does not say which items a
  given shipment charge covered
- Item lists keep every distinct line item rather than spending the per-order
  cap on repeated units of one product

### Security

- `.gitignore` now excludes saved Amazon page copies (`Local-Data/`,
  `orders.txt`, `order-details.txt`, `transactions.txt`, `*.amazon.txt`), which
  contain names, addresses, card last-4 digits, and real order IDs

## [2.4.2] - 2026-07-10

### Added

- Display tool version number in configuration summary at startup

## [2.4.1] - 2026-07-10

### Added

- `--include-reconciled` flag to also surface already-reconciled Amazon transactions
- Split entry accepts a pre-tax base price and auto-adds sales tax by category (`=` prefix for an exact total; rates overridable via `YNAB_DEFAULT_TAX_RATE` / `YNAB_GROCERY_TAX_RATE`)
- `YNAB_SKIP_SPLIT_PROMPT_SINGLE_ITEM` env flag to skip the split prompt for single-item transactions
- Category selection now needs two consecutive blank Enters to cancel, instead of one
- Transactions with no matching parsed order are auto-skipped (with a run-end summary) instead of prompting blind
- `Ctrl+C` now exits cleanly instead of an uncaught traceback

### Fixed

- Quantity-badge items now expand to one entry per unit instead of collapsing to a single item
- Near-duplicate item lines (reworded alt-text/title pairs) are deduped by token overlap instead of exact/prefix matching
- Single-word variants of the same listing (e.g. "Black" vs "White") are kept as separate items instead of being merged as duplicates
- Markdown-link-wrapped order copies now parse correctly
- Recommendation carousels and footer nav after real order content are fully trimmed, not just the copyright line
- Delivery-status lines ("Your package was...") no longer misidentified as items
- `skip_words`/product-signal matching uses word boundaries, so "cart"/"prime" no longer false-match inside "Carton"/"Primer", etc.
- Fixed an accidentally case-insensitive all-caps check that wrongly rejected ordinary all-letter product titles
- Cancelled-order detection is now case-insensitive
- A raw `OSError` from a YNAB API call is now caught and reported like other API errors instead of crashing the session

## [2.4.0] - 2026-07-10

### Added

- Typed domain and YNAB update models, plus dedicated minimal payload builders.
- A batch dry-run smoke test and a 65% minimum coverage gate in CI.
- Digital and subscription order parsing, qualified-dollar/pound/euro totals, and day-first English order dates.
- CI now installs the built wheel in a clean environment and smoke-tests its CLI entry point.
- Focused transaction-selection and batch-processing modules extracted from the interactive CLI.

### Changed

- Interactive order matches now reject exact-amount orders more than 14 days away.
- CLI and package descriptions now reflect guided categorization rather than automatic category suggestions.
- `.env` discovery is limited to the current working directory instead of silently searching parent directories.

### Fixed

- Batch enrichment now preserves existing memo text, remains idempotent, and skips updates that would truncate user content.
- Transaction updates no longer resend unrelated date, amount, payee, category, clearing, flag, or import fields from stale snapshots.
- Invalid or structurally unexpected YNAB responses now raise a typed API response error instead of crashing or appearing as empty data.
- YNAB transaction collections now validate required IDs, account IDs, ISO dates, milliunit amounts, and consumed optional fields before processing.
- Amazon payee detection now uses standalone merchant markers, avoiding false positives such as “Ramzi Market.”
- Ctrl+C and EOF at interactive prompts now exit cleanly with status 130 instead of exposing a traceback.
- International currency prefixes are preserved in order and transaction displays, and recommendation footers using any accepted currency no longer leak suggested products into order items.

## [2.3.1] - 2026-07-09

### Fixed

- Amazon order item extraction now stops at unparsed order-like blocks and post-pagination recommendation sections, preventing unrelated page content from leaking into YNAB memos.

## [2.3.0] - 2026-06-19

### Added

- `--dry-run` flag: preview every update without sending changes to YNAB
- `--batch` flag: non-interactively auto-set memos (items + order link) for confidently matched transactions, leaving categories unchanged; combine with `--dry-run` to preview
- CI workflow running tests (Ubuntu + Windows, Python 3.12 & 3.13), lint, and type checks on every push and pull request

### Changed

- All interactive prompts now go through `prompt_toolkit` for consistent input handling (replaces the builtin `input()`)

### Fixed

- A matched Amazon order is no longer reused for multiple same-amount transactions in a single run
- "Subscribe & Save" / delivery-management lines and refund-status lines are no longer extracted as order items

## [2.2.4] - 2026-05-13

### Fixed

- Order totals with thousands separators (e.g. `$1,234.56`) are now parsed correctly
- Single-category matched orders now show all item names in the suggested memo, not just the first
- Transaction amount is displayed with the correct sign (no spurious negation)

### Changed

- Minimum supported Python version clarified to 3.12+ in README and DEVELOPMENT.md

## [2.2.3] - 2026-05-05

### Fixed

- Footer and nav boilerplate from the Amazon orders page (copyright lines, address blocks, credit card promos, accessibility hints) is no longer extracted as order items

## [2.2.2] - 2026-04-23

### Fixed

- Pin `ty==0.0.14` in dev dependencies so CI uses the same type-checker version as the lock file

## [2.2.1] - 2026-04-23

### Fixed

- Cancelled orders no longer cause their items to bleed into the preceding valid order
- Quantity badge numbers (e.g. trailing ` 2`, ` 4`) are stripped from item names when the bare form also appears, without corrupting size numbers like `Size 10`
- `"Now arriving today X:XX p.m."` delivery status lines are no longer extracted as item names

## [2.2.0] - 2026-02-23

### Fixed

- Comprehensive audit fixes across all modules
- Preserve Amazon order link in memo truncation when prefix space is too small

## [2.1.8] - 2026-02-05

### Fixed

- Ruff formatting compliance for CI

## [2.1.7] - 2026-02-04

### Changed

- Extracted testable functions from `cli.py` for better modularity
- Added retry logic to YNAB API client (3 retries with backoff on 429/5xx)
- Added stdlib logging throughout the codebase

### Added

- Comprehensive test suite for extracted CLI functions
