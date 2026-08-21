# YNAB Amazon Categorizer

A Python package that assists in categorizing Amazon transactions in YNAB (You Need A Budget) with rich item information, automatic memo generation, and tab-completion for categories.

## Features

When you paste in the text from your Amazon pages:

🎯 **Smart Order Matching**: Automatically matches YNAB transactions with Amazon orders by amount and date  
🧾 **Charge-Level Matching**: Reads your Amazon payments page so transactions that *don't* equal an order total still match — multi-shipment orders, gift-card/points split payments, and refunds  
🏷️ **Per-Item Prices**: Reads an order details page for the full item list with prices and tax, so splits use real numbers  
📝 **Enhanced Memos**: Generates detailed memos with item names and direct Amazon order links  
🔄 **Intelligent Splitting**: Suggests splitting transactions with multiple items into separate categories  
⚡ **Streamlined Workflow**: Smart defaults and tab completion for fast categorization  
📚 **Digital Orders**: Recognizes physical, digital, and subscription order IDs
🌍 **UTF-8 Support**: Full emoji support in category names  
📊 **Rich Previews**: Shows category names and transaction details before updating  

The parser supports English-language Amazon order pages with month-first or
day-first English dates and `$`, `CA$`, `US$`, `£`, or `€` totals. Translated
order-page labels and comma-decimal totals are not currently supported.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or standard Python tooling
- YNAB account with API access

## Installation

### Method 1: Run with uvx (Recommended)

```bash
# Run directly without installing (fastest and cleanest)
uvx ynab-amazon-categorizer
```

### Method 2: Install as a tool

```bash
# Install globally with uv
uv tool install ynab-amazon-categorizer

# Then run
ynab-amazon-categorizer
```

### Method 3: Development Installation

```bash
# Clone the repository
git clone https://github.com/dizzlkheinz/ynab-amazon-categorizer.git
cd ynab-amazon-categorizer

# Install in development mode
uv pip install -e .
```

## Configuration Setup

After installation, you'll need to set up your YNAB API credentials:

### Configuration File (.env)
Create a `.env` file in your working directory with your credentials:
```
YNAB_API_KEY=your_api_key_here
YNAB_BUDGET_ID=your_budget_id_here
YNAB_ACCOUNT_ID=none
```

For predictable credential selection, only the current working directory is
checked; parent directories are not searched for `.env` files.

### Alternative: Environment Variables
```bash
# Windows
set YNAB_API_KEY=your_api_key_here
set YNAB_BUDGET_ID=your_budget_id_here

# Mac/Linux
export YNAB_API_KEY=your_api_key_here
export YNAB_BUDGET_ID=your_budget_id_here
```

## Getting Your YNAB Credentials

### API Key
1. Go to [YNAB Developer Settings](https://app.ynab.com/settings/developer)
2. Click "New Token"
3. Copy the generated token

### Budget ID
1. Open your budget in YNAB
2. Look at the URL: `https://app.ynab.com/[budget_id]/budget`
3. Copy the budget_id part

### Account ID (Optional)
1. Click on a specific account in YNAB
2. Look at the URL: `https://app.ynab.com/[budget_id]/accounts/[account_id]`
3. Copy the account_id part (or leave as 'none' to process all accounts)

## Usage

### Basic Usage

```bash
# Run with uvx (no installation needed)
uvx ynab-amazon-categorizer

# Or if installed as a tool
ynab-amazon-categorizer

# Or run as a Python module
python -m ynab_amazon_categorizer
```

### Dry run

Use `--dry-run` to walk through the full interactive flow and preview every
update without sending any changes to YNAB:

```bash
ynab-amazon-categorizer --dry-run
```

The tool still shows the JSON preview for each transaction but skips the API
call, so it is safe for trying the tool out or verifying matches.

### Batch mode

Use `--batch` to run non-interactively: for every transaction with a single
high-confidence order match (unique exact-amount match within ~7 days), the
tool sets the memo (items + order link) automatically and **leaves the category
unchanged**, so you can still review/categorize later. Transactions with no
match or an ambiguous match are skipped.

Existing memo text is preserved and the Amazon context is appended. If the
existing memo is too long to retain in full alongside at least the order link,
the transaction is skipped rather than truncating user data.

```bash
# Preview what batch mode would enrich
ynab-amazon-categorizer --batch --dry-run

# Apply memo enrichment
ynab-amazon-categorizer --batch
```

You still paste the Amazon pages once when prompted; `--batch` only removes
the per-transaction prompting. Charge-matched transactions are enriched too,
including both transactions of a two-shipment order.

### Workflow
1. **Provide Amazon Data** (optional but recommended):
   - Run the tool and paste one or more Amazon pages when prompted. The page
     type is detected automatically, so you can paste them in any order, and
     paste as many as you like before continuing.
   - The script will automatically match transactions with orders.

   | Page | URL | What it adds |
   | --- | --- | --- |
   | Your Orders | `/gp/css/order-history` | Order totals and item names |
   | Order details | open an order → *View order details* | Every item with its price, plus the order's tax |
   | Your Transactions | `/cpe/yourpayments/transactions` | Which card charge paid for which order |

   Select all and copy the whole page each time.

   After each paste the tool reports coverage against your pending
   transactions and links the exact orders it still needs details for:

   ```
   ✓ Transactions page: 20 new charge(s) linked to orders.

     Coverage: 5 of 8 transaction(s) matched with item details.
       • 3 matched an order with no item data.
           https://www.amazon.com/gp/your-account/order-details?...orderID=114-...
     → Paste the order details page for the order(s) listed above to get
       their items and prices.
   ```

2. **Review Matched Transactions**:
   - The script shows order details, items, and links before asking to categorize
   - For multiple items, it suggests splitting the transaction
   - When a charge covers only part of an order, it says so — the items listed
     are the whole order's, because Amazon doesn't state which items shipped
     under which charge
   - If a charge names an order you haven't pasted a page for, the tool offers
     to take that order's details page right then

3. **Categorize Transactions**:
   - Use tab completion to select categories
   - Accept suggested memos or customize them
   - While splitting, press `i` to use the item's price from the order details
     page as the base amount
   - Confirm updates with enhanced previews

### Optional settings

| Variable | Default | Effect |
| --- | --- | --- |
| `AMAZON_DOMAIN` | `amazon.ca` | Storefront used for every Amazon link |
| `YNAB_SKIP_SPLIT_PROMPT_SINGLE_ITEM` | `true` | Skip "Split this transaction?" when the order holds one item. Set to `false` to always be asked. Transactions with no item data are asked either way. |

### Keyboard Shortcuts
- **Tab**: Auto-complete category names
- **Enter**: Accept defaults (categorize, use suggested memo, confirm update)
- **Alt+Enter**: Submit multiline input (Amazon page data, custom memos)
- **Ctrl+C**: Cancel current operation

## Example Output

```
🎯 MATCHED ORDER FOUND:
   Order ID: 702-8237239-1234567
   Total: $57.57
   Date: July 31, 2025
   Order Link: https://www.amazon.ca/gp/your-account/order-details?ie=UTF8&orderID=702-8237239-1234567
   Items:
     - Fancy Feast Grilled Wet Cat Food, Tuna Feast - 85 g Can (24 Pack)
     - Fancy Feast Grilled Wet Cat Food, Salmon & Shrimp Feast in Gravy - 85 g Can (24 Pack)

Action? (c = categorize/split, s = skip, q = quit, default c): 
There is more than one item in this transaction.
Split this transaction? (y/n, default n): y
```

With the transactions and order details pages pasted, a charge that is only
part of an order matches too, and items carry their prices:

```
🎯 MATCHED ORDER FOUND:
   Order ID: 114-1234567-1234567
   Total: $157.90
   Date: August 13, 2026
   Charge: -$115.22 on August 15, 2026 via Prime Visa ****1234
   ⚠ This charge covers PART of the order — the items below are the whole order.
   Order Link: https://www.amazon.com/gp/your-account/order-details?ie=UTF8&orderID=114-1234567-1234567
   Order tax: $11.52
   Items:
     - Caribou Coffee Caribou Blend, Keurig K-Cup Pods, 32 Count — $19.99
     - Large Ceramic Coffee Mug Set of 4, 16 oz Tea Cups with Handle — $19.99
     - Amazon Basics Square Sticky Notes, 3x3 Inches, 12-Pack — $7.19
```

## Generated Memos

### Single Item Transaction
```
Fancy Feast Grilled Wet Cat Food, Tuna Feast - 85 g Can (24 Pack)
 https://www.amazon.ca/gp/your-account/order-details?ie=UTF8&orderID=702-8237239-0563450
```

### Split Transaction Main Memo
```
2 Items:
- Fancy Feast Grilled Wet Cat Food, Tuna Feast - 85 g Can (24 Pack)
- Fancy Feast Grilled Wet Cat Food, Salmon & Shrimp Feast in Gravy - 85 g Can (24 Pack)
```

## Security Notes

⚠️ **Important**: Never commit your `.env` file to version control!

⚠️ Saved copies of Amazon pages contain your name, shipping address, card
last-4 digits, and real order IDs. Keep them out of version control —
`.gitignore` excludes the obvious filenames, but check before committing.

- The script loads credentials from environment variables or config file
- Your API key is never hardcoded in the script
- Add `.env` to your `.gitignore` if using git

## Troubleshooting

### "No orders could be parsed"
- Make sure you're copying the full Amazon orders page content
- Try copying from a different browser or clearing browser cache

### "No matching order found" for transactions that clearly are Amazon orders
YNAB records what hit your card; the orders page shows what the *order* cost.
Those differ when an order ships in several packages (billed once per package),
when part of it is paid with a gift card or reward points, or when it's a
refund. Paste the **Your Transactions** page
(`https://www.amazon.com/cpe/yourpayments/transactions`) — it states the order
ID for every individual charge, which resolves all of those cases.

### Matched order shows fewer items than the order really had
The orders list page paginates items inside each order card, so a large order
shows only some of them. Paste that order's **details** page for the full list
with per-item prices.

### "API Key not found"
- Verify your `.env` file exists and has the correct format
- Check that your API key is valid in YNAB Developer Settings

### "No transactions found"
- Ensure you have uncategorized Amazon transactions in YNAB
- Check that the payee names contain "amazon", "amzn", or "amz"

### Emoji display issues
- Use `python -X utf8` on Windows for proper emoji support
- Ensure your terminal supports UTF-8 encoding

## Contributing

This package was developed to streamline YNAB Amazon transaction categorization. Feel free to suggest improvements or report issues!

## License

This project is licensed under the GNU General Public License v3.0 (GPL-3.0). See the [LICENSE](LICENSE) file for details.

Please respect YNAB's API terms of service when using this software.
