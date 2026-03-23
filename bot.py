"""
Discord Checkout Tracker Bot
Tracks SOLUS webhook checkouts, applies pricing formulas, and generates summaries/invoices.
"""

import discord
from discord.ext import commands
import re
import sqlite3
import hashlib
from pathlib import Path

try:
    from config import (
        BOT_TOKEN,
        CHECKOUT_CHANNEL_IDS,
        COMMAND_PREFIX,
        PRICE_MARGIN_MULTIPLIER,
    )
    ADMIN_LOG_CHANNEL_ID = getattr(__import__("config"), "ADMIN_LOG_CHANNEL_ID", None)
except ImportError:
    BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
    CHECKOUT_CHANNEL_IDS = [123456789, 987654321]
    COMMAND_PREFIX = "!"
    PRICE_MARGIN_MULTIPLIER = 0.50
    ADMIN_LOG_CHANNEL_ID = None

DB_PATH = Path(__file__).parent / "checkouts.db"


def init_db():
    """Create SQLite tables."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS checkouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checkout_hash TEXT UNIQUE NOT NULL,
            timestamp TEXT NOT NULL,
            user TEXT,
            site TEXT,
            item TEXT,
            profile TEXT,
            status TEXT,
            retail_price REAL,
            market_price REAL,
            raw_data TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_checkout_hash ON checkouts(checkout_hash)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_checkout_user ON checkouts(user)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_checkout_profile ON checkouts(profile)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS profile_mappings (
            profile TEXT NOT NULL,
            user TEXT NOT NULL,
            PRIMARY KEY (profile)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_display_names (
            user TEXT PRIMARY KEY,
            display_name TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_aliases (
            alias TEXT PRIMARY KEY,
            canonical_user TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            discord_id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL UNIQUE,
            username TEXT NOT NULL,
            address TEXT,
            state TEXT,
            zip TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_profiles_canonical ON user_profiles(canonical_name)")

    c.execute("""
        CREATE TABLE IF NOT EXISTS balance_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            amount REAL NOT NULL,
            type TEXT NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_balance_user ON balance_transactions(user)")

    conn.commit()
    conn.close()


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_user(username: str) -> str | None:
    if not username:
        return None
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT canonical_user FROM user_aliases WHERE LOWER(alias) = LOWER(?)", (username,))
    row = c.fetchone()
    conn.close()
    return row["canonical_user"] if row else username


def _get_canonical_user(input_user: str) -> str:
    resolved = _resolve_user(input_user)
    return resolved or input_user


def _resolve_user_ref_to_username(ctx: commands.Context, user_ref: str) -> str | None:
    if not user_ref:
        return None
    mention_match = re.match(r"<@!?(\d+)>", user_ref.strip())
    if mention_match:
        discord_id = mention_match.group(1)
        profile = _get_profile_by_discord_id(discord_id)
        if profile:
            return profile["username"]
        try:
            member = ctx.guild.get_member(int(discord_id))
            if member:
                return member.name
        except (ValueError, AttributeError):
            pass
    return user_ref


def _get_display_name(user: str) -> str:
    if not user or user == "Unknown":
        return user or "Unknown"
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT display_name FROM user_display_names WHERE LOWER(user) = LOWER(?)", (user,))
    row = c.fetchone()
    conn.close()
    return row["display_name"] if row else user


def _resolve_profile_to_user(profile: str | None) -> str | None:
    if not profile:
        return None
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user FROM profile_mappings WHERE LOWER(profile) = LOWER(?)", (profile,))
    row = c.fetchone()
    conn.close()
    return row["user"] if row else None


def _ensure_profile_mapping(profile: str, user: str) -> None:
    if not profile or not user:
        return
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO profile_mappings (profile, user) VALUES (?, ?)",
        (profile.strip(), user),
    )
    conn.commit()
    conn.close()


def _canonicalize_profile(raw: str) -> str:
    if not raw or not raw.strip():
        return ""
    parts = raw.strip().split()
    base = parts[0] if parts else ""
    alpha_only = re.sub(r"[^A-Za-z]", "", base)
    return re.sub(r"\d", "", alpha_only).lower()


def _get_profile_by_canonical(canonical: str) -> dict | None:
    if not canonical:
        return None
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM user_profiles WHERE LOWER(canonical_name) = LOWER(?)", (canonical,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def _get_profile_by_discord_id(discord_id: str | int) -> dict | None:
    if discord_id is None:
        return None
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM user_profiles WHERE discord_id = ?", (str(discord_id),))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def _get_profile_for_username(username: str) -> dict | None:
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM user_profiles WHERE LOWER(username) = LOWER(?) OR LOWER(canonical_name) = LOWER(?)", (username, _canonicalize_profile(username)))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def _resolve_profile_to_user_canonical(profile: str) -> str | None:
    canonical = _canonicalize_profile(profile)
    if not canonical:
        return None
    p = _get_profile_by_canonical(canonical)
    return p["username"] if p else None


def _get_checkout_stats(user: str) -> dict:
    checkouts = _get_checkouts(user=user)
    total = len(checkouts)
    success = sum(1 for c in checkouts if c.get("status") == "Success")
    declined = sum(1 for c in checkouts if c.get("status") and "Declined" in str(c["status"]))
    return {"total": total, "success": success, "declined": declined}


async def _log_payment(bot_instance, guild_id: int, tx_type: str, user: str, amount: float, note: str, author_name: str):
    if not ADMIN_LOG_CHANNEL_ID:
        return
    try:
        channel = bot_instance.get_channel(ADMIN_LOG_CHANNEL_ID)
        if not channel:
            guild = bot_instance.get_guild(guild_id)
            if guild:
                channel = await guild.fetch_channel(ADMIN_LOG_CHANNEL_ID)
        if not channel:
            return
        color = 0x2ECC71 if tx_type == "PAID" else 0xE67E22
        title = "Payment recorded" if tx_type == "PAID" else "Balance adjustment"
        desc = f"**User:** {user}\n**Amount:** ${amount:.2f}"
        if note:
            desc += f"\n**Note:** {note}"
        desc += f"\n**By:** {author_name}"
        embed = discord.Embed(title=title, description=desc, color=color)
        await channel.send(embed=embed)
    except Exception as e:
        print(f"Admin log error: {e}")


def _get_balance(user: str) -> dict:
    canonical = _get_canonical_user(user)
    checkouts = _get_checkouts(user=canonical)
    owed_from_checkouts = 0.0
    for c in checkouts:
        p = calculate_price(c["retail_price"], c["market_price"])
        if p:
            owed_from_checkouts += p["member_cost"]
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM balance_transactions WHERE LOWER(user) = LOWER(?) AND type = 'PAID'",
        (canonical,),
    )
    total_paid = c.fetchone()[0] or 0.0
    c.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM balance_transactions WHERE LOWER(user) = LOWER(?) AND type = 'OWE'",
        (canonical,),
    )
    total_owe = c.fetchone()[0] or 0.0
    conn.close()
    balance = owed_from_checkouts + total_owe - total_paid
    return {
        "owed_from_checkouts": owed_from_checkouts,
        "total_paid": total_paid,
        "total_owe": total_owe,
        "balance": balance,
    }


async def _get_avatar_url(guild: discord.Guild, user_identifier: str) -> str | None:
    profile = _get_profile_for_username(user_identifier)
    if profile:
        discord_id = profile["discord_id"]
        try:
            member = guild.get_member(int(discord_id)) or await guild.fetch_member(int(discord_id))
            if member and member.display_avatar:
                return member.display_avatar.url
        except (ValueError, discord.NotFound):
            pass
    return None


def checkout_hash(data: dict) -> str:
    key = f"{data.get('user', '')}|{data.get('item', '')}|{data.get('timestamp', '')}|{data.get('site', '')}"
    return hashlib.sha256(key.encode()).hexdigest()


def calculate_price(retail: float | None, market: float | None, quantity: int = 1) -> dict | None:
    if market is None or retail is None:
        return None
    margin = (market - retail) * PRICE_MARGIN_MULTIPLIER
    member_cost = retail + margin
    total = member_cost * quantity
    return {
        "retail": retail,
        "market": market,
        "member_cost": round(member_cost, 2),
        "quantity": quantity,
        "total": round(total, 2),
    }


_EMBED_FIELD_ALIASES = {
    "site": "site",
    "store": "site",
    "retailer": "site",
    "website": "site",
    "shop": "site",
    "vendor": "site",
    "item": "item",
    "product": "item",
    "products": "item",
    "sku": "item",
    "name": "item",
    "profile": "profile",
    "account": "profile",
    "solus profile": "profile",
}


def _normalize_embed_field_name(raw: str) -> str:
    s = raw.strip().lower()
    s = re.sub(r"^[*_`]+|[*_`]+$", "", s)
    s = s.rstrip(":").strip()
    return s


def _checkout_item_site_labels(co: dict) -> tuple[str, str]:
    item = co.get("item") or "Unknown item"
    site = co.get("site") or "Unknown site"
    return item, site


def parse_checkout_embed(embed: discord.Embed, message: discord.Message) -> dict | None:
    try:
        data = {
            "timestamp": message.created_at.isoformat(),
            "user": None,
            "site": None,
            "item": None,
            "profile": None,
            "status": None,
            "market_price": None,
            "retail_price": None,
        }
        desc = embed.description or ""
        user_match = re.search(r"@(\S+)", desc)
        if user_match:
            data["user"] = user_match.group(1).rstrip(".,;:!?)")
        mention_id = re.search(r"<@!?(\d+)>", desc)
        if mention_id:
            data["_mention_user_id"] = mention_id.group(1)
        if "✅" in desc or "Success" in desc.lower():
            data["status"] = "Success"
        elif "❌" in desc or "Declined" in desc:
            data["status"] = "Declined"
        for field in embed.fields:
            key = _normalize_embed_field_name(field.name or "")
            key = _EMBED_FIELD_ALIASES.get(key, key)
            value = (field.value or "").strip()
            if not value:
                continue
            if key == "site":
                data["site"] = value
            elif key == "item":
                data["item"] = value
            elif key == "profile":
                data["profile"] = value
        return data
    except Exception as e:
        print(f"Parse error: {e}")
        return None


intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"   Watching channels: {CHECKOUT_CHANNEL_IDS}")


@bot.event
async def on_member_join(member: discord.Member):
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


@bot.event
async def on_message(message: discord.Message):
    if message.channel.id in CHECKOUT_CHANNEL_IDS:
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
                        (chash, parsed["timestamp"], user, parsed["site"], parsed["item"], parsed["profile"], parsed["status"], parsed["retail_price"], parsed["market_price"]),
                    )
                    conn.commit()
                    if conn.total_changes > 0:
                        print(f"Tracked: {user} - {parsed['item']} on {parsed['site']}")
                finally:
                    conn.close()
    await bot.process_commands(message)


def _get_checkouts(user: str | None = None, since: str | None = None) -> list[dict]:
    canonical = _get_canonical_user(user) if user else None
    conn = get_connection()
    c = conn.cursor()
    query = "SELECT * FROM checkouts WHERE 1=1"
    params: list = []
    if canonical:
        query += " AND LOWER(user) = LOWER(?)"
        params.append(canonical)
    if since:
        query += " AND timestamp >= ?"
        params.append(since)
    query += " ORDER BY timestamp ASC"
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]


@bot.command(name="setprice")
@commands.has_permissions(administrator=True)
async def set_price(ctx, item_keyword: str, retail: float, market: float):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "UPDATE checkouts SET retail_price = ?, market_price = ? WHERE LOWER(COALESCE(item, '')) LIKE ?",
        (retail, market, f"%{item_keyword.lower()}%"),
    )
    updated = c.rowcount
    conn.commit()
    conn.close()
    hint = ""
    if updated == 0:
        hint = " No rows matched — item text may differ from the keyword, or site+item pricing may fit better: `!setpricesiteitem`."
    await ctx.send(f"Updated **{updated}** checkouts matching `{item_keyword}`.{hint}")


@bot.command(name="setpricesite")
@commands.has_permissions(administrator=True)
async def set_price_by_site(ctx, site: str, retail: float, market: float):
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "UPDATE checkouts SET retail_price = ?, market_price = ? WHERE LOWER(COALESCE(site, '')) LIKE ?",
        (retail, market, f"%{site.lower()}%"),
    )
    updated = c.rowcount
    conn.commit()
    conn.close()
    hint = ""
    if updated == 0:
        hint = " No rows matched — check exact site string in `!export`."
    await ctx.send(f"Updated **{updated}** checkouts from `{site}`.{hint}")


@bot.command(name="setpricesiteitem")
@commands.has_permissions(administrator=True)
async def set_price_by_site_and_item(ctx, site_keyword: str, item_keyword: str, retail: float, market: float):
    """Set retail & market only for rows matching both site and item substrings (per-retailer MSRP)."""
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        "UPDATE checkouts SET retail_price = ?, market_price = ? WHERE LOWER(COALESCE(site, '')) LIKE ? AND LOWER(COALESCE(item, '')) LIKE ?",
        (retail, market, f"%{site_keyword.lower()}%", f"%{item_keyword.lower()}%"),
    )
    updated = c.rowcount
    conn.commit()
    conn.close()
    hint = ""
    if updated == 0:
        hint = " No rows matched — check `!export` for exact site/item text, or ensure checkouts were parsed (embed fields)."
    await ctx.send(f"Updated **{updated}** checkouts matching site `{site_keyword}` and item `{item_keyword}`.{hint}")


@bot.command(name="profile")
async def show_profile(ctx, username: str = None):
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
    embed.add_field(name="Checkout Stats", value=f"Total: {stats['total']} | Success: {stats['success']} | Declined: {stats['declined']}", inline=False)
    embed.add_field(name="Balance", value=f"Owed from checkouts: ${bal['owed_from_checkouts']:.2f}\nPaid: ${bal['total_paid']:.2f}\nAdjustments (owe): ${bal['total_owe']:.2f}\n**Balance: ${bal['balance']:.2f}**", inline=False)
    avatar_url = await _get_avatar_url(ctx.guild, profile["username"])
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    await ctx.send(embed=embed)


@bot.command(name="setaddress")
@commands.has_permissions(administrator=True)
async def set_address(ctx, user_ref: str, *, address_line: str):
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
    c.execute("UPDATE user_profiles SET address = ?, state = ?, zip = ?, updated_at = ? WHERE discord_id = ?", (address, state, zip_code, discord.utils.utcnow().isoformat(), profile["discord_id"]))
    conn.commit()
    conn.close()
    await ctx.send(f"Updated address for **{profile['username']}**.")


@bot.command(name="myaddress")
async def set_own_address(ctx, *, address_line: str):
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
    c.execute("UPDATE user_profiles SET address = ?, state = ?, zip = ?, updated_at = ? WHERE discord_id = ?", (address, state, zip_code, discord.utils.utcnow().isoformat(), str(ctx.author.id)))
    conn.commit()
    conn.close()
    await ctx.send("Updated your address.")


@bot.command(name="balance")
async def show_balance(ctx, username: str = None):
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
    embed = discord.Embed(title=f"Balance: {display}", description=f"Owed from checkouts: **${bal['owed_from_checkouts']:.2f}**\nPaid: **${bal['total_paid']:.2f}**\nAdjustments (owe): **${bal['total_owe']:.2f}**\n\n**Balance: ${bal['balance']:.2f}**", color=0x3498DB)
    avatar_url = await _get_avatar_url(ctx.guild, canonical or target)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)
    await ctx.send(embed=embed)


@bot.command(name="paid")
@commands.has_permissions(administrator=True)
async def record_payment(ctx, user_ref: str, amount: float, *, note: str = ""):
    if ctx.guild:
        user_ref = _resolve_user_ref_to_username(ctx, user_ref) or user_ref
    canonical = _get_canonical_user(user_ref)
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO balance_transactions (user, amount, type, note, created_by) VALUES (?, ?, 'PAID', ?, ?)", (canonical, amount, note, str(ctx.author.id)))
    conn.commit()
    conn.close()
    author_name = ctx.author.display_name or ctx.author.name
    if ctx.guild:
        await _log_payment(ctx.bot, ctx.guild.id, "PAID", canonical, amount, note, author_name)
    extra = f"\nNote: {note}" if note.strip() else ""
    await ctx.send(f"Recorded payment of **${amount:.2f}** for **{canonical}**.{extra}")


@bot.command(name="owe")
@commands.has_permissions(administrator=True)
async def record_owe(ctx, user_ref: str, amount: float, *, note: str = ""):
    if ctx.guild:
        user_ref = _resolve_user_ref_to_username(ctx, user_ref) or user_ref
    canonical = _get_canonical_user(user_ref)
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT INTO balance_transactions (user, amount, type, note, created_by) VALUES (?, ?, 'OWE', ?, ?)", (canonical, amount, note, str(ctx.author.id)))
    conn.commit()
    conn.close()
    author_name = ctx.author.display_name or ctx.author.name
    if ctx.guild:
        await _log_payment(ctx.bot, ctx.guild.id, "OWE", canonical, amount, note, author_name)
    extra = f"\nNote: {note}" if note.strip() else ""
    await ctx.send(f"Added **${amount:.2f}** to balance for **{canonical}**.{extra}")


@bot.command(name="stats")
@commands.has_permissions(administrator=True)
async def show_stats(ctx, username: str = None):
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
    embed = discord.Embed(title=f"Checkout Stats: {display}", description=f"Total: **{stats['total']}** | Success: **{stats['success']}** | Declined: **{stats['declined']}**", color=0x2ECC71)
    await ctx.send(embed=embed)


@bot.command(name="syncprofiles")
@commands.has_permissions(administrator=True)
async def sync_profiles(ctx):
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
            c.execute("INSERT OR IGNORE INTO user_profiles (discord_id, canonical_name, username, updated_at) VALUES (?, ?, ?, ?)", (str(member.id), canonical, member.name, discord.utils.utcnow().isoformat()))
            if c.rowcount > 0:
                count += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    conn.close()
    await ctx.send(f"Synced profiles. Created **{count}** new profiles.")


@bot.command(name="linkprofile")
@commands.has_permissions(administrator=True)
async def link_profile(ctx, username: str, profile_name: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO profile_mappings (profile, user) VALUES (?, ?)", (profile_name.strip(), username))
    conn.commit()
    conn.close()
    await ctx.send(f"Linked profile `{profile_name}` -> **@{username}**")


@bot.command(name="unlinkprofile")
@commands.has_permissions(administrator=True)
async def unlink_profile(ctx, profile_name: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM profile_mappings WHERE LOWER(profile) = LOWER(?)", (profile_name,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    await ctx.send(f"Unlinked profile `{profile_name}`" if deleted else f"Profile `{profile_name}` was not linked.")


@bot.command(name="whois")
@commands.has_permissions(administrator=True)
async def whois_profile(ctx, profile_name: str):
    user = _resolve_profile_to_user(profile_name)
    if user:
        display = _get_display_name(user)
        await ctx.send(f"Profile `{profile_name}` -> **@{user}**" + (f" ({display})" if display != user else ""))
    else:
        await ctx.send(f"No user linked to profile `{profile_name}`.")


@bot.command(name="setname")
@commands.has_permissions(administrator=True)
async def set_display_name(ctx, username: str, *, display_name: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_display_names (user, display_name) VALUES (?, ?)", (username, display_name.strip()))
    conn.commit()
    conn.close()
    await ctx.send(f"Display name for @{username}: **{display_name.strip()}**")


@bot.command(name="alias")
@commands.has_permissions(administrator=True)
async def add_alias(ctx, canonical_user: str, alias_name: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO user_aliases (alias, canonical_user) VALUES (?, ?)", (alias_name.lower(), canonical_user))
    conn.commit()
    conn.close()
    await ctx.send(f"Alias `{alias_name}` -> **@{canonical_user}**")


@bot.command(name="unalias")
@commands.has_permissions(administrator=True)
async def remove_alias(ctx, alias_name: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM user_aliases WHERE LOWER(alias) = LOWER(?)", (alias_name,))
    deleted = c.rowcount
    conn.commit()
    conn.close()
    await ctx.send(f"Removed alias `{alias_name}`" if deleted else f"Alias `{alias_name}` not found.")


@bot.command(name="reassignprofile")
@commands.has_permissions(administrator=True)
async def reassign_by_profile(ctx, profile_name: str, username: str):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE checkouts SET user = ? WHERE LOWER(profile) = LOWER(?) AND (user IS NULL OR user = 'Unknown')", (username, profile_name))
    updated = c.rowcount
    c.execute("INSERT OR REPLACE INTO profile_mappings (profile, user) VALUES (?, ?)", (profile_name.strip(), username))
    conn.commit()
    conn.close()
    await ctx.send(f"Reassigned **{updated}** checkouts from profile `{profile_name}` -> **@{username}**")


@bot.command(name="users")
@commands.has_permissions(administrator=True)
async def list_users(ctx):
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


@bot.command(name="summary")
@commands.has_permissions(administrator=True)
async def summary(ctx, username: str = None):
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


@bot.command(name="invoice")
@commands.has_permissions(administrator=True)
async def invoice(ctx, username: str):
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
            lines.append(f"**{item_lbl}**\n  Site: {site_lbl} | Retail: ${pricing['retail']} | Market: ${pricing['market']} | **You Pay: ${pricing['member_cost']}**")
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


@bot.command(name="postinvoices")
@commands.has_permissions(administrator=True)
async def post_invoices(ctx, channel: discord.TextChannel):
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


@bot.command(name="export")
@commands.has_permissions(administrator=True)
async def export_csv(ctx):
    import csv
    import io
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


def main():
    init_db()
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
