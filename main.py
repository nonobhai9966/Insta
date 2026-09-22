"""
Instagram Likes & Views — Telegram SMM reseller bot  (v2, single-message UI)

Highlights
  • Single-message flow: every screen edits ONE panel message, user inputs are auto-deleted
  • Bot API 10.x: Rich Messages (sendRichMessage / editMessageText rich_message) with auto-fallback,
    sendMessageDraft live progress, colored + custom-emoji buttons, copy-text buttons
  • Atomic wallet (ledger for every coin movement), race-safe orders / refunds / redeem / bonus
  • Auto order placement on SMM panel (API v2) + background status sync with auto partial/full refunds
  • Deposits with screenshot / UTR proof, expiry, admin approve/reject from the notification itself
  • Full admin panel: stats, queues, services editor, provider balance, user manager, redeem codes,
    media broadcast with live progress, maintenance mode

Requires: python-telegram-bot>=22.1, httpx, python-dotenv, pymongo>=4.13 (only when MONGO_URI is set)
All secrets come from environment variables / .env — never hardcode them.
"""
from __future__ import annotations

import asyncio
import html
import inspect
import logging
import math
import os
import random
import re
import secrets
import time
import warnings
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, InvalidToken, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    PersistenceInput,
    PicklePersistence,
    filters,
)

load_dotenv()
warnings.filterwarnings("ignore", message=r".*do_api_request.*")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
log = logging.getLogger("igbot")
for noisy in ("httpx", "httpcore", "telegram.ext.Updater"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ======================================================================================
# Settings
# ======================================================================================
def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None or not raw.strip() else raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Env %s=%r is not an integer, using %s", name, raw, default)
        return default


def _parse_packages(raw: str) -> tuple[tuple[int, int], ...]:
    """COIN_PACKAGES="100:100,500:450" -> ((100, 100), (500, 450)) as (coins, price)."""
    out = []
    for chunk in raw.split(","):
        coins, _, price = chunk.strip().partition(":")
        if coins.isdigit() and price.isdigit() and int(coins) > 0 and int(price) > 0:
            out.append((int(coins), int(price)))
    return tuple(out) or ((100, 100), (500, 450), (1000, 850), (2500, 2000))


@dataclass(frozen=True)
class Settings:
    bot_token: str
    mongo_uri: str
    database_name: str
    owner_id: int
    admin_ids: frozenset[int]
    bot_name: str
    support_url: str
    force_join_channel: str | None
    persistence_path: str
    deposit_info: str
    upi_id: str
    currency: str
    coin_packages: tuple[tuple[int, int], ...]
    rich_messages: bool
    message_drafts: bool
    colored_buttons: bool
    fancy_font: bool
    clean_chat: bool
    referral_bonus: int
    daily_bonus: int
    daily_cooldown_hours: int
    deposit_expiry_minutes: int
    status_sync_minutes: int

    @classmethod
    def from_env(cls) -> "Settings":
        owner_id = _env_int("OWNER_ID", 0)
        admins = {owner_id} if owner_id else set()
        for item in os.getenv("ADMIN_IDS", "").split(","):
            item = item.strip()
            if item.isdigit():
                admins.add(int(item))
        return cls(
            bot_token=os.getenv("BOT_TOKEN", "").strip(),
            mongo_uri=(os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "").strip(),
            database_name=os.getenv("DATABASE_NAME", "instagram_bot").strip(),
            owner_id=owner_id,
            admin_ids=frozenset(admins),
            bot_name=os.getenv("BOT_NAME", "IG Like & Views Bot").strip(),
            support_url=os.getenv("SUPPORT_URL", "https://t.me/your_support").strip(),
            force_join_channel=os.getenv("FORCE_JOIN_CHANNEL", "").strip().lstrip("@") or None,
            persistence_path=os.getenv("PERSISTENCE_PATH", "bot_state.pickle").strip(),
            deposit_info=os.getenv("DEPOSIT_INFO", "Pay via UPI and send the screenshot here.").replace("\\n", "\n"),
            upi_id=os.getenv("UPI_ID", "").strip(),
            currency=os.getenv("CURRENCY_SYMBOL", "₹").strip() or "₹",
            coin_packages=_parse_packages(os.getenv("COIN_PACKAGES", "")),
            rich_messages=_env_bool("RICH_MESSAGES", True),
            message_drafts=_env_bool("MESSAGE_DRAFTS", True),
            colored_buttons=_env_bool("COLORED_BUTTONS", True),
            fancy_font=_env_bool("FANCY_FONT", True),
            clean_chat=_env_bool("CLEAN_CHAT", True),
            referral_bonus=max(0, _env_int("REFERRAL_BONUS", 3)),
            daily_bonus=max(0, _env_int("DAILY_BONUS", 2)),
            daily_cooldown_hours=max(1, _env_int("DAILY_BONUS_COOLDOWN_HOURS", 24)),
            deposit_expiry_minutes=max(5, _env_int("DEPOSIT_EXPIRY_MINUTES", 60)),
            status_sync_minutes=max(1, _env_int("STATUS_SYNC_MINUTES", 10)),
        )


settings = Settings.from_env()


# ---- runtime state (editable from the admin panel, persisted in DB) -----------------
EXTRA_ADMINS: set[int] = set()     # admins added from the panel
CHANNELS: list[dict] = []          # force-join channels: {"id", "title", "url"}
CONFIG: dict[str, Any] = {}        # bot settings overrides


def config_defaults() -> dict[str, Any]:
    return {"bot_name": settings.bot_name, "support_url": settings.support_url, "deposit_info": settings.deposit_info,
            "upi_id": settings.upi_id, "currency": settings.currency,
            "coin_packages": [list(p) for p in settings.coin_packages],
            "referral_bonus": settings.referral_bonus, "daily_bonus": settings.daily_bonus,
            "daily_cooldown_hours": settings.daily_cooldown_hours, "deposit_expiry_minutes": settings.deposit_expiry_minutes}


def cfg(key: str) -> Any:
    return CONFIG[key] if key in CONFIG else config_defaults()[key]


class _Cfg:
    def __getattr__(self, key: str) -> Any:
        return cfg(key)


C = _Cfg()  # C.currency, C.support_url … always the live value


def is_owner(user_id: int) -> bool:
    return user_id == settings.owner_id if settings.owner_id else user_id in settings.admin_ids


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids or user_id in EXTRA_ADMINS


def all_admin_ids() -> set[int]:
    return set(settings.admin_ids) | EXTRA_ADMINS


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def new_id() -> str:
    return uuid4().hex[:10].upper()


# ======================================================================================
# Emoji + text styling
# ======================================================================================
# ======================================================================================
# PREMIUM EMOJI — yahan apni custom emoji ID daalo (sirf digits), quotes ke andar.
# Khali "" chhodoge to normal emoji dikhega. Bot owner ke paas Telegram Premium hona zaroori hai.
# Example:  "HEART": "5250903242417415799",
# ======================================================================================
PREMIUM_EMOJI_IDS: dict[str, str] = {
    "SKULL": "6082409105501195897",       # 💀
    "FIRE": "6084629985845317971",        # 🔥
    "HEART": "5388790256772331442",       # ❤️
    "EYE": "5465281390931113531",         # 👁️
    "GEM": "6147439566107186310",         # 💎
    "DOWN": "6041761071155386269",        # 👇
    "ROCKET": "6032608126480421344",      # 🚀
    "PROFILE": "5472363448404809929",     # 👤
    "WALLET": "5992390297533816030",      # 👛
    "ORDER": "6030864215139422409",       # 📦
    "REFER": "5463064457661934722",       # 🔗
    "BONUS": "6168088385886886324",       # 🎁
    "LEADER": "5377599075237502153",      # 🏆
    "REDEEM": "5904248647972820334",      # 🎟️
    "SUPPORT": "5312486108309757006",     # 💬
    "ADMIN": "5884161133174067365",       # 🔧
    "HOME": "5269547387516388870",        # 🏠
    "BACK": "5267410576862118569",        # ◀️
    "CANCEL": "5197269100878907942",      # ✖️
    "CHECK": "6080263490163973583",       # ✅
    "CROSS": "6080263490163973583",       # ❌
    "LOADING": "5341715473882955310",     # ⏳
    "PROCESS": "5854722989240619332",     # ⚙️
    "DONE": "5454415424319931791",        # ✔️
    "REFUND": "5774077015388852135",      # ↩️
    "PARTIAL": "5895507195524550741",     # 🌗
    "ERROR": "6080263490163973583",       # ❌
    "WARN": "5039665997506675838",        # ⚠️
    "INFO": "6195239687368481708",        # 💡
    "COINS": "5267409292666898509",       # 🪙
    "DEPOSIT": "5406809207947142040",     # 🏦
    "CREDIT": "6082586710988820084",      # 💰
    "MONEY": "5332455502917949981",       # 💸
    "QUANTITY": "5226929552319594190",    # 🔢
    "SERVICE": "5433825729060018456",     # 🧭
    "TARGET": "6109432142079466939",      # 🎯
    "LINK": "5463064457661934722",        # 🔗
    "SHIELD": "6030594491193234103",      # 🛡️
    "TIME": "5260502250815513613",        # 🕒
    "ID": "5267287216811444804",          # 🆔
    "NAME": "",        # ✨
    "STATS": "5433811242135331842",       # 📊
    "QUEUE": "5863945989127148135",       # 📥
    "SETTING": "5854722989240619332",     # ⚙️
    "SEARCH": "5262919351035518271",      # 🔍
    "BAN": "5458382591121964689",         # 🚫
    "BROADCAST": "5213395028038132311",   # 📢
    "MAINTENANCE": "6098031288831187632", # 🛠️
    "EDIT": "5330115548900501467",        # ✍️
    "KEY": "5258476306152038031",         # 🔑
    "COPY": "5433614747381538714",        # 📋
    "REFRESH": "5780405967527089720",     # 🔄
    "NEXT": "5260342697075416641",        # ▶️
    "PLUS": "5469957729848159635",        # ➕
    "MINUS": "6129450086997959180",       # ➖
    "POWER": "6194884545112709374",       # 🔌
    "SEND": "5226794552907554474",        # 📨
    "PHOTO": "5460713671736960588",       # 🖼️
    "RETRY": "6100520888099149405",       # 🔁
    "UPI": "5257974976094412956",         # 📲
    "SYSTEM": "5449569374065152798",      # 🌀
    "REFERRAL": "5886568200350472339",    # 🎉
    "USER": "5472363448404809929",        # 👤
    "JOIN": "5213395028038132311",        # 📢
    "HELP": "",        # ❓
    "LOCK": "5882207227997066107",        # 🔒
    "SHARE": "5017470156276761427",       # 📤
    "LIST": "",        # 📋
    "REPORT": "5433614747381538714",      # 📋
}

DEFAULTS = {
    "SKULL": "💀", "FIRE": "🔥", "LIGHTNING": "⚡", "SHIELD": "🛡️", "CROWN": "👑", "GEM": "💎",
    "ROCKET": "🚀", "TARGET": "🎯", "LOCK": "🔒", "STAR": "⭐", "WARN": "⚠️", "CHECK": "✅",
    "CROSS": "❌", "LOADING": "⏳", "STATS": "📊", "REFER": "🔗", "PROFILE": "👤", "SUPPORT": "💬",
    "ADMIN": "🔧", "BROADCAST": "📢", "TIME": "🕒", "NAME": "✨", "ID": "🆔", "REPORT": "📋",
    "BACK": "◀️", "CANCEL": "✖️", "GIFT": "🎁", "CREDIT": "💰", "HEART": "❤️", "EYE": "👁️",
    "ORDER": "📦", "LINK": "🔗", "SETTING": "⚙️", "MAINTENANCE": "🛠️", "NEW_USER": "👋",
    "REFERRAL": "🎉", "SYSTEM": "🌀", "SERVICE": "🧭", "QUANTITY": "🔢", "COINS": "🪙",
    "ERROR": "❌", "INFO": "💡", "DONE": "✔️", "REFUND": "↩️", "SHARE": "📤", "HELP": "❓",
    "KEY": "🔑", "PREMIUM": "💫", "USER": "👤", "QUEUE": "📥", "EDIT": "✍️", "MONEY": "💸",
    "DOWN": "👇", "WALLET": "👛", "DEPOSIT": "🏦", "BAN": "🚫", "SEARCH": "🔍", "JOIN": "📢",
    "BONUS": "🎁", "LEADER": "🏆", "REDEEM": "🎟️", "LIST": "📋", "COPY": "📋", "REFRESH": "🔄",
    "HOME": "🏠", "NEXT": "▶️", "PLUS": "➕", "MINUS": "➖", "POWER": "🔌", "PROCESS": "⚙️",
    "PARTIAL": "🌗", "CHART": "📈", "SEND": "📨", "PHOTO": "🖼️", "RETRY": "🔁", "UPI": "📲",
}


def get_id(key: str) -> str | None:
    key = key.upper()
    value = str(PREMIUM_EMOJI_IDS.get(key, "")).strip() or (os.getenv(f"EMOJI_{key}") or "").strip()
    return value if value.isdigit() else None


def e(key: str) -> str:
    """Emoji for classic HTML messages (premium custom emoji when configured)."""
    key = key.upper()
    fallback = DEFAULTS.get(key, "⚡")
    cid = get_id(key)
    return f'<tg-emoji emoji-id="{cid}">{fallback}</tg-emoji>' if cid else fallback


def pe(key: str) -> str:
    """Plain unicode emoji (rich messages, toasts, button labels)."""
    return DEFAULTS.get(key.upper(), "⚡")


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


_SMALL_CAPS = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ")


def fancy(text: str) -> str:
    """'Your Wallet' -> 'Yᴏᴜʀ Wᴀʟʟᴇᴛ' (keeps acronyms like IG / ID untouched)."""
    if not settings.fancy_font:
        return text
    words = []
    for word in text.split(" "):
        if len(word) <= 3 and word.isupper():
            words.append(word)
        else:
            words.append(word[:1] + word[1:].lower().translate(_SMALL_CAPS))
    return " ".join(words)


HR = "━━━━━━━━━━━━━━━━━━"


def title(key: str, text: str) -> str:
    return f"{e(key)} <b>{fancy(text)}</b>\n{HR}\n\n"


def fmt_num(value: float | int) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{int(value):,}"


def fmt_short(value: int) -> str:
    if value >= 1_000_000 and value % 100_000 == 0:
        return f"{value / 1_000_000:g}M"
    if value >= 1_000 and value % 100 == 0:
        return f"{value / 1_000:g}K"
    return f"{value:,}"


def fmt_dt(dt: datetime | None) -> str:
    dt = aware(dt)
    return dt.strftime("%d %b %Y, %H:%M UTC") if dt else "—"


def fmt_left(delta: timedelta) -> str:
    secs = max(0, int(delta.total_seconds()))
    hours, rem = divmod(secs, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return ""
    ratio = min(1.0, max(0.0, done / total))
    filled = round(ratio * width)
    return f"{'▰' * filled}{'▱' * (width - filled)} {ratio * 100:.0f}%"


# ======================================================================================
# Buttons (Bot API 9.4 style + icon_custom_emoji_id, 7.11 copy_text)
# ======================================================================================
def btn(text: str, cb: str | None = None, *, url: str | None = None, copy: str | None = None,
        icon: str | None = None, style: str | None = None) -> InlineKeyboardButton:
    api: dict[str, Any] = {}
    if settings.colored_buttons and style in {"primary", "success", "danger"}:
        api["style"] = style
    label = text
    if icon:
        cid = get_id(icon)
        if cid:
            api["icon_custom_emoji_id"] = cid
        else:
            label = f"{pe(icon)} {text}"
    kw: dict[str, Any] = {"api_kwargs": api} if api else {}
    if url:
        return InlineKeyboardButton(label, url=url, **kw)
    if copy is not None:
        return InlineKeyboardButton(label, copy_text=CopyTextButton(copy[:256]), **kw)
    data = (cb or "noop")[:64]
    return InlineKeyboardButton(label, callback_data=data, **kw)


def kb(*rows: list[InlineKeyboardButton] | None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(row) for row in rows if row])


# ======================================================================================
# Services (runtime-editable, persisted in DB settings "services")
# ======================================================================================
GROUP_DEFAULTS: dict[str, Any] = {"name": "New Service", "description": "", "emoji_key": "STAR", "style": "primary", "enabled": True}
SUB_DEFAULTS: dict[str, Any] = {
    "group": "", "name": "Standard", "description": "", "mode": "manual", "rate_per_1k": 10,
    "min": 100, "max": 10_000, "step": 1, "api_url": "", "api_key": "", "service_id": "",
    "type": "Default", "refill": False, "cancel": False, "enabled": True,
}
# fallback shape for orders whose service was deleted
SERVICE_DEFAULTS: dict[str, Any] = {**SUB_DEFAULTS, "label": "Service", "short": "Service", "category": "Service",
                                    "unit": "units", "emoji_key": "SERVICE", "style": "primary"}

BASE_GROUPS: dict[str, dict[str, Any]] = {
    "g_likes": {"name": "Instagram Likes", "description": "Likes on any public post or reel.", "emoji_key": "HEART", "style": "danger"},
    "g_views": {"name": "Instagram Views", "description": "Views for reels & videos — pick your speed.", "emoji_key": "EYE", "style": "primary"},
}
BASE_SERVICES: dict[str, dict[str, Any]] = {
    "likes": {**SUB_DEFAULTS, "group": "g_likes", "name": "Likes", "mode": "auto", "rate_per_1k": 90, "min": 100, "max": 2_000,
              "step": 100, "api_url": "https://electrosmm.com/api/v2"},
    "views_slow": {**SUB_DEFAULTS, "group": "g_views", "name": "Slow", "description": "Natural, gradual delivery.", "mode": "auto",
                   "rate_per_1k": 3, "step": 100, "api_url": "https://luvsmm.com/api/v2", "service_id": "1137"},
    "views_instant": {**SUB_DEFAULTS, "group": "g_views", "name": "Instant", "description": "Starts within minutes.", "mode": "auto",
                      "rate_per_1k": 5, "step": 100, "api_url": "https://luvsmm.com/api/v2", "service_id": "160"},
}

# sub-service field code -> (storage key, label, type)
EDITABLE_FIELDS: dict[str, tuple[str, str, str]] = {
    "name": ("name", "Name", "str"),
    "desc": ("description", "Description", "text"),
    "rate": ("rate_per_1k", "Price per 1K (coins)", "float"),
    "min": ("min", "Min quantity", "int"),
    "max": ("max", "Max quantity", "int"),
    "step": ("step", "Quantity step", "int"),
    "url": ("api_url", "API base URL", "url"),
    "key": ("api_key", "API key", "secret"),
    "sid": ("service_id", "Service ID", "str"),
}
GROUP_FIELDS: dict[str, tuple[str, str, str]] = {
    "name": ("name", "Service name", "str"),
    "desc": ("description", "Description", "text"),
}
STYLES = ("danger", "primary", "success")
CATEGORY_EMOJI = {"like": "HEART", "view": "EYE", "follow": "USER", "comment": "SUPPORT", "share": "SHARE",
                  "save": "STAR", "story": "PHOTO", "reel": "ROCKET", "member": "USER", "reaction": "FIRE"}

GROUPS: dict[str, dict[str, Any]] = {}
SERVICES: dict[str, dict[str, Any]] = {}   # sub-services (the things users actually order)


def guess_emoji(text: str) -> str:
    low = text.lower()
    return next((v for k, v in CATEGORY_EMOJI.items() if k in low), "STAR")


def refresh_derived() -> None:
    """Fill display fields (label, emoji, style, unit…) of every sub-service from its parent service."""
    for spec in SERVICES.values():
        g = GROUPS.get(spec["group"]) or {**GROUP_DEFAULTS, "name": spec.get("name", "Service")}
        siblings = sum(1 for x in SERVICES.values() if x["group"] == spec["group"])
        spec["category"], spec["emoji_key"], spec["style"] = g["name"], g["emoji_key"], g["style"]
        spec["short"] = spec["name"]
        spec["label"] = g["name"] if siblings == 1 else f"{g['name']} · {spec['name']}"
        words = g["name"].lower().split()
        spec["unit"] = words[-1] if words else "units"


def service(kind: str) -> dict[str, Any]:
    spec = SERVICES.get(kind)
    if spec is None:
        raise ValueError(f"Unknown service {kind!r}")
    return spec


def subs_of(gid: str, enabled_only: bool = True) -> list[str]:
    return [k for k, s in SERVICES.items() if s["group"] == gid and (s.get("enabled", True) or not enabled_only)]


def groups(enabled_only: bool = True) -> list[str]:
    return [gid for gid, g in GROUPS.items() if (g.get("enabled", True) or not enabled_only) and subs_of(gid, enabled_only)]


def is_live(kind: str) -> bool:
    spec = SERVICES.get(kind)
    return bool(spec) and spec.get("enabled", True) and GROUPS.get(spec["group"], {}).get("enabled", True)


def provider_ready(spec: dict) -> bool:
    return (spec.get("mode", "auto") == "auto" and bool(str(spec.get("api_url", "")).strip())
            and bool(str(spec.get("api_key", "")).strip()) and bool(str(spec.get("service_id", "")).strip()))


def api_cfg(spec: dict) -> dict:
    return {"api_url": str(spec.get("api_url", "")).strip(), "api_key": str(spec.get("api_key", "")).strip(),
            "service_id": str(spec.get("service_id", "")).strip()}


def is_comments(spec: dict) -> bool:
    return spec.get("mode") == "auto" and "custom comments" in str(spec.get("type", "")).lower()


def truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("true", "1", "yes")


def order_svc(order: dict) -> dict:
    spec = SERVICES.get(order["kind"])
    if spec:
        return spec
    name = order.get("service_label") or order["kind"]
    return {**SERVICE_DEFAULTS, "label": name, "short": name}


def price_for(kind: str, quantity: int) -> int:
    return max(1, math.ceil(quantity / 1_000 * float(service(kind)["rate_per_1k"]) - 1e-9))


def parse_quantity(raw: str) -> int:
    text = raw.lower().replace(",", "").replace(" ", "").strip()
    mult = 1
    if text.endswith("k"):
        mult, text = 1_000, text[:-1]
    elif text.endswith("m"):
        mult, text = 1_000_000, text[:-1]
    value = float(text)
    if value <= 0 or not math.isfinite(value):
        raise ValueError
    qty = value * mult
    if not qty.is_integer():
        raise ValueError
    return int(qty)


def check_quantity(kind: str, quantity: int, comments: bool = False) -> str | None:
    spec = service(kind)
    if quantity < spec["min"] or quantity > spec["max"]:
        what = "Number of comments" if comments else "Quantity"
        return f"{what} must be between {spec['min']:,} and {spec['max']:,}."
    if not comments and spec["step"] > 1 and quantity % spec["step"]:
        return f"Quantity must be a multiple of {spec['step']:,}."
    return None


def quick_quantities(kind: str) -> list[int]:
    spec = service(kind)
    lo, hi, step = int(spec["min"]), int(spec["max"]), max(1, int(spec["step"]))
    picks = {lo, hi}
    for candidate in (500, 1_000, 2_000, 5_000, 10_000, 25_000, 50_000):
        if lo < candidate < hi and candidate % step == 0:
            picks.add(candidate)
    return sorted(picks)[:6]


IG_HOSTS = {"instagram.com", "instagr.am"}
_URL_RE = re.compile(r"(https?://)?(www\.|m\.)?(instagram\.com|instagr\.am)/[^\s<>\"']+", re.IGNORECASE)


def validate_instagram_url(value: str) -> tuple[bool, str]:
    """Accepts post/reel/tv links (with or without username prefix), strips tracking params."""
    match = _URL_RE.search(value or "")
    if not match:
        return False, (value or "").strip()
    raw = match.group(0).rstrip(").,!?;:]}>")
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host not in IG_HOSTS:
        return False, raw
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/")
    m = re.match(r"^(?:/[A-Za-z0-9._]+)?/(p|reel|reels|tv)/([A-Za-z0-9_-]{5,})$", path)
    if not m:
        return False, raw
    kind = "reel" if m.group(1) == "reels" else m.group(1)
    return True, f"https://www.instagram.com/{kind}/{m.group(2)}/"


# ======================================================================================
# Storage — MongoDB (pymongo async / motor) with an in-memory fallback for local testing
# ======================================================================================
ORDER_OPEN = ("pending", "placing", "processing")
ORDER_STATUS_ICON = {
    "pending": "LOADING", "placing": "SYSTEM", "processing": "PROCESS", "completed": "CHECK",
    "partial": "PARTIAL", "refunded": "REFUND",
}


async def _to_list(cursor, length: int | None) -> list[dict]:
    if inspect.isawaitable(cursor):
        cursor = await cursor
    return await cursor.to_list(length=length)


class MongoStore:
    def __init__(self, uri: str, dbname: str):
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError
        try:
            from pymongo import AsyncMongoClient  # pymongo >= 4.13
            self.client = AsyncMongoClient(uri, serverSelectionTimeoutMS=15_000, tz_aware=True)
        except ImportError:  # older installs: motor
            from motor.motor_asyncio import AsyncIOMotorClient
            self.client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=15_000, tz_aware=True)
        self._after = ReturnDocument.AFTER
        self._dup = DuplicateKeyError
        db = self.client[dbname]
        self.users, self.orders, self.deposits = db.users, db.orders, db.deposits
        self.codes, self.code_uses, self.ledger, self.kv = db.redeem_codes, db.redeem_uses, db.ledger, db.settings

    async def close(self) -> None:
        result = self.client.close()
        if inspect.isawaitable(result):
            await result

    async def ensure_indexes(self) -> None:
        await self.users.create_index("telegram_id", unique=True)
        await self.users.create_index("username_lc")
        await self.users.create_index([("referrals", -1)])
        await self.orders.create_index("order_id", unique=True)
        await self.orders.create_index([("user_id", 1), ("created_at", -1)])
        await self.orders.create_index([("status", 1), ("created_at", 1)])
        await self.deposits.create_index("deposit_id", unique=True)
        await self.deposits.create_index([("user_id", 1), ("status", 1)])
        await self.deposits.create_index([("status", 1), ("created_at", 1)])
        await self.codes.create_index("code", unique=True)
        await self.code_uses.create_index([("code", 1), ("user_id", 1)], unique=True)
        await self.ledger.create_index([("user_id", 1), ("at", -1)])
        await self.kv.create_index("key", unique=True)

    # ---- users -----------------------------------------------------------------------
    async def ensure_user(self, uid: int, username: str | None, full_name: str) -> tuple[dict, bool]:
        now = utcnow()
        res = await self.users.update_one(
            {"telegram_id": uid},
            {"$set": {"username": username, "username_lc": (username or "").lower() or None,
                      "full_name": full_name, "last_seen": now, "blocked": False},
             "$setOnInsert": {"telegram_id": uid, "coins": 0, "referrals": 0, "ref_earned": 0, "banned": False,
                              "orders_count": 0, "spent": 0, "created_at": now}},
            upsert=True,
        )
        return await self.users.find_one({"telegram_id": uid}), res.upserted_id is not None

    async def get_user(self, uid: int) -> dict | None:
        return await self.users.find_one({"telegram_id": uid})

    async def find_user(self, query: str) -> dict | None:
        query = query.strip()
        if query.isdigit():
            return await self.get_user(int(query))
        return await self.users.find_one({"username_lc": query.lstrip("@").lower()})

    async def change_coins(self, uid: int, delta: int, reason: str, ref: str | None = None) -> dict | None:
        """Atomic balance change. Negative delta only succeeds if balance is sufficient."""
        flt: dict[str, Any] = {"telegram_id": uid}
        if delta < 0:
            flt["coins"] = {"$gte": -delta}
        user = await self.users.find_one_and_update(flt, {"$inc": {"coins": delta}}, return_document=self._after)
        if user and delta:
            await self.ledger.insert_one({"user_id": uid, "delta": delta, "balance": user["coins"],
                                          "reason": reason, "ref": ref, "at": utcnow()})
        return user

    async def remove_coins_clamped(self, uid: int, amount: int, reason: str) -> dict | None:
        user = await self.get_user(uid)
        if not user:
            return None
        take = min(amount, max(0, int(user.get("coins", 0))))
        if take <= 0:
            return user
        return await self.change_coins(uid, -take, reason) or await self.get_user(uid)

    async def ledger_for(self, uid: int, limit: int = 6) -> list[dict]:
        return await _to_list(self.ledger.find({"user_id": uid}).sort("at", -1), limit)

    async def set_banned(self, uid: int, banned: bool) -> dict | None:
        return await self.users.find_one_and_update({"telegram_id": uid}, {"$set": {"banned": banned}},
                                                    return_document=self._after)

    async def mark_blocked(self, uid: int) -> None:
        await self.users.update_one({"telegram_id": uid}, {"$set": {"blocked": True}})

    async def bump_order_stats(self, uid: int, coins: int) -> None:
        await self.users.update_one({"telegram_id": uid}, {"$inc": {"orders_count": 1, "spent": coins}})

    async def claim_daily(self, uid: int, amount: int, hours: int) -> tuple[bool, dict | None, datetime | None]:
        now = utcnow()
        cutoff = now - timedelta(hours=hours)
        user = await self.users.find_one_and_update(
            {"telegram_id": uid, "$or": [{"last_bonus_at": {"$exists": False}}, {"last_bonus_at": None},
                                         {"last_bonus_at": {"$lte": cutoff}}]},
            {"$inc": {"coins": amount}, "$set": {"last_bonus_at": now}},
            return_document=self._after,
        )
        if user:
            await self.ledger.insert_one({"user_id": uid, "delta": amount, "balance": user["coins"],
                                          "reason": "daily_bonus", "ref": None, "at": now})
            return True, user, now + timedelta(hours=hours)
        user = await self.get_user(uid)
        last = aware(user.get("last_bonus_at")) if user else None
        return False, user, (last + timedelta(hours=hours)) if last else None

    async def top_referrers(self, limit: int = 10) -> list[dict]:
        return await _to_list(self.users.find({"referrals": {"$gt": 0}}).sort("referrals", -1), limit)

    async def claim_referral(self, new_uid: int, referrer_id: int, bonus: int) -> bool:
        if new_uid == referrer_id or not await self.users.find_one({"telegram_id": referrer_id}, {"_id": 1}):
            return False
        claimed = await self.users.find_one_and_update(
            {"telegram_id": new_uid, "referred_by": {"$exists": False}}, {"$set": {"referred_by": referrer_id}})
        if not claimed:
            return False
        ref = await self.users.find_one_and_update(
            {"telegram_id": referrer_id}, {"$inc": {"coins": bonus, "referrals": 1, "ref_earned": bonus}},
            return_document=self._after)
        if ref and bonus:
            await self.ledger.insert_one({"user_id": referrer_id, "delta": bonus, "balance": ref["coins"],
                                          "reason": "referral", "ref": str(new_uid), "at": utcnow()})
        return ref is not None

    async def iter_user_ids(self):
        async for doc in self.users.find({"banned": {"$ne": True}}, {"telegram_id": 1}):
            yield doc["telegram_id"]

    async def count_audience(self) -> int:
        return await self.users.count_documents({"banned": {"$ne": True}})

    # ---- redeem codes ----------------------------------------------------------------
    async def create_code(self, code: str, coins: int, limit: int, expiry: datetime | None) -> bool:
        try:
            await self.codes.insert_one({"code": code, "coins": coins, "usage_limit": limit, "used_count": 0,
                                         "expiry": expiry, "status": "active", "created_at": utcnow()})
            return True
        except self._dup:
            return False

    async def get_code(self, code: str) -> dict | None:
        return await self.codes.find_one({"code": code})

    async def list_codes(self, limit: int = 15) -> list[dict]:
        return await _to_list(self.codes.find({"status": "active"}).sort("created_at", -1), limit)

    async def disable_code(self, code: str) -> bool:
        res = await self.codes.update_one({"code": code, "status": "active"}, {"$set": {"status": "disabled"}})
        return res.modified_count > 0

    async def redeem(self, code: str, uid: int) -> tuple[bool, str, int]:
        doc = await self.codes.find_one({"code": code})
        if not doc or doc.get("status") != "active":
            return False, "Invalid or inactive code.", 0
        if doc.get("expiry") and aware(doc["expiry"]) < utcnow():
            await self.codes.update_one({"code": code}, {"$set": {"status": "expired"}})
            return False, "This code has expired.", 0
        try:
            await self.code_uses.insert_one({"code": code, "user_id": uid, "at": utcnow()})
        except self._dup:
            return False, "You have already redeemed this code.", 0
        taken = await self.codes.find_one_and_update(
            {"code": code, "status": "active",
             "$expr": {"$or": [{"$lte": ["$usage_limit", 0]}, {"$lt": ["$used_count", "$usage_limit"]}]}},
            {"$inc": {"used_count": 1}})
        if not taken:
            await self.code_uses.delete_one({"code": code, "user_id": uid})
            return False, "This code has reached its usage limit.", 0
        await self.change_coins(uid, int(doc["coins"]), "redeem", code)
        return True, "", int(doc["coins"])

    # ---- orders ----------------------------------------------------------------------
    async def create_order(self, order_id: str, uid: int, kind: str, link: str, qty: int, coins: int, label: str = "") -> dict:
        now = utcnow()
        order = {"order_id": order_id, "user_id": uid, "kind": kind, "service_label": label, "link": link, "quantity": qty, "coins": coins,
                 "status": "pending", "provider_order_id": None, "provider_error": None, "provider_status": None,
                 "remains": None, "start_count": None, "refunded": 0, "created_at": now, "updated_at": now}
        await self.orders.insert_one(dict(order))
        return order

    async def get_order(self, oid: str) -> dict | None:
        return await self.orders.find_one({"order_id": oid})

    async def user_orders(self, uid: int, skip: int, limit: int) -> tuple[list[dict], int]:
        total = await self.orders.count_documents({"user_id": uid})
        rows = await _to_list(self.orders.find({"user_id": uid}).sort("created_at", -1).skip(skip).limit(limit), limit)
        return rows, total

    async def orders_by_status(self, statuses: list[str], limit: int = 20) -> list[dict]:
        return await _to_list(self.orders.find({"status": {"$in": statuses}}).sort("created_at", 1), limit)

    async def count_orders(self, statuses: list[str]) -> int:
        return await self.orders.count_documents({"status": {"$in": statuses}})

    async def transition_order(self, oid: str, from_statuses: list[str], to: str, **fields) -> dict | None:
        return await self.orders.find_one_and_update(
            {"order_id": oid, "status": {"$in": from_statuses}},
            {"$set": {"status": to, "updated_at": utcnow(), **fields}}, return_document=self._after)

    async def update_order(self, oid: str, **fields) -> None:
        await self.orders.update_one({"order_id": oid}, {"$set": {**fields, "updated_at": utcnow()}})

    async def reset_stuck_placing(self) -> int:
        res = await self.orders.update_many(
            {"status": "placing"},
            {"$set": {"status": "pending", "provider_error": "Interrupted during placement — verify on the provider panel before retrying."}})
        return res.modified_count

    # ---- deposits --------------------------------------------------------------------
    async def create_deposit(self, uid: int, coins: int, price: int, expiry_minutes: int) -> dict:
        now = utcnow()
        await self.deposits.update_many({"user_id": uid, "status": "awaiting_proof"}, {"$set": {"status": "cancelled"}})
        dep = {"deposit_id": new_id(), "user_id": uid, "coins": coins, "price": price, "status": "awaiting_proof",
               "expires_at": now + timedelta(minutes=expiry_minutes), "created_at": now}
        await self.deposits.insert_one(dict(dep))
        return dep

    async def get_deposit(self, did: str) -> dict | None:
        return await self.deposits.find_one({"deposit_id": did})

    async def transition_deposit(self, did: str, from_statuses: list[str], to: str, **fields) -> dict | None:
        return await self.deposits.find_one_and_update(
            {"deposit_id": did, "status": {"$in": from_statuses}},
            {"$set": {"status": to, "updated_at": utcnow(), **fields}}, return_document=self._after)

    async def deposits_by_status(self, statuses: list[str], limit: int = 20) -> list[dict]:
        return await _to_list(self.deposits.find({"status": {"$in": statuses}}).sort("created_at", 1), limit)

    async def expire_deposits(self) -> int:
        res = await self.deposits.update_many({"status": "awaiting_proof", "expires_at": {"$lt": utcnow()}},
                                              {"$set": {"status": "expired"}})
        return res.modified_count

    # ---- settings + stats ------------------------------------------------------------
    async def get_setting(self, key: str, default=None):
        row = await self.kv.find_one({"key": key})
        return row["value"] if row else default

    async def set_setting(self, key: str, value) -> None:
        await self.kv.update_one({"key": key}, {"$set": {"key": key, "value": value, "updated_at": utcnow()}}, upsert=True)

    async def stats(self) -> dict:
        today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        spent = await _to_list(self.orders.aggregate([
            {"$match": {"status": {"$in": ["processing", "completed", "partial"]}}},
            {"$group": {"_id": None, "t": {"$sum": {"$subtract": ["$coins", {"$ifNull": ["$refunded", 0]}]}}}}]), 1)
        revenue = await _to_list(self.deposits.aggregate([
            {"$match": {"status": "approved"}}, {"$group": {"_id": None, "t": {"$sum": "$price"}}}]), 1)
        return {
            "users": await self.users.count_documents({}),
            "users_today": await self.users.count_documents({"created_at": {"$gte": today}}),
            "banned": await self.users.count_documents({"banned": True}),
            "blocked": await self.users.count_documents({"blocked": True}),
            "pending": await self.orders.count_documents({"status": {"$in": ["pending", "placing"]}}),
            "processing": await self.orders.count_documents({"status": "processing"}),
            "completed": await self.orders.count_documents({"status": {"$in": ["completed", "partial"]}}),
            "orders_today": await self.orders.count_documents({"created_at": {"$gte": today}}),
            "coins_spent": spent[0]["t"] if spent else 0,
            "revenue": revenue[0]["t"] if revenue else 0,
            "deposits_review": await self.deposits.count_documents({"status": "review"}),
        }


class MemoryStore:
    """Same interface, in-memory. Data is LOST on restart — only for local testing."""

    def __init__(self):
        self.users: dict[int, dict] = {}
        self.orders: dict[str, dict] = {}
        self.deposits: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.code_uses: set[tuple[str, int]] = set()
        self.ledger: list[dict] = []
        self.kv: dict[str, Any] = {}

    async def close(self) -> None:
        return None

    async def ensure_indexes(self) -> None:
        return None

    def _log(self, uid, delta, balance, reason, ref=None):
        if delta:
            self.ledger.append({"user_id": uid, "delta": delta, "balance": balance, "reason": reason, "ref": ref, "at": utcnow()})

    async def ensure_user(self, uid, username, full_name):
        now = utcnow()
        is_new = uid not in self.users
        user = self.users.setdefault(uid, {"telegram_id": uid, "coins": 0, "referrals": 0, "ref_earned": 0,
                                           "banned": False, "orders_count": 0, "spent": 0, "created_at": now})
        user.update({"username": username, "username_lc": (username or "").lower() or None,
                     "full_name": full_name, "last_seen": now, "blocked": False})
        return dict(user), is_new

    async def get_user(self, uid):
        user = self.users.get(uid)
        return dict(user) if user else None

    async def find_user(self, query):
        query = query.strip()
        if query.isdigit():
            return await self.get_user(int(query))
        name = query.lstrip("@").lower()
        return next((dict(u) for u in self.users.values() if u.get("username_lc") == name), None)

    async def change_coins(self, uid, delta, reason, ref=None):
        user = self.users.get(uid)
        if not user or (delta < 0 and user["coins"] < -delta):
            return None
        user["coins"] += delta
        self._log(uid, delta, user["coins"], reason, ref)
        return dict(user)

    async def remove_coins_clamped(self, uid, amount, reason):
        user = self.users.get(uid)
        if not user:
            return None
        take = min(amount, max(0, user["coins"]))
        return await self.change_coins(uid, -take, reason) if take else dict(user)

    async def ledger_for(self, uid, limit=6):
        return sorted((x for x in self.ledger if x["user_id"] == uid), key=lambda x: x["at"], reverse=True)[:limit]

    async def set_banned(self, uid, banned):
        user = self.users.get(uid)
        if not user:
            return None
        user["banned"] = banned
        return dict(user)

    async def mark_blocked(self, uid):
        if uid in self.users:
            self.users[uid]["blocked"] = True

    async def bump_order_stats(self, uid, coins):
        if uid in self.users:
            self.users[uid]["orders_count"] += 1
            self.users[uid]["spent"] += coins

    async def claim_daily(self, uid, amount, hours):
        user = self.users.get(uid)
        if not user:
            return False, None, None
        now, last = utcnow(), user.get("last_bonus_at")
        if last and now - last < timedelta(hours=hours):
            return False, dict(user), last + timedelta(hours=hours)
        user["coins"] += amount
        user["last_bonus_at"] = now
        self._log(uid, amount, user["coins"], "daily_bonus")
        return True, dict(user), now + timedelta(hours=hours)

    async def top_referrers(self, limit=10):
        rows = [u for u in self.users.values() if u.get("referrals", 0) > 0]
        return [dict(u) for u in sorted(rows, key=lambda u: u["referrals"], reverse=True)[:limit]]

    async def claim_referral(self, new_uid, referrer_id, bonus):
        new, ref = self.users.get(new_uid), self.users.get(referrer_id)
        if new_uid == referrer_id or not new or not ref or "referred_by" in new:
            return False
        new["referred_by"] = referrer_id
        ref["coins"] += bonus
        ref["referrals"] += 1
        ref["ref_earned"] += bonus
        self._log(referrer_id, bonus, ref["coins"], "referral", str(new_uid))
        return True

    async def iter_user_ids(self):
        for uid, user in list(self.users.items()):
            if not user.get("banned"):
                yield uid

    async def count_audience(self):
        return sum(1 for u in self.users.values() if not u.get("banned"))

    async def create_code(self, code, coins, limit, expiry):
        if code in self.codes:
            return False
        self.codes[code] = {"code": code, "coins": coins, "usage_limit": limit, "used_count": 0, "expiry": expiry,
                            "status": "active", "created_at": utcnow()}
        return True

    async def get_code(self, code):
        return self.codes.get(code)

    async def list_codes(self, limit=15):
        rows = [c for c in self.codes.values() if c["status"] == "active"]
        return sorted(rows, key=lambda c: c["created_at"], reverse=True)[:limit]

    async def disable_code(self, code):
        doc = self.codes.get(code)
        if not doc or doc["status"] != "active":
            return False
        doc["status"] = "disabled"
        return True

    async def redeem(self, code, uid):
        doc = self.codes.get(code)
        if not doc or doc["status"] != "active":
            return False, "Invalid or inactive code.", 0
        if doc.get("expiry") and doc["expiry"] < utcnow():
            doc["status"] = "expired"
            return False, "This code has expired.", 0
        if (code, uid) in self.code_uses:
            return False, "You have already redeemed this code.", 0
        if doc["usage_limit"] > 0 and doc["used_count"] >= doc["usage_limit"]:
            return False, "This code has reached its usage limit.", 0
        self.code_uses.add((code, uid))
        doc["used_count"] += 1
        await self.change_coins(uid, doc["coins"], "redeem", code)
        return True, "", doc["coins"]

    async def create_order(self, order_id, uid, kind, link, qty, coins, label=""):
        now = utcnow()
        order = {"order_id": order_id, "user_id": uid, "kind": kind, "service_label": label, "link": link, "quantity": qty, "coins": coins,
                 "status": "pending", "provider_order_id": None, "provider_error": None, "provider_status": None,
                 "remains": None, "start_count": None, "refunded": 0, "created_at": now, "updated_at": now}
        self.orders[order_id] = order
        return dict(order)

    async def get_order(self, oid):
        order = self.orders.get(oid)
        return dict(order) if order else None

    async def user_orders(self, uid, skip, limit):
        rows = sorted((o for o in self.orders.values() if o["user_id"] == uid), key=lambda o: o["created_at"], reverse=True)
        return [dict(o) for o in rows[skip:skip + limit]], len(rows)

    async def orders_by_status(self, statuses, limit=20):
        rows = sorted((o for o in self.orders.values() if o["status"] in statuses), key=lambda o: o["created_at"])
        return [dict(o) for o in rows[:limit]]

    async def count_orders(self, statuses):
        return sum(1 for o in self.orders.values() if o["status"] in statuses)

    async def transition_order(self, oid, from_statuses, to, **fields):
        order = self.orders.get(oid)
        if not order or order["status"] not in from_statuses:
            return None
        order.update({"status": to, "updated_at": utcnow(), **fields})
        return dict(order)

    async def update_order(self, oid, **fields):
        if oid in self.orders:
            self.orders[oid].update({**fields, "updated_at": utcnow()})

    async def reset_stuck_placing(self):
        count = 0
        for order in self.orders.values():
            if order["status"] == "placing":
                order["status"], count = "pending", count + 1
        return count

    async def create_deposit(self, uid, coins, price, expiry_minutes):
        for dep in self.deposits.values():
            if dep["user_id"] == uid and dep["status"] == "awaiting_proof":
                dep["status"] = "cancelled"
        now = utcnow()
        dep = {"deposit_id": new_id(), "user_id": uid, "coins": coins, "price": price, "status": "awaiting_proof",
               "expires_at": now + timedelta(minutes=expiry_minutes), "created_at": now}
        self.deposits[dep["deposit_id"]] = dep
        return dict(dep)

    async def get_deposit(self, did):
        dep = self.deposits.get(did)
        return dict(dep) if dep else None

    async def transition_deposit(self, did, from_statuses, to, **fields):
        dep = self.deposits.get(did)
        if not dep or dep["status"] not in from_statuses:
            return None
        dep.update({"status": to, "updated_at": utcnow(), **fields})
        return dict(dep)

    async def deposits_by_status(self, statuses, limit=20):
        rows = sorted((d for d in self.deposits.values() if d["status"] in statuses), key=lambda d: d["created_at"])
        return [dict(d) for d in rows[:limit]]

    async def expire_deposits(self):
        count, now = 0, utcnow()
        for dep in self.deposits.values():
            if dep["status"] == "awaiting_proof" and dep["expires_at"] < now:
                dep["status"], count = "expired", count + 1
        return count

    async def get_setting(self, key, default=None):
        return self.kv.get(key, default)

    async def set_setting(self, key, value):
        self.kv[key] = value

    async def stats(self):
        today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        orders = list(self.orders.values())
        return {
            "users": len(self.users),
            "users_today": sum(1 for u in self.users.values() if u["created_at"] >= today),
            "banned": sum(1 for u in self.users.values() if u.get("banned")),
            "blocked": sum(1 for u in self.users.values() if u.get("blocked")),
            "pending": sum(1 for o in orders if o["status"] in ("pending", "placing")),
            "processing": sum(1 for o in orders if o["status"] == "processing"),
            "completed": sum(1 for o in orders if o["status"] in ("completed", "partial")),
            "orders_today": sum(1 for o in orders if o["created_at"] >= today),
            "coins_spent": sum(o["coins"] - o.get("refunded", 0) for o in orders if o["status"] in ("processing", "completed", "partial")),
            "revenue": sum(d["price"] for d in self.deposits.values() if d["status"] == "approved"),
            "deposits_review": sum(1 for d in self.deposits.values() if d["status"] == "review"),
        }


store: MongoStore | MemoryStore = MongoStore(settings.mongo_uri, settings.database_name) if settings.mongo_uri else MemoryStore()


async def load_runtime() -> None:
    saved_groups = await store.get_setting("groups")
    saved = await store.get_setting("services") or {}
    GROUPS.clear()
    SERVICES.clear()
    if saved_groups:
        for gid, g in saved_groups.items():
            GROUPS[gid] = {**GROUP_DEFAULTS, **{k: v for k, v in g.items() if k in GROUP_DEFAULTS}}
        for kind, raw in saved.items():
            SERVICES[kind] = {**SUB_DEFAULTS, **{k: v for k, v in raw.items() if k in SUB_DEFAULTS}}
    else:  # first run, or migrating from the older flat layouts
        legacy_providers = await store.get_setting("providers") or {}
        GROUPS.update({gid: {**GROUP_DEFAULTS, **g} for gid, g in BASE_GROUPS.items()})
        if not saved:
            for kind, base in BASE_SERVICES.items():
                legacy = await store.get_setting(f"provider.{kind}") or {}
                saved[kind] = {**base, **{k: v for k, v in legacy.items() if k in SUB_DEFAULTS}}
        for kind, raw in saved.items():
            base = BASE_SERVICES.get(kind)
            sub = {**SUB_DEFAULTS, **(base or {}), **{k: v for k, v in raw.items() if k in SUB_DEFAULTS and k not in ("group", "name")}}
            prov = legacy_providers.get(raw.get("provider") or "")
            if prov:
                sub["api_url"], sub["api_key"] = prov.get("api_url", ""), prov.get("api_key", "")
            if not base:
                cat = str(raw.get("category") or raw.get("label") or "Service")[:30]
                gid = next((g for g, x in GROUPS.items() if x["name"] == cat), None)
                if not gid:
                    gid = "g" + new_id()[:6].lower()
                    GROUPS[gid] = {**GROUP_DEFAULTS, "name": cat, "emoji_key": raw.get("emoji_key") or guess_emoji(cat),
                                   "style": raw.get("style") or "primary"}
                sub["group"], sub["name"] = gid, str(raw.get("short") or raw.get("label") or "Standard")[:40]
            SERVICES[kind] = sub
    CONFIG.clear()
    CONFIG.update(await store.get_setting("config") or {})
    EXTRA_ADMINS.clear()
    EXTRA_ADMINS.update(int(x) for x in (await store.get_setting("admins") or []))
    CHANNELS.clear()
    CHANNELS.extend(await store.get_setting("channels") or [])
    await save_services()


async def save_services() -> None:
    refresh_derived()
    await store.set_setting("groups", {k: dict(v) for k, v in GROUPS.items()})
    await store.set_setting("services", {k: {f: v[f] for f in SUB_DEFAULTS if f in v} for k, v in SERVICES.items()})


async def save_config() -> None:
    await store.set_setting("config", dict(CONFIG))


async def save_admins() -> None:
    await store.set_setting("admins", sorted(EXTRA_ADMINS))


async def save_channels() -> None:
    await store.set_setting("channels", [dict(c) for c in CHANNELS])


# ======================================================================================
# UI engine — ONE panel message per user, edited in place.
#   • Rich Messages (Bot API 10.1+) when available, classic HTML fallback otherwise
#   • sendMessageDraft (Bot API 9.3+) live progress bubble during slow operations
# ======================================================================================
PANEL = "panel_id"      # user_data key: message id of the live panel
SEQ = "panel_seq"       # user_data key: increments every render (lets background tasks detect navigation)
AWAIT = "await"         # user_data key: pending text/media input state
ORDER = "order_draft"   # user_data key: order being built


@dataclass
class Screen:
    text: str
    markup: InlineKeyboardMarkup | None = None
    rich: str | None = None   # optional rich-HTML version


def _not_modified(exc: Exception) -> bool:
    return "not modified" in str(exc).lower()


class Rich:
    enabled = settings.rich_messages
    _send_failures = 0

    @classmethod
    def ok(cls) -> None:
        cls._send_failures = 0

    @classmethod
    def failed(cls, exc: Exception) -> None:
        cls._send_failures += 1
        log.debug("Rich message failed: %s", exc)
        if cls._send_failures >= 3 and cls.enabled:
            cls.enabled = False
            log.warning("Rich Messages disabled for this run (API rejected them 3x): %s", exc)


def rich_card(key: str, heading: str, rows: list[tuple[str, str]] | None = None,
              paragraphs: list[str] | None = None, footer: str | None = None) -> str:
    parts = [f"<h2>{pe(key)} {esc(heading)}</h2>"]
    for para in paragraphs or []:
        parts.append(f"<p>{para}</p>")
    if rows:
        parts.append("<table>" + "".join(f"<tr><td><b>{esc(k)}</b></td><td>{v}</td></tr>" for k, v in rows) + "</table>")
    if footer:
        parts.append(f"<p><i>{footer}</i></p>")
    return "".join(parts)


async def ui_send(bot, chat_id: int, screen: Screen) -> int:
    if screen.rich and Rich.enabled:
        payload: dict[str, Any] = {"chat_id": chat_id, "rich_message": {"html": screen.rich}}
        if screen.markup:
            payload["reply_markup"] = screen.markup.to_dict()
        try:
            res = await bot.do_api_request("sendRichMessage", api_kwargs=payload)
            Rich.ok()
            return int(res["message_id"])
        except (TelegramError, KeyError, TypeError, ValueError) as exc:
            Rich.failed(exc)
    msg = await bot.send_message(chat_id, screen.text, reply_markup=screen.markup)
    return msg.message_id


async def ui_edit(bot, chat_id: int, message_id: int, screen: Screen) -> bool:
    if screen.rich and Rich.enabled:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "rich_message": {"html": screen.rich}}
        if screen.markup:
            payload["reply_markup"] = screen.markup.to_dict()
        try:
            await bot.do_api_request("editMessageText", api_kwargs=payload)
            return True
        except BadRequest as exc:
            if _not_modified(exc):
                return True
        except TelegramError:
            pass
    try:
        await bot.edit_message_text(screen.text, chat_id=chat_id, message_id=message_id, reply_markup=screen.markup)
        return True
    except BadRequest as exc:
        if _not_modified(exc):
            return True
        log.debug("edit failed (%s) — will send a fresh panel", exc)
        return False
    except TelegramError as exc:
        log.debug("edit failed (%s)", exc)
        return False


async def safe_delete(bot, chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass


async def render(update: Update, context: ContextTypes.DEFAULT_TYPE, screen: Screen, *, force_new: bool = False) -> int:
    """Show `screen` in the user's single panel message (edit in place; send fresh only if needed)."""
    bot, chat_id, ud = context.bot, update.effective_chat.id, context.user_data
    ud[SEQ] = ud.get(SEQ, 0) + 1
    query = update.callback_query
    target = None if force_new else (query.message.message_id if query and query.message else ud.get(PANEL))
    if target and await ui_edit(bot, chat_id, target, screen):
        ud[PANEL] = target
        return target
    new_id_ = await ui_send(bot, chat_id, screen)
    old = ud.get(PANEL)
    ud[PANEL] = new_id_
    if old and old != new_id_:
        await safe_delete(bot, chat_id, old)
    return new_id_


async def clean_input(update: Update, *, force: bool = False) -> None:
    msg = update.effective_message
    if msg and (settings.clean_chat or force) and update.effective_chat.type == "private":
        await safe_delete(msg.get_bot(), msg.chat_id, msg.message_id)


async def safe_answer(query, text: str | None = None, alert: bool = False) -> None:
    try:
        await query.answer(text=text[:200] if text else None, show_alert=alert)
    except TelegramError:
        pass  # already answered / too old


class Draft:
    """Streams an animated live draft bubble (sendMessageDraft) while slow work runs."""
    enabled = settings.message_drafts

    def __init__(self, bot, chat_id: int, frames: list[str], interval: float = 1.0):
        self.bot, self.chat_id, self.frames, self.interval = bot, chat_id, frames, interval
        self.draft_id = random.randint(1, 2**31 - 1)
        self._task: asyncio.Task | None = None

    async def _push(self, text: str) -> None:
        await self.bot.do_api_request("sendMessageDraft", api_kwargs={
            "chat_id": self.chat_id, "draft_id": self.draft_id, "text": text, "parse_mode": "HTML"})

    async def _run(self) -> None:
        i = 0
        while True:
            try:
                await self._push(self.frames[i % len(self.frames)])
            except RetryAfter as exc:
                ra = exc.retry_after
                await asyncio.sleep(ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra))
                continue
            except TelegramError as exc:
                log.info("Message drafts unavailable (%s) — disabled for this run", exc)
                Draft.enabled = False
                return
            i += 1
            await asyncio.sleep(self.interval)

    async def __aenter__(self) -> "Draft":
        if Draft.enabled and self.chat_id > 0:
            self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                await self._push("")  # clear the bubble
            except TelegramError:
                pass


# ======================================================================================
# Small runtime helpers: throttling, per-user locks, force-join cache
# ======================================================================================
_last_click: dict[int, float] = {}
_user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_join_cache: dict[int, float] = {}
_refresh_cache: dict[str, float] = {}
BG: dict[str, Any] = {"broadcast": False}


def throttled(uid: int, gap: float = 0.35) -> bool:
    now = time.monotonic()
    last = _last_click.get(uid, 0.0)
    _last_click[uid] = now
    return now - last < gap


async def missing_channels(bot, uid: int) -> list[dict]:
    """Force-join channels the user hasn't joined yet (fail-open on API errors)."""
    if not CHANNELS or is_admin(uid):
        return []
    if time.monotonic() - _join_cache.get(uid, -1e9) < 300:
        return []
    missing = []
    for ch in list(CHANNELS):
        try:
            member = await bot.get_chat_member(ch["id"], uid)
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except TelegramError as exc:
            log.warning("Force-join check failed for %s (%s) — allowing", ch.get("title"), exc)
    if not missing:
        _join_cache[uid] = time.monotonic()
    return missing


async def force_join_ok(bot, uid: int) -> bool:
    return not await missing_channels(bot, uid)


# ======================================================================================
# SMM provider (standard "API v2": add / status / balance)
# ======================================================================================
class ProviderError(Exception):
    pass


class SMMProvider:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0),
                                        headers={"User-Agent": "Mozilla/5.0 (IGBot/2.0)"})

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, cfg: dict, **params) -> Any:
        url, key = str(cfg.get("api_url", "")).strip(), str(cfg.get("api_key", "")).strip()
        if not url or not key:
            raise ProviderError("Provider URL / API key not configured")
        try:
            resp = await self.client.post(url, data={"key": key, **params})
        except httpx.HTTPError as exc:
            raise ProviderError(f"Network error: {type(exc).__name__}") from exc
        if resp.status_code >= 500:
            raise ProviderError(f"Provider HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"Invalid response (HTTP {resp.status_code})") from exc
        if isinstance(data, dict) and data.get("error"):
            raise ProviderError(str(data["error"])[:200])
        return data

    async def add(self, cfg: dict, link: str, quantity: int, comments: str | None = None) -> str:
        params: dict[str, Any] = {"action": "add", "service": cfg["service_id"], "link": link}
        if comments:
            params["comments"] = comments
        else:
            params["quantity"] = quantity
        data = await self.call(cfg, **params)
        if isinstance(data, dict) and data.get("order"):
            return str(data["order"])
        raise ProviderError("Provider did not return an order id")

    async def status_many(self, cfg: dict, ids: list[str]) -> dict[str, dict]:
        if len(ids) == 1:
            data = await self.call(cfg, action="status", order=ids[0])
            return {ids[0]: data if isinstance(data, dict) else {}}
        data = await self.call(cfg, action="status", orders=",".join(ids))
        return {str(k): v for k, v in data.items()} if isinstance(data, dict) else {}

    async def refill(self, cfg: dict, provider_order_id: str) -> str:
        data = await self.call(cfg, action="refill", order=provider_order_id)
        if isinstance(data, dict) and data.get("refill") not in (None, "", 0, "0", False):
            return str(data["refill"])
        raise ProviderError("Refill was not accepted")

    async def cancel(self, cfg: dict, provider_order_id: str) -> None:
        data = await self.call(cfg, action="cancel", orders=provider_order_id)
        row = data[0] if isinstance(data, list) and data else data
        result = row.get("cancel") if isinstance(row, dict) else None
        if isinstance(result, dict) and result.get("error"):
            raise ProviderError(str(result["error"])[:200])
        if not result:
            raise ProviderError("Cancel was not accepted")

    async def services(self, cfg: dict) -> list[dict]:
        data = await self.call(cfg, action="services")
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []

    async def balance(self, cfg: dict) -> str:
        data = await self.call(cfg, action="balance")
        if isinstance(data, dict) and "balance" in data:
            return f"{data['balance']} {data.get('currency', '')}".strip()
        raise ProviderError("Unexpected balance response")


provider = SMMProvider()


def _to_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ======================================================================================
# Order engine
# ======================================================================================
async def notify(bot, uid: int, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        await bot.send_message(uid, text, reply_markup=markup)
        return True
    except Forbidden:
        await store.mark_blocked(uid)
    except TelegramError as exc:
        log.info("notify %s failed: %s", uid, exc)
    return False


async def notify_admins(bot, text: str, markup: InlineKeyboardMarkup | None = None, photo: str | None = None) -> None:
    for admin_id in all_admin_ids():
        try:
            if photo:
                await bot.send_photo(admin_id, photo, caption=text[:1024], reply_markup=markup)
            else:
                await bot.send_message(admin_id, text, reply_markup=markup)
        except TelegramError as exc:
            log.warning("Could not notify admin %s: %s", admin_id, exc)


async def auto_place(order: dict) -> tuple[bool, str | None]:
    """Try to place a pending order on the provider. Returns (placed, error)."""
    try:
        spec = service(order["kind"])
    except ValueError:
        return False, "This service no longer exists — fulfil manually."
    if order.get("manual"):
        return False, "Manual service — waiting for admin approval."
    if not provider_ready(spec):
        return False, "API not configured for this service — manual fulfilment."
    cfg_ = api_cfg(spec)
    claimed = await store.transition_order(order["order_id"], ["pending"], "placing")
    if not claimed:
        return False, "Order is no longer pending."
    try:
        pid = await provider.add(cfg_, order["link"], int(order["quantity"]), order.get("comments"))
    except ProviderError as exc:
        await store.transition_order(order["order_id"], ["placing"], "pending", provider_error=str(exc))
        return False, str(exc)
    except Exception as exc:  # never leave an order stuck in "placing"
        log.exception("auto_place crashed for %s", order["order_id"])
        await store.transition_order(order["order_id"], ["placing"], "pending", provider_error=f"Internal: {exc}")
        return False, "Internal error"
    await store.transition_order(order["order_id"], ["placing"], "processing", provider_order_id=pid,
                                 provider_error=None, provider_url=cfg_["api_url"], provider_service=cfg_["service_id"],
                                 placed_at=utcnow())
    return True, None


async def refund_order(bot, order: dict, to_status: str, amount: int, reason: str, from_statuses: list[str],
                       **fields) -> dict | None:
    amount = max(0, min(int(amount), int(order["coins"])))
    updated = await store.transition_order(order["order_id"], from_statuses, to_status, refunded=amount, **fields)
    if not updated:
        return None
    if amount:
        await store.change_coins(order["user_id"], amount, reason, order["order_id"])
    return updated


def _order_markup(oid: str) -> InlineKeyboardMarkup:
    return kb([btn("Track Order", f"od:{oid}", icon="ORDER", style="primary")])


async def apply_provider_status(bot, order: dict, info: dict) -> str | None:
    """Apply one provider status row. Returns the new final status if it changed."""
    if not isinstance(info, dict) or info.get("error"):
        return None
    status = str(info.get("status", "")).strip().lower().replace("_", " ")
    remains, start = _to_int(info.get("remains")), _to_int(info.get("start_count"))
    oid, qty, uid = order["order_id"], int(order["quantity"]), order["user_id"]
    label = order_svc(order)["label"]
    if status == "completed":
        if await store.transition_order(oid, ["processing"], "completed", remains=0, start_count=start, completed_at=utcnow()):
            await notify(bot, uid, f"{e('DONE')} <b>{fancy('Order Completed')}</b>\n\n<code>#{oid}</code> · {esc(label)} · "
                                   f"<b>{qty:,}</b> delivered. Thank you!", _order_markup(oid))
            return "completed"
    elif status == "partial":
        remains = max(0, min(qty, remains or 0))
        refund = math.floor(int(order["coins"]) * remains / qty) if qty else 0
        if await refund_order(bot, order, "partial", refund, "partial_refund", ["processing"],
                              remains=remains, start_count=start, completed_at=utcnow()):
            await notify(bot, uid, f"{e('PARTIAL')} <b>{fancy('Order Partially Completed')}</b>\n\n<code>#{oid}</code> · "
                                   f"{qty - remains:,}/{qty:,} delivered.\n{e('REFUND')} <b>{refund}</b> coins refunded.",
                         _order_markup(oid))
            return "partial"
    elif status in ("canceled", "cancelled", "refunded", "fail", "failed", "error"):
        if await refund_order(bot, order, "refunded", int(order["coins"]), "order_refund", ["processing"],
                              provider_status=status, completed_at=utcnow()):
            await notify(bot, uid, f"{e('REFUND')} <b>{fancy('Order Cancelled')}</b>\n\n<code>#{oid}</code> was cancelled by the "
                                   f"provider. <b>{order['coins']}</b> coins returned to your wallet.", _order_markup(oid))
            return "refunded"
    elif status and (status != order.get("provider_status") or remains != order.get("remains")):
        await store.update_order(oid, provider_status=status, remains=remains, start_count=start)
    return None


def _cfg_for_order(order: dict) -> dict | None:
    if order.get("manual"):
        return None
    url = order.get("provider_url")
    spec = SERVICES.get(order["kind"])
    if spec and spec.get("api_key") and spec.get("api_url") and (not url or spec["api_url"] == url):
        return api_cfg(spec)
    for other in SERVICES.values():
        if url and other.get("api_url") == url and other.get("api_key"):
            return api_cfg(other)
    return None


async def sync_orders(bot, only: list[dict] | None = None) -> dict[str, int]:
    orders = only if only is not None else await store.orders_by_status(["processing"], limit=1_000)
    groups: dict[tuple[str, str], tuple[dict, list[dict]]] = {}
    for order in orders:
        cfg = _cfg_for_order(order)
        if cfg and order.get("provider_order_id"):
            groups.setdefault((cfg["api_url"], cfg["api_key"]), (cfg, []))[1].append(order)
    result = {"checked": 0, "completed": 0, "partial": 0, "refunded": 0, "errors": 0}
    for cfg, rows in groups.values():
        for i in range(0, len(rows), 100):
            chunk = rows[i:i + 100]
            try:
                data = await provider.status_many(cfg, [str(o["provider_order_id"]) for o in chunk])
            except ProviderError as exc:
                log.warning("Status sync failed for %s: %s", cfg.get("api_url"), exc)
                result["errors"] += 1
                continue
            for order in chunk:
                result["checked"] += 1
                changed = await apply_provider_status(bot, order, data.get(str(order["provider_order_id"])) or {})
                if changed:
                    result[changed] += 1
    return result


async def background_loop(app: Application) -> None:
    while True:
        await asyncio.sleep(settings.status_sync_minutes * 60)
        try:
            res = await sync_orders(app.bot)
            expired = await store.expire_deposits()
            if res["checked"] or expired:
                log.info("Sync: %s · expired deposits: %s", res, expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background sync failed")


# ======================================================================================
# Screens — pure functions returning Screen(text, markup, rich)
# ======================================================================================
REASONS = {
    "order": "Order", "order_rollback": "Order rollback", "order_refund": "Order refund", "partial_refund": "Partial refund",
    "admin_refund": "Refund", "deposit": "Deposit", "redeem": "Redeem code", "referral": "Referral bonus",
    "daily_bonus": "Daily bonus", "admin_add": "Added by admin", "admin_remove": "Removed by admin",
}


def home_btn() -> list[InlineKeyboardButton]:
    return [btn("Main Menu", "home", icon="HOME", style="danger")]


def cancel_btn(cb: str = "cancel") -> list[InlineKeyboardButton]:
    return [btn("Cancel", cb, icon="CANCEL", style="danger")]


def svc_label(kind: str) -> str:
    spec = service(kind)
    return f"{spec['short']} · {fmt_num(spec['rate_per_1k'])}/K" + ("" if spec.get("enabled", True) else " · OFF")


def mode_badge(spec: dict) -> str:
    if spec.get("mode", "auto") != "auto":
        return "✋ Manual"
    return "⚡ Automatic" if provider_ready(spec) else "⚠️ Automatic (API incomplete)"


def extras(spec: dict) -> str:
    tags = []
    if spec.get("refill"):
        tags.append("♻️ Refill")
    if spec.get("cancel") and spec.get("mode") == "auto":
        tags.append("✖️ Cancel")
    if is_comments(spec):
        tags.append("💬 Custom comments")
    return " · ".join(tags)


def home_screen(user: dict, admin: bool) -> Screen:
    gids = groups()
    lineup = "  ·  ".join(f"{e(GROUPS[g]['emoji_key'])} {fancy(GROUPS[g]['name'])}" for g in gids[:4])
    text = (
        f"{e('SKULL')} <b>{fancy('Welcome To')} {esc(C.bot_name)}</b> {e('SKULL')}\n{HR}\n\n"
        f"{e('FIRE')} <b>{fancy('The Premium Instagram Growth Panel')}</b>\n\n"
        + (f"{lineup}\n\n" if lineup else "") +
        f"{e('GEM')} <b>{fancy('Balance')}:</b> <code>{fmt_num(user.get('coins', 0))} coins</code>\n"
        f"{e('ORDER')} <b>{fancy('Orders')}:</b> <code>{user.get('orders_count', 0)}</code>\n\n"
        f"{e('INFO')} <i>Tip: just paste an Instagram link here to order instantly.</i>\n\n"
        f"{e('DOWN')} <b>{fancy('Choose An Option Below')}</b>"
    )
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for gid in gids[:12]:
        g, kinds = GROUPS[gid], subs_of(gid)
        single = len(kinds) == 1
        label = f"{g['name']} · {fmt_num(SERVICES[kinds[0]]['rate_per_1k'])}/K" if single else g["name"]
        row.append(btn(label, f"svc:{kinds[0]}" if single else f"grp:{gid}", icon=g["emoji_key"], style=g["style"]))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows += [
        [btn("Profile", "profile", icon="PROFILE"), btn("Wallet", "wallet", icon="WALLET", style="success")],
        [btn("My Orders", "orders:0", icon="ORDER"), btn("Referral", "ref", icon="REFER")],
        [btn("Daily Bonus", "bonus", icon="BONUS", style="success"), btn("Leaderboard", "lb", icon="LEADER")],
        [btn("Redeem Code", "redeem", icon="REDEEM"), btn("Support", "support", icon="SUPPORT")],
    ]
    if admin:
        rows.append([btn("Admin Panel", "a", icon="ADMIN", style="primary")])
    return Screen(text, kb(*rows))


def group_screen(gid: str) -> Screen:
    g = GROUPS.get(gid)
    kinds = subs_of(gid) if g and g.get("enabled", True) else []
    if not kinds:
        return Screen(title("WARN", "Unavailable") + "This service isn't available right now.", kb(home_btn()))
    blocks = []
    for k in kinds:
        sp = SERVICES[k]
        line = (f"{e(g['emoji_key'])} <b>{esc(sp['name'])}</b> — <b>{fmt_num(sp['rate_per_1k'])} coins / 1K</b>\n"
                f"   {e('TARGET')} {sp['min']:,} – {sp['max']:,}" + (f" · {extras(sp)}" if extras(sp) else ""))
        if sp.get("description"):
            line += f"\n   <i>{esc(sp['description'])}</i>"
        blocks.append(line)
    text = (title(g["emoji_key"], g["name"]) + (f"<i>{esc(g['description'])}</i>\n\n" if g.get("description") else "")
            + f"{e('DOWN')} Choose a type:\n\n" + "\n\n".join(blocks))
    rows = [[btn(f"{SERVICES[k]['name']} · {fmt_num(SERVICES[k]['rate_per_1k'])}/K", f"svc:{k}", icon=g["emoji_key"], style=g["style"])]
            for k in kinds]
    return Screen(text, kb(*rows, [btn("Back", "home", icon="BACK", style="danger")]))


def pick_service_screen(link: str) -> Screen:
    text = title("LINK", "Link Detected") + f"<code>{esc(link)}</code>\n\n{e('DOWN')} Which service do you want for this post?"
    rows = [[btn(svc_label(k), f"svc:{k}", icon=s["emoji_key"], style=s["style"])] for k, s in SERVICES.items() if is_live(k)]
    return Screen(text, kb(*rows[:14], cancel_btn()))


def _svc_header(spec: dict) -> str:
    out = title(spec["emoji_key"], spec["label"])
    if spec.get("description"):
        out += f"<i>{esc(spec['description'])}</i>\n"
    tags = extras(spec)
    out += (f"{tags}\n" if tags else "") + ("\n" if spec.get("description") or tags else "")
    return out


def link_prompt(kind: str, error: str | None = None) -> Screen:
    spec = service(kind)
    text = (_svc_header(spec) +
            f"{e('LINK')} <b>Step 1/2</b> — send the <b>public</b> Instagram post or reel link.\n\n"
            f"{e('TARGET')} Example:\n<code>https://www.instagram.com/reel/ABC123xyz/</code>\n\n"
            f"{e('SHIELD')} Accepted: /p/, /reel/, /tv/ links. Private accounts can't receive {spec['unit']}.")
    if error:
        text += f"\n\n{e('ERROR')} <b>{esc(error)}</b>"
    return Screen(text, kb(cancel_btn()))


def comments_prompt(kind: str, link: str, error: str | None = None) -> Screen:
    spec = service(kind)
    text = (_svc_header(spec) + f"{e('LINK')} <code>{esc(link)}</code>\n\n"
            f"{e('SUPPORT')} <b>Step 2/2</b> — send your comments, <b>one per line</b>.\n"
            f"Each line = 1 comment · allowed <b>{spec['min']:,} – {spec['max']:,}</b> comments\n"
            f"{e('COINS')} Rate: <b>{fmt_num(spec['rate_per_1k'])} coins / 1K</b>")
    if error:
        text += f"\n\n{e('ERROR')} <b>{esc(error)}</b>"
    return Screen(text, kb([btn("Change Link", "ord:link", icon="EDIT")], cancel_btn()))


def qty_screen(kind: str, link: str, error: str | None = None) -> Screen:
    spec = service(kind)
    text = (_svc_header(spec) +
            f"{e('LINK')} <code>{esc(link)}</code>\n\n"
            f"{e('QUANTITY')} <b>Step 2/2</b> — pick a quantity or type one (e.g. <code>1500</code> or <code>2k</code>).\n"
            f"Allowed: <b>{spec['min']:,} – {spec['max']:,}</b>" + (f" · step <b>{spec['step']:,}</b>" if spec["step"] > 1 else "") +
            f"\n{e('COINS')} Rate: <b>{fmt_num(spec['rate_per_1k'])} coins / 1K</b>")
    if error:
        text += f"\n\n{e('ERROR')} <b>{esc(error)}</b>"
    picks = [btn(f"{fmt_short(q)} · {price_for(kind, q)}🪙", f"qty:{q}") for q in quick_quantities(kind)]
    rows = [picks[i:i + 2] for i in range(0, len(picks), 2)]
    return Screen(text, kb(*rows, [btn("Change Link", "ord:link", icon="EDIT")], cancel_btn()))


def review_screen(draft: dict, balance: int, note: str | None = None) -> Screen:
    kind, qty, coins = draft["kind"], draft["quantity"], draft["coins"]
    spec = service(kind)
    enough = balance >= coins
    what = f"{qty:,} comments" if draft.get("comments") else f"{qty:,} {spec['unit']}"
    text = (title("REPORT", "Review Your Order") +
            f"{e('SERVICE')} Service: {e(spec['emoji_key'])} <b>{esc(spec['label'])}</b>\n"
            f"{e('LINK')} Link: <code>{esc(draft['link'])}</code>\n"
            f"{e('QUANTITY')} Quantity: <b>{what}</b>\n"
            f"{e('COINS')} Cost: <b>{coins} coins</b>\n"
            f"{e('WALLET')} Balance: <b>{fmt_num(balance)} coins</b>\n"
            + (f"{e('RETRY')} Refill: <b>available</b>\n" if spec.get("refill") else "") + "\n")
    if spec.get("mode") != "auto":
        text += f"{e('INFO')} This is a manual service — an admin will approve it and share an Order ID.\n"
    text += (f"{e('WARN')} Confirm only if the link is <b>public</b> and correct."
             if enough else f"{e('ERROR')} <b>Insufficient balance</b> — you need <b>{coins - balance}</b> more coins.")
    if note:
        text += f"\n\n{e('INFO')} {esc(note)}"
    rows_rich = [("Service", esc(spec["label"])), ("Quantity", what), ("Cost", f"{coins} coins"),
                 ("Balance", f"{fmt_num(balance)} coins")]
    rich = rich_card("REPORT", "Review Your Order", rows_rich, [f"<code>{esc(draft['link'])}</code>"],
                     "Confirm only if the link is public and correct." if enough else f"Insufficient balance — need {coins - balance} more coins.")
    first = ([btn(f"Confirm & Pay {coins}", "ord:ok", icon="CHECK", style="success")] if enough
             else [btn("Deposit Coins", "dep", icon="DEPOSIT", style="success")])
    change = btn("Comments", "ord:com", icon="EDIT") if draft.get("comments") else btn("Quantity", "ord:qty", icon="EDIT")
    return Screen(text, kb(first, [change, btn("Link", "ord:link", icon="LINK")], cancel_btn()), rich)


def processing_screen(order: dict) -> Screen:
    spec = order_svc(order)
    text = (title("LOADING", "Placing Your Order") +
            f"{e('ID')} Order: <code>#{order['order_id']}</code>\n{e(spec['emoji_key'])} {esc(spec['label'])} · "
            f"<b>{order['quantity']:,}</b>\n\n{e('SYSTEM')} Connecting to the growth network…")
    return Screen(text, None)


def receipt_screen(order: dict, error: str | None = None) -> Screen:
    spec = order_svc(order)
    placed = order["status"] == "processing"
    status_line = (f"{e('ROCKET')} <b>Live on the network</b> — delivery has started." if placed else
                   f"{e('LOADING')} <b>Waiting for admin approval</b> — you'll get your Order ID soon." if order.get("manual") else
                   f"{e('LOADING')} <b>Queued</b> — our team will process it shortly.")
    text = (title("CHECK", "Order Received") +
            f"{e('ID')} Order ID: <code>#{order['order_id']}</code>\n"
            f"{e('SERVICE')} Service: {esc(spec['label'])}\n"
            f"{e('QUANTITY')} Quantity: <b>{order['quantity']:,}</b>\n"
            f"{e('COINS')} Paid: <b>{order['coins']} coins</b>\n\n{status_line}")
    rich = rich_card("CHECK", "Order Received",
                     [("Order ID", f"<code>#{order['order_id']}</code>"), ("Service", esc(spec["label"])),
                      ("Quantity", f"{order['quantity']:,}"), ("Paid", f"{order['coins']} coins"),
                      ("Status", "Live — delivering" if placed else "Queued for processing")])
    markup = kb([btn("Track Order", f"od:{order['order_id']}", icon="ORDER", style="primary"),
                 btn("Copy ID", copy=order["order_id"], icon="COPY")],
                [btn("New Order", f"svc:{order['kind']}", icon="ROCKET", style="success")], home_btn())
    return Screen(text, markup, rich)


PAGE = 5


def orders_screen(rows: list[dict], total: int, page: int) -> Screen:
    if not rows:
        return Screen(title("ORDER", "My Orders") + "No orders yet. Choose Likes or Views from the menu to start.",
                      kb([btn("Start Ordering", "home", icon="ROCKET", style="success")]))
    pages = max(1, math.ceil(total / PAGE))
    lines, buttons = [], []
    for o in rows:
        spec = order_svc(o)
        lines.append(f"{e(ORDER_STATUS_ICON.get(o['status'], 'INFO'))} <b>#{o['order_id']}</b> · {esc(spec['short'])} · "
                     f"{o['quantity']:,} · <i>{o['status'].title()}</i>")
        buttons.append([btn(f"#{o['order_id']} · {fmt_short(o['quantity'])} · {o['status'].title()}", f"od:{o['order_id']}",
                            icon=ORDER_STATUS_ICON.get(o["status"], "INFO"))])
    nav = []
    if page > 0:
        nav.append(btn("Prev", f"orders:{page - 1}", icon="BACK"))
    nav.append(btn(f"{page + 1}/{pages}", "noop"))
    if page + 1 < pages:
        nav.append(btn("Next", f"orders:{page + 1}", icon="NEXT"))
    text = title("ORDER", "My Orders") + "\n".join(lines) + f"\n\n{e('INFO')} Tap an order for live status."
    return Screen(text, kb(*buttons, nav, home_btn()))


def _order_rows(o: dict) -> list[tuple[str, str]]:
    spec = order_svc(o)
    rows = [("Order ID", f"<code>#{o['order_id']}</code>"), ("Service", esc(spec["label"])), ("Quantity", f"{o['quantity']:,}"),
            ("Paid", f"{o['coins']} coins"), ("Status", esc(o["status"].title()))]
    if o.get("start_count") is not None:
        rows.append(("Start count", f"{o['start_count']:,}"))
    if o.get("remains") is not None and o["status"] in ("processing", "partial"):
        rows.append(("Delivered", f"{max(0, o['quantity'] - o['remains']):,} / {o['quantity']:,}"))
    if o.get("external_id") or o.get("provider_order_id"):
        rows.append(("Panel order ID", f"<code>{esc(o.get('external_id') or o.get('provider_order_id'))}</code>"))
    if o.get("comments"):
        rows.append(("Comments", f"{len(o['comments'].splitlines())} lines"))
    if o.get("refunded"):
        rows.append(("Refunded", f"{o['refunded']} coins"))
    if o.get("can_refill"):
        rows.append(("Refill", "requested" if o.get("refill_status") == "requested" else "available"))
    rows.append(("Created", fmt_dt(o.get("created_at"))))
    return rows


def order_detail_screen(o: dict, note: str | None = None) -> Screen:
    body = "\n".join(f"<b>{k}:</b> {v}" for k, v in _order_rows(o))
    if o.get("remains") is not None and o["status"] == "processing":
        body += f"\n\n{progress_bar(o['quantity'] - o['remains'], o['quantity'])}"
    text = title(ORDER_STATUS_ICON.get(o["status"], "ORDER"), "Order Details") + body + f"\n\n{e('LINK')} <code>{esc(o['link'])}</code>"
    if note:
        text += f"\n\n{e('INFO')} {esc(note)}"
    rich = rich_card("ORDER", "Order Details", _order_rows(o), [f"<code>{esc(o['link'])}</code>"], note and esc(note))
    rows = []
    oid = o["order_id"]
    if o["status"] == "processing" and not o.get("manual"):
        rows.append([btn("Refresh Status", f"od:r:{oid}", icon="REFRESH", style="primary")])
    if o.get("can_refill") and o["status"] in ("processing", "completed", "partial") and (o.get("external_id") or o.get("provider_order_id")):
        rows.append([btn("Request Refill", f"od:rf:{oid}", icon="RETRY", style="success")])
    if (o["status"] == "pending" and o.get("manual")) or (o["status"] == "processing" and o.get("can_cancel") and not o.get("manual")
                                                         and not o.get("cancel_requested")):
        rows.append([btn("Cancel Order", f"od:cx:{oid}", icon="CANCEL", style="danger")])
    rows.append([btn("Open Post", url=o["link"], icon="LINK"), btn("Copy ID", copy=o["order_id"], icon="COPY")])
    rows.append([btn("My Orders", "orders:0", icon="BACK"), btn("Home", "home", icon="HOME", style="danger")])
    return Screen(text, kb(*rows), rich)


def profile_screen(user: dict) -> Screen:
    rows = [("Name", esc(user.get("full_name", "User"))), ("User ID", f"<code>{user['telegram_id']}</code>"),
            ("Balance", f"{fmt_num(user.get('coins', 0))} coins"), ("Orders", str(user.get("orders_count", 0))),
            ("Coins spent", fmt_num(user.get("spent", 0))), ("Referrals", str(user.get("referrals", 0))),
            ("Joined", fmt_dt(user.get("created_at")))]
    text = title("PROFILE", "Your Profile") + "\n".join(f"<b>{k}:</b> {v}" for k, v in rows)
    return Screen(text, kb([btn("Copy My ID", copy=str(user["telegram_id"]), icon="COPY")], home_btn()),
                  rich_card("PROFILE", "Your Profile", rows))


def wallet_screen(user: dict, ledger: list[dict]) -> Screen:
    hist = []
    for row in ledger:
        sign = "+" if row["delta"] > 0 else ""
        hist.append(f"{'🟢' if row['delta'] > 0 else '🔴'} <code>{sign}{row['delta']}</code> · {esc(REASONS.get(row['reason'], row['reason']))}"
                    f" · <i>{aware(row['at']).strftime('%d %b')}</i>")
    text = (title("WALLET", "Your Wallet") + f"{e('CREDIT')} <b>Available:</b> <code>{fmt_num(user.get('coins', 0))} coins</code>\n\n"
            + (f"<b>{fancy('Recent Activity')}</b>\n" + "\n".join(hist) if hist else f"{e('INFO')} No transactions yet.")
            + f"\n\n{e('SHIELD')} Coins are deducted only when you confirm an order.")
    rich_rows = [(f"{'+' if r['delta'] > 0 else ''}{r['delta']}", f"{esc(REASONS.get(r['reason'], r['reason']))} · {aware(r['at']).strftime('%d %b')}")
                 for r in ledger]
    rich = rich_card("WALLET", "Your Wallet", rich_rows or None, [f"<b>Available:</b> {fmt_num(user.get('coins', 0))} coins"])
    return Screen(text, kb([btn("Deposit Coins", "dep", icon="DEPOSIT", style="success")],
                           [btn("Redeem Code", "redeem", icon="REDEEM"), btn("Daily Bonus", "bonus", icon="BONUS")],
                           home_btn()), rich)


def deposit_menu_screen() -> Screen:
    text = title("DEPOSIT", "Deposit Coins") + f"{esc(C.deposit_info)}\n\n{e('DOWN')} Pick a package:"
    rows = [[btn(f"{coins:,} coins · {C.currency}{price:,}", f"dep:p:{i}", icon="COINS")]
            for i, (coins, price) in enumerate(C.coin_packages)]
    return Screen(text, kb(*rows, [btn("Back", "wallet", icon="BACK", style="danger")]))


def deposit_pay_screen(dep: dict, error: str | None = None) -> Screen:
    left = fmt_left(aware(dep["expires_at"]) - utcnow())
    text = (title("DEPOSIT", f"Deposit #{dep['deposit_id']}") +
            f"{e('COINS')} Package: <b>{dep['coins']:,} coins</b>\n{e('MONEY')} Pay: <b>{C.currency}{dep['price']:,}</b>\n"
            + (f"{e('UPI')} UPI: <code>{esc(C.upi_id)}</code>\n" if C.upi_id else "")
            + f"\n{esc(C.deposit_info)}\n\n"
            f"{e('PHOTO')} After paying, send the <b>payment screenshot</b> here (put the UTR in the caption), "
            f"or just send the <b>UTR / transaction ID</b>.\n{e('TIME')} Expires in <b>{left}</b>.")
    if error:
        text += f"\n\n{e('ERROR')} <b>{esc(error)}</b>"
    rows = []
    if C.upi_id:
        rows.append([btn("Copy UPI ID", copy=C.upi_id, icon="COPY"), btn("Copy Amount", copy=str(dep["price"]), icon="COPY")])
    return Screen(text, kb(*rows, [btn("Cancel Deposit", "dep:x", icon="CANCEL", style="danger")]))


def deposit_sent_screen(dep: dict) -> Screen:
    text = (title("CHECK", "Proof Received") + f"Deposit <code>#{dep['deposit_id']}</code> · <b>{dep['coins']:,} coins</b>\n\n"
            f"{e('LOADING')} The operator is reviewing it. You'll get a message the moment it's approved.")
    return Screen(text, kb([btn("Wallet", "wallet", icon="WALLET")], home_btn()))


def referral_screen(user: dict, link: str) -> Screen:
    share = f"https://t.me/share/url?url={quote(link, safe='')}&text={quote('Grow your Instagram with ' + C.bot_name)}"
    text = (title("REFER", "Referral Program") +
            f"Invite friends and earn <b>+{C.referral_bonus} coins</b> for every new user who joins with your link.\n\n"
            f"{e('USER')} Referrals: <b>{user.get('referrals', 0)}</b>\n"
            f"{e('COINS')} Earned: <b>{user.get('ref_earned', 0)} coins</b>\n\n<code>{esc(link)}</code>")
    return Screen(text, kb([btn("Copy Link", copy=link, icon="COPY", style="primary"), btn("Share", url=share, icon="SHARE")],
                           [btn("Leaderboard", "lb", icon="LEADER")], home_btn()))


def bonus_screen(claimed: bool, user: dict | None, next_at: datetime | None) -> Screen:
    if claimed:
        text = (title("BONUS", "Daily Bonus Claimed") + f"{e('GEM')} <b>+{C.daily_bonus} coins</b> added!\n"
                f"New balance: <b>{fmt_num((user or {}).get('coins', 0))}</b>\n\nCome back in {C.daily_cooldown_hours}h for more.")
    else:
        left = fmt_left(next_at - utcnow()) if next_at else f"{C.daily_cooldown_hours}h"
        text = title("TIME", "Already Claimed") + f"Your next bonus unlocks in <b>{left}</b>."
    return Screen(text, kb([btn("Wallet", "wallet", icon="WALLET")], home_btn()))


def leaderboard_screen(rows: list[dict]) -> Screen:
    if not rows:
        return Screen(title("LEADER", "Referral Leaderboard") + "No referrals yet. Be the first!", kb([btn("Referral", "ref", icon="REFER")], home_btn()))
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else f'{i + 1}.'} {esc((u.get('full_name') or 'User')[:24])} — <b>{u.get('referrals', 0)}</b>"
             for i, u in enumerate(rows)]
    rich = rich_card("LEADER", "Referral Leaderboard",
                     [(medals[i] if i < 3 else f"#{i + 1}", f"{esc((u.get('full_name') or 'User')[:24])} — {u.get('referrals', 0)}") for i, u in enumerate(rows)])
    return Screen(title("LEADER", "Referral Leaderboard") + "\n".join(lines), kb([btn("Referral", "ref", icon="REFER")], home_btn()), rich)


def redeem_screen(error: str | None = None) -> Screen:
    text = title("REDEEM", "Redeem Code") + "Send your promo code below."
    if error:
        text += f"\n\n{e('ERROR')} <b>{esc(error)}</b>"
    return Screen(text, kb(cancel_btn()))


def redeem_ok_screen(coins: int, balance: int) -> Screen:
    return Screen(title("CHECK", "Code Redeemed") + f"{e('GEM')} <b>+{coins} coins</b> added.\nNew balance: <b>{fmt_num(balance)}</b>",
                  kb([btn("Wallet", "wallet", icon="WALLET")], home_btn()))


def support_screen() -> Screen:
    text = title("SUPPORT", "Support") + f"Need help with an order or a deposit? Contact the operator:\n\n{esc(C.support_url)}"
    return Screen(text, kb([btn("Open Support", url=C.support_url, icon="SUPPORT", style="primary")], home_btn()))


def join_screen(missing: list[dict] | None = None) -> Screen:
    chans = missing or CHANNELS
    text = (title("JOIN", "Join Our Channels") + "Please join " + ("these channels" if len(chans) > 1 else "our channel")
            + " to use this bot, then tap <b>I've Joined</b>.\n\n" + "\n".join(f"{e('JOIN')} <b>{esc(c['title'])}</b>" for c in chans))
    rows = [[btn(f"Join {c['title'][:28]}", url=c["url"], icon="JOIN", style="primary")] for c in chans if c.get("url")]
    return Screen(text, kb(*rows, [btn("I've Joined", "join", icon="CHECK", style="success")]))


def banned_screen() -> Screen:
    return Screen(title("BAN", "Access Restricted") + "You are banned from using this bot. Contact support if this is a mistake.",
                  kb([btn("Support", url=C.support_url, icon="SUPPORT")]))


def maintenance_screen() -> Screen:
    return Screen(title("MAINTENANCE", "Under Maintenance") + "New orders are paused for a short while. Please try again soon.",
                  kb(home_btn()))


def expired_screen() -> Screen:
    return Screen(title("WARN", "Session Expired") + "This order session expired. Please start again.", kb(home_btn()))


# ---------------------------------------------------------------------------- admin ----
def admin_screen(st: dict, maintenance: bool) -> Screen:
    text = (title("ADMIN", "Admin Panel") +
            f"{e('USER')} Users: <b>{st['users']:,}</b> (+{st['users_today']} today)\n"
            f"{e('QUEUE')} Pending: <b>{st['pending']}</b> · {e('PROCESS')} Processing: <b>{st['processing']}</b>\n"
            f"{e('DEPOSIT')} Deposits to review: <b>{st['deposits_review']}</b>\n"
            f"{e('SERVICE')} Services: <b>{len(GROUPS)}</b> · Sub-services: <b>{len(SERVICES)}</b> · Channels: <b>{len(CHANNELS)}</b>\n"
            f"{e('MAINTENANCE')} Maintenance: <b>{'ON' if maintenance else 'OFF'}</b> · "
            f"Rich: <b>{'ON' if Rich.enabled else 'OFF'}</b> · Drafts: <b>{'ON' if Draft.enabled else 'OFF'}</b>")
    return Screen(text, kb(
        [btn("Stats", "a:stats", icon="STATS"), btn("Sync Orders", "a:sync", icon="REFRESH", style="primary")],
        [btn(f"Pending ({st['pending']})", "a:q:pending", icon="QUEUE", style="primary"),
         btn(f"Processing ({st['processing']})", "a:q:processing", icon="PROCESS")],
        [btn(f"Deposits ({st['deposits_review']})", "a:deps", icon="DEPOSIT", style="success")],
        [btn("Services", "a:sv", icon="SERVICE", style="primary"), btn("Channels", "a:ch", icon="JOIN")],
        [btn("Admins", "a:ad", icon="ADMIN")],
        [btn("Bot Settings", "a:cf", icon="SETTING"), btn("Coin Packages", "a:pk", icon="COINS")],
        [btn("Find User", "a:find", icon="SEARCH"), btn("Redeem Codes", "a:rc", icon="REDEEM")],
        [btn("Broadcast", "a:bc", icon="BROADCAST"),
         btn(f"Maintenance: {'ON' if maintenance else 'OFF'}", "a:mt", icon="MAINTENANCE", style="danger" if maintenance else None)],
        home_btn()))


def admin_stats_screen(st: dict) -> Screen:
    rows = [("Total users", f"{st['users']:,}"), ("New today", str(st["users_today"])), ("Banned", str(st["banned"])),
            ("Blocked bot", str(st["blocked"])), ("Orders today", str(st["orders_today"])), ("Pending", str(st["pending"])),
            ("Processing", str(st["processing"])), ("Completed", str(st["completed"])),
            ("Coins spent", fmt_num(st["coins_spent"])), ("Deposit revenue", f"{C.currency}{fmt_num(st['revenue'])}")]
    text = title("STATS", "Bot Stats") + "\n".join(f"<b>{k}:</b> {v}" for k, v in rows)
    return Screen(text, kb([btn("Refresh", "a:stats", icon="REFRESH"), btn("Back", "a", icon="BACK", style="danger")]),
                  rich_card("STATS", "Bot Stats", rows))


def admin_queue_screen(status: str, rows: list[dict]) -> Screen:
    head = title("QUEUE" if status == "pending" else "PROCESS", f"{status.title()} Orders")
    if not rows:
        return Screen(head + f"{e('DONE')} Queue is clear.", kb([btn("Back", "a", icon="BACK", style="danger")]))
    lines = [f"<code>#{o['order_id']}</code> · {esc(order_svc(o)['short'])} · {o['quantity']:,} · "
             f"<code>{o['user_id']}</code>" + (f"\n   {e('WARN')} <i>{esc(o['provider_error'][:80])}</i>" if o.get("provider_error") else "")
             for o in rows]
    buttons = [[btn(f"#{o['order_id']} · {fmt_short(o['quantity'])}", f"a:o:{o['order_id']}", icon="ORDER")] for o in rows[:12]]
    return Screen(head + "\n".join(lines), kb(*buttons, [btn("Refresh", f"a:q:{status}", icon="REFRESH"), btn("Back", "a", icon="BACK", style="danger")]))


def admin_order_actions(o: dict, back: bool = True) -> InlineKeyboardMarkup:
    oid = o["order_id"]
    rows = []
    if o["status"] == "pending" and o.get("manual"):
        rows.append([btn("Approve", f"a:oa:ap:{oid}", icon="CHECK", style="success"),
                     btn("Reject & Refund", f"a:oa:ref:{oid}", icon="REFUND", style="danger")])
    elif o["status"] in ("pending", "processing"):
        rows.append([btn("Mark Complete", f"a:oa:done:{oid}", icon="CHECK", style="success"),
                     btn("Refund", f"a:oa:ref:{oid}", icon="REFUND", style="danger")])
    if o["status"] == "pending" and not o.get("manual"):
        rows.append([btn("Retry Auto-Place", f"a:oa:retry:{oid}", icon="RETRY", style="primary")])
    if o["status"] == "processing" and not o.get("manual"):
        rows.append([btn("Check Provider", f"a:oa:sync:{oid}", icon="REFRESH")])
    if o.get("refill_status") == "requested" and (o.get("manual") or not o.get("refill_id")):
        rows.append([btn("Refill Done", f"a:oa:rfd:{oid}", icon="RETRY", style="success")])
    rows.append([btn("Open Post", url=o["link"], icon="LINK"), btn("Copy Link", copy=o["link"], icon="COPY")])
    if back:
        rows.append([btn("Pending", "a:q:pending", icon="BACK"), btn("Admin", "a", icon="ADMIN", style="danger")])
    return kb(*rows)


def admin_order_text(o: dict, user: dict | None, note: str | None = None) -> str:
    spec = order_svc(o)
    host = urlparse(o.get("provider_url") or spec.get("api_url") or "").hostname or "—"
    text = (title(ORDER_STATUS_ICON.get(o["status"], "ORDER"), f"Order #{o['order_id']}") +
            f"<b>User:</b> {esc((user or {}).get('full_name', 'User'))} · <code>{o['user_id']}</code>\n"
            f"<b>Service:</b> {esc(spec['label'])} · {mode_badge(spec)}\n"
            + ("" if o.get("manual") else f"<b>API:</b> {esc(host)} · service ID <code>{esc(o.get('provider_service') or spec.get('service_id') or '—')}</code>\n") +
            f"<b>Quantity:</b> {o['quantity']:,} · <b>Coins:</b> {o['coins']}\n"
            f"<b>Status:</b> {esc(o['status'].title())}"
            + (f" · panel ID <code>{esc(o.get('external_id') or o.get('provider_order_id'))}</code>"
               if o.get("external_id") or o.get("provider_order_id") else "")
            + (f"\n<b>Comments:</b> {len(o['comments'].splitlines())} lines" if o.get("comments") else "")
            + (f"\n{e('RETRY')} <b>Refill requested</b>" if o.get("refill_status") == "requested" else "")
            + f"\n<b>Link:</b> <code>{esc(o['link'])}</code>")
    if o.get("provider_error"):
        text += f"\n\n{e('WARN')} <i>{esc(o['provider_error'])}</i>"
    if note:
        text += f"\n\n{e('INFO')} {esc(note)}"
    return text


def admin_deposits_screen(rows: list[dict]) -> Screen:
    head = title("DEPOSIT", "Deposits")
    if not rows:
        return Screen(head + f"{e('DONE')} Nothing to review.", kb([btn("Back", "a", icon="BACK", style="danger")]))
    lines = [f"<code>#{d['deposit_id']}</code> · <code>{d['user_id']}</code> · {d['coins']:,} coins · {C.currency}{d['price']:,}"
             f" · <i>{d['status'].replace('_', ' ')}</i>" for d in rows]
    buttons = [[btn(f"#{d['deposit_id']} · {C.currency}{d['price']:,}", f"a:d:{d['deposit_id']}", icon="DEPOSIT")] for d in rows[:12]]
    return Screen(head + "\n".join(lines), kb(*buttons, [btn("Refresh", "a:deps", icon="REFRESH"), btn("Back", "a", icon="BACK", style="danger")]))


def deposit_admin_text(d: dict, user: dict | None) -> str:
    return (title("DEPOSIT", f"Deposit #{d['deposit_id']}") +
            f"<b>User:</b> {esc((user or {}).get('full_name', 'User'))} · <code>{d['user_id']}</code>"
            + (f" · @{esc(user['username'])}" if user and user.get("username") else "") +
            f"\n<b>Package:</b> {d['coins']:,} coins for {C.currency}{d['price']:,}\n"
            f"<b>UTR:</b> <code>{esc(d.get('utr') or '—')}</code>\n<b>Status:</b> {esc(d['status'].replace('_', ' ').title())}")


def deposit_admin_actions(did: str, has_photo: bool = False, back: bool = False) -> InlineKeyboardMarkup:
    rows = [[btn("Approve", f"a:da:ok:{did}", icon="CHECK", style="success"), btn("Reject", f"a:da:no:{did}", icon="CROSS", style="danger")]]
    if has_photo:
        rows.append([btn("View Screenshot", f"a:dp:{did}", icon="PHOTO")])
    if back:
        rows.append([btn("Deposits", "a:deps", icon="BACK", style="danger")])
    return kb(*rows)


def mask_key(key: str) -> str:
    return f"••••{key[-4:]}" if len(key) > 6 else ("set" if key else "not set")


def confirm_screen(heading: str, body: str, yes_cb: str, no_cb: str) -> Screen:
    return Screen(title("WARN", heading) + body, kb([btn("Yes, delete", yes_cb, icon="CROSS", style="danger"),
                                                    btn("Cancel", no_cb, icon="BACK")]))


def prompt_screen(key: str, heading: str, body: str, error: str | None = None, cancel: str = "a",
                  rows: list[list[InlineKeyboardButton]] | None = None) -> Screen:
    text = title(key, heading) + body
    if error:
        text += f"\n\n{e('ERROR')} <b>{esc(error)}</b>"
    return Screen(text, kb(*(rows or []), cancel_btn(cancel)))


def admin_services_screen(note: str | None = None) -> Screen:
    lines = []
    for gid, g in GROUPS.items():
        subs = subs_of(gid, enabled_only=False)
        lines.append(f"{e(g['emoji_key'])} <b>{esc(g['name'])}</b> {'🟢' if g.get('enabled', True) else '🔴'}")
        for k in subs:
            sp = SERVICES[k]
            lines.append(f"   • {esc(sp['name'])} · {fmt_num(sp['rate_per_1k'])}/K · {mode_badge(sp)}"
                         + ("" if sp.get("enabled", True) else " · 🔴 off"))
        if not subs:
            lines.append("   <i>no sub-services yet</i>")
    text = title("SERVICE", "Services") + ("\n".join(lines) or "No services yet — add your first one.")
    if note:
        text += f"\n\n{e('INFO')} {esc(note)}"
    rows = [[btn(g["name"], f"a:g:{gid}", icon=g["emoji_key"], style=g["style"])] for gid, g in GROUPS.items()]
    return Screen(text, kb([btn("Add Service", "a:gadd", icon="PLUS", style="success")], *rows[:20],
                           [btn("Back", "a", icon="BACK", style="danger")]))


def admin_group_screen(gid: str, note: str | None = None) -> Screen:
    g = GROUPS[gid]
    subs = subs_of(gid, enabled_only=False)
    text = (title(g["emoji_key"], g["name"]) +
            f"<b>Status:</b> {'🟢 Visible to users' if g.get('enabled', True) else '🔴 Hidden'}\n"
            f"<b>Description:</b> {esc(g.get('description') or '—')}\n\n<b>Sub-services ({len(subs)})</b>\n"
            + ("\n".join(f"• {esc(SERVICES[k]['name'])} · {fmt_num(SERVICES[k]['rate_per_1k'])}/K · {mode_badge(SERVICES[k])}" for k in subs)
               or "<i>None yet — add one below.</i>"))
    if note:
        text += f"\n\n{e('INFO')} {esc(note)}"
    rows = [[btn(f"{SERVICES[k]['name']} · {fmt_num(SERVICES[k]['rate_per_1k'])}/K", f"a:s:{k}",
                 icon="LIGHTNING" if SERVICES[k].get("mode") == "auto" else "EDIT")] for k in subs]
    return Screen(text, kb(*rows, [btn("Add Sub-service", f"a:subadd:{gid}", icon="PLUS", style="success")],
                           [btn("Name", f"a:gf:name:{gid}", icon="EDIT"), btn("Description", f"a:gf:desc:{gid}", icon="EDIT")],
                           [btn("Hide" if g.get("enabled", True) else "Show", f"a:gx:{gid}", icon="POWER",
                                style="danger" if g.get("enabled", True) else "success"),
                            btn("Delete Service", f"a:gdel:{gid}", icon="CROSS", style="danger")],
                           [btn("Services", "a:sv", icon="BACK", style="danger")]))


def admin_sub_screen(kind: str, note: str | None = None) -> Screen:
    sp = service(kind)
    g = GROUPS.get(sp["group"], GROUP_DEFAULTS)
    auto = sp.get("mode") == "auto"
    text = (title(g["emoji_key"], f"{g['name']} › {sp['name']}") +
            f"<b>Type:</b> {mode_badge(sp)} · {'🟢 On' if sp.get('enabled', True) else '🔴 Off'}\n"
            f"<b>Description:</b> {esc(sp.get('description') or '—')}\n"
            f"<b>Price:</b> {fmt_num(sp['rate_per_1k'])} coins / 1K\n"
            f"<b>Quantity:</b> {sp['min']:,} – {sp['max']:,}" + (f" · step {sp['step']:,}" if sp["step"] > 1 else "") + "\n"
            f"<b>Refill:</b> {'ON' if sp.get('refill') else 'OFF'}" + (f" · <b>Cancel:</b> {'ON' if sp.get('cancel') else 'OFF'}" if auto else ""))
    if auto:
        text += (f"\n\n<b>API URL:</b> <code>{esc(sp.get('api_url') or '—')}</code>\n<b>API key:</b> <code>{mask_key(sp.get('api_key', ''))}</code>\n"
                 f"<b>Service ID:</b> <code>{esc(sp.get('service_id') or '—')}</code> · <b>API type:</b> {esc(sp.get('type') or 'Default')}")
        if not provider_ready(sp):
            text += f"\n\n{e('WARN')} Add API URL, key and service ID — until then orders go to admins manually."
    if note:
        text += f"\n\n{e('INFO')} {esc(note)}"
    rows = [
        [btn("Name", f"a:sf:name:{kind}", icon="EDIT"), btn("Description", f"a:sf:desc:{kind}", icon="EDIT")],
        [btn("Price /1K", f"a:sf:rate:{kind}", icon="COINS"), btn("Step", f"a:sf:step:{kind}", icon="QUANTITY")],
        [btn("Min", f"a:sf:min:{kind}", icon="TARGET"), btn("Max", f"a:sf:max:{kind}", icon="TARGET")],
    ]
    if auto:
        rows += [[btn("API URL", f"a:sf:url:{kind}", icon="LINK"), btn("API Key", f"a:sf:key:{kind}", icon="KEY")],
                 [btn("Service ID", f"a:sf:sid:{kind}", icon="ID"), btn("Sync from API", f"a:sr:{kind}", icon="REFRESH", style="primary")],
                 [btn("API Balance", f"a:sb:{kind}", icon="MONEY"), btn("Find Service IDs", f"a:ps:{kind}", icon="SEARCH")]]
    rows += [
        [btn("Switch to Manual" if auto else "Switch to Automatic", f"a:sm:{kind}", icon="SYSTEM", style="primary")],
        [btn(f"Refill: {'ON' if sp.get('refill') else 'OFF'}", f"a:srf:{kind}", icon="RETRY")]
        + ([btn(f"Cancel: {'ON' if sp.get('cancel') else 'OFF'}", f"a:scx:{kind}", icon="CANCEL")] if auto else []),
        [btn("Turn Off" if sp.get("enabled", True) else "Turn On", f"a:sx:{kind}", icon="POWER",
             style="danger" if sp.get("enabled", True) else "success"),
         btn("Delete", f"a:sdel:{kind}", icon="CROSS", style="danger")],
        [btn(f"Back to {g['name'][:20]}", f"a:g:{sp['group']}", icon="BACK", style="danger")],
    ]
    return Screen(text, kb(*rows))


SUB_WIZ = {
    "type": "Choose the type:\n\n⚡ <b>Automatic</b> — orders go straight to your SMM panel API.\n✋ <b>Manual</b> — orders come to admins to approve.",
    "name": "Send the <b>sub-service name</b> (e.g. <code>Slow</code>, <code>Fast</code>, <code>HQ</code>).",
    "desc": "Send a short <b>description</b> users will see, or tap Skip.",
    "url": "Send the SMM panel <b>API base URL</b>\n(e.g. <code>https://luvsmm.com/api/v2</code>).",
    "key": "Send the <b>API key</b>. Your message is deleted immediately.",
    "sid": "Send the provider's <b>service ID</b> (e.g. <code>1137</code>).",
    "rate": "Your selling <b>price in coins per 1,000</b> (e.g. <code>90</code>).",
    "min": "Send the <b>minimum quantity</b>.",
    "max": "Send the <b>maximum quantity</b>.",
}
GROUP_WIZ = {
    "name": "Send the <b>service name</b> users will see (e.g. <code>Instagram Views</code>).",
    "desc": "Send a short <b>description</b> for this service, or tap Skip.",
}


def known_api_urls() -> list[str]:
    urls: list[str] = []
    for sp in SERVICES.values():
        if sp.get("api_url") and sp["api_url"] not in urls:
            urls.append(sp["api_url"])
    return urls[:6]


def saved_key_for(url: str) -> str:
    return next((sp["api_key"] for sp in SERVICES.values() if sp.get("api_url") == url and sp.get("api_key")), "")


def sub_wizard_screen(step: str, draft: dict, error: str | None = None) -> Screen:
    g = GROUPS.get(draft.get("_group", ""), GROUP_DEFAULTS)
    shown = {k: (mask_key(v) if k == "API key" else v) for k, v in draft.items() if not k.startswith("_")}
    done = "\n".join(f"• {esc(k)}: <b>{esc(v)}</b>" for k, v in shown.items())
    body = f"Service: <b>{esc(g['name'])}</b>\n" + (f"{done}\n" if done else "") + "\n" + SUB_WIZ[step]
    info = draft.get("_info")
    if info and step in ("rate", "min", "max"):
        body += (f"\n\n{e('INFO')} <b>From API:</b> {esc(str(info.get('name', ''))[:70])}\n"
                 f"Cost {esc(info.get('rate', '?'))}/1K · {esc(info.get('min', '?'))}–{esc(info.get('max', '?'))}"
                 f" · refill {'✅' if truthy(info.get('refill')) else '❌'} · cancel {'✅' if truthy(info.get('cancel')) else '❌'}"
                 f" · {esc(info.get('type', 'Default'))}")
    rows: list[list[InlineKeyboardButton]] = []
    if step == "type":
        rows = [[btn("Automatic (API)", "a:sw:t:a", icon="LIGHTNING", style="success"), btn("Manual", "a:sw:t:m", icon="EDIT", style="primary")]]
    elif step == "desc":
        rows = [[btn("Skip", "a:sw:skip", icon="NEXT")]]
    elif step == "url":
        rows = [[btn(urlparse(u).hostname or u, f"a:sw:u:{i}", icon="LINK")] for i, u in enumerate(known_api_urls())]
    elif step == "key" and saved_key_for(draft.get("API URL", "")):
        rows = [[btn("Use saved key for this URL", "a:sw:k", icon="KEY", style="success")]]
    elif step in ("min", "max") and draft.get("_s" + step):
        rows = [[btn(f"Use {draft['_s' + step]:,}", "a:sw:ok", icon="CHECK", style="success")]]
    return prompt_screen("PLUS", "Add Sub-service", body, error, f"a:g:{draft.get('_group', '')}" if draft.get("_group") in GROUPS else "a:sv", rows)


# ---------------- channels / admins / config / packages
def admin_channels_screen(note: str | None = None) -> Screen:
    lines = [f"{i + 1}. <b>{esc(c['title'])}</b> · <code>{c['id']}</code>\n   {esc(c.get('url') or '—')}" for i, c in enumerate(CHANNELS)]
    text = (title("JOIN", "Force-Join Channels") + ("\n".join(lines) or "No channels — force join is OFF.")
            + f"\n\n{e('INFO')} Users must join every channel here before using the bot.")
    if note:
        text += f"\n\n{e('INFO')} {esc(note)}"
    rows = [[btn(f"Remove {c['title'][:24]}", f"a:ch:del:{i}", icon="CROSS", style="danger")] for i, c in enumerate(CHANNELS)]
    return Screen(text, kb([btn("Add Channel", "a:ch:add", icon="PLUS", style="success")], *rows,
                           [btn("Back", "a", icon="BACK", style="danger")]))


def admin_admins_screen(owner: bool, note: str | None = None) -> Screen:
    fixed = [f"🔒 <code>{a}</code>" + (" · owner" if a == settings.owner_id else "") for a in sorted(settings.admin_ids)]
    extra = [f"👮 <code>{a}</code>" for a in sorted(EXTRA_ADMINS)]
    text = (title("ADMIN", "Admins") + "\n".join(fixed + extra or ["—"])
            + f"\n\n{e('INFO')} 🔒 = set in .env/Railway (can't be removed here)."
            + ("" if owner else f"\n{e('LOCK')} Only the owner can add or remove admins."))
    if note:
        text += f"\n\n{e('INFO')} {esc(note)}"
    rows = [[btn(f"Remove {a}", f"a:ad:del:{a}", icon="CROSS", style="danger")] for a in sorted(EXTRA_ADMINS)] if owner else []
    first = [btn("Add Admin", "a:ad:add", icon="PLUS", style="success")] if owner else None
    return Screen(text, kb(first, *rows, [btn("Back", "a", icon="BACK", style="danger")]))


# key -> (label, type)   types: str, url, text, opt (send - to clear), int, int0
CONFIG_FIELDS: dict[str, tuple[str, str]] = {
    "bot_name": ("Bot name", "str"),
    "support_url": ("Support link", "url"),
    "upi_id": ("UPI ID", "opt"),
    "deposit_info": ("Deposit instructions", "text"),
    "currency": ("Currency symbol", "str"),
    "referral_bonus": ("Referral bonus (coins)", "int0"),
    "daily_bonus": ("Daily bonus (coins)", "int0"),
    "daily_cooldown_hours": ("Daily bonus cooldown (hours)", "int"),
    "deposit_expiry_minutes": ("Deposit expiry (minutes)", "int"),
}


def admin_config_screen(note: str | None = None) -> Screen:
    lines = [f"<b>{label}:</b> {esc(str(cfg(k))[:60]) or '—'}" for k, (label, _) in CONFIG_FIELDS.items()]
    text = title("SETTING", "Bot Settings") + "\n".join(lines)
    if note:
        text += f"\n\n{e('INFO')} {esc(note)}"
    items = [btn(label.split(" (")[0], f"a:cf:{k}", icon="EDIT") for k, (label, _) in CONFIG_FIELDS.items()]
    rows = [items[i:i + 2] for i in range(0, len(items), 2)]
    return Screen(text, kb(*rows, [btn("Back", "a", icon="BACK", style="danger")]))


def admin_packages_screen(note: str | None = None) -> Screen:
    pk = cfg("coin_packages")
    lines = [f"{i + 1}. <b>{c:,} coins</b> → {C.currency}{p:,}" for i, (c, p) in enumerate(pk)]
    text = title("COINS", "Coin Packages") + ("\n".join(lines) or "No packages — users can't deposit.")
    if note:
        text += f"\n\n{e('INFO')} {esc(note)}"
    rows = [[btn(f"Remove {c:,} coins", f"a:pk:del:{i}", icon="CROSS", style="danger")] for i, (c, _) in enumerate(pk)]
    return Screen(text, kb([btn("Add Package", "a:pk:add", icon="PLUS", style="success")], *rows,
                           [btn("Back", "a", icon="BACK", style="danger")]))


def admin_field_prompt(kind: str, field: str, error: str | None = None) -> Screen:
    sp = service(kind)
    storage, label, ftype = EDITABLE_FIELDS[field]
    current = mask_key(sp.get(storage, "")) if ftype == "secret" else (esc(sp.get(storage, "")) or "—")
    body = f"Sub-service: <b>{esc(sp['label'])}</b>\nCurrent: <code>{current}</code>\n\nSend the new value."
    if ftype == "secret":
        body += f"\n{e('LOCK')} Your message is deleted immediately."
    if ftype == "text":
        body += "\nSend <code>-</code> to clear it."
    return prompt_screen("EDIT", f"Edit {label}", body, error, f"a:s:{kind}")


def admin_group_field_prompt(gid: str, field: str, error: str | None = None) -> Screen:
    storage, label, ftype = GROUP_FIELDS[field]
    body = f"Current: <code>{esc(GROUPS[gid].get(storage) or '—')}</code>\n\nSend the new value." + (
        "\nSend <code>-</code> to clear it." if ftype == "text" else "")
    return prompt_screen("EDIT", f"Edit {label}", body, error, f"a:g:{gid}")


def admin_find_prompt(error: str | None = None) -> Screen:
    text = title("SEARCH", "Find User") + "Send a numeric <b>User ID</b> or <b>@username</b>."
    if error:
        text += f"\n\n{e('ERROR')} <b>{esc(error)}</b>"
    return Screen(text, kb(cancel_btn("a")))


def admin_user_screen(u: dict, orders: list[dict], note: str | None = None) -> Screen:
    uid = u["telegram_id"]
    recent = "\n".join(f"  {e(ORDER_STATUS_ICON.get(o['status'], 'INFO'))} <code>#{o['order_id']}</code> · {o['quantity']:,} · {o['status']}"
                       for o in orders) or "  —"
    text = (title("USER", "User Profile") +
            f"<b>Name:</b> {esc(u.get('full_name', 'User'))}" + (f" · @{esc(u['username'])}" if u.get("username") else "") +
            f"\n<b>ID:</b> <code>{uid}</code>\n<b>Coins:</b> {fmt_num(u.get('coins', 0))} · <b>Spent:</b> {fmt_num(u.get('spent', 0))}\n"
            f"<b>Orders:</b> {u.get('orders_count', 0)} · <b>Referrals:</b> {u.get('referrals', 0)}\n"
            f"<b>Banned:</b> {'Yes' if u.get('banned') else 'No'} · <b>Blocked bot:</b> {'Yes' if u.get('blocked') else 'No'}\n"
            f"<b>Joined:</b> {fmt_dt(u.get('created_at'))}\n\n<b>Recent orders</b>\n{recent}")
    if note:
        text += f"\n\n{e('INFO')} {esc(note)}"
    ban = (btn("Unban", f"a:ub:unban:{uid}", icon="CHECK", style="success") if u.get("banned")
           else btn("Ban", f"a:ub:ban:{uid}", icon="BAN", style="danger"))
    return Screen(text, kb([btn("Add Coins", f"a:uc:add:{uid}", icon="PLUS", style="success"),
                            btn("Remove Coins", f"a:uc:rem:{uid}", icon="MINUS", style="danger")],
                           [ban, btn("Copy ID", copy=str(uid), icon="COPY")],
                           [btn("Find Another", "a:find", icon="SEARCH"), btn("Admin", "a", icon="BACK", style="danger")]))


def admin_coins_prompt(uid: int, mode: str, error: str | None = None) -> Screen:
    verb = "add to" if mode == "add" else "remove from"
    text = title("MONEY", "Adjust Coins") + f"How many coins to <b>{verb}</b> user <code>{uid}</code>?"
    if error:
        text += f"\n\n{e('ERROR')} <b>{esc(error)}</b>"
    return Screen(text, kb(cancel_btn(f"a:u:{uid}")))


def admin_codes_screen(codes: list[dict], note: str | None = None) -> Screen:
    lines = [f"<code>{esc(c['code'])}</code> · {c['coins']} coins · {c['used_count']}/{c['usage_limit'] or '∞'}"
             + (f" · until {aware(c['expiry']).strftime('%d %b')}" if c.get("expiry") else "") for c in codes]
    text = title("REDEEM", "Redeem Codes") + ("\n".join(lines) if lines else "No active codes.")
    if note:
        text += f"\n\n{e('INFO')} {esc(note)}"
    rows = [[btn(f"Disable {c['code']}", f"a:rc:off:{c['code']}", icon="CROSS")] for c in codes[:8]]
    return Screen(text, kb([btn("Create Code", "a:rc:new", icon="PLUS", style="success")], *rows, [btn("Back", "a", icon="BACK", style="danger")]))


CODE_STEPS = {
    "code": "Send the code text (letters/numbers), or <code>auto</code> to generate one.",
    "coins": "How many coins should the code give per user?",
    "limit": "How many users can redeem it in total? Send <code>0</code> for unlimited.",
    "days": "Valid for how many days? Send <code>0</code> for no expiry.",
}


def admin_code_prompt(step: str, draft: dict, error: str | None = None) -> Screen:
    done = " · ".join(f"{k}: <b>{esc(v)}</b>" for k, v in draft.items())
    text = title("REDEEM", "Create Code") + (f"{done}\n\n" if done else "") + CODE_STEPS[step]
    if error:
        text += f"\n\n{e('ERROR')} <b>{esc(error)}</b>"
    return Screen(text, kb(cancel_btn("a:rc")))


def admin_broadcast_prompt() -> Screen:
    return Screen(title("BROADCAST", "Broadcast") + "Send the message to broadcast — text, photo, video, document… "
                  "Formatting, media and buttons are copied exactly.", kb(cancel_btn("a")))


def admin_broadcast_confirm(audience: int) -> Screen:
    return Screen(title("BROADCAST", "Confirm Broadcast") + f"The message above will be sent to <b>{audience:,}</b> users.",
                  kb([btn("Send Now", "a:bc:go", icon="SEND", style="success"), btn("Cancel", "a:bc:no", icon="CANCEL", style="danger")]))


def admin_broadcast_progress(sent: int, failed: int, blocked: int, total: int, done: bool) -> Screen:
    head = title("DONE" if done else "BROADCAST", "Broadcast Finished" if done else "Broadcasting…")
    text = (head + f"{progress_bar(sent + failed + blocked, max(total, 1))}\n\n"
            f"{e('CHECK')} Sent: <b>{sent:,}</b>\n{e('BAN')} Blocked: <b>{blocked:,}</b>\n{e('ERROR')} Failed: <b>{failed:,}</b>\n"
            f"{e('USER')} Audience: <b>{total:,}</b>")
    return Screen(text, kb([btn("Admin", "a", icon="ADMIN")]) if done else None)


# ======================================================================================
# Callback router — one handler, longest-prefix routing, callback answered exactly once
# ======================================================================================
Result = str | tuple[str, bool] | None
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE, list[str]], Awaitable[Result]]
ROUTES: dict[str, tuple[Handler, bool]] = {}
KEEP_AWAIT = {"noop", "dep"}


# input-state handlers registry
InputHandler = Callable[[Update, ContextTypes.DEFAULT_TYPE, dict, str], Awaitable[None]]
INPUTS: dict[str, InputHandler] = {}


def on_input(kind: str):
    def deco(fn: InputHandler) -> InputHandler:
        INPUTS[kind] = fn
        return fn
    return deco


def route(*names: str, admin: bool = False):
    def deco(fn: Handler) -> Handler:
        for name in names:
            ROUTES[name] = (fn, admin)
        return fn
    return deco


async def touch_user(update: Update) -> tuple[dict, bool]:
    tg = update.effective_user
    return await store.ensure_user(tg.id, tg.username, tg.full_name)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    uid = query.from_user.id
    if throttled(uid):
        await safe_answer(query, "⏳ Slow down…")
        return
    parts = (query.data or "noop").split(":")
    for i in range(len(parts), 0, -1):
        entry = ROUTES.get(":".join(parts[:i]))
        if entry:
            fn, admin_only, args = *entry, parts[i:]
            break
    else:
        await safe_answer(query)
        return
    if admin_only and not is_admin(uid):
        await safe_answer(query, "Admin access only.", True)
        return
    if not admin_only:
        user, _ = await touch_user(update)
        if user.get("banned") and not is_admin(uid):
            await safe_answer(query, "🚫 You are banned from using this bot.", True)
            return
        missing = [] if fn is cb_join else await missing_channels(context.bot, uid)
        if missing:
            await render(update, context, join_screen(missing))
            await safe_answer(query, "Please join the channel first.", True)
            return
    if ":".join(parts[:1]) not in KEEP_AWAIT:
        context.user_data.pop(AWAIT, None)  # navigating away cancels any pending text input
    try:
        result = await fn(update, context, args)
    except Exception:
        await safe_answer(query, "Something went wrong. Please try again.", True)
        raise
    toast, alert = (result if isinstance(result, tuple) else (result, False))
    await safe_answer(query, toast, alert)


def set_await(context: ContextTypes.DEFAULT_TYPE, kind: str, **data) -> None:
    context.user_data[AWAIT] = {"kind": kind, **data}


def clear_await(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(AWAIT, None)


async def maintenance_on() -> bool:
    return bool(await store.get_setting("maintenance", False))


async def go_home(update: Update, context: ContextTypes.DEFAULT_TYPE, *, force_new: bool = False) -> None:
    clear_await(context)
    user = await store.get_user(update.effective_user.id) or (await touch_user(update))[0]
    await render(update, context, home_screen(user, is_admin(update.effective_user.id)), force_new=force_new)


# ------------------------------------------------------------------------ user routes --
@route("noop")
async def cb_noop(update, context, args):
    return None


@route("home", "cancel")
async def cb_home(update, context, args):
    context.user_data.pop(ORDER, None)
    await go_home(update, context)


@route("join")
async def cb_join(update, context, args):
    _join_cache.pop(update.effective_user.id, None)
    missing = await missing_channels(context.bot, update.effective_user.id)
    if missing:
        await render(update, context, join_screen(missing))
        return "You haven't joined all channels yet.", True
    await go_home(update, context)
    return "✅ Welcome!"


@route("grp")
async def cb_group(update, context, args):
    await render(update, context, group_screen(args[0] if args else ""))


async def start_order(update, context, kind: str, link: str | None = None) -> Result:
    if kind not in SERVICES:
        return "Unknown service.", True
    if await maintenance_on() and not is_admin(update.effective_user.id):
        await render(update, context, maintenance_screen())
        return None
    if not is_live(kind):
        return "⏸ This service is paused right now.", True
    context.user_data[ORDER] = {"kind": kind}
    if link:
        context.user_data[ORDER]["link"] = link
        await ask_amount(update, context, kind, link)
    else:
        set_await(context, "order_link")
        await render(update, context, link_prompt(kind))
    return None


@route("svc")
async def cb_service(update, context, args):
    kind = args[0] if args else ""
    link = context.user_data.pop("prefill_link", None) or (context.user_data.get(ORDER) or {}).get("link")
    return await start_order(update, context, kind, link)


async def ask_amount(update, context, kind: str, link: str, error: str | None = None) -> None:
    """Step 2: quantity (or the comment list for 'Custom Comments' services)."""
    if is_comments(service(kind)):
        set_await(context, "order_comments")
        await render(update, context, comments_prompt(kind, link, error))
    else:
        set_await(context, "order_qty")
        await render(update, context, qty_screen(kind, link, error))


async def show_review(update, context, qty: int, note: str | None = None) -> None:
    draft = context.user_data[ORDER]
    draft.update(quantity=qty, coins=price_for(draft["kind"], qty))
    set_await(context, "order_comments" if draft.get("comments") else "order_qty")  # typing again updates it
    user = await store.get_user(update.effective_user.id) or {}
    await render(update, context, review_screen(draft, int(user.get("coins", 0)), note))


@route("qty")
async def cb_qty(update, context, args):
    draft = context.user_data.get(ORDER)
    if not draft or "link" not in draft:
        await render(update, context, expired_screen())
        return None
    try:
        qty = int(args[0])
    except (IndexError, ValueError):
        return None
    if err := check_quantity(draft["kind"], qty):
        return err, True
    await show_review(update, context, qty)


@route("ord")
async def cb_order(update, context, args):
    action = args[0] if args else ""
    draft = context.user_data.get(ORDER)
    if action == "x" or not draft:
        context.user_data.pop(ORDER, None)
        if not draft:
            await render(update, context, expired_screen())
            return None
        await go_home(update, context)
        return "Order cancelled."
    if draft["kind"] not in SERVICES:
        context.user_data.pop(ORDER, None)
        await render(update, context, expired_screen())
        return None
    if action == "link":
        draft.pop("quantity", None)
        set_await(context, "order_link")
        await render(update, context, link_prompt(draft["kind"]))
    elif action in ("qty", "com") and "link" in draft:
        draft.pop("comments", None)
        await ask_amount(update, context, draft["kind"], draft["link"])
    elif action == "ok":
        return await confirm_order(update, context)
    return None


async def confirm_order(update, context) -> Result:
    uid, chat_id = update.effective_user.id, update.effective_chat.id
    lock = _user_locks[uid]
    if lock.locked():
        return "⏳ Already processing…"
    async with lock:
        draft = context.user_data.get(ORDER)
        if not draft or "coins" not in draft:
            await render(update, context, expired_screen())
            return None
        if await maintenance_on() and not is_admin(uid):
            await render(update, context, maintenance_screen())
            return None
        kind = draft["kind"]
        if not is_live(kind):
            return "⏸ This service is paused right now.", True
        spec = service(kind)
        if is_comments(spec) and not draft.get("comments"):
            await ask_amount(update, context, kind, draft["link"])
            return None
        if err := check_quantity(kind, draft["quantity"], comments=bool(draft.get("comments"))):
            await ask_amount(update, context, kind, draft["link"], err)
            return None
        fresh_price = price_for(kind, draft["quantity"])
        if fresh_price != draft["coins"]:
            await show_review(update, context, draft["quantity"], "The price was updated — please review again.")
            return "Price updated", True
        oid = new_id()
        if not await store.change_coins(uid, -fresh_price, "order", oid):
            await show_review(update, context, draft["quantity"])
            return "❌ Insufficient balance.", True
        try:
            order = await store.create_order(oid, uid, kind, draft["link"], draft["quantity"], fresh_price, service(kind)["label"])
        except Exception:
            await store.change_coins(uid, fresh_price, "order_rollback", oid)
            raise
        extra = {"manual": spec.get("mode") != "auto", "can_refill": bool(spec.get("refill")),
                 "can_cancel": bool(spec.get("cancel")) and spec.get("mode") == "auto"}
        if draft.get("comments"):
            extra["comments"] = draft["comments"]
        await store.update_order(oid, **extra)
        order.update(extra)
        await store.bump_order_stats(uid, fresh_price)
        context.user_data.pop(ORDER, None)
        clear_await(context)
        panel_id = await render(update, context, processing_screen(order))
        seq = context.user_data.get(SEQ)
    context.application.create_task(place_order_flow(context.application, order, uid, chat_id, panel_id, seq), update=update)
    return "✅ Order placed!"


async def place_order_flow(app: Application, order: dict, uid: int, chat_id: int, panel_id: int, seq: int) -> None:
    bot = app.bot
    spec = order_svc(order)
    frames = [f"{e('SYSTEM')} <b>Placing order #{order['order_id']}</b>\n{bar}  {esc(spec['short'])} · {order['quantity']:,}"
              for bar in ("▰▱▱▱▱", "▰▰▱▱▱", "▰▰▰▱▱", "▰▰▰▰▱", "▰▰▰▰▰")]
    async with Draft(bot, chat_id, frames, interval=0.9):
        placed, error = await auto_place(order)
    fresh = await store.get_order(order["order_id"]) or order
    screen = receipt_screen(fresh, error)
    ud = app.user_data.get(uid, {})
    if ud.get(SEQ) == seq and ud.get(PANEL) == panel_id and await ui_edit(bot, chat_id, panel_id, screen):
        pass  # user is still looking at the panel — updated in place
    else:
        try:
            await ui_send(bot, chat_id, screen)
        except TelegramError as exc:
            log.info("Could not deliver receipt to %s: %s", uid, exc)
    user = await store.get_user(uid)
    note = (f"{e('CHECK')} Auto-placed on provider (<code>{esc(fresh.get('provider_order_id'))}</code>)." if placed else
            f"{e('EDIT')} <b>Manual order</b> — approve it and send the panel Order ID." if fresh.get("manual") else
            f"{e('WARN')} Needs manual action: <i>{esc(error)}</i>")
    await notify_admins(bot, admin_order_text(fresh, user) + f"\n\n{note}", None if placed else admin_order_actions(fresh, back=False))


@route("orders")
async def cb_orders(update, context, args):
    page = max(0, int(args[0])) if args and args[0].isdigit() else 0
    rows, total = await store.user_orders(update.effective_user.id, page * PAGE, PAGE)
    if not rows and page:
        page = 0
        rows, total = await store.user_orders(update.effective_user.id, 0, PAGE)
    await render(update, context, orders_screen(rows, total, page))


@route("od")
async def cb_order_detail(update, context, args):
    action = args[0] if args and args[0] in ("r", "rf", "cx") else ""
    oid = args[1] if action and len(args) > 1 else (args[0] if args else "")
    order = await store.get_order(oid)
    uid = update.effective_user.id
    if not order or (order["user_id"] != uid and not is_admin(uid)):
        return "Order not found.", True
    note, toast = None, None
    if action == "r" and order["status"] == "processing" and not order.get("manual"):
        if time.monotonic() - _refresh_cache.get(oid, -1e9) < 20:
            return "⏳ Just checked — try again in a few seconds."
        _refresh_cache[oid] = time.monotonic()
        res = await sync_orders(context.bot, only=[order])
        note = "Status refreshed from the provider." if not res["errors"] else "Provider is not responding right now."
        toast = "🔄 Updated"
    elif action == "rf":
        note = await request_refill(context.bot, order)
        toast = note
    elif action == "cx":
        note = await request_cancel(context.bot, order)
        toast = note
    order = await store.get_order(oid) or order
    await render(update, context, order_detail_screen(order, note))
    return (toast, True) if action in ("rf", "cx") else toast


REFILL_COOLDOWN = timedelta(hours=24)


async def request_refill(bot, order: dict) -> str:
    oid = order["order_id"]
    if not order.get("can_refill"):
        return "Refill isn't available for this order."
    last = aware(order.get("last_refill_at"))
    if last and utcnow() - last < REFILL_COOLDOWN:
        return f"Refill already requested — try again in {fmt_left(last + REFILL_COOLDOWN - utcnow())}."
    lock = _user_locks[order["user_id"]]
    async with lock:
        if order.get("manual") or not order.get("provider_order_id"):
            await store.update_order(oid, last_refill_at=utcnow(), refill_status="requested")
            fresh = await store.get_order(oid) or order
            await notify_admins(bot, f"{e('RETRY')} <b>{fancy('Refill Request')}</b>\n\n" + admin_order_text(fresh, await store.get_user(order["user_id"])),
                                admin_order_actions(fresh, back=False))
            return "♻️ Refill request sent to the team."
        cfg_ = _cfg_for_order(order)
        if not cfg_:
            return "Refill is unavailable right now — contact support."
        try:
            refill_id = await provider.refill(cfg_, str(order["provider_order_id"]))
        except ProviderError as exc:
            return f"❌ Refill failed: {exc}"
        await store.update_order(oid, last_refill_at=utcnow(), refill_status="requested", refill_id=refill_id)
    return "♻️ Refill requested successfully!"


async def request_cancel(bot, order: dict) -> str:
    oid = order["order_id"]
    if order["status"] == "pending" and order.get("manual"):
        done = await refund_order(bot, order, "refunded", order["coins"], "order_refund", ["pending"], cancelled_by_user=True,
                                  completed_at=utcnow())
        if not done:
            return "This order can't be cancelled any more."
        await notify_admins(bot, f"{e('CANCEL')} User cancelled manual order <code>#{oid}</code> — {order['coins']} coins refunded.")
        return f"✅ Cancelled — {order['coins']} coins refunded."
    if order["status"] != "processing" or not order.get("can_cancel") or order.get("manual"):
        return "This order can't be cancelled."
    if order.get("cancel_requested"):
        return "Cancel already requested."
    cfg_ = _cfg_for_order(order)
    if not cfg_:
        return "Cancel is unavailable right now — contact support."
    try:
        await provider.cancel(cfg_, str(order["provider_order_id"]))
    except ProviderError as exc:
        return f"❌ Cancel failed: {exc}"
    await store.update_order(oid, cancel_requested=True, cancel_requested_at=utcnow())
    return "✖️ Cancel requested — unused coins are refunded automatically once the panel confirms."


@route("profile")
async def cb_profile(update, context, args):
    user = await store.get_user(update.effective_user.id)
    await render(update, context, profile_screen(user))


@route("wallet")
async def cb_wallet(update, context, args):
    clear_await(context)
    uid = update.effective_user.id
    await render(update, context, wallet_screen(await store.get_user(uid), await store.ledger_for(uid)))


@route("dep")
async def cb_deposit(update, context, args):
    uid = update.effective_user.id
    if args and args[0] == "p" and len(args) > 1 and args[1].isdigit():
        idx = int(args[1])
        if idx >= len(C.coin_packages):
            return "Package not available.", True
        coins, price = C.coin_packages[idx]
        dep = await store.create_deposit(uid, coins, price, C.deposit_expiry_minutes)
        set_await(context, "deposit_proof", deposit_id=dep["deposit_id"])
        await render(update, context, deposit_pay_screen(dep))
        return None
    if args and args[0] == "x":
        state = context.user_data.get(AWAIT) or {}
        if state.get("deposit_id"):
            await store.transition_deposit(state["deposit_id"], ["awaiting_proof"], "cancelled")
        clear_await(context)
        await cb_wallet(update, context, [])
        return "Deposit cancelled."
    clear_await(context)
    await render(update, context, deposit_menu_screen())


@route("ref")
async def cb_referral(update, context, args):
    user = await store.get_user(update.effective_user.id)
    link = f"https://t.me/{context.bot.username}?start=ref{update.effective_user.id}"
    await render(update, context, referral_screen(user, link))


@route("bonus")
async def cb_bonus(update, context, args):
    claimed, user, next_at = await store.claim_daily(update.effective_user.id, C.daily_bonus, C.daily_cooldown_hours)
    await render(update, context, bonus_screen(claimed, user, next_at))
    return f"🎁 +{C.daily_bonus} coins!" if claimed else None


@route("lb")
async def cb_leaderboard(update, context, args):
    await render(update, context, leaderboard_screen(await store.top_referrers(10)))


@route("redeem")
async def cb_redeem(update, context, args):
    set_await(context, "redeem")
    await render(update, context, redeem_screen())


@route("support")
async def cb_support(update, context, args):
    await render(update, context, support_screen())


# ----------------------------------------------------------------------- admin routes --
@route("a", admin=True)
async def cb_admin(update, context, args):
    clear_await(context)
    await render(update, context, admin_screen(await store.stats(), await maintenance_on()))


@route("a:stats", admin=True)
async def cb_admin_stats(update, context, args):
    await render(update, context, admin_stats_screen(await store.stats()))


@route("a:sync", admin=True)
async def cb_admin_sync(update, context, args):
    res = await sync_orders(context.bot)
    await store.expire_deposits()
    await render(update, context, admin_screen(await store.stats(), await maintenance_on()))
    return (f"Checked {res['checked']} · ✅{res['completed']} · 🌗{res['partial']} · ↩️{res['refunded']}"
            + (f" · ⚠️{res['errors']} errors" if res["errors"] else "")), True


@route("a:q", admin=True)
async def cb_admin_queue(update, context, args):
    status = args[0] if args and args[0] in ("pending", "processing") else "pending"
    statuses = ["pending", "placing"] if status == "pending" else ["processing"]
    await render(update, context, admin_queue_screen(status, await store.orders_by_status(statuses, 20)))


@route("a:o", admin=True)
async def cb_admin_order(update, context, args):
    order = await store.get_order(args[0] if args else "")
    if not order:
        return "Order not found.", True
    await render(update, context, Screen(admin_order_text(order, await store.get_user(order["user_id"])), admin_order_actions(order)))


def _is_panel(update: Update, context) -> bool:
    msg = update.callback_query.message
    return bool(msg) and msg.message_id == context.user_data.get(PANEL)


async def _finish_admin_message(update, context, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Update the message the admin tapped: panel -> re-render, notification -> edit in place."""
    msg = update.callback_query.message
    if _is_panel(update, context):
        await render(update, context, Screen(text, markup))
        return
    try:
        if msg.photo:
            await msg.edit_caption(caption=text[:1024], reply_markup=markup)
        else:
            await msg.edit_text(text, reply_markup=markup)
    except TelegramError as exc:
        if not _not_modified(exc):
            log.info("Could not update admin message: %s", exc)


@route("a:oa", admin=True)
async def cb_admin_order_action(update, context, args):
    if len(args) < 2:
        return None
    action, oid = args[0], args[1]
    order = await store.get_order(oid)
    if not order:
        return "Order not found.", True
    bot, note, toast = context.bot, None, None
    if action == "done":
        updated = await store.transition_order(oid, ["pending", "processing"], "completed", completed_at=utcnow(),
                                               completed_by=update.effective_user.id)
        if not updated:
            return f"Order is already {order['status']}.", True
        await notify(bot, order["user_id"], f"{e('DONE')} <b>{fancy('Order Completed')}</b>\n\n<code>#{oid}</code> has been fulfilled. Thank you!",
                     _order_markup(oid))
        order, toast = updated, "✅ Marked complete"
    elif action == "ref":
        updated = await refund_order(bot, order, "refunded", order["coins"], "admin_refund", ["pending", "processing"],
                                     completed_at=utcnow(), completed_by=update.effective_user.id)
        if not updated:
            return f"Order is already {order['status']}.", True
        await notify(bot, order["user_id"], f"{e('REFUND')} <b>{fancy('Order Refunded')}</b>\n\n<code>#{oid}</code> was cancelled. "
                                            f"<b>{order['coins']}</b> coins returned to your wallet.", _order_markup(oid))
        note = "If it was already placed on the provider, cancel it there too." if order["status"] == "processing" else None
        order, toast = updated, "↩️ Refunded"
    elif action == "retry":
        placed, error = await auto_place(order)
        order = await store.get_order(oid) or order
        if placed:
            await notify(bot, order["user_id"], f"{e('ROCKET')} <b>{fancy('Order Processing')}</b>\n\n<code>#{oid}</code> is now live.",
                         _order_markup(oid))
        note, toast = ("Placed on provider." if placed else f"Failed: {error}"), ("🚀 Placed" if placed else "❌ Failed")
    elif action == "sync":
        await sync_orders(bot, only=[order])
        order = await store.get_order(oid) or order
        toast = f"Provider status: {order.get('provider_status') or order['status']}"
    elif action == "ap":
        if order["status"] != "pending":
            return f"Order is already {order['status']}.", True
        set_await(context, "order_approve", oid=oid, admin=True)
        await render(update, context, prompt_screen(
            "CHECK", f"Approve #{oid}", admin_order_text(order, await store.get_user(order["user_id"]))
            + f"\n\n{e('DOWN')} Send the <b>Order ID</b> from your panel (used for refills), or tap Skip.",
            cancel=f"a:o:{oid}", rows=[[btn("Approve without ID", f"a:oa:aps:{oid}", icon="NEXT")]]))
        return None
    elif action == "aps":
        clear_await(context)
        return await approve_manual(update, context, oid, None)
    elif action == "rfd":
        await store.update_order(oid, refill_status="done", refill_done_at=utcnow())
        await notify(bot, order["user_id"], f"{e('RETRY')} <b>{fancy('Refill Completed')}</b>\n\nRefill for <code>#{oid}</code> has been processed.",
                     _order_markup(oid))
        order, toast = await store.get_order(oid) or order, "♻️ Refill marked done"
    text = admin_order_text(order, await store.get_user(order["user_id"]), note)
    await _finish_admin_message(update, context, text, admin_order_actions(order, back=_is_panel(update, context)))
    return toast


async def approve_manual(update, context, oid: str, external_id: str | None) -> Result:
    order = await store.transition_order(oid, ["pending"], "processing", external_id=external_id,
                                         approved_by=update.effective_user.id, approved_at=utcnow())
    if not order:
        current = await store.get_order(oid)
        return f"Order is already {current['status'] if current else 'gone'}.", True
    await notify(context.bot, order["user_id"],
                 f"{e('CHECK')} <b>{fancy('Order Approved')}</b>\n\n<code>#{oid}</code> · {esc(order_svc(order)['label'])} · "
                 f"<b>{order['quantity']:,}</b>" + (f"\n{e('ID')} Order ID: <code>{esc(external_id)}</code>" if external_id else "")
                 + "\nDelivery is in progress.", _order_markup(oid))
    await render(update, context, Screen(admin_order_text(order, await store.get_user(order["user_id"]), "Approved ✅"),
                                         admin_order_actions(order)))
    return "✅ Approved"


@on_input("order_approve")
async def in_order_approve(update, context, state, text):
    ext = re.sub(r"\s", "", text)[:40]
    if not re.fullmatch(r"[A-Za-z0-9_\-#]{1,40}", ext):
        await render(update, context, prompt_screen("CHECK", "Approve Order", "Send the Order ID (letters/numbers).", "Invalid Order ID.",
                                                    f"a:o:{state['oid']}", [[btn("Approve without ID", f"a:oa:aps:{state['oid']}", icon="NEXT")]]))
        return
    clear_await(context)
    await approve_manual(update, context, state["oid"], ext.lstrip("#"))


@route("a:deps", admin=True)
async def cb_admin_deposits(update, context, args):
    await store.expire_deposits()
    await render(update, context, admin_deposits_screen(await store.deposits_by_status(["review", "awaiting_proof"], 20)))


@route("a:d", admin=True)
async def cb_admin_deposit(update, context, args):
    dep = await store.get_deposit(args[0] if args else "")
    if not dep:
        return "Deposit not found.", True
    markup = deposit_admin_actions(dep["deposit_id"], bool(dep.get("proof_file_id")), back=True) \
        if dep["status"] in ("review", "awaiting_proof") else kb([btn("Deposits", "a:deps", icon="BACK", style="danger")])
    await render(update, context, Screen(deposit_admin_text(dep, await store.get_user(dep["user_id"])), markup))


@route("a:dp", admin=True)
async def cb_admin_deposit_photo(update, context, args):
    dep = await store.get_deposit(args[0] if args else "")
    if not dep or not dep.get("proof_file_id"):
        return "No screenshot attached.", True
    await context.bot.send_photo(update.effective_chat.id, dep["proof_file_id"],
                                 caption=deposit_admin_text(dep, await store.get_user(dep["user_id"]))[:1024],
                                 reply_markup=deposit_admin_actions(dep["deposit_id"]) if dep["status"] == "review" else None)


@route("a:da", admin=True)
async def cb_admin_deposit_action(update, context, args):
    if len(args) < 2:
        return None
    approve, did = args[0] == "ok", args[1]
    dep = await store.transition_deposit(did, ["review", "awaiting_proof"], "approved" if approve else "rejected",
                                         decided_by=update.effective_user.id, decided_at=utcnow())
    if not dep:
        current = await store.get_deposit(did)
        return (f"Already {current['status']}." if current else "Deposit not found."), True
    if approve:
        user = await store.change_coins(dep["user_id"], int(dep["coins"]), "deposit", did)
        await notify(context.bot, dep["user_id"], f"{e('CHECK')} <b>{fancy('Deposit Approved')}</b>\n\n<b>+{dep['coins']:,} coins</b> added. "
                                                  f"New balance: <b>{fmt_num((user or {}).get('coins', 0))}</b>",
                     kb([btn("Start Order", "home", icon="ROCKET", style="success")]))
    else:
        await notify(context.bot, dep["user_id"], f"{e('CROSS')} <b>{fancy('Deposit Rejected')}</b>\n\nDeposit <code>#{did}</code> was rejected. "
                                                  "Contact support if you believe this is a mistake.",
                     kb([btn("Support", url=C.support_url, icon="SUPPORT")]))
    stamp = f"\n\n{e('DONE')} <b>{'Approved' if approve else 'Rejected'}</b> by <code>{update.effective_user.id}</code>"
    base = deposit_admin_text(dep, await store.get_user(dep["user_id"]))
    back = kb([btn("Deposits", "a:deps", icon="BACK", style="danger")]) if _is_panel(update, context) else None
    await _finish_admin_message(update, context, base + stamp, back)
    return "✅ Approved" if approve else "❌ Rejected"


@route("a:sv", admin=True)
async def cb_admin_services(update, context, args):
    clear_await(context)
    await render(update, context, admin_services_screen())


@route("a:g", admin=True)
async def cb_admin_group(update, context, args):
    clear_await(context)
    gid = args[0] if args else ""
    if gid not in GROUPS:
        await render(update, context, admin_services_screen("Service not found."))
        return None
    await render(update, context, admin_group_screen(gid))


@route("a:s", admin=True)
async def cb_admin_sub(update, context, args):
    clear_await(context)
    kind = args[0] if args else ""
    if kind not in SERVICES:
        await render(update, context, admin_services_screen("Sub-service not found."))
        return None
    await render(update, context, admin_sub_screen(kind))


@route("a:sf", admin=True)
async def cb_admin_service_field(update, context, args):
    if len(args) < 2 or args[0] not in EDITABLE_FIELDS or args[1] not in SERVICES:
        return None
    set_await(context, "svc_field", field=args[0], service=args[1], admin=True, secret=EDITABLE_FIELDS[args[0]][2] == "secret")
    await render(update, context, admin_field_prompt(args[1], args[0]))


@route("a:sx", admin=True)
async def cb_admin_service_toggle(update, context, args):
    kind = args[0] if args else ""
    if kind not in SERVICES:
        return None
    SERVICES[kind]["enabled"] = not SERVICES[kind].get("enabled", True)
    await save_services()
    await render(update, context, admin_sub_screen(kind))
    return "🟢 Turned on" if SERVICES[kind]["enabled"] else "🔴 Turned off"


@route("a:sb", admin=True)
async def cb_admin_service_balance(update, context, args):
    kind = args[0] if args else ""
    if kind not in SERVICES:
        return None
    sp = SERVICES[kind]
    if not (sp.get("api_url") and sp.get("api_key")):
        return "Set the API URL and key first.", True
    try:
        return f"💰 API balance: {await provider.balance(api_cfg(sp))}", True
    except ProviderError as exc:
        return f"❌ {exc}", True


@route("a:find", admin=True)
async def cb_admin_find(update, context, args):
    set_await(context, "find_user", admin=True)
    await render(update, context, admin_find_prompt())


async def show_admin_user(update, context, uid: int, note: str | None = None) -> bool:
    user = await store.get_user(uid)
    if not user:
        return False
    orders, _ = await store.user_orders(uid, 0, 5)
    await render(update, context, admin_user_screen(user, orders, note))
    return True


@route("a:u", admin=True)
async def cb_admin_user(update, context, args):
    clear_await(context)
    if not args or not args[0].isdigit() or not await show_admin_user(update, context, int(args[0])):
        return "User not found.", True


@route("a:uc", admin=True)
async def cb_admin_user_coins(update, context, args):
    if len(args) < 2 or args[0] not in ("add", "rem") or not args[1].isdigit():
        return None
    set_await(context, "user_coins", mode=args[0], target=int(args[1]), admin=True)
    await render(update, context, admin_coins_prompt(int(args[1]), args[0]))


@route("a:ub", admin=True)
async def cb_admin_user_ban(update, context, args):
    if len(args) < 2 or not args[1].isdigit():
        return None
    uid, ban = int(args[1]), args[0] == "ban"
    if ban and is_admin(uid):
        return "You can't ban an admin.", True
    await store.set_banned(uid, ban)
    await show_admin_user(update, context, uid, "User banned." if ban else "User unbanned.")
    return "🚫 Banned" if ban else "✅ Unbanned"


@route("a:rc", admin=True)
async def cb_admin_codes(update, context, args):
    note = None
    if args and args[0] == "new":
        context.user_data["code_draft"] = {}
        set_await(context, "code_wizard", step="code", admin=True)
        await render(update, context, admin_code_prompt("code", {}))
        return None
    if args and args[0] == "off" and len(args) > 1:
        note = f"Code {args[1]} disabled." if await store.disable_code(args[1]) else "Code not found."
    clear_await(context)
    await render(update, context, admin_codes_screen(await store.list_codes(), note))


@route("a:bc", admin=True)
async def cb_admin_broadcast(update, context, args):
    action = args[0] if args else ""
    if action == "no":
        context.user_data.pop("broadcast", None)
        await cb_admin(update, context, [])
        return "Broadcast cancelled."
    if action == "go":
        src = context.user_data.pop("broadcast", None)
        if not src:
            return "Nothing to send.", True
        if BG["broadcast"]:
            return "A broadcast is already running.", True
        total = await store.count_audience()
        panel_id = await render(update, context, admin_broadcast_progress(0, 0, 0, total, False))
        BG["broadcast"] = True
        context.application.create_task(run_broadcast(context.application, src, update.effective_chat.id, panel_id, total))
        return "📨 Broadcast started"
    if BG["broadcast"]:
        return "A broadcast is already running.", True
    set_await(context, "broadcast", admin=True)
    await render(update, context, admin_broadcast_prompt())


async def run_broadcast(app: Application, src: dict, admin_chat: int, panel_id: int, total: int) -> None:
    bot, sent, failed, blocked, last_edit = app.bot, 0, 0, 0, time.monotonic()
    try:
        async for uid in store.iter_user_ids():
            while True:
                try:
                    await bot.copy_message(uid, src["chat_id"], src["message_id"])
                    sent += 1
                except RetryAfter as exc:
                    ra = exc.retry_after
                    await asyncio.sleep((ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)) + 1)
                    continue
                except Forbidden:
                    blocked += 1
                    await store.mark_blocked(uid)
                except TelegramError:
                    failed += 1
                break
            await asyncio.sleep(0.045)  # ~22 msg/s, under Telegram's global limit
            if time.monotonic() - last_edit > 4:
                last_edit = time.monotonic()
                await ui_edit(bot, admin_chat, panel_id, admin_broadcast_progress(sent, failed, blocked, total, False))
    except Exception:
        log.exception("Broadcast crashed")
    finally:
        BG["broadcast"] = False
        if not await ui_edit(bot, admin_chat, panel_id, admin_broadcast_progress(sent, failed, blocked, total, True)):
            await ui_send(bot, admin_chat, admin_broadcast_progress(sent, failed, blocked, total, True))


@route("a:mt", admin=True)
async def cb_admin_maintenance(update, context, args):
    now_on = not await maintenance_on()
    await store.set_setting("maintenance", now_on)
    await render(update, context, admin_screen(await store.stats(), now_on))
    return "🛠 Maintenance ON" if now_on else "✅ Maintenance OFF"



# ------------------------------------------------------------ admin: services ------
@route("a:sm", admin=True)
async def cb_admin_service_mode(update, context, args):
    kind = args[0] if args else ""
    if kind not in SERVICES:
        return None
    sp = SERVICES[kind]
    sp["mode"] = "manual" if sp.get("mode") == "auto" else "auto"
    await save_services()
    await render(update, context, admin_sub_screen(kind))
    return "✋ Manual" if sp["mode"] == "manual" else "⚡ Automatic"


@route("a:srf", "a:scx", admin=True)
async def cb_admin_service_flag(update, context, args):
    kind = args[0] if args else ""
    if kind not in SERVICES:
        return None
    flag = "refill" if update.callback_query.data.startswith("a:srf") else "cancel"
    SERVICES[kind][flag] = not SERVICES[kind].get(flag)
    await save_services()
    await render(update, context, admin_sub_screen(kind))
    return f"{flag.title()} {'ON' if SERVICES[kind][flag] else 'OFF'}"


_api_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


async def api_services(cfg_: dict) -> list[dict]:
    key = (cfg_["api_url"], cfg_["api_key"])
    hit = _api_cache.get(key)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    rows = await provider.services(cfg_)
    _api_cache[key] = (time.monotonic(), rows)
    return rows


async def lookup_api_service(cfg_: dict, sid: str) -> dict | None | bool:
    """dict = found · False = the panel has no such id · None = couldn't check."""
    if not cfg_.get("api_url") or not cfg_.get("api_key"):
        return None
    try:
        rows = await api_services(cfg_)
    except ProviderError:
        return None
    except Exception:  # never let a lookup break the admin flow
        log.exception("Service lookup failed")
        return None
    if not rows:
        return None
    return next((r for r in rows if str(r.get("service")) == str(sid)), False)


def apply_api_info(sp: dict, info: dict) -> None:
    sp["type"] = str(info.get("type") or "Default")[:40]
    sp["refill"] = truthy(info.get("refill"))
    sp["cancel"] = truthy(info.get("cancel"))


@route("a:sr", admin=True)
async def cb_admin_service_sync(update, context, args):
    kind = args[0] if args else ""
    if kind not in SERVICES:
        return None
    sp = SERVICES[kind]
    info = await lookup_api_service(api_cfg(sp), sp.get("service_id", ""))
    if info is None:
        return "❌ Couldn't reach the API — check URL and key.", True
    if info is False:
        return f"❌ Service ID {sp.get('service_id')} not found on this API.", True
    apply_api_info(sp, info)
    await save_services()
    note = (f"Synced: {str(info.get('name', ''))[:60]} · cost {info.get('rate')}/1K · range {info.get('min')}–{info.get('max')} · "
            f"type {sp['type']} · refill {'on' if sp['refill'] else 'off'} · cancel {'on' if sp['cancel'] else 'off'}")
    await render(update, context, admin_sub_screen(kind, note))
    return "🔄 Synced"


@route("a:sdel", admin=True)
async def cb_admin_service_delete(update, context, args):
    kind = args[0] if args else ""
    if kind not in SERVICES:
        return "Not found.", True
    gid = SERVICES[kind]["group"]
    if len(args) > 1 and args[1] == "y":
        name = SERVICES.pop(kind)["name"]
        await save_services()
        await render(update, context, admin_group_screen(gid, f"Deleted {name}. Existing orders are kept.") if gid in GROUPS
                     else admin_services_screen(f"Deleted {name}."))
        return "🗑 Deleted"
    await render(update, context, confirm_screen("Delete Sub-service?", f"<b>{esc(SERVICES[kind]['label'])}</b> will be removed. "
                                                 "Existing orders stay safe.", f"a:sdel:{kind}:y", f"a:s:{kind}"))


# ---------------- services (groups)
@route("a:gadd", admin=True)
async def cb_admin_group_add(update, context, args):
    context.user_data["grp_draft"] = {}
    set_await(context, "grp_wizard", step="name", admin=True)
    await render(update, context, prompt_screen("PLUS", "Add Service", GROUP_WIZ["name"], cancel="a:sv"))


async def finish_group_wizard(update, context, draft: dict) -> None:
    gid = "g" + new_id()[:6].lower()
    GROUPS[gid] = {**GROUP_DEFAULTS, "name": draft["name"], "description": draft.get("description", ""),
                   "emoji_key": guess_emoji(draft["name"]), "style": STYLES[len(GROUPS) % len(STYLES)]}
    await save_services()
    context.user_data.pop("grp_draft", None)
    await start_sub_wizard(update, context, gid, note=f"Service “{draft['name']}” created. Now add its first sub-service.")


@on_input("grp_wizard")
async def in_group_wizard(update, context, state, text):
    draft = context.user_data.setdefault("grp_draft", {})
    value = text.strip()
    if state.get("step") == "name":
        name = re.sub(r"\s+", " ", value)[:30]
        if len(name) < 2:
            return await render(update, context, prompt_screen("PLUS", "Add Service", GROUP_WIZ["name"], "Name is too short.", "a:sv"))
        if any(g["name"].lower() == name.lower() for g in GROUPS.values()):
            return await render(update, context, prompt_screen("PLUS", "Add Service", GROUP_WIZ["name"], "A service with this name exists.", "a:sv"))
        draft["name"] = name
        set_await(context, "grp_wizard", step="desc", admin=True)
        return await render(update, context, prompt_screen("PLUS", "Add Service", f"• Name: <b>{esc(name)}</b>\n\n" + GROUP_WIZ["desc"],
                                                           cancel="a:sv", rows=[[btn("Skip", "a:gskip", icon="NEXT")]]))
    draft["description"] = "" if value == "-" else value[:300]
    await finish_group_wizard(update, context, draft)


@route("a:gskip", admin=True)
async def cb_admin_group_skip(update, context, args):
    draft = context.user_data.get("grp_draft")
    if not draft or "name" not in draft:
        await render(update, context, admin_services_screen("Wizard expired — start again."))
        return None
    await finish_group_wizard(update, context, draft)


@route("a:gf", admin=True)
async def cb_admin_group_field(update, context, args):
    if len(args) < 2 or args[0] not in GROUP_FIELDS or args[1] not in GROUPS:
        return None
    set_await(context, "grp_field", field=args[0], gid=args[1], admin=True)
    await render(update, context, admin_group_field_prompt(args[1], args[0]))


@on_input("grp_field")
async def in_group_field(update, context, state, text):
    gid, field = state["gid"], state["field"]
    if gid not in GROUPS:
        clear_await(context)
        return await render(update, context, admin_services_screen())
    storage, label, ftype = GROUP_FIELDS[field]
    value = text.strip()
    if ftype == "text":
        value = "" if value == "-" else value[:300]
    else:
        value = re.sub(r"\s+", " ", value)[:30]
        if len(value) < 2:
            return await render(update, context, admin_group_field_prompt(gid, field, "Too short."))
        if any(g["name"].lower() == value.lower() for k, g in GROUPS.items() if k != gid):
            return await render(update, context, admin_group_field_prompt(gid, field, "Another service already uses this name."))
    GROUPS[gid][storage] = value
    await save_services()
    clear_await(context)
    await render(update, context, admin_group_screen(gid, f"{label} updated."))


@route("a:gx", admin=True)
async def cb_admin_group_toggle(update, context, args):
    gid = args[0] if args else ""
    if gid not in GROUPS:
        return None
    GROUPS[gid]["enabled"] = not GROUPS[gid].get("enabled", True)
    await save_services()
    await render(update, context, admin_group_screen(gid))
    return "🟢 Visible" if GROUPS[gid]["enabled"] else "🔴 Hidden"


@route("a:gdel", admin=True)
async def cb_admin_group_delete(update, context, args):
    gid = args[0] if args else ""
    if gid not in GROUPS:
        return "Not found.", True
    subs = subs_of(gid, enabled_only=False)
    if len(args) > 1 and args[1] == "y":
        name = GROUPS.pop(gid)["name"]
        for k in subs:
            SERVICES.pop(k, None)
        await save_services()
        await render(update, context, admin_services_screen(f"Deleted {name} and {len(subs)} sub-service(s). Existing orders are kept."))
        return "🗑 Deleted"
    await render(update, context, confirm_screen("Delete Service?", f"<b>{esc(GROUPS[gid]['name'])}</b> and its <b>{len(subs)}</b> "
                                                 "sub-service(s) will be removed. Existing orders stay safe.", f"a:gdel:{gid}:y", f"a:g:{gid}"))


# ---------------- sub-service wizard
async def start_sub_wizard(update, context, gid: str, note: str | None = None) -> None:
    context.user_data["sub_draft"] = {"_group": gid}
    set_await(context, "sub_wizard", step="type", admin=True)
    await render(update, context, sub_wizard_screen("type", context.user_data["sub_draft"], None))
    if note:
        pass  # the service header already shows the parent service


@route("a:subadd", admin=True)
async def cb_admin_sub_add(update, context, args):
    gid = args[0] if args else ""
    if gid not in GROUPS:
        return "Service not found.", True
    await start_sub_wizard(update, context, gid)


async def sub_step(update, context, draft: dict, step: str, error: str | None = None) -> None:
    set_await(context, "sub_wizard", step=step, admin=True, secret=step == "key")
    await render(update, context, sub_wizard_screen(step, draft, error))


async def sub_after_sid(update, context, draft: dict, sid: str) -> None:
    info = await lookup_api_service({"api_url": draft["API URL"], "api_key": draft["_key"]}, sid)
    if info is False:
        return await sub_step(update, context, draft, "sid", f"Service ID {sid} doesn't exist on this API.")
    draft["Service ID"] = sid
    if info:
        draft["_info"] = info
    await sub_step(update, context, draft, "rate")


async def finish_sub_wizard(update, context, draft: dict) -> None:
    gid = draft["_group"]
    if gid not in GROUPS:
        clear_await(context)
        return await render(update, context, admin_services_screen("Parent service was deleted."))
    auto = draft["Type"] == "Automatic"
    lo, hi = int(draft["Min"]), int(draft["Max"])
    kind = "s" + new_id()[:7].lower()
    sp = {**SUB_DEFAULTS, "group": gid, "name": draft["Name"], "description": draft.get("Description", ""),
          "mode": "auto" if auto else "manual", "rate_per_1k": draft["Price /1K"], "min": lo, "max": hi,
          "step": 100 if lo % 100 == 0 and hi % 100 == 0 and lo >= 100 else 1, "enabled": True}
    if auto:
        sp.update(api_url=draft["API URL"], api_key=draft["_key"], service_id=draft["Service ID"])
        if draft.get("_info"):
            apply_api_info(sp, draft["_info"])
    SERVICES[kind] = sp
    await save_services()
    clear_await(context)
    context.user_data.pop("sub_draft", None)
    await render(update, context, admin_sub_screen(kind, "Sub-service created ✅ It's live in the user menu."))


@route("a:sw", admin=True)
async def cb_admin_sub_wizard(update, context, args):
    draft = context.user_data.get("sub_draft")
    if not draft or not args:
        await render(update, context, admin_services_screen("Wizard expired — start again."))
        return None
    act = args[0]
    if act == "t" and len(args) > 1:
        draft["Type"] = "Automatic" if args[1] == "a" else "Manual"
        return await sub_step(update, context, draft, "name")
    if act == "skip":
        draft["Description"] = ""
        return await sub_step(update, context, draft, "url" if draft.get("Type") == "Automatic" else "rate")
    if act == "u" and len(args) > 1 and args[1].isdigit():
        urls = known_api_urls()
        if int(args[1]) < len(urls):
            draft["API URL"] = urls[int(args[1])]
            return await sub_step(update, context, draft, "key")
    if act == "k":
        key = saved_key_for(draft.get("API URL", ""))
        if key:
            draft["_key"], draft["API key"] = key, key
            return await sub_step(update, context, draft, "sid")
    if act == "ok":
        state = context.user_data.get(AWAIT) or {}
        step = state.get("step") or ("max" if "Min" in draft else "min")
        if step in ("min", "max") and draft.get("_s" + step):
            return await sub_wizard_value(update, context, draft, step, draft["_s" + step])
    return None


async def sub_wizard_value(update, context, draft: dict, step: str, value: int) -> None:
    if step == "min":
        draft["Min"] = value
        draft["_smax"] = max(value, draft.get("_smax") or 0) or None
        return await sub_step(update, context, draft, "max")
    if value < draft["Min"]:
        return await sub_step(update, context, draft, "max", "Max must be greater than or equal to min.")
    draft["Max"] = value
    await finish_sub_wizard(update, context, draft)


@on_input("sub_wizard")
async def in_sub_wizard(update, context, state, text):
    draft = context.user_data.get("sub_draft")
    if not draft:
        clear_await(context)
        return await render(update, context, admin_services_screen("Wizard expired — start again."))
    step, value = state.get("step", "type"), text.strip()
    if step == "type":
        return await sub_step(update, context, draft, "type", "Tap Automatic or Manual below.")
    if step == "name":
        name = re.sub(r"\s+", " ", value)[:40]
        if len(name) < 1:
            return await sub_step(update, context, draft, "name", "Send a name.")
        draft["Name"] = name
        return await sub_step(update, context, draft, "desc")
    if step == "desc":
        draft["Description"] = "" if value == "-" else value[:300]
        return await sub_step(update, context, draft, "url" if draft.get("Type") == "Automatic" else "rate")
    if step == "url":
        if not re.match(r"^https?://\S+$", value):
            return await sub_step(update, context, draft, "url", "Send a full URL starting with https://")
        draft["API URL"] = value.rstrip("/") if not value.endswith("/v2/") else value
        return await sub_step(update, context, draft, "key")
    if step == "key":
        if len(value) < 6 or " " in value:
            return await sub_step(update, context, draft, "key", "That doesn't look like an API key.")
        draft["_key"], draft["API key"] = value, value
        return await sub_step(update, context, draft, "sid")
    if step == "sid":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", value):
            return await sub_step(update, context, draft, "sid", "Send a valid service ID.")
        return await sub_after_sid(update, context, draft, value)
    if step == "rate":
        try:
            rate = float(value.replace(",", ""))
            if rate <= 0 or not math.isfinite(rate):
                raise ValueError
        except ValueError:
            return await sub_step(update, context, draft, "rate", "Send a positive number.")
        draft["Price /1K"] = int(rate) if rate.is_integer() else round(rate, 4)
        info = draft.get("_info") or {}
        draft["_smin"], draft["_smax"] = _to_int(info.get("min")), _to_int(info.get("max"))
        return await sub_step(update, context, draft, "min")
    if step in ("min", "max"):
        try:
            number = parse_quantity(value)
        except (ValueError, OverflowError):
            return await sub_step(update, context, draft, step, "Send a whole number (e.g. 100 or 10k).")
        return await sub_wizard_value(update, context, draft, step, number)


# ---------------- find service IDs on a sub-service's API
@route("a:ps", admin=True)
async def cb_admin_api_search(update, context, args):
    kind = args[0] if args else ""
    if kind not in SERVICES:
        return None
    if not (SERVICES[kind].get("api_url") and SERVICES[kind].get("api_key")):
        return "Set the API URL and key first.", True
    set_await(context, "api_search", service=kind, admin=True)
    await render(update, context, prompt_screen("SEARCH", "Find Service IDs",
                                                f"API: <code>{esc(SERVICES[kind]['api_url'])}</code>\n\nSend a keyword "
                                                "(e.g. <code>instagram views</code>) or a service ID.", cancel=f"a:s:{kind}"))


@on_input("api_search")
async def in_api_search(update, context, state, text):
    kind = state["service"]
    if kind not in SERVICES:
        clear_await(context)
        return await render(update, context, admin_services_screen())
    try:
        rows = await api_services(api_cfg(SERVICES[kind]))
    except ProviderError as exc:
        return await render(update, context, prompt_screen("SEARCH", "Find Service IDs", "Send a keyword or ID.", str(exc), f"a:s:{kind}"))
    words = text.lower().split()
    hits = [r for r in rows if str(r.get("service")) == text.strip()
            or all(w in f"{r.get('name', '')} {r.get('category', '')} {r.get('type', '')}".lower() for w in words)][:15]
    lines = [f"<code>{esc(r.get('service'))}</code> · {esc(str(r.get('name', ''))[:60])}\n   💲{esc(r.get('rate', '?'))}/1K · "
             f"{esc(r.get('min', '?'))}–{esc(r.get('max', '?'))} · {esc(r.get('type', 'Default'))}"
             + (" · ♻️" if truthy(r.get("refill")) else "") + (" · ✖️" if truthy(r.get("cancel")) else "") for r in hits]
    body = (f"Results for <b>{esc(text[:40])}</b> ({len(hits)} shown of {len(rows)}):\n\n" + "\n".join(lines)
            if hits else f"No match for <b>{esc(text[:40])}</b> in {len(rows)} services.") + "\n\nSend another keyword to search again."
    await render(update, context, prompt_screen("SEARCH", "Find Service IDs", body[:3800], cancel=f"a:s:{kind}"))


# ------------------------------------------------------------ admin: channels ------
def channel_add_prompt(error: str | None = None) -> Screen:
    body = ("1️⃣ Add this bot as an <b>admin</b> in your channel/group.\n"
            "2️⃣ Then send its <code>@username</code>, <code>t.me/username</code> link or numeric ID "
            "(<code>-100…</code>), <b>or forward any post</b> from it here.")
    return prompt_screen("JOIN", "Add Channel", body, error, "a:ch")


def forwarded_chat(msg):
    origin = getattr(msg, "forward_origin", None)
    chat = getattr(origin, "chat", None)
    return chat if chat is not None and getattr(chat, "type", "") in ("channel", "supergroup", "group") else None


def parse_chat_ref(text: str) -> str | int | None:
    t = text.strip()
    if re.fullmatch(r"-?\d{5,}", t):
        return int(t)
    m = re.search(r"(?:t\.me/|telegram\.me/|@)([A-Za-z][A-Za-z0-9_]{3,})", t)
    if m and m.group(1).lower() not in ("joinchat",):
        return "@" + m.group(1)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", t):
        return "@" + t
    return None


async def add_channel(update, context, ref) -> None:
    bot = context.bot
    try:
        chat = await bot.get_chat(ref)
    except TelegramError:
        return await render(update, context, channel_add_prompt("Channel not found. Add the bot as admin first, then try again."))
    try:
        me = await bot.get_chat_member(chat.id, bot.id)
        is_bot_admin = me.status in ("administrator", "creator")
    except TelegramError:
        is_bot_admin = False
    if not is_bot_admin:
        return await render(update, context, channel_add_prompt(f"The bot isn't an admin in {chat.title or ref}. Make it admin first."))
    if any(c["id"] == chat.id for c in CHANNELS):
        return await render(update, context, channel_add_prompt("This channel is already added."))
    url = f"https://t.me/{chat.username}" if getattr(chat, "username", None) else getattr(chat, "invite_link", None)
    if not url:
        try:
            url = (await bot.create_chat_invite_link(chat.id, name="Force join")).invite_link
        except TelegramError:
            return await render(update, context, channel_add_prompt("Couldn't create an invite link — give the bot the 'Invite users' right."))
    CHANNELS.append({"id": chat.id, "title": chat.title or chat.username or str(chat.id), "url": url})
    await save_channels()
    _join_cache.clear()
    clear_await(context)
    await render(update, context, admin_channels_screen(f"Added {CHANNELS[-1]['title']} ✅ Force join is ON."))


@route("a:ch", admin=True)
async def cb_admin_channels(update, context, args):
    if args and args[0] == "add":
        set_await(context, "channel_add", admin=True)
        await render(update, context, channel_add_prompt())
        return None
    clear_await(context)
    note = None
    if args and args[0] == "del" and len(args) > 1 and args[1].isdigit() and int(args[1]) < len(CHANNELS):
        removed = CHANNELS.pop(int(args[1]))
        await save_channels()
        _join_cache.clear()
        note = f"Removed {removed['title']}." + ("" if CHANNELS else " Force join is now OFF.")
    await render(update, context, admin_channels_screen(note))


@on_input("channel_add")
async def in_channel_add(update, context, state, text):
    ref = parse_chat_ref(text)
    if ref is None:
        return await render(update, context, channel_add_prompt("Send @username, a t.me link, a -100… ID, or forward a post. "
                                                               "(Private t.me/+ links can't be read — forward a post instead.)"))
    await add_channel(update, context, ref)


# ------------------------------------------------------------ admin: admins --------
@route("a:ad", admin=True)
async def cb_admin_admins(update, context, args):
    uid = update.effective_user.id
    owner = is_owner(uid)
    if args and args[0] in ("add", "del") and not owner:
        return "Only the owner can manage admins.", True
    if args and args[0] == "add":
        set_await(context, "admin_add", admin=True)
        await render(update, context, prompt_screen("ADMIN", "Add Admin", "Send the new admin's numeric <b>User ID</b> or <b>@username</b> "
                                                    "(username works only if they've started the bot).", cancel="a:ad"))
        return None
    clear_await(context)
    note = None
    if args and args[0] == "del" and len(args) > 1 and args[1].isdigit():
        target = int(args[1])
        if target in EXTRA_ADMINS:
            EXTRA_ADMINS.discard(target)
            await save_admins()
            note = f"Removed admin {target}."
            try:
                await context.bot.delete_my_commands(scope=BotCommandScopeChat(target))
            except TelegramError:
                pass
    await render(update, context, admin_admins_screen(owner, note))


@on_input("admin_add")
async def in_admin_add(update, context, state, text):
    if not is_owner(update.effective_user.id):
        clear_await(context)
        return
    raw = text.strip()
    target = int(raw) if raw.isdigit() else ((await store.find_user(raw)) or {}).get("telegram_id")
    if not target:
        return await render(update, context, prompt_screen("ADMIN", "Add Admin", "Send a User ID or @username.",
                                                           "User not found — ask them to /start the bot or send their numeric ID.", "a:ad"))
    if is_admin(target):
        return await render(update, context, prompt_screen("ADMIN", "Add Admin", "Send a User ID or @username.", "Already an admin.", "a:ad"))
    EXTRA_ADMINS.add(int(target))
    await save_admins()
    clear_await(context)
    try:
        await context.bot.set_my_commands(
            [BotCommand("start", "Open the main menu"), BotCommand("admin", "Admin panel"), BotCommand("orders", "My orders"),
             BotCommand("wallet", "Wallet & deposit"), BotCommand("help", "How it works"), BotCommand("cancel", "Cancel current action")],
            scope=BotCommandScopeChat(int(target)))
    except TelegramError:
        pass
    await notify(context.bot, int(target), f"{e('ADMIN')} <b>{fancy('You Are Now An Admin')}</b>\n\nSend /admin to open the admin panel.")
    await render(update, context, admin_admins_screen(True, f"Added admin {target} ✅"))


# ------------------------------------------------------------ admin: bot settings --
@route("a:cf", admin=True)
async def cb_admin_config(update, context, args):
    key = args[0] if args else ""
    if key in CONFIG_FIELDS:
        label, ftype = CONFIG_FIELDS[key]
        set_await(context, "cfg_field", key=key, admin=True)
        hint = {"opt": "\nSend <code>-</code> to clear it.", "text": "\nMultiple lines are OK.", "int0": "\nSend 0 to turn it off."}.get(ftype, "")
        await render(update, context, prompt_screen("EDIT", f"Edit {label}", f"Current: <code>{esc(cfg(key))}</code>\n\nSend the new value.{hint}",
                                                    cancel="a:cf"))
        return None
    clear_await(context)
    await render(update, context, admin_config_screen())


@on_input("cfg_field")
async def in_config_field(update, context, state, text):
    key = state["key"]
    label, ftype = CONFIG_FIELDS[key]
    value: Any = text.strip()
    error = None
    if ftype in ("int", "int0"):
        try:
            value = int(value.replace(",", ""))
            if value < (1 if ftype == "int" else 0) or value > 1_000_000:
                raise ValueError
        except ValueError:
            error = "Send a valid whole number."
    elif ftype == "url" and not re.match(r"^(https?://|tg://)\S+$", value):
        error = "Send a full link (https://t.me/…)."
    elif ftype == "opt":
        value = "" if value == "-" else value[:100]
    elif ftype == "text":
        value = value[:800]
    elif not value or len(value) > 60:
        error = "Send 1–60 characters."
    if error:
        return await render(update, context, prompt_screen("EDIT", f"Edit {label}", "Send the new value.", error, "a:cf"))
    CONFIG[key] = value
    await save_config()
    clear_await(context)
    await render(update, context, admin_config_screen(f"{label} updated."))


# ------------------------------------------------------------ admin: coin packages -
@route("a:pk", admin=True)
async def cb_admin_packages(update, context, args):
    if args and args[0] == "add":
        set_await(context, "pkg_add", admin=True)
        await render(update, context, prompt_screen("COINS", "Add Package", f"Send <b>coins</b> and <b>price</b> separated by a space.\n"
                                                    f"Example: <code>500 450</code> → 500 coins for {esc(C.currency)}450", cancel="a:pk"))
        return None
    clear_await(context)
    note = None
    pk = [list(x) for x in cfg("coin_packages")]
    if args and args[0] == "del" and len(args) > 1 and args[1].isdigit() and int(args[1]) < len(pk):
        coins, _ = pk.pop(int(args[1]))
        CONFIG["coin_packages"] = pk
        await save_config()
        note = f"Removed the {coins:,} coins package."
    await render(update, context, admin_packages_screen(note))


@on_input("pkg_add")
async def in_package_add(update, context, state, text):
    nums = re.findall(r"\d+", text.replace(",", ""))
    if len(nums) != 2 or int(nums[0]) <= 0 or int(nums[1]) <= 0:
        return await render(update, context, prompt_screen("COINS", "Add Package", "Send coins and price, e.g. <code>500 450</code>.",
                                                           "Send exactly two positive numbers.", "a:pk"))
    pk = [list(x) for x in cfg("coin_packages") if int(x[0]) != int(nums[0])]
    pk.append([int(nums[0]), int(nums[1])])
    CONFIG["coin_packages"] = sorted(pk)[:12]
    await save_config()
    clear_await(context)
    await render(update, context, admin_packages_screen(f"Added {int(nums[0]):,} coins for {C.currency}{int(nums[1]):,}."))


# ======================================================================================
# Text / media input — dispatched by the current AWAIT state
# ======================================================================================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    user, _ = await touch_user(update)
    state = context.user_data.get(AWAIT)
    secret = bool(state and state.get("secret"))
    if state and state.get("kind") == "broadcast" and is_admin(uid):
        await prepare_broadcast(update, context)
        return
    if state and state.get("kind") == "channel_add" and is_admin(uid) and forwarded_chat(update.effective_message):
        await clean_input(update)
        await add_channel(update, context, forwarded_chat(update.effective_message).id)
        return
    await clean_input(update, force=secret)
    if user.get("banned") and not is_admin(uid):
        await render(update, context, banned_screen())
        return
    missing = await missing_channels(context.bot, uid)
    if missing:
        await render(update, context, join_screen(missing))
        return
    text = (update.effective_message.text or "").strip()
    if not state:
        ok, link = validate_instagram_url(text)
        if ok:  # smart shortcut: paste a link from anywhere
            context.user_data["prefill_link"] = link
            await render(update, context, pick_service_screen(link))
        else:
            await go_home(update, context)
        return
    if state.get("admin") and not is_admin(uid):
        clear_await(context)
        return
    handler = INPUTS.get(state.get("kind", ""))
    if handler:
        await handler(update, context, state, text)
    else:
        clear_await(context)
        await go_home(update, context)


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    state = context.user_data.get(AWAIT) or {}
    if state.get("kind") == "broadcast" and is_admin(uid):
        await prepare_broadcast(update, context)
        return
    await clean_input(update)
    if state.get("kind") == "channel_add" and is_admin(uid):
        chat = forwarded_chat(update.effective_message)
        if chat:
            await add_channel(update, context, chat.id)
        else:
            await render(update, context, channel_add_prompt("Forward a post from the channel, or send its @username / ID."))
        return
    if state.get("kind") == "deposit_proof":
        msg = update.effective_message
        file_id = msg.photo[-1].file_id if msg.photo else (
            msg.document.file_id if msg.document and (msg.document.mime_type or "").startswith("image/") else None)
        if not file_id:
            dep = await store.get_deposit(state["deposit_id"])
            if dep:
                await render(update, context, deposit_pay_screen(dep, "Please send a photo/screenshot or the UTR number."))
            return
        await submit_deposit(update, context, state, file_id=file_id, utr=(msg.caption or "").strip()[:40] or None)


async def prepare_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    clear_await(context)
    context.user_data["broadcast"] = {"chat_id": msg.chat_id, "message_id": msg.message_id}
    await render(update, context, admin_broadcast_confirm(await store.count_audience()), force_new=True)


@on_input("order_link")
async def in_order_link(update, context, state, text):
    draft = context.user_data.get(ORDER)
    if not draft:
        clear_await(context)
        await render(update, context, expired_screen())
        return
    ok, link = validate_instagram_url(text)
    if not ok:
        await render(update, context, link_prompt(draft["kind"], "That isn't a valid public Instagram post/reel link."))
        return
    if draft["kind"] not in SERVICES:
        clear_await(context)
        return await render(update, context, expired_screen())
    draft["link"] = link
    await ask_amount(update, context, draft["kind"], link)


@on_input("order_comments")
async def in_order_comments(update, context, state, text):
    draft = context.user_data.get(ORDER)
    if not draft or "link" not in draft or draft["kind"] not in SERVICES:
        clear_await(context)
        return await render(update, context, expired_screen())
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if err := check_quantity(draft["kind"], len(lines), comments=True):
        return await render(update, context, comments_prompt(draft["kind"], draft["link"], f"You sent {len(lines)}. {err}"))
    draft["comments"] = "\n".join(ln[:300] for ln in lines)
    await show_review(update, context, len(lines))


@on_input("order_qty")
async def in_order_qty(update, context, state, text):
    draft = context.user_data.get(ORDER)
    if not draft or "link" not in draft or draft["kind"] not in SERVICES:
        clear_await(context)
        await render(update, context, expired_screen())
        return
    ok, link = validate_instagram_url(text)
    if ok:  # user pasted a new link instead of a number
        draft["link"] = link
        draft.pop("quantity", None)
        await render(update, context, qty_screen(draft["kind"], link))
        return
    try:
        qty = parse_quantity(text)
    except (ValueError, OverflowError):
        await render(update, context, qty_screen(draft["kind"], draft["link"], "Send a number like 1000 or 2k."))
        return
    if err := check_quantity(draft["kind"], qty):
        await render(update, context, qty_screen(draft["kind"], draft["link"], err))
        return
    await show_review(update, context, qty)


@on_input("redeem")
async def in_redeem(update, context, state, text):
    code = re.sub(r"[^A-Za-z0-9]", "", text).upper()[:32]
    if not code:
        await render(update, context, redeem_screen("Please send a valid code."))
        return
    ok, error, coins = await store.redeem(code, update.effective_user.id)
    if not ok:
        await render(update, context, redeem_screen(error))
        return
    clear_await(context)
    user = await store.get_user(update.effective_user.id) or {}
    await render(update, context, redeem_ok_screen(coins, int(user.get("coins", 0))))


@on_input("deposit_proof")
async def in_deposit_proof(update, context, state, text):
    utr = re.sub(r"\s", "", text)
    if not re.fullmatch(r"[A-Za-z0-9]{6,40}", utr):
        dep = await store.get_deposit(state["deposit_id"])
        if dep:
            await render(update, context, deposit_pay_screen(dep, "Send the payment screenshot or a valid UTR / transaction ID."))
        else:
            clear_await(context)
            await go_home(update, context)
        return
    await submit_deposit(update, context, state, utr=utr)


async def submit_deposit(update, context, state: dict, *, file_id: str | None = None, utr: str | None = None) -> None:
    did = state.get("deposit_id", "")
    dep = await store.get_deposit(did)
    if dep and dep["status"] == "awaiting_proof" and aware(dep["expires_at"]) < utcnow():
        await store.transition_deposit(did, ["awaiting_proof"], "expired")
        dep = None
    if not dep or dep["status"] != "awaiting_proof":
        clear_await(context)
        await render(update, context, Screen(title("WARN", "Deposit Expired") + "This deposit is no longer active. Please create a new one.",
                                             kb([btn("Deposit Coins", "dep", icon="DEPOSIT", style="success")], home_btn())))
        return
    dep = await store.transition_deposit(did, ["awaiting_proof"], "review", proof_file_id=file_id, utr=utr, submitted_at=utcnow())
    clear_await(context)
    if not dep:
        await go_home(update, context)
        return
    await render(update, context, deposit_sent_screen(dep))
    user = await store.get_user(dep["user_id"])
    await notify_admins(context.bot, deposit_admin_text(dep, user), deposit_admin_actions(did), photo=file_id)


@on_input("svc_field")
async def in_service_field(update, context, state, text):
    kind, field = state["service"], state["field"]
    if kind not in SERVICES:
        clear_await(context)
        return await render(update, context, admin_services_screen())
    storage, label, ftype = EDITABLE_FIELDS[field]
    value: Any = text.strip()
    try:
        if ftype == "int":
            value = parse_quantity(value)
        elif ftype == "float":
            value = float(value.replace(",", ""))
            if value <= 0 or not math.isfinite(value):
                raise ValueError
            value = int(value) if value.is_integer() else round(value, 4)
        elif ftype == "url":
            if not re.match(r"^https?://\S+$", value):
                raise ValueError
        elif ftype == "secret":
            if len(value) < 6 or " " in value:
                raise ValueError
        elif ftype == "text":
            value = "" if value == "-" else value[:300]
        else:
            value = re.sub(r"\s+", " ", value)[:40]
            if not value:
                raise ValueError
    except (ValueError, OverflowError):
        await render(update, context, admin_field_prompt(kind, field, "Invalid value — try again."))
        return
    spec = dict(SERVICES[kind], **{storage: value})
    if spec["min"] > spec["max"]:
        await render(update, context, admin_field_prompt(kind, field, "Min can't be greater than max."))
        return
    SERVICES[kind][storage] = value
    note = f"{label} updated."
    if field in ("sid", "url", "key") and provider_ready(SERVICES[kind]):
        info = await lookup_api_service(api_cfg(SERVICES[kind]), SERVICES[kind]["service_id"])
        if info:
            apply_api_info(SERVICES[kind], info)
            note += f" Found on API: {str(info.get('name', ''))[:50]} (cost {info.get('rate')}/1K, {info.get('min')}–{info.get('max')})."
        elif info is False:
            note += " ⚠️ This service ID was not found on the API."
    await save_services()
    clear_await(context)
    await render(update, context, admin_sub_screen(kind, note))


@on_input("find_user")
async def in_find_user(update, context, state, text):
    user = await store.find_user(text)
    if not user:
        await render(update, context, admin_find_prompt("No user found — they must /start the bot first."))
        return
    clear_await(context)
    await show_admin_user(update, context, user["telegram_id"])


@on_input("user_coins")
async def in_user_coins(update, context, state, text):
    uid, mode = state["target"], state["mode"]
    try:
        amount = int(text.replace(",", ""))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await render(update, context, admin_coins_prompt(uid, mode, "Send a positive whole number."))
        return
    clear_await(context)
    if mode == "add":
        user = await store.change_coins(uid, amount, "admin_add")
    else:
        user = await store.remove_coins_clamped(uid, amount, "admin_remove")
    if not user:
        await render(update, context, admin_find_prompt("User not found."))
        return
    await notify(context.bot, uid, f"{e('MONEY')} <b>{fancy('Wallet Updated')}</b>\n\n"
                                   f"{'+' if mode == 'add' else '-'}{amount} coins by the operator.\nNew balance: <b>{fmt_num(user['coins'])}</b>")
    await show_admin_user(update, context, uid, f"{'Added' if mode == 'add' else 'Removed'} {amount} coins.")


@on_input("code_wizard")
async def in_code_wizard(update, context, state, text):
    draft = context.user_data.setdefault("code_draft", {})
    step = state.get("step", "code")
    if step == "code":
        code = secrets.token_hex(4).upper() if text.lower() == "auto" else re.sub(r"[^A-Za-z0-9]", "", text).upper()[:32]
        if len(code) < 3:
            await render(update, context, admin_code_prompt("code", draft, "Use at least 3 letters/numbers."))
            return
        if await store.get_code(code):
            await render(update, context, admin_code_prompt("code", draft, "That code already exists."))
            return
        draft["code"], nxt = code, "coins"
    else:
        try:
            number = int(text.replace(",", ""))
            if number < (1 if step == "coins" else 0):
                raise ValueError
        except ValueError:
            await render(update, context, admin_code_prompt(step, draft, "Send a valid whole number."))
            return
        draft[step] = number
        nxt = {"coins": "limit", "limit": "days", "days": None}[step]
    if nxt:
        set_await(context, "code_wizard", step=nxt, admin=True)
        await render(update, context, admin_code_prompt(nxt, draft))
        return
    clear_await(context)
    context.user_data.pop("code_draft", None)
    expiry = utcnow() + timedelta(days=draft["days"]) if draft["days"] else None
    created = await store.create_code(draft["code"], draft["coins"], draft["limit"], expiry)
    note = (f"Created {draft['code']} · {draft['coins']} coins · limit {draft['limit'] or '∞'} · {draft['days'] or '∞'} days"
            if created else "That code already exists.")
    await render(update, context, admin_codes_screen(await store.list_codes(), note))


# ======================================================================================
# Commands
# ======================================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, is_new = await touch_user(update)
    await clean_input(update)
    clear_await(context)
    context.user_data.pop(ORDER, None)
    uid = update.effective_user.id
    if user.get("banned") and not is_admin(uid):
        await render(update, context, banned_screen(), force_new=True)
        return
    if is_new and context.args:
        raw = context.args[0].strip().lower().removeprefix("ref")
        if raw.isdigit() and await store.claim_referral(uid, int(raw), C.referral_bonus):
            await notify(context.bot, int(raw), f"{e('REFERRAL')} <b>{fancy('Referral Confirmed')}</b>\n{HR}\n\n"
                                                f"{e('GEM')} <b>+{C.referral_bonus} coins</b> — "
                                                f"{esc(update.effective_user.first_name)} joined with your link!")
    missing = await missing_channels(context.bot, uid)
    if missing:
        await render(update, context, join_screen(missing), force_new=True)
        return
    await go_home(update, context, force_new=True)


def _command_screen(builder: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[Screen | None]], admin: bool = False):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, _ = await touch_user(update)
        await clean_input(update)
        if admin and not is_admin(update.effective_user.id):
            return
        if user.get("banned") and not is_admin(update.effective_user.id):
            await render(update, context, banned_screen(), force_new=True)
            return
        clear_await(context)
        screen = await builder(update, context)
        if screen:
            await render(update, context, screen, force_new=True)
    return handler


async def _scr_home(update, context):
    return home_screen(await store.get_user(update.effective_user.id), is_admin(update.effective_user.id))


async def _scr_orders(update, context):
    rows, total = await store.user_orders(update.effective_user.id, 0, PAGE)
    return orders_screen(rows, total, 0)


async def _scr_wallet(update, context):
    uid = update.effective_user.id
    return wallet_screen(await store.get_user(uid), await store.ledger_for(uid))


async def _scr_admin(update, context):
    return admin_screen(await store.stats(), await maintenance_on())


async def _scr_help(update, context):
    text = (title("HELP", "How It Works") +
            "1️⃣ Top up coins from <b>Wallet → Deposit</b> (or earn them with the daily bonus, referrals and promo codes)\n"
            "2️⃣ Choose <b>Likes</b> or <b>Views</b>, send your public post link and pick a quantity\n"
            "3️⃣ Confirm — delivery starts automatically and you can track it in <b>My Orders</b>\n\n"
            f"{e('INFO')} Shortcut: paste any Instagram link in this chat to order instantly.\n"
            f"{e('REFUND')} Undelivered parts of an order are refunded automatically.")
    return Screen(text, kb(home_btn()))


# ======================================================================================
# Errors + bootstrap
# ======================================================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error while processing an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat and update.effective_chat.type == "private" and not update.callback_query:
        try:
            await context.bot.send_message(update.effective_chat.id, f"{e('ERROR')} Something went wrong. Please try /start again.")
        except TelegramError:
            pass


async def post_init(app: Application) -> None:
    await store.ensure_indexes()
    await load_runtime()
    if settings.force_join_channel and not CHANNELS and not await store.get_setting("channels_migrated"):
        try:
            chat = await app.bot.get_chat(f"@{settings.force_join_channel}")
            CHANNELS.append({"id": chat.id, "title": chat.title or settings.force_join_channel,
                             "url": f"https://t.me/{settings.force_join_channel}"})
            await save_channels()
        except TelegramError as exc:
            log.warning("Could not migrate FORCE_JOIN_CHANNEL: %s", exc)
        await store.set_setting("channels_migrated", True)
    stuck = await store.reset_stuck_placing()
    if stuck:
        log.warning("%s order(s) were interrupted during placement and moved back to pending", stuck)
    user_cmds = [BotCommand("start", "Open the main menu"), BotCommand("orders", "My orders"),
                 BotCommand("wallet", "Wallet & deposit"), BotCommand("help", "How it works"), BotCommand("cancel", "Cancel current action")]
    try:
        await app.bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
        for admin_id in all_admin_ids():
            try:
                await app.bot.set_my_commands(user_cmds + [BotCommand("admin", "Admin panel")], scope=BotCommandScopeChat(admin_id))
            except TelegramError:
                pass  # admin hasn't started the bot yet
    except TelegramError as exc:
        log.warning("Could not set bot commands: %s", exc)
    app.bot_data["bg_task"] = asyncio.create_task(background_loop(app))
    log.info("%s ready as @%s · storage=%s · rich=%s · drafts=%s", C.bot_name, app.bot.username,
             type(store).__name__, Rich.enabled, Draft.enabled)


async def post_shutdown(app: Application) -> None:
    task = app.bot_data.get("bg_task")
    if task:
        task.cancel()
    await provider.close()
    await store.close()


def build_application() -> Application:
    persistence = PicklePersistence(
        filepath=settings.persistence_path,
        store_data=PersistenceInput(bot_data=False, chat_data=False, user_data=True, callback_data=False),
        update_interval=30,
    )
    defaults = Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True))
    app = (Application.builder().token(settings.bot_token).defaults(defaults).persistence(persistence)
           .connect_timeout(20).read_timeout(30).write_timeout(30).pool_timeout(20)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler(["menu", "cancel"], _command_screen(_scr_home), filters=private))
    app.add_handler(CommandHandler("orders", _command_screen(_scr_orders), filters=private))
    app.add_handler(CommandHandler("wallet", _command_screen(_scr_wallet), filters=private))
    app.add_handler(CommandHandler("help", _command_screen(_scr_help), filters=private))
    app.add_handler(CommandHandler("admin", _command_screen(_scr_admin, admin=True), filters=private))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(private & ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL, on_media))
    app.add_error_handler(error_handler)
    return app


def main() -> None:
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is missing — put it in your .env file (see .env.example).")
    if not settings.admin_ids:
        log.warning("OWNER_ID / ADMIN_IDS not set — nobody can open the admin panel.")
    if not settings.mongo_uri:
        log.warning("MONGO_URI not set — using IN-MEMORY storage. ALL DATA IS LOST ON RESTART.")
    try:
        build_application().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    except InvalidToken:
        raise SystemExit("Invalid BOT_TOKEN — get a fresh one from @BotFather.")


if __name__ == "__main__":
    main()
