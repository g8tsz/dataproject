# Discord Checkout Tracker Bot

A Discord bot that **scrapes SOLUS webhook checkout messages**, tracks purchases per user, and lets you plug in your own pricing formula. Automatically parses checkout embeds, applies member pricing, and generates summaries and invoices.

---

## Features

- **Auto-parse checkouts** — Watches designated channels for SOLUS webhook embeds and extracts user, item, site, and profile
- **Discord profiles** — Profiles created when users join; store address/state/zip only (no card/VCC data)
- **Canonical profile matching** — SOLUS profile `"UserName PKC #1"` matches canonical `username` (username without numbers)
- **Balance tracking** — `!paid` / `!owe` to record payments and adjustments; `!balance` shows owed, paid, and current balance
- **Checkout stats** — Total, success, and declined counts per user
- **User & profile tracking** — Link profiles to users; auto-infer user from profile when embed has no @user
- **Display names & aliases** — Set display names for invoices; use aliases so `!summary shortcut` = `!summary UserName`
- **Custom pricing formula** — Configurable margin multiplier (e.g., 50% profit split)
- **Manual price input** — Set retail/market by item keyword, by site, or by **site + item** together (different MSRP per retailer)
- **Summary & invoices** — View all users or per-user totals; generate detailed invoices
- **Post to channel** — Bulk post all user invoices to a billing channel
- **CSV export** — Export all checkouts to a spreadsheet
- **SQLite persistence** — Data survives restarts; duplicate detection prevents double-counting

---

## Requirements

- Python 3.10+
- A Discord Bot (create one at [Discord Developer Portal](https://discord.com/developers/applications))

---

## Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/g8tsz/dataproject.git
cd dataproject
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your config

```bash
cp config.example.py config.py
```

Edit `config.py` with your values:

| Setting | Description |
|---------|-------------|
| `BOT_TOKEN` | Your bot token from the [Discord Developer Portal](https://discord.com/developers/applications) → your app → Bot → Reset Token |
| `CHECKOUT_CHANNEL_IDS` | List of channel IDs where SOLUS posts checkout webhooks (e.g. `[123456789, 987654321]`) |
| `COMMAND_PREFIX` | Prefix for commands (default `!`) |
| `PRICE_MARGIN_MULTIPLIER` | Fraction of (market − retail) added to retail for member cost (e.g. `0.50` = 50% profit split) |

**Finding your Channel ID:**  
Enable Developer Mode in Discord (Settings → App Settings → Advanced → Developer Mode). Right-click the channel → Copy Channel ID.

### 4. Configure the Discord bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications) → your application
2. **Bot** tab → enable **Message Content Intent** and **Server Members Intent** (required for profiles and avatars)
3. **OAuth2** → **URL Generator** → Scopes: `bot`; Permissions: `Read Messages/View Channels`, `Send Messages`, `Use Slash Commands`
4. Use the generated URL to invite the bot to your server

### 5. Run the bot

```bash
python bot.py
```

You should see: `✅ Logged in as YourBot#1234`

---

## Commands

### Admin only

| Command | Example | Description |
|---------|---------|-------------|
| `!setprice` | `!setprice "Bowman's Best" 149.99 219.99` | Set retail & market price for all checkouts matching that item keyword |
| `!setpricesite` | `!setpricesite Toppsus 149.99 219.99` | Bulk-set retail & market for all checkouts from a site |
| `!setpricesiteitem` | `!setpricesiteitem Target "Prismatic Evolutions Booster Bundle" 31.99 75` | Set retail & market only when **both** site and item match (substring match; use `!export` to see exact strings) |
| `!summary` | `!summary` | Show everyone's checkout totals |
| `!summary UserName` | `!summary UserName` | Show totals for a specific user (accepts aliases) |
| `!invoice` | `!invoice UserName` | Generate a detailed invoice for one user (accepts aliases) |
| `!postinvoices` | `!postinvoices #billing` | Post all user invoices to the specified channel |
| `!export` | `!export` | Export all checkouts to CSV (uploaded as attachment) |
| `!setaddress` | `!setaddress UserName 123 Main St CA 90210` | Set address, state, zip for a user |
| `!paid` | `!paid UserName 25` or `!paid UserName 25 note text` | Record a payment (reduces balance); anything after the amount is stored as a note |
| `!owe` | `!owe UserName 25` or `!owe UserName 25 note text` | Add to balance (fees, adjustments); anything after the amount is stored as a note |
| `!stats` | `!stats [username]` | Show checkout stats (total, success, declined) |
| `!syncprofiles` | `!syncprofiles` | Create profiles for all current members |
| **User & profile tracking** | | |
| `!linkprofile` | `!linkprofile UserName "ProfileName"` | Link a profile to a user |
| `!unlinkprofile` | `!unlinkprofile "ProfileName"` | Remove a profile → user mapping |
| `!whois` | `!whois "ProfileName"` | Show which user is linked to that profile |
| `!setname` | `!setname UserName Display Name` | Set display name for invoices |
| `!alias` | `!alias UserName shortcut` | Add alias so `!summary shortcut` works |
| `!unalias` | `!unalias shortcut` | Remove an alias |
| `!reassignprofile` | `!reassignprofile "ProfileName" UserName` | Reassign orphan checkouts to a user |
| `!users` | `!users` | List all tracked users with profiles, aliases, and display names |

### All users

| Command | Example | Description |
|---------|---------|-------------|
| `!profile` | `!profile` or `!profile UserName` | Show profile (address, stats, balance) with PFP |
| `!balance` | `!balance` or `!balance @user` | Show balance: owed, paid, adjustments |
| `!myaddress` | `!myaddress 123 Main St CA 90210` | Set your own address, state, zip |

---

## Workflow

```
SOLUS webhooks fire in #checkouts
         │
         ▼
   Bot auto-parses every embed
   (user, item, site, profile)
         │
         ▼
  You run:  !setprice "Bowman's Best" 149.99 219.99
            !setpricesite Toppsus 149.99 219.99
            !setpricesiteitem Target "Item name" 31.99 75
         │
         ▼
  Formula auto-calculates member cost
  member_cost = retail + ((market - retail) × PRICE_MARGIN_MULTIPLIER)
         │
         ▼
  !summary          → See everyone at a glance
  !invoice User     → Detailed per-person breakdown
  !postinvoices #ch → Push all invoices to a channel
```

---

## Pricing Formula

The bot uses:

```
member_cost = retail + ((market - retail) × PRICE_MARGIN_MULTIPLIER)
```

Example with `PRICE_MARGIN_MULTIPLIER = 0.50`:
- Retail: $149.99, Market: $219.99  
- Profit: $70.00  
- Member pays: $149.99 + ($70 × 0.50) = **$184.99**

Adjust `PRICE_MARGIN_MULTIPLIER` in `config.py` to match your split (e.g. `0.75` for 75% of profit).

---

## File Structure

```
dataproject/
├── bot.py              # Main bot logic
├── config.example.py   # Example config (copy to config.py)
├── requirements.txt   # Python dependencies
├── README.md           # This file
├── .gitignore          # Excludes config.py, checkouts.db
└── checkouts.db        # SQLite DB (created on first run; do not commit)
```

---

## Discord profiles

Profiles are **created automatically when users join** the server. Run `!syncprofiles` to create profiles for existing members.

- **Stored data:** `discord_id`, `canonical_name` (username without numbers), `username`, `address`, `state`, `zip`
- **Not stored:** No card or VCC data is ever stored
- **Canonical matching:** SOLUS profile `"UserName PKC #1"` normalizes to `username` and matches the profile. Name your SOLUS profiles after your Discord username (without numbers) for automatic matching.
- **PFP:** Profile and balance embeds show the user's Discord avatar in the top right.

## Balance tracking

- **Owed from checkouts** = sum of member costs for that user's checkouts
- **Paid** = payments recorded via `!paid`
- **Owe** = adjustments (fees, manual adds) via `!owe`
- **Balance** = owed + owe − paid

Invoices and post-invoices show the current balance in the footer when it differs from the invoice total.

## User & profile tracking

When a checkout embed has **profile but no @user**, the bot infers the user from:

1. **profile_mappings** — Manual links or auto-learned when both @user and profile are present
2. **Canonical match** — Profile `"UserName PKC #1"` → `username` → matches `user_profiles.canonical_name`

**Display names** — Use `!setname UserName Display Name` so invoices show "Display Name" instead of @UserName.

**Aliases** — Use `!alias UserName shortcut` so `!summary shortcut` and `!invoice shortcut` resolve to UserName.

---

## Embed Format (SOLUS webhook)

The bot expects embeds with:

- **Description:** Contains `@Username` or a Discord mention (`<@userId>`), and optionally status (✅ / Success)
- **Fields:** `Site`, `Item`, `Profile` (case-insensitive). Common aliases are recognized (e.g. Store/Retailer → site, Product → item).

If your webhook format differs, you may need to tweak `parse_checkout_embed()` in `bot.py`.

---

## Security

- **Never commit `config.py`** — It contains your bot token. Use `config.example.py` as a template.
- **No card/VCC storage** — Profiles store only address, state, zip. Card data is never collected or stored.
- Add `config.py` and `checkouts.db` to `.gitignore` (already included).

---

## License

MIT — Use and modify as needed.
