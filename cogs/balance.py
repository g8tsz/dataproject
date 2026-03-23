"""Balance, payments, adjustments, and transaction history."""

import discord
from discord.ext import commands

from common import (
    _get_avatar_url,
    _get_balance,
    _get_canonical_user,
    _get_display_name,
    _get_profile_by_discord_id,
    _get_profile_for_username,
    _log_payment,
    _resolve_user_ref_to_username,
    get_connection,
    list_balance_transactions,
)


class Balance(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="balance")
    async def show_balance(self, ctx: commands.Context, username: str = None):
        if username and not ctx.author.guild_permissions.administrator:
            return await ctx.send("Only admins can view other users' balances.")
        target = username or (ctx.author.name if ctx.author else None)
        if username and ctx.guild:
            target = _resolve_user_ref_to_username(ctx, username) or target
        if not target:
            return await ctx.send("Could not resolve user.")
        canonical = _get_canonical_user(target or "")
        profile = _get_profile_for_username(canonical)
        if not profile and ctx.author and not username:
            profile = _get_profile_by_discord_id(ctx.author.id)
            if profile:
                canonical = profile["username"]
        if not profile and username:
            canonical = _get_canonical_user(username)
        bal = _get_balance(canonical or target)
        display = _get_display_name(canonical or target)
        embed = discord.Embed(
            title=f"Balance: {display}",
            description=(
                f"Owed from checkouts: **${bal['owed_from_checkouts']:.2f}**\n"
                f"Paid: **${bal['total_paid']:.2f}**\n"
                f"Adjustments (owe): **${bal['total_owe']:.2f}**\n\n"
                f"**Balance: ${bal['balance']:.2f}**"
            ),
            color=0x3498DB,
        )
        avatar_url = await _get_avatar_url(ctx.guild, canonical or target)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        await ctx.send(embed=embed)

    @commands.command(name="paid")
    @commands.has_permissions(administrator=True)
    async def record_payment(self, ctx: commands.Context, user_ref: str, amount: float, *, note: str = ""):
        if ctx.guild:
            user_ref = _resolve_user_ref_to_username(ctx, user_ref) or user_ref
        canonical = _get_canonical_user(user_ref)
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            "INSERT INTO balance_transactions (user, amount, type, note, created_by) VALUES (?, ?, 'PAID', ?, ?)",
            (canonical, amount, note, str(ctx.author.id)),
        )
        conn.commit()
        conn.close()
        author_name = ctx.author.display_name or ctx.author.name
        if ctx.guild:
            await _log_payment(ctx.bot, ctx.guild.id, "PAID", canonical, amount, note, author_name)
        extra = f"\nNote: {note}" if note.strip() else ""
        await ctx.send(f"Recorded payment of **${amount:.2f}** for **{canonical}**.{extra}")

    @commands.command(name="owe")
    @commands.has_permissions(administrator=True)
    async def record_owe(self, ctx: commands.Context, user_ref: str, amount: float, *, note: str = ""):
        if ctx.guild:
            user_ref = _resolve_user_ref_to_username(ctx, user_ref) or user_ref
        canonical = _get_canonical_user(user_ref)
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            "INSERT INTO balance_transactions (user, amount, type, note, created_by) VALUES (?, ?, 'OWE', ?, ?)",
            (canonical, amount, note, str(ctx.author.id)),
        )
        conn.commit()
        conn.close()
        author_name = ctx.author.display_name or ctx.author.name
        if ctx.guild:
            await _log_payment(ctx.bot, ctx.guild.id, "OWE", canonical, amount, note, author_name)
        extra = f"\nNote: {note}" if note.strip() else ""
        await ctx.send(f"Added **${amount:.2f}** to balance for **{canonical}**.{extra}")

    @commands.command(name="history")
    async def balance_history(self, ctx: commands.Context, *, args: str = ""):
        """Show recent !paid / !owe rows with notes. Usage: !history | !history N | !history User (admin) | !history User N"""
        parts = args.strip().split()
        limit = 10
        max_limit = 50
        admin = ctx.author.guild_permissions.administrator if ctx.author.guild else False

        if not parts:
            target = ctx.author.name
        elif not admin:
            if len(parts) == 1 and parts[0].isdigit():
                limit = min(int(parts[0]), max_limit)
                target = ctx.author.name
            else:
                return await ctx.send("Usage: `!history` or `!history N` (max 50). Admins can use `!history UserName` or `!history UserName N`.")
        else:
            if len(parts) == 1:
                if parts[0].isdigit():
                    limit = min(int(parts[0]), max_limit)
                    target = ctx.author.name
                else:
                    ref = parts[0]
                    target = _resolve_user_ref_to_username(ctx, ref) or ref
                    target = _get_canonical_user(target)
            else:
                if parts[-1].isdigit():
                    limit = min(int(parts[-1]), max_limit)
                    ref = " ".join(parts[:-1])
                else:
                    ref = " ".join(parts)
                    limit = 10
                if ref.startswith("<@"):
                    target = _resolve_user_ref_to_username(ctx, ref) or ref
                else:
                    target = ref
                target = _get_canonical_user(target)

        rows = list_balance_transactions(target, limit)
        display = _get_display_name(target)
        if not rows:
            return await ctx.send(f"No balance adjustments recorded for **{display}**.")

        lines = []
        for r in rows:
            t = r["type"]
            amt = r["amount"]
            note = (r["note"] or "").strip()
            when = r["created_at"] or ""
            label = "Paid" if t == "PAID" else "Owe"
            note_s = f" — _{note}_" if note else ""
            lines.append(f"**{label}** ${amt:.2f}{note_s}\n`{when}`")

        body = "\n\n".join(lines)
        if len(body) > 3800:
            body = body[:3797] + "..."
        embed = discord.Embed(
            title=f"Balance history: {display}",
            description=body,
            color=0x1ABC9C,
        )
        embed.set_footer(text=f"Showing up to {limit} most recent PAID/OWE entries")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Balance(bot))
