"""
Shared database helpers, checkout parsing, and pricing utilities for the Discord bot.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

import discord
from discord.ext import commands

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

# Bulk price updates affecting this many or more rows require a reaction confirmation.
PRICE_CONFIRM_MIN_ROWS = 2


def init_db() -> None:
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
    c.execute(
        "SELECT * FROM user_profiles WHERE LOWER(username) = LOWER(?) OR LOWER(canonical_name) = LOWER(?)",
        (username, _canonicalize_profile(username)),
    )
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def _resolve_profile_to_user_canonical(profile: str) -> str | None:
    canonical = _canonicalize_profile(profile)
    if not canonical:
        return None
    p = _get_profile_by_canonical(canonical)
    return p["username"] if p else None


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


def list_balance_transactions(user: str, limit: int) -> list[dict]:
    """Recent PAID/OWE rows for a canonical user, newest first."""
    canonical = _get_canonical_user(user)
    conn = get_connection()
    c = conn.cursor()
    c.execute(
        """
        SELECT id, amount, type, note, created_at, created_by
        FROM balance_transactions
        WHERE LOWER(user) = LOWER(?)
        ORDER BY datetime(created_at) DESC LIMIT ?
        """,
        (canonical, limit),
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


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
