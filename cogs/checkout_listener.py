"""Listens to checkout channels and records SOLUS webhook embeds."""

import discord
from discord.ext import commands

from common import (
    CHECKOUT_CHANNEL_IDS,
    _ensure_profile_mapping,
    _resolve_profile_to_user,
    _resolve_profile_to_user_canonical,
    checkout_hash,
    get_connection,
    parse_checkout_embed,
)


class CheckoutListener(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.channel.id not in CHECKOUT_CHANNEL_IDS:
            await self.bot.process_commands(message)
            return
        for embed in message.embeds:
            parsed = parse_checkout_embed(embed, message)
            if parsed:
                user = parsed.get("user")
                profile = parsed.get("profile")
                mid = parsed.pop("_mention_user_id", None)
                if not user and mid and message.guild:
                    try:
                        mem = message.guild.get_member(int(mid)) or await message.guild.fetch_member(int(mid))
                        if mem:
                            user = mem.name
                    except (ValueError, discord.NotFound):
                        pass
                if not user and profile:
                    user = _resolve_profile_to_user(profile)
                if not user and profile:
                    user = _resolve_profile_to_user_canonical(profile)
                if not user:
                    user = "Unknown"
                if user and user != "Unknown" and profile:
                    _ensure_profile_mapping(profile, user)
                chash = checkout_hash({**parsed, "user": user})
                conn = get_connection()
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO checkouts (checkout_hash, timestamp, user, site, item, profile, status, retail_price, market_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            chash,
                            parsed["timestamp"],
                            user,
                            parsed["site"],
                            parsed["item"],
                            parsed["profile"],
                            parsed["status"],
                            parsed["retail_price"],
                            parsed["market_price"],
                        ),
                    )
                    conn.commit()
                    if conn.total_changes > 0:
                        print(f"Tracked: {user} - {parsed['item']} on {parsed['site']}")
                finally:
                    conn.close()
        await self.bot.process_commands(message)


async def setup(bot: commands.Bot):
    await bot.add_cog(CheckoutListener(bot))
