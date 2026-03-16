"""
Copy this file to config.py and fill in your values.
Never commit config.py to version control.
"""

# Required: Your Discord bot token (from https://discord.com/developers/applications)
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"

# Channel IDs where SOLUS webhook checkouts are posted
CHECKOUT_CHANNEL_IDS = [123456789, 987654321]

# Command prefix (e.g., !summary, !setprice)
COMMAND_PREFIX = "!"

# Pricing: fraction of (market - retail) added to retail for member cost
# 0.50 = 50% of profit split; 0.75 = 75% of profit
PRICE_MARGIN_MULTIPLIER = 0.50

# Admin log channel: payment and balance-adjustment logs (optional; set to None to disable)
ADMIN_LOG_CHANNEL_ID = None  # e.g. 1234567890123456789
