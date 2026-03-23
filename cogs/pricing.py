"""Admin commands that set retail/market prices on checkout rows."""

import asyncio

import discord
from discord.ext import commands

from common import PRICE_CONFIRM_MIN_ROWS, get_connection


async def _confirm_bulk_price_update(
    ctx: commands.Context,
    *,
    row_count: int,
    title: str,
    detail: str,
    apply_update,
) -> bool:
    """
    If row_count >= PRICE_CONFIRM_MIN_ROWS, ask the admin to react ✅ to apply or ❌ to cancel.
    Returns True if update ran, False if skipped/cancelled/error.
    """
    if row_count == 0:
        await ctx.send("No matching checkouts — nothing to update.")
        return False

    if row_count < PRICE_CONFIRM_MIN_ROWS:
        await apply_update()
        return True

    embed = discord.Embed(
        title=title,
        description=f"This will update **{row_count}** checkout rows.\n\n{detail}\n\nReact with ✅ to confirm or ❌ to cancel (60s).",
        color=0xE67E22,
    )
    msg = await ctx.send(embed=embed)
    await msg.add_reaction("✅")
    await msg.add_reaction("❌")

    def check(reaction: discord.Reaction, user: discord.User) -> bool:
        return (
            user.id == ctx.author.id
            and not user.bot
            and reaction.message.id == msg.id
            and str(reaction.emoji) in ("✅", "❌")
        )

    try:
        reaction, _user = await ctx.bot.wait_for("reaction_add", timeout=60.0, check=check)
    except asyncio.TimeoutError:
        await ctx.send("Confirmation timed out — no prices were changed.")
        return False

    if str(reaction.emoji) == "❌":
        await ctx.send("Cancelled — no prices were changed.")
        return False

    await apply_update()
    return True


class Pricing(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="setprice")
    @commands.has_permissions(administrator=True)
    async def set_price(self, ctx: commands.Context, item_keyword: str, retail: float, market: float):
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM checkouts WHERE LOWER(COALESCE(item, '')) LIKE ?",
            (f"%{item_keyword.lower()}%",),
        )
        row_count = c.fetchone()[0]
        conn.close()

        async def apply():
            conn2 = get_connection()
            c2 = conn2.cursor()
            c2.execute(
                "UPDATE checkouts SET retail_price = ?, market_price = ? WHERE LOWER(COALESCE(item, '')) LIKE ?",
                (retail, market, f"%{item_keyword.lower()}%"),
            )
            updated = c2.rowcount
            conn2.commit()
            conn2.close()
            hint = ""
            if updated == 0:
                hint = " No rows matched — item text may differ from the keyword, or site+item pricing may fit better: `!setpricesiteitem`."
            await ctx.send(f"Updated **{updated}** checkouts matching `{item_keyword}`.{hint}")

        await _confirm_bulk_price_update(
            ctx,
            row_count=row_count,
            title="Confirm price update",
            detail=f"Item contains: `{item_keyword}`\nRetail **${retail:.2f}**, market **${market:.2f}**",
            apply_update=apply,
        )

    @commands.command(name="setpricesite")
    @commands.has_permissions(administrator=True)
    async def set_price_by_site(self, ctx: commands.Context, site: str, retail: float, market: float):
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM checkouts WHERE LOWER(COALESCE(site, '')) LIKE ?",
            (f"%{site.lower()}%",),
        )
        row_count = c.fetchone()[0]
        conn.close()

        async def apply():
            conn2 = get_connection()
            c2 = conn2.cursor()
            c2.execute(
                "UPDATE checkouts SET retail_price = ?, market_price = ? WHERE LOWER(COALESCE(site, '')) LIKE ?",
                (retail, market, f"%{site.lower()}%"),
            )
            updated = c2.rowcount
            conn2.commit()
            conn2.close()
            hint = ""
            if updated == 0:
                hint = " No rows matched — check exact site string in `!export`."
            await ctx.send(f"Updated **{updated}** checkouts from `{site}`.{hint}")

        await _confirm_bulk_price_update(
            ctx,
            row_count=row_count,
            title="Confirm site-wide price update",
            detail=f"Site contains: `{site}`\nRetail **${retail:.2f}**, market **${market:.2f}**",
            apply_update=apply,
        )

    @commands.command(name="setpricesiteitem")
    @commands.has_permissions(administrator=True)
    async def set_price_by_site_and_item(
        self,
        ctx: commands.Context,
        site_keyword: str,
        item_keyword: str,
        retail: float,
        market: float,
    ):
        """Set retail & market only for rows matching both site and item substrings (per-retailer MSRP)."""
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM checkouts WHERE LOWER(COALESCE(site, '')) LIKE ? AND LOWER(COALESCE(item, '')) LIKE ?",
            (f"%{site_keyword.lower()}%", f"%{item_keyword.lower()}%"),
        )
        row_count = c.fetchone()[0]
        conn.close()

        async def apply():
            conn2 = get_connection()
            c2 = conn2.cursor()
            c2.execute(
                "UPDATE checkouts SET retail_price = ?, market_price = ? WHERE LOWER(COALESCE(site, '')) LIKE ? AND LOWER(COALESCE(item, '')) LIKE ?",
                (retail, market, f"%{site_keyword.lower()}%", f"%{item_keyword.lower()}%"),
            )
            updated = c2.rowcount
            conn2.commit()
            conn2.close()
            hint = ""
            if updated == 0:
                hint = " No rows matched — check `!export` for exact site/item text, or ensure checkouts were parsed (embed fields)."
            await ctx.send(
                f"Updated **{updated}** checkouts matching site `{site_keyword}` and item `{item_keyword}`.{hint}"
            )

        await _confirm_bulk_price_update(
            ctx,
            row_count=row_count,
            title="Confirm site + item price update",
            detail=f"Site contains: `{site_keyword}`\nItem contains: `{item_keyword}`\nRetail **${retail:.2f}**, market **${market:.2f}**",
            apply_update=apply,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Pricing(bot))
