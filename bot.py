"""
Discord Checkout Tracker Bot
Tracks SOLUS webhook checkouts, applies pricing formulas, and generates summaries/invoices.
"""

import discord
from discord.ext import commands

from common import BOT_TOKEN, CHECKOUT_CHANNEL_IDS, COMMAND_PREFIX, init_db

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


@bot.event
async def setup_hook():
    await bot.load_extension("cogs.checkout_listener")
    await bot.load_extension("cogs.pricing")
    await bot.load_extension("cogs.balance")
    await bot.load_extension("cogs.core")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   Watching channels: {CHECKOUT_CHANNEL_IDS}")


def main():
    init_db()
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
