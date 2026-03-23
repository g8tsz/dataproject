"""Profiles, addresses, checkout summaries, invoices, and exports."""

import csv
import io
import sqlite3

import discord
import discord.utils
from discord.ext import commands

from common import (
    _checkout_item_site_labels,
    _get_avatar_url,
    _get_balance,
    _get_canonical_user,
    _get_checkout_stats,
    _get_checkouts,
    _get_display_name,
    _get_profile_by_discord_id,
    _get_profile_for_username,
    _resolve_profile_to_user,
    _resolve_user_ref_to_username,
    _canonicalize_profile,
    calculate_price,
    get_connection,
)


class Core(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        canonical = _canonicalize_profile(member.name)
        if not canonical:
            canonical = member.name.lower().replace(" ", "")
        username = member.name
        conn = get_connection()
        c = conn.cursor()
        try:
            c.execute(
                "INSERT OR IGNORE INTO user_profiles (discord_id, canonical_name, username, updated_at) VALUES (?, ?, ?, ?)",
                (str(member.id), canonical, username, discord.utils.utcnow().isoformat()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass
        finally:
            conn.close()

    @commands.command(name="profile")
    async def show_profile(self, ctx: commands.Context, username: str = None):
        if username and not ctx.author.guild_permissions.administrator:
            return await ctx.send("Only admins can view other users' profiles.")
        target = username or (ctx.author.name if ctx.author else None)
        if not target:
            return await ctx.send("Could not resolve user.")
        canonical = _get_canonical_user(target or "")
        profile = _get_profile_for_username(canonical)
        if not profile and ctx.author and not username:
            profile = _get_profile_by_discord_id(ctx.author.id)
            if not profile:
                return await ctx.send("You don't have a profile yet. Ask an admin to run `!syncprofiles`.")
        if not profile:
            return await ctx.send(f"No profile found for `{target}`.")
        stats = _get_checkout_stats(profile["username"])
        bal = _get_balance(profile["username"])
        display = _get_display_name(profile["username"])
        addr = profile.get("address") or "-"
        state = profile.get("state") or "-"
        zip_ = profile.get("zip") or "-"
        embed = discord.Embed(title=f"Profile: {display}", color=0x9B59B6)
        embed.add_field(name="Canonical", value=f"`{profile['canonical_name']}`", inline=True)
        embed.add_field(name="Address", value=addr, inline=False)
        embed.add_field(name="State / ZIP", value=f"{state} {zip_}".strip() or "-", inline=True)
        embed.add_field(
            name="Checkout Stats",
            value=f"Total: {stats['total']} | Success: {stats['success']} | Declined: {stats['declined']}",
            inline=False,
        )
        embed.add_field(
            name="Balance",
            value=(
                f"Owed from checkouts: ${bal['owed_from_checkouts']:.2f}\n"
                f"Paid: ${bal['total_paid']:.2f}\n"
                f"Adjustments (owe): ${bal['total_owe']:.2f}\n"
                f"**Balance: ${bal['balance']:.2f}**"
            ),
            inline=False,
        )
        avatar_url = await _get_avatar_url(ctx.guild, profile["username"])
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        await ctx.send(embed=embed)

    @commands.command(name="setaddress")
    @commands.has_permissions(administrator=True)
    async def set_address(self, ctx: commands.Context, user_ref: str, *, address_line: str):
        if ctx.guild:
            user_ref = _resolve_user_ref_to_username(ctx, user_ref) or user_ref
        parts = address_line.strip().split()
        if len(parts) < 3:
            return await ctx.send("Usage: !setaddress <user> <address> <state> <zip>")
        zip_code, state = parts[-1], parts[-2]
        address = " ".join(parts[:-2])
        canonical = _get_canonical_user(user_ref)
        profile = _get_profile_for_username(canonical)
        if not profile:
            return await ctx.send(f"No profile found for `{user_ref}`. Run `!syncprofiles`.")
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE user_profiles SET address = ?, state = ?, zip = ?, updated_at = ? WHERE discord_id = ?",
            (address, state, zip_code, discord.utils.utcnow().isoformat(), profile["discord_id"]),
        )
        conn.commit()
        conn.close()
        await ctx.send(f"Updated address for **{profile['username']}**.")

    @commands.command(name="myaddress")
    async def set_own_address(self, ctx: commands.Context, *, address_line: str):
        profile = _get_profile_by_discord_id(ctx.author.id)
        if not profile:
            return await ctx.send("You don't have a profile yet. Ask an admin to run `!syncprofiles`.")
        parts = address_line.strip().split()
        if len(parts) < 3:
            return await ctx.send("Usage: !myaddress <address> <state> <zip>")
        zip_code, state = parts[-1], parts[-2]
        address = " ".join(parts[:-2])
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE user_profiles SET address = ?, state = ?, zip = ?, updated_at = ? WHERE discord_id = ?",
            (address, state, zip_code, discord.utils.utcnow().isoformat(), str(ctx.author.id)),
        )
        conn.commit()
        conn.close()
        await ctx.send("Updated your address.")

    @commands.command(name="stats")
    @commands.has_permissions(administrator=True)
    async def show_stats(self, ctx: commands.Context, username: str = None):
        if username:
            canonical = _get_canonical_user(username)
            stats = _get_checkout_stats(canonical)
            display = _get_display_name(canonical)
        else:
            checkouts = _get_checkouts()
            total = len(checkouts)
            success = sum(1 for c in checkouts if c.get("status") == "Success")
            declined = sum(1 for c in checkouts if c.get("status") and "Declined" in str(c["status"]))
            stats = {"total": total, "success": success, "declined": declined}
            display = "All users"
        embed = discord.Embed(
            title=f"Checkout Stats: {display}",
            description=f"Total: **{stats['total']}** | Success: **{stats['success']}** | Declined: **{stats['declined']}**",
            color=0x2ECC71,
        )
        await ctx.send(embed=embed)

    @commands.command(name="syncprofiles")
    @commands.has_permissions(administrator=True)
    async def sync_profiles(self, ctx: commands.Context):
        count = 0
        conn = get_connection()
        c = conn.cursor()
        for member in ctx.guild.members:
            if member.bot:
                continue
            canonical = _canonicalize_profile(member.name)
            if not canonical:
                canonical = member.name.lower().replace(" ", "")
            try:
                c.execute(
                    "INSERT OR IGNORE INTO user_profiles (discord_id, canonical_name, username, updated_at) VALUES (?, ?, ?, ?)",
                    (str(member.id), canonical, member.name, discord.utils.utcnow().isoformat()),
                )
                if c.rowcount > 0:
                    count += 1
            except sqlite3.IntegrityError:
                pass
        conn.commit()
        conn.close()
        await ctx.send(f"Synced profiles. Created **{count}** new profiles.")

    @commands.command(name="linkprofile")
    @commands.has_permissions(administrator=True)
    async def link_profile(self, ctx: commands.Context, username: str, profile_name: str):
        conn = get_connection()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO profile_mappings (profile, user) VALUES (?, ?)", (profile_name.strip(), username))
        conn.commit()
        conn.close()
        await ctx.send(f"Linked profile `{profile_name}` -> **@{username}**")

    @commands.command(name="unlinkprofile")
    @commands.has_permissions(administrator=True)
    async def unlink_profile(self, ctx: commands.Context, profile_name: str):
        conn = get_connection()
        c = conn.cursor()
        c.execute("DELETE FROM profile_mappings WHERE LOWER(profile) = LOWER(?)", (profile_name,))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        await ctx.send(f"Unlinked profile `{profile_name}`" if deleted else f"Profile `{profile_name}` was not linked.")

    @commands.command(name="whois")
    @commands.has_permissions(administrator=True)
    async def whois_profile(self, ctx: commands.Context, profile_name: str):
        user = _resolve_profile_to_user(profile_name)
        if user:
            display = _get_display_name(user)
            await ctx.send(f"Profile `{profile_name}` -> **@{user}**" + (f" ({display})" if display != user else ""))
        else:
            await ctx.send(f"No user linked to profile `{profile_name}`.")

    @commands.command(name="setname")
    @commands.has_permissions(administrator=True)
    async def set_display_name(self, ctx: commands.Context, username: str, *, display_name: str):
        conn = get_connection()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_display_names (user, display_name) VALUES (?, ?)", (username, display_name.strip()))
        conn.commit()
        conn.close()
        await ctx.send(f"Display name for @{username}: **{display_name.strip()}**")

    @commands.command(name="alias")
    @commands.has_permissions(administrator=True)
    async def add_alias(self, ctx: commands.Context, canonical_user: str, alias_name: str):
        conn = get_connection()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_aliases (alias, canonical_user) VALUES (?, ?)", (alias_name.lower(), canonical_user))
        conn.commit()
        conn.close()
        await ctx.send(f"Alias `{alias_name}` -> **@{canonical_user}**")

    @commands.command(name="unalias")
    @commands.has_permissions(administrator=True)
    async def remove_alias(self, ctx: commands.Context, alias_name: str):
        conn = get_connection()
        c = conn.cursor()
        c.execute("DELETE FROM user_aliases WHERE LOWER(alias) = LOWER(?)", (alias_name,))
        deleted = c.rowcount
        conn.commit()
        conn.close()
        await ctx.send(f"Removed alias `{alias_name}`" if deleted else f"Alias `{alias_name}` not found.")

    @commands.command(name="reassignprofile")
    @commands.has_permissions(administrator=True)
    async def reassign_by_profile(self, ctx: commands.Context, profile_name: str, username: str):
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE checkouts SET user = ? WHERE LOWER(profile) = LOWER(?) AND (user IS NULL OR user = 'Unknown')",
            (username, profile_name),
        )
        updated = c.rowcount
        c.execute("INSERT OR REPLACE INTO profile_mappings (profile, user) VALUES (?, ?)", (profile_name.strip(), username))
        conn.commit()
        conn.close()
        await ctx.send(f"Reassigned **{updated}** checkouts from profile `{profile_name}` -> **@{username}**")

    @commands.command(name="users")
    @commands.has_permissions(administrator=True)
    async def list_users(self, ctx: commands.Context):
        conn = get_connection()
        c = conn.cursor()
        c.execute("SELECT DISTINCT user FROM checkouts WHERE user IS NOT NULL AND user != 'Unknown' ORDER BY user")
        users = [r["user"] for r in c.fetchall()]
        lines = []
        for u in users:
            display = _get_display_name(u)
            c.execute("SELECT profile FROM profile_mappings WHERE LOWER(user) = LOWER(?)", (u,))
            profiles = [r["profile"] for r in c.fetchall()]
            c.execute("SELECT alias FROM user_aliases WHERE LOWER(canonical_user) = LOWER(?)", (u,))
            aliases = [r["alias"] for r in c.fetchall()]
            parts = [f"**@{u}**"]
            if display != u:
                parts.append(f"({display})")
            if profiles:
                parts.append(f"Profiles: {', '.join(profiles)}")
            if aliases:
                parts.append(f"Aliases: {', '.join(aliases)}")
            lines.append(" | ".join(parts))
        conn.close()
        if not lines:
            return await ctx.send("No users tracked yet.")
        embed = discord.Embed(title="Tracked Users", description="\n".join(lines), color=0x9B59B6)
        await ctx.send(embed=embed)

    @commands.command(name="summary")
    @commands.has_permissions(administrator=True)
    async def summary(self, ctx: commands.Context, username: str = None):
        checkouts_list = _get_checkouts(user=username)
        if not checkouts_list:
            return await ctx.send("No checkouts found.")
        user_data: dict[str, dict] = {}
        for co in checkouts_list:
            user = co["user"] or "Unknown"
            if user not in user_data:
                user_data[user] = {"items": [], "total": 0.0}
            pricing = calculate_price(co["retail_price"], co["market_price"])
            item_lbl, site_lbl = _checkout_item_site_labels(co)
            item_line = f"- {item_lbl} ({site_lbl})"
            if pricing:
                item_line += f" - **${pricing['member_cost']}**"
                user_data[user]["total"] += pricing["member_cost"]
            else:
                item_line += " - No price set"
            user_data[user]["items"].append(item_line)
        embed = discord.Embed(title="Checkout Summary", color=0x00FF88)
        for user, info in user_data.items():
            items_text = "\n".join(info["items"])
            if len(items_text) > 1024:
                items_text = items_text[:1021] + "..."
            label = _get_display_name(user) if user != "Unknown" else user
            embed.add_field(name=f"{label} - Total: ${info['total']:.2f}", value=items_text, inline=False)
        if username and len(user_data) == 1:
            single_user = next(iter(user_data.keys()))
            avatar_url = await _get_avatar_url(ctx.guild, single_user)
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)
        await ctx.send(embed=embed)

    @commands.command(name="invoice")
    @commands.has_permissions(administrator=True)
    async def invoice(self, ctx: commands.Context, username: str):
        canonical = _get_canonical_user(username)
        user_checkouts = _get_checkouts(user=username)
        if not user_checkouts:
            return await ctx.send(f"No checkouts found for `{username}`.")
        lines = []
        grand_total = 0.0
        for co in user_checkouts:
            pricing = calculate_price(co["retail_price"], co["market_price"])
            item_lbl, site_lbl = _checkout_item_site_labels(co)
            if pricing:
                lines.append(
                    f"**{item_lbl}**\n  Site: {site_lbl} | Retail: ${pricing['retail']} | Market: ${pricing['market']} | **You Pay: ${pricing['member_cost']}**"
                )
                grand_total += pricing["member_cost"]
            else:
                lines.append(f"**{item_lbl}** ({site_lbl}) - Price not set yet")
        display = _get_display_name(canonical)
        bal = _get_balance(canonical)
        embed = discord.Embed(title=f"Invoice for {display}", description="\n\n".join(lines), color=0x3498DB)
        footer = f"Grand Total: ${grand_total:.2f}"
        if bal["balance"] != grand_total:
            footer += f" | Balance: ${bal['balance']:.2f}"
        embed.set_footer(text=footer)
        avatar_url = await _get_avatar_url(ctx.guild, canonical)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        await ctx.send(embed=embed)

    @commands.command(name="postinvoices")
    @commands.has_permissions(administrator=True)
    async def post_invoices(self, ctx: commands.Context, channel: discord.TextChannel):
        checkouts_list = _get_checkouts()
        users = set(c["user"] for c in checkouts_list if c["user"])
        for user in users:
            user_cos = [c for c in checkouts_list if c["user"] == user]
            lines = []
            total = 0.0
            for co in user_cos:
                p = calculate_price(co["retail_price"], co["market_price"])
                if p:
                    item_lbl, site_lbl = _checkout_item_site_labels(co)
                    lines.append(f"- {item_lbl} ({site_lbl}) - **${p['member_cost']}**")
                    total += p["member_cost"]
            if lines:
                label = _get_display_name(user) if user != "Unknown" else user
                bal = _get_balance(user)
                embed = discord.Embed(title=label, description="\n".join(lines), color=0x2ECC71)
                footer = f"Total: ${total:.2f}"
                if bal["balance"] != total:
                    footer += f" | Balance: ${bal['balance']:.2f}"
                embed.set_footer(text=footer)
                avatar_url = await _get_avatar_url(ctx.guild, user)
                if avatar_url:
                    embed.set_thumbnail(url=avatar_url)
                await channel.send(embed=embed)
        await ctx.send(f"Posted all invoices to {channel.mention}")

    @commands.command(name="export")
    @commands.has_permissions(administrator=True)
    async def export_csv(self, ctx: commands.Context):
        checkouts_list = _get_checkouts()
        if not checkouts_list:
            return await ctx.send("No checkouts to export.")
        buf = io.StringIO()
        fieldnames = ["timestamp", "user", "site", "item", "profile", "status", "retail_price", "market_price", "member_cost"]
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for co in checkouts_list:
            pricing = calculate_price(co["retail_price"], co["market_price"])
            row = {k: co.get(k) for k in fieldnames if k != "member_cost"}
            row["member_cost"] = pricing["member_cost"] if pricing else ""
            writer.writerow(row)
        buf.seek(0)
        file = discord.File(io.BytesIO(buf.getvalue().encode("utf-8")), filename="checkouts_export.csv")
        await ctx.send("Checkout export:", file=file)


async def setup(bot: commands.Bot):
    await bot.add_cog(Core(bot))
