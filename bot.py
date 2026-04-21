
import asyncio
import hashlib
import logging
import os
import re
import tempfile
import time
import unicodedata
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional

import cv2
import imagehash
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from PIL import Image

load_dotenv()

# -----------------------------------------------------
# Config
# -----------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip()
DB_NAME = os.getenv("DB_NAME", "hallow_match_bot").strip()

OWNER_IDS = {
    int(x.strip())
    for x in os.getenv("OWNER_IDS", os.getenv("OWNER_ID", "")).split(",")
    if x.strip().isdigit()
}

PHOTO_PHASH_THRESHOLD = int(os.getenv("PHOTO_PHASH_THRESHOLD", "8"))
VIDEO_FRAME_THRESHOLD = int(os.getenv("VIDEO_FRAME_THRESHOLD", "10"))
VIDEO_AVG_THRESHOLD = int(os.getenv("VIDEO_AVG_THRESHOLD", "12"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

OWNER_USERNAME = os.getenv("OWNER_USERNAME", "@Official_Bika").strip()
DEFAULT_COMMAND = os.getenv("DEFAULT_COMMAND", "/hallow").strip() or "/hallow"

SUPPORT_GROUP_USERNAME = os.getenv("SUPPORT_GROUP_USERNAME", "").strip()
SUPPORT_CHANNEL_USERNAME = os.getenv("SUPPORT_CHANNEL_USERNAME", "").strip()

SOURCE_CHANNEL_IDS = {
    int(x.strip())
    for x in os.getenv("SOURCE_CHANNEL_IDS", "").split(",")
    if x.strip() and re.fullmatch(r"-?\d+", x.strip())
}
SOURCE_CHANNEL_USERNAMES = {
    x.strip().lstrip("@").lower()
    for x in os.getenv("SOURCE_CHANNEL_USERNAMES", "").split(",")
    if x.strip()
}
SOURCE_CHANNEL_TITLES = {
    x.strip().casefold()
    for x in os.getenv("SOURCE_CHANNEL_TITLES", "").split(",")
    if x.strip()
}
FORWARD_SOURCE_COMMANDS_RAW = os.getenv(
    "FORWARD_SOURCE_COMMANDS",
    "@CaptureDatabase:/capture,@Seizer_Database:/seize,CAPTURE|UPLOADS:/capture,SEIZER DATABASE:/seize",
).strip()

SNAPSHOT_REFRESH_SECONDS = int(os.getenv("SNAPSHOT_REFRESH_SECONDS", "60"))
RESULT_CACHE_MAX_ITEMS = int(os.getenv("RESULT_CACHE_MAX_ITEMS", "3000"))
RESULT_CACHE_TTL_SECONDS = int(os.getenv("RESULT_CACHE_TTL_SECONDS", "600"))
FORCE_JOIN_CACHE_SECONDS = int(os.getenv("FORCE_JOIN_CACHE_SECONDS", "259200"))
MISS_REFRESH_COOLDOWN_SECONDS = int(os.getenv("MISS_REFRESH_COOLDOWN_SECONDS", "15"))

MODE = os.getenv("MODE", "auto").strip().lower()  # auto|polling|webhook
USE_WEBHOOK_ENV = os.getenv("USE_WEBHOOK", "").strip().lower()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook").strip() or "/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
HOST = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))

if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not MONGO_URI:
    raise RuntimeError("MONGO_URI is required")
if not OWNER_IDS:
    raise RuntimeError("OWNER_ID or OWNER_IDS is required")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("bikanamebot")

# -----------------------------------------------------
# Database
# -----------------------------------------------------
client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]
items = db.items
sudo_users = db.sudo_users
blacklisted_users = db.blacklisted_users
known_users = db.known_users
known_groups = db.known_groups
gapproved_groups = db.gapproved_groups

# -----------------------------------------------------
# Patterns / helpers
# -----------------------------------------------------
NAME_PATTERNS = [
    re.compile(r"^[^\n\r]*?Character\s*Name\s*[:：﹕꞉-]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?\bNAME\s*[:：﹕꞉-]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?\bName\s*[:：﹕꞉-]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
]
ANIME_PATTERNS = [
    re.compile(r"^[^\n\r]*?Anime\s*Name\s*[:：﹕꞉-]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?Anime\s*[:：﹕꞉-]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
]
RARITY_PATTERNS = [
    re.compile(r"^[^\n\r]*?Rarity\s*[:：﹕꞉-]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
]
CARD_ID_PATTERNS = [
    re.compile(r"^[^\n\r]*?Character\s*ID\s*[:：﹕꞉-]?\s*#?\s*([0-9]+)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?Card\s*ID\s*[:：﹕꞉-]?\s*#?\s*([0-9]+)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?ID\s*[:：﹕꞉-]?\s*#?\s*([0-9]+)\s*$", re.IGNORECASE | re.MULTILINE),
]
SOURCE_NAME_PATTERNS = [
    re.compile(r"^[^\n\r]*?\bName\b\s*[:：﹕꞉-]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?\bCharacter\s*Name\b\s*[:：﹕꞉-]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?🍀\s*Name\s*[:：﹕꞉-]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?👤\s*Name\s*[:：﹕꞉-]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
]
SOURCE_ANIME_PATTERNS = [
    re.compile(r"^[^\n\r]*?\bAnime\b\s*[:：﹕꞉-]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
]
SOURCE_RARITY_PATTERNS = [
    re.compile(r"^[^\n\r]*?\bRarity\b\s*[:：﹕꞉-]?\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
]
SOURCE_CARD_ID_PATTERNS = [
    re.compile(r"^[^\n\r]*?\bCharacter\s*ID\b\s*[:：﹕꞉-]?\s*#?\s*([0-9]+)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?\bID\b\s*[:：﹕꞉-]?\s*#?\s*([0-9]+)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?🆔\s*ID\s*[:：﹕꞉-]?\s*#?\s*([0-9]+)\s*$", re.IGNORECASE | re.MULTILINE),
]
COMMAND_PATTERNS = [
    re.compile(r"(?:using|use|hint|full).*?/\s*([A-Za-z0-9_]+)\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"/\s*([A-Za-z0-9_]+)\s*(?:\[[^\]]*name[^\]]*\]|\([^\)]*name[^\)]*\)|\bname\b)", re.IGNORECASE | re.DOTALL),
    re.compile(r"/\s*([A-Za-z0-9_]+)\b", re.IGNORECASE),
]
NAME_TRIGGER_RE = re.compile(r"^(?:\.name|/name)(?:@\w+)?$", re.IGNORECASE)
WAIFU_TRIGGER_RE = re.compile(r"^(?:\.wa|/waifu)(?:@\w+)?$", re.IGNORECASE)
CHARACTER_CATCHER_HEADER_RE = re.compile(r"OwO!\s*Check out this character!", re.IGNORECASE)
NUMBERED_NAME_RE = re.compile(r"^\s*(\d+)\s*:?[ \t]+(.+?)\s*(?:\[[^\]]*\]|\([^\)]*\))?\s*$", re.IGNORECASE | re.MULTILINE)
TRAILING_BADGE_RE = re.compile(r"\s*\[([^\[\]]+)\]\s*$")

KNOWN_INLINE_SOURCE_COMMAND_MAP: dict[str, str] = {
    "characters_hallow_bot": "/hallow",
    "character_catcher_bot": "/catch",
    "character_seizer_bot": "/seize",
    "capturecharacterbot": "/capture",
    "capture_character_bot": "/capture",
    "takers_character_bot": "/take",
    "grab_your_waifu_bot": "/grab",
}
INLINE_SOURCE_COMMAND_MAP: dict[str, str] = dict(KNOWN_INLINE_SOURCE_COMMAND_MAP)

SUPPORTED_BOTS = [
    ("hallow", "@Characters_Hallow_bot", ["/hallow"]),
    ("catcher", "@Character_Catcher_Bot", ["/catch"]),
    ("seizer", "@Character_Seizer_Bot", ["/seize", "/sezer"]),
    ("capture", "@CaptureCharacterBot", ["/capture"]),
    ("takers", "@Takers_character_bot", ["/take"]),
    ("grab", "@Grab_Your_Waifu_Bot", ["/grab"]),
]

FORCE_JOIN_VERIFY_CALLBACK = "forcejoin_verify"
FORCE_JOIN_START_PAYLOAD = "verify"
router = Router()

# -----------------------------------------------------
# Runtime models / caches
# -----------------------------------------------------
@dataclass
class MediaMeta:
    media_type: str
    file_id: str
    file_unique_id: str
    sha256: str
    phash: Optional[str] = None
    frame_hashes: Optional[list[str]] = None


@dataclass
class ParsedText:
    name: Optional[str]
    anime_name: Optional[str]
    rarity: Optional[str]
    card_id: Optional[str]
    command_name: Optional[str]
    raw_text: str


class PerfTracker:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.lookup_count = 0
        self.lookup_hits = 0
        self.lookup_last_ms = 0.0
        self.lookup_ema_ms = 0.0
        self.lookup_max_ms = 0.0
        self.db_ping_last_ms = 0.0

    async def observe_lookup(self, elapsed_ms: float, hit: bool) -> None:
        async with self.lock:
            self.lookup_count += 1
            if hit:
                self.lookup_hits += 1
            self.lookup_last_ms = elapsed_ms
            if self.lookup_ema_ms <= 0:
                self.lookup_ema_ms = elapsed_ms
            else:
                self.lookup_ema_ms = (self.lookup_ema_ms * 0.85) + (elapsed_ms * 0.15)
            if elapsed_ms > self.lookup_max_ms:
                self.lookup_max_ms = elapsed_ms

    async def set_db_ping(self, ms: float) -> None:
        async with self.lock:
            self.db_ping_last_ms = ms

    async def snapshot(self) -> dict[str, float]:
        async with self.lock:
            hit_rate = (self.lookup_hits / self.lookup_count * 100.0) if self.lookup_count else 0.0
            return {
                "lookup_count": float(self.lookup_count),
                "lookup_hits": float(self.lookup_hits),
                "lookup_last_ms": self.lookup_last_ms,
                "lookup_ema_ms": self.lookup_ema_ms,
                "lookup_max_ms": self.lookup_max_ms,
                "lookup_hit_rate": hit_rate,
                "db_ping_last_ms": self.db_ping_last_ms,
            }


class TTLResultCache:
    def __init__(self, max_items: int, ttl_seconds: int):
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds
        self.data: OrderedDict[str, tuple[float, Optional[dict[str, Any]]]] = OrderedDict()
        self.lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Optional[dict[str, Any]]]:
        now = time.time()
        async with self.lock:
            value = self.data.get(key)
            if value is None:
                return None
            expires_at, payload = value
            if expires_at < now:
                self.data.pop(key, None)
                return None
            self.data.move_to_end(key)
            return payload

    async def set(self, key: str, payload: Optional[dict[str, Any]]) -> None:
        async with self.lock:
            self.data[key] = (time.time() + self.ttl_seconds, payload)
            self.data.move_to_end(key)
            while len(self.data) > self.max_items:
                self.data.popitem(last=False)


class ItemSnapshot:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.last_refresh_monotonic = 0.0
        self.by_file_unique_id: dict[str, dict[str, Any]] = {}
        self.by_sha256: dict[str, dict[str, Any]] = {}
        self.photos: list[tuple[int, dict[str, Any]]] = []
        self.videos: list[tuple[list[int], dict[str, Any]]] = []
        self.photos_by_command: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        self.videos_by_command: dict[str, list[tuple[list[int], dict[str, Any]]]] = defaultdict(list)
        self.count = 0

    async def refresh(self) -> None:
        async with self.lock:
            projection = {
                "_id": 0,
                "name": 1,
                "anime_name": 1,
                "rarity": 1,
                "card_id": 1,
                "command_name": 1,
                "source_bot_key": 1,
                "media_type": 1,
                "file_unique_id": 1,
                "sha256": 1,
                "phash": 1,
                "frame_hashes": 1,
            }
            rows = await items.find({}, projection).to_list(length=None)
            by_file_unique_id: dict[str, dict[str, Any]] = {}
            by_sha256: dict[str, dict[str, Any]] = {}
            photos: list[tuple[int, dict[str, Any]]] = []
            videos: list[tuple[list[int], dict[str, Any]]] = []
            photos_by_command: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
            videos_by_command: dict[str, list[tuple[list[int], dict[str, Any]]]] = defaultdict(list)

            for row in rows:
                file_unique_id = row.get("file_unique_id") or ""
                sha256_value = row.get("sha256") or ""
                if file_unique_id:
                    by_file_unique_id[file_unique_id] = row
                if sha256_value:
                    by_sha256[sha256_value] = row

                command_aliases = get_command_aliases(row.get("command_name") or DEFAULT_COMMAND) or [DEFAULT_COMMAND]

                if row.get("media_type") == "photo" and row.get("phash"):
                    try:
                        phash_int = int(str(row["phash"]), 16)
                        photos.append((phash_int, row))
                        for command_name in command_aliases:
                            photos_by_command[command_name].append((phash_int, row))
                    except Exception:
                        pass

                if row.get("media_type") == "video" and row.get("frame_hashes"):
                    try:
                        frame_ints = [int(str(h), 16) for h in list(row["frame_hashes"]) if h]
                        if frame_ints:
                            videos.append((frame_ints, row))
                            for command_name in command_aliases:
                                videos_by_command[command_name].append((frame_ints, row))
                    except Exception:
                        pass

            self.by_file_unique_id = by_file_unique_id
            self.by_sha256 = by_sha256
            self.photos = photos
            self.videos = videos
            self.photos_by_command = photos_by_command
            self.videos_by_command = videos_by_command
            self.count = len(rows)
            self.last_refresh_monotonic = time.monotonic()
            logger.info(
                "Snapshot refreshed | total=%s | exact=%s/%s | photos=%s | videos=%s",
                self.count,
                len(self.by_file_unique_id),
                len(self.by_sha256),
                len(self.photos),
                len(self.videos),
            )

    def age_seconds(self) -> float:
        if self.last_refresh_monotonic <= 0:
            return 10**9
        return time.monotonic() - self.last_refresh_monotonic


RESULT_CACHE = TTLResultCache(RESULT_CACHE_MAX_ITEMS, RESULT_CACHE_TTL_SECONDS)
SNAPSHOT = ItemSnapshot()
PERF = PerfTracker()
LAST_MISS_REFRESH = 0.0
FORWARD_SOURCE_USERNAME_COMMAND_MAP: dict[str, str] = {}
FORWARD_SOURCE_TITLE_COMMAND_MAP: dict[str, str] = {}

# -----------------------------------------------------
# Basic helpers
# -----------------------------------------------------
def html_escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean_value(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_name(name: str) -> str:
    return clean_value(name).casefold()


def normalize_parse_text(text: Optional[str]) -> str:
    text = text or ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r", "\n")
    text = text.replace("：", ":").replace("﹕", ":").replace("꞉", ":")
    text = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def parse_field(text: str, patterns: list[re.Pattern]) -> Optional[str]:
    for pattern in patterns:
        match = pattern.search(text or "")
        if match:
            return clean_value(match.group(1))
    return None


def clean_command_name(value: str) -> str:
    cmd = clean_value(value)
    if not cmd:
        return DEFAULT_COMMAND
    if not cmd.startswith("/"):
        cmd = f"/{cmd.lstrip('/')}"
    return cmd.split()[0]


def dedupe_commands(commands: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in commands:
        if not value:
            continue
        cmd = clean_command_name(value)
        key = cmd.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(cmd)
    return ordered


def build_command_alias_index() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    index: dict[str, list[str]] = {}
    for _key, _bot, commands in SUPPORTED_BOTS:
        canonical = clean_command_name(commands[0])
        aliases = dedupe_commands(commands)
        groups[canonical] = aliases
        for alias in aliases:
            index[alias.casefold()] = aliases
            index[alias.lstrip('/').casefold()] = aliases
    return groups, index


COMMAND_ALIAS_GROUPS, COMMAND_ALIAS_INDEX = build_command_alias_index()


def get_command_aliases(value: Optional[str]) -> list[str]:
    if not value:
        return []
    cmd = clean_command_name(value)
    aliases = COMMAND_ALIAS_GROUPS.get(cmd)
    return list(aliases) if aliases else [cmd]


def extract_command_candidates(text: Optional[str]) -> list[str]:
    raw = unicodedata.normalize("NFKC", normalize_parse_text(text or ""))
    if not raw:
        return []

    candidates: list[str] = []
    for pattern in COMMAND_PATTERNS:
        for match in pattern.finditer(raw):
            key = clean_value(match.group(1)).casefold().lstrip('/')
            aliases = COMMAND_ALIAS_INDEX.get(key) or COMMAND_ALIAS_INDEX.get(f"/{key}")
            if aliases:
                candidates.extend(aliases)

    for match in re.finditer(r"(?<!\w)/?([A-Za-z][A-Za-z0-9_]{1,31})\b", raw):
        key = clean_value(match.group(1)).casefold().lstrip('/')
        aliases = COMMAND_ALIAS_INDEX.get(key) or COMMAND_ALIAS_INDEX.get(f"/{key}")
        if aliases:
            candidates.extend(aliases)

    return dedupe_commands(candidates)


def normalize_forward_mapping_key(value: str) -> str:
    return clean_value(value).lstrip("@").casefold()


def strip_leading_symbols(value: str) -> str:
    value = clean_value(value)
    return re.sub(r"^[^\w\u00C0-\u024F\u0400-\u04FF\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]+", "", value).strip()


def strip_trailing_badge(value: str) -> str:
    value = clean_value(value)
    if not value:
        return value
    while True:
        match = TRAILING_BADGE_RE.search(value)
        if not match:
            break
        inner = match.group(1)
        if re.search(r"[A-Za-z0-9]", inner):
            break
        value = clean_value(value[: match.start()])
    return value


def strip_grab_name_suffix(value: str) -> str:
    value = clean_value(value)
    return clean_value(re.sub(r"\s*[-–—|]+\s*(?:rarity|anime|id)\b.*$", "", value, flags=re.IGNORECASE))


def finalize_parsed_text(parsed: ParsedText) -> ParsedText:
    parsed.name = strip_grab_name_suffix(strip_trailing_badge(strip_leading_symbols(parsed.name or ""))) or None
    parsed.anime_name = strip_trailing_badge(strip_leading_symbols(parsed.anime_name or "")) or None
    parsed.rarity = strip_leading_symbols(parsed.rarity or "") or None
    parsed.card_id = clean_value(parsed.card_id or "") or None
    if parsed.command_name:
        parsed.command_name = clean_command_name(parsed.command_name)
    parsed.raw_text = normalize_parse_text(parsed.raw_text)
    return parsed


def hamming_distance_int(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def register_forward_source_command(key: str, command_name: str) -> None:
    normalized_key = normalize_forward_mapping_key(key)
    if not normalized_key:
        return
    normalized_command = clean_command_name(command_name)
    if key.strip().startswith("@") or re.fullmatch(r"[A-Za-z0-9_]+", key.strip()):
        FORWARD_SOURCE_USERNAME_COMMAND_MAP[normalized_key] = normalized_command
    else:
        FORWARD_SOURCE_TITLE_COMMAND_MAP[normalized_key] = normalized_command


for _mapping in [x.strip() for x in FORWARD_SOURCE_COMMANDS_RAW.split(",") if x.strip() and ":" in x]:
    _key, _cmd = _mapping.split(":", 1)
    register_forward_source_command(_key, _cmd)

# -----------------------------------------------------
# Parsing
# -----------------------------------------------------
def parse_command_name(text: str) -> Optional[str]:
    candidates = extract_command_candidates(text)
    return candidates[0] if candidates else None


def parse_caption_text(text: Optional[str]) -> ParsedText:
    raw = normalize_parse_text(text)
    return finalize_parsed_text(
        ParsedText(
            name=parse_field(raw, NAME_PATTERNS),
            anime_name=parse_field(raw, ANIME_PATTERNS),
            rarity=parse_field(raw, RARITY_PATTERNS),
            card_id=parse_field(raw, CARD_ID_PATTERNS),
            command_name=parse_command_name(raw),
            raw_text=raw,
        )
    )


def collect_candidate_texts(message: Message) -> list[str]:
    candidates: list[str] = []
    for value in [getattr(message, "caption", None), getattr(message, "text", None)]:
        value = normalize_parse_text(value)
        if value and value not in candidates:
            candidates.append(value)
    ext = getattr(message, "external_reply", None)
    if ext is not None:
        for value in [getattr(ext, "caption", None), getattr(ext, "text", None)]:
            value = normalize_parse_text(value)
            if value and value not in candidates:
                candidates.append(value)
    return candidates


def parse_caption_text_from_message(message: Message) -> ParsedText:
    candidates = collect_candidate_texts(message)
    for raw in candidates:
        parsed = parse_caption_text(raw)
        if parsed.name:
            return parsed
    raw = candidates[0] if candidates else ""
    return parse_caption_text(raw)


def get_combined_message_text(message: Message) -> str:
    return "\n".join(collect_candidate_texts(message)).strip()


def get_hint_name(full_name: str) -> str:
    name = clean_value(full_name)
    if not name:
        return ""
    return name.split(" ")[0]


def extract_media_handle(message: Message):
    if message.photo:
        return "photo", message.photo[-1]
    if message.video:
        return "video", message.video
    return None, None


def is_group_chat(message: Message) -> bool:
    return bool(message.chat and getattr(message.chat, "type", "") in {"group", "supergroup"})


def is_private_chat(message: Message) -> bool:
    return bool(message.chat and getattr(message.chat, "type", "") == "private")


def powered_by_html() -> str:
    username = OWNER_USERNAME.strip().lstrip("@")
    if not username:
        return "Powered by Official Bika."
    return f'Powered by <a href="https://t.me/{html_escape(username)}">Official Bika</a>.'


def build_result_text(item: dict[str, Any], command_name: Optional[str] = None) -> str:
    name = clean_value(item.get("name") or "Unknown")
    command_name = clean_command_name(command_name or item.get("command_name") or DEFAULT_COMMAND)
    hint_name = get_hint_name(name)
    lines = [
        f"<b>NAME :</b> <code>{html_escape(name)}</code>",
        "────────────────",
        f"🔹 <b>Hint :</b> <code>{html_escape(f'{command_name} {hint_name}')}</code>",
        f"🔸 <b>Full :</b> <code>{html_escape(f'{command_name} {name}')}</code>",
        "",
        powered_by_html(),
    ]
    return "\n".join(lines)


def build_copy_keyboard(command_name: str, name: str) -> InlineKeyboardMarkup:
    hint_cmd = f"{command_name} {get_hint_name(name)}".strip()
    full_cmd = f"{command_name} {clean_value(name)}".strip()
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="📋 Copy Hint", copy_text=CopyTextButton(text=hint_cmd[:256])),
            InlineKeyboardButton(text="📋 Copy Full", copy_text=CopyTextButton(text=full_cmd[:256])),
        ]]
    )


def get_inline_source_username(message: Message) -> str:
    via_bot = getattr(message, "via_bot", None)
    if via_bot is None:
        return ""
    return (getattr(via_bot, "username", "") or "").lower().strip()


def get_inline_source_command(message: Message) -> Optional[str]:
    return INLINE_SOURCE_COMMAND_MAP.get(get_inline_source_username(message))


def is_character_catcher_style_message(message: Message) -> bool:
    raw = get_combined_message_text(message)
    return bool(raw and CHARACTER_CATCHER_HEADER_RE.search(raw) and NUMBERED_NAME_RE.search(raw))


def infer_anime_from_lines(lines: list[str], match_line_index: int) -> Optional[str]:
    if match_line_index <= 0:
        return None
    for i in range(match_line_index - 1, -1, -1):
        line = clean_value(lines[i])
        if not line:
            continue
        if CHARACTER_CATCHER_HEADER_RE.search(line):
            continue
        if re.search(r"\b(?:rarity|added\s*by|price|id|character\s*id)\b", line, re.IGNORECASE):
            continue
        if re.search(r"new\s+(?:character|waifu)\s+added", line, re.IGNORECASE):
            continue
        return strip_trailing_badge(strip_leading_symbols(line)) or None
    return None


def parse_numbered_name_message(message: Message, forced_command: str) -> ParsedText:
    raw = get_combined_message_text(message)
    lines = [clean_value(x) for x in raw.splitlines() if clean_value(x)]
    name = None
    anime_name = None
    card_id = None
    match = NUMBERED_NAME_RE.search(raw)
    if match:
        card_id = clean_value(match.group(1))
        name = clean_value(match.group(2))
        match_line = clean_value(match.group(0))
        if match_line in lines:
            anime_name = infer_anime_from_lines(lines, lines.index(match_line))
    return finalize_parsed_text(
        ParsedText(
            name=name,
            anime_name=anime_name,
            rarity=parse_field(raw, SOURCE_RARITY_PATTERNS) or parse_field(raw, RARITY_PATTERNS),
            card_id=card_id,
            command_name=forced_command,
            raw_text=raw,
        )
    )


def parse_grab_inline_message(message: Message, forced_command: str) -> ParsedText:
    raw = get_combined_message_text(message)
    parsed = ParsedText(
        name=parse_field(raw, SOURCE_NAME_PATTERNS) or parse_field(raw, NAME_PATTERNS),
        anime_name=parse_field(raw, SOURCE_ANIME_PATTERNS) or parse_field(raw, ANIME_PATTERNS),
        rarity=parse_field(raw, SOURCE_RARITY_PATTERNS) or parse_field(raw, RARITY_PATTERNS),
        card_id=parse_field(raw, SOURCE_CARD_ID_PATTERNS) or parse_field(raw, CARD_ID_PATTERNS),
        command_name=forced_command,
        raw_text=raw,
    )
    return finalize_parsed_text(parsed)


def is_forwarded_message(message: Message) -> bool:
    return bool(
        getattr(message, "forward_origin", None)
        or getattr(message, "forward_from_chat", None)
        or getattr(message, "forward_from", None)
        or getattr(message, "forward_sender_name", None)
    )


def get_forward_source_info(message: Message) -> dict[str, Any]:
    info: dict[str, Any] = {"chat_id": None, "username": "", "title": "", "origin_type": ""}
    origin = getattr(message, "forward_origin", None)
    if origin:
        info["origin_type"] = origin.__class__.__name__
        chat = getattr(origin, "chat", None)
        if chat is None:
            sender_chat = getattr(origin, "sender_chat", None)
            if sender_chat is not None:
                chat = sender_chat
        if chat is not None:
            info["chat_id"] = getattr(chat, "id", None)
            info["username"] = (getattr(chat, "username", "") or "").lower()
            info["title"] = clean_value(getattr(chat, "title", "") or "").casefold()
            return info
        sender_user_name = (getattr(origin, "sender_user_name", "") or "").lower()
        if sender_user_name:
            info["username"] = sender_user_name
            return info
    legacy_chat = getattr(message, "forward_from_chat", None)
    if legacy_chat is not None:
        info["chat_id"] = getattr(legacy_chat, "id", None)
        info["username"] = (getattr(legacy_chat, "username", "") or "").lower()
        info["title"] = clean_value(getattr(legacy_chat, "title", "") or "").casefold()
        info["origin_type"] = "legacy_forward_chat"
        return info
    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None:
        info["chat_id"] = getattr(sender_chat, "id", None)
        info["username"] = (getattr(sender_chat, "username", "") or "").lower()
        info["title"] = clean_value(getattr(sender_chat, "title", "") or "").casefold()
        info["origin_type"] = "sender_chat"
        return info
    return info


def get_forward_source_command(message: Message) -> Optional[str]:
    if not is_forwarded_message(message):
        return None
    info = get_forward_source_info(message)
    username = normalize_forward_mapping_key(info.get("username", ""))
    title = normalize_forward_mapping_key(info.get("title", ""))
    if username and username in FORWARD_SOURCE_USERNAME_COMMAND_MAP:
        return FORWARD_SOURCE_USERNAME_COMMAND_MAP[username]
    if title and title in FORWARD_SOURCE_TITLE_COMMAND_MAP:
        return FORWARD_SOURCE_TITLE_COMMAND_MAP[title]
    for key, command_name in FORWARD_SOURCE_TITLE_COMMAND_MAP.items():
        if key and title and (key in title or title in key):
            return command_name
    return None


def is_allowed_forward_source(message: Message) -> bool:
    if not is_forwarded_message(message):
        return False
    if get_forward_source_command(message):
        return True
    if not SOURCE_CHANNEL_IDS and not SOURCE_CHANNEL_USERNAMES and not SOURCE_CHANNEL_TITLES:
        return True
    info = get_forward_source_info(message)
    chat_id = info.get("chat_id")
    username = info.get("username", "")
    title = info.get("title", "")
    if chat_id is not None and chat_id in SOURCE_CHANNEL_IDS:
        return True
    if username and username in SOURCE_CHANNEL_USERNAMES:
        return True
    if title and title in SOURCE_CHANNEL_TITLES:
        return True
    return False


def is_hallow_forward_source_message(message: Message) -> bool:
    return is_forwarded_message(message) and is_allowed_forward_source(message)


def parse_forward_source_message(message: Message, forced_command: str) -> ParsedText:
    raw = get_combined_message_text(message)
    numbered = parse_numbered_name_message(message, forced_command)
    if numbered.name:
        return numbered
    parsed = ParsedText(
        name=parse_field(raw, SOURCE_NAME_PATTERNS) or parse_field(raw, NAME_PATTERNS),
        anime_name=parse_field(raw, SOURCE_ANIME_PATTERNS) or parse_field(raw, ANIME_PATTERNS),
        rarity=parse_field(raw, SOURCE_RARITY_PATTERNS) or parse_field(raw, RARITY_PATTERNS),
        card_id=parse_field(raw, SOURCE_CARD_ID_PATTERNS) or parse_field(raw, CARD_ID_PATTERNS),
        command_name=forced_command,
        raw_text=raw,
    )
    return finalize_parsed_text(parsed)


def get_effective_parsed_message(message: Message) -> ParsedText:
    parsed = parse_caption_text_from_message(message)
    inline_cmd = get_inline_source_command(message)
    if inline_cmd:
        if inline_cmd == "/grab":
            grab_parsed = parse_grab_inline_message(message, inline_cmd)
            if grab_parsed.name or grab_parsed.card_id:
                return grab_parsed
        inline_parsed = parse_numbered_name_message(message, inline_cmd)
        if inline_parsed.name:
            return inline_parsed
        parsed.command_name = inline_cmd
        return finalize_parsed_text(parsed)

    forward_cmd = get_forward_source_command(message)
    if forward_cmd:
        forward_parsed = parse_forward_source_message(message, forward_cmd)
        if forward_parsed.name or forward_parsed.card_id:
            return forward_parsed
        parsed.command_name = forward_cmd
        return finalize_parsed_text(parsed)

    if is_character_catcher_style_message(message):
        cc_parsed = parse_numbered_name_message(message, "/catch")
        if cc_parsed.name:
            return cc_parsed
        parsed.command_name = "/catch"
        return finalize_parsed_text(parsed)

    if is_hallow_forward_source_message(message):
        parsed.command_name = "/hallow"
        return finalize_parsed_text(parsed)

    return finalize_parsed_text(parsed)


def get_effective_command_candidates_for_message(message: Message, parsed: Optional[ParsedText] = None) -> list[str]:
    candidates: list[str] = []

    inline_cmd = get_inline_source_command(message)
    if inline_cmd:
        candidates.extend(get_command_aliases(inline_cmd))

    forward_cmd = get_forward_source_command(message)
    if forward_cmd:
        candidates.extend(get_command_aliases(forward_cmd))

    if is_character_catcher_style_message(message):
        candidates.extend(get_command_aliases("/catch"))

    if is_hallow_forward_source_message(message):
        candidates.extend(get_command_aliases("/hallow"))

    for raw in collect_candidate_texts(message):
        candidates.extend(extract_command_candidates(raw))

    if parsed and parsed.command_name:
        candidates.extend(get_command_aliases(parsed.command_name))

    return dedupe_commands(candidates)


def is_group_auto_lookup_source_message(message: Message) -> bool:
    if get_inline_source_command(message):
        return True
    if get_forward_source_command(message):
        return True
    if is_character_catcher_style_message(message):
        return True
    if is_hallow_forward_source_message(message):
        return True
    return False

# -----------------------------------------------------
# Force join
# -----------------------------------------------------
def build_start_keyboard() -> Optional[InlineKeyboardMarkup]:
    rows: list[list[InlineKeyboardButton]] = []
    first_row: list[InlineKeyboardButton] = []
    if SUPPORT_GROUP_USERNAME:
        first_row.append(InlineKeyboardButton(text="👥 Support Group", url=f"https://t.me/{SUPPORT_GROUP_USERNAME.lstrip('@')}"))
    if SUPPORT_CHANNEL_USERNAME:
        first_row.append(InlineKeyboardButton(text="📢 Support Channel", url=f"https://t.me/{SUPPORT_CHANNEL_USERNAME.lstrip('@')}"))
    if first_row:
        rows.append(first_row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def support_chat_ref(username: str) -> Optional[str]:
    value = clean_value(username).strip()
    if not value:
        return None
    return f"@{value.lstrip('@')}"


def support_chat_url(username: str) -> Optional[str]:
    value = clean_value(username).strip()
    if not value:
        return None
    return f"https://t.me/{value.lstrip('@')}"


def force_join_enabled() -> bool:
    return bool(SUPPORT_CHANNEL_USERNAME or SUPPORT_GROUP_USERNAME)


def build_force_join_group_text() -> str:
    return (
        "🔒 <b>Verification Needed</b>\n\n"
        "<b>My</b>\n"
        "ဒီ bot ကိုသုံးဖို့ Support Channel နဲ့ Support Group နှစ်ခုလုံး join ထားရပါမယ်။\n"
        "အောက်က button ကိုနှိပ်ပြီး DM ထဲမှာ verify လုပ်ပေးပါ။\n\n"
        "<b>Eng</b>\n"
        "To use this bot, you need to join both our Support Channel and Support Group.\n"
        "Tap the button below and verify your membership in DM."
    )


def build_force_join_dm_text(cjoin: bool, gjoin: bool) -> str:
    channel_state = "✅ Joined" if cjoin else "❌ Not Joined"
    group_state = "✅ Joined" if gjoin else "❌ Not Joined"
    return (
        "🚫 <b>Access Limited</b>\n\n"
        "<b>My</b>\n"
        "ဒီ bot ကိုအသုံးပြုဖို့ Support Channel နဲ့ Support Group နှစ်ခုလုံး join ထားရပါမယ်။\n"
        "အောက်က button တွေကနေ အရင် join လုပ်ပြီး <b>Verify Membership</b> ကိုနှိပ်ပါ။\n\n"
        f"• Channel : <b>{channel_state}</b>\n"
        f"• Group : <b>{group_state}</b>\n\n"
        "<b>Eng</b>\n"
        "To use this bot, you must join both our Support Channel and Support Group first.\n"
        "Use the buttons below to join, then tap <b>Verify Membership</b>.\n\n"
        f"• Channel : <b>{channel_state}</b>\n"
        f"• Group : <b>{group_state}</b>"
    )


def build_force_join_success_text() -> str:
    return (
        "✅ <b>Verification Complete</b>\n\n"
        "<b>My</b>\n"
        "Support Channel နဲ့ Support Group join စစ်ဆေးမှု အောင်မြင်ပါတယ်။\n"
        "အခု bot ကို ဆက်သုံးလို့ရပါပြီ။\n\n"
        "<b>Eng</b>\n"
        "Your membership check is complete.\n"
        "You can now continue using the bot."
    )


def build_force_join_group_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    verify_url = f"https://t.me/{bot_username.lstrip('@')}?start={FORCE_JOIN_START_PAYLOAD}"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Verify in DM", url=verify_url)]])


def build_force_join_dm_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    first_row: list[InlineKeyboardButton] = []
    channel_url = support_chat_url(SUPPORT_CHANNEL_USERNAME)
    group_url = support_chat_url(SUPPORT_GROUP_USERNAME)
    if channel_url:
        first_row.append(InlineKeyboardButton(text="📢 Join Channel", url=channel_url))
    if group_url:
        first_row.append(InlineKeyboardButton(text="👥 Join Group", url=group_url))
    if first_row:
        rows.append(first_row)
    rows.append([InlineKeyboardButton(text="✅ Verify Membership", callback_data=FORCE_JOIN_VERIFY_CALLBACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def is_active_member_status(member: Any) -> bool:
    status = getattr(member, "status", None)
    if status in {"member", "administrator", "creator"}:
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def check_single_support_membership(bot: Bot, user_id: int, chat_ref: Optional[str]) -> bool:
    if not chat_ref:
        return True
    try:
        member = await bot.get_chat_member(chat_id=chat_ref, user_id=user_id)
        return is_active_member_status(member)
    except Exception as exc:
        logger.warning("support membership check failed for %s in %s: %s", user_id, chat_ref, exc)
        return False


async def refresh_force_join_flags(bot: Bot, user_id: Optional[int]) -> tuple[bool, bool]:
    if not user_id:
        return True, True
    cjoin = await check_single_support_membership(bot, user_id, support_chat_ref(SUPPORT_CHANNEL_USERNAME))
    gjoin = await check_single_support_membership(bot, user_id, support_chat_ref(SUPPORT_GROUP_USERNAME))
    await known_users.update_one(
        {"user_id": user_id},
        {"$set": {"cjoin": bool(cjoin), "gjoin": bool(gjoin), "join_checked_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return bool(cjoin), bool(gjoin)


async def get_force_join_flags_cached(bot: Bot, user_id: int) -> tuple[bool, bool]:
    row = await known_users.find_one({"user_id": user_id}, {"cjoin": 1, "gjoin": 1, "join_checked_at": 1})
    if row:
        checked_at = row.get("join_checked_at")
        if isinstance(checked_at, datetime):
            age = (datetime.now(timezone.utc) - checked_at).total_seconds()
            if age <= FORCE_JOIN_CACHE_SECONDS:
                return bool(row.get("cjoin")), bool(row.get("gjoin"))
    return await refresh_force_join_flags(bot, user_id)


async def send_force_join_group_prompt(message: Message, bot: Bot) -> None:
    me = await bot.get_me()
    await message.reply(
        build_force_join_group_text(),
        parse_mode=ParseMode.HTML,
        reply_markup=build_force_join_group_keyboard(me.username or ""),
        disable_web_page_preview=True,
    )


async def send_force_join_dm_prompt(message: Message, bot: Bot) -> None:
    user_id = message.from_user.id if message.from_user else None
    cjoin, gjoin = await get_force_join_flags_cached(bot, user_id) if user_id else (True, True)
    await message.reply(
        build_force_join_dm_text(cjoin, gjoin),
        parse_mode=ParseMode.HTML,
        reply_markup=build_force_join_dm_keyboard(),
        disable_web_page_preview=True,
    )


async def ensure_force_join_access(message: Message, bot: Bot, *, group_prompt: bool = True, dm_prompt: bool = True) -> bool:
    if not force_join_enabled():
        return True
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return False
    if user_id in OWNER_IDS or await is_sudo_user(user_id):
        return True
    cjoin, gjoin = await get_force_join_flags_cached(bot, user_id)
    if cjoin and gjoin:
        return True
    if is_private_chat(message):
        if dm_prompt:
            await send_force_join_dm_prompt(message, bot)
        return False
    if is_group_chat(message):
        if group_prompt:
            await send_force_join_group_prompt(message, bot)
        return False
    if dm_prompt:
        await send_force_join_dm_prompt(message, bot)
    return False

# -----------------------------------------------------
# Media hashing / lookup
# -----------------------------------------------------
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def download_file_bytes(bot: Bot, file_id: str) -> bytes:
    tg_file = await bot.get_file(file_id)
    if not tg_file.file_path:
        raise RuntimeError("Telegram did not return file_path")
    buffer = BytesIO()
    await bot.download_file(tg_file.file_path, destination=buffer)
    return buffer.getvalue()


def compute_photo_phash(data: bytes) -> str:
    with Image.open(BytesIO(data)) as img:
        img = img.convert("RGB")
        return str(imagehash.phash(img))


def _frame_to_hash(frame) -> str:
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    return str(imagehash.phash(image))


def compute_video_hashes(data: bytes) -> list[str]:
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=True) as tmp:
        tmp.write(data)
        tmp.flush()
        cap = cv2.VideoCapture(tmp.name)
        if not cap.isOpened():
            raise RuntimeError("Failed to open video")
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if frame_count <= 0:
            cap.release()
            raise RuntimeError("Video contains no readable frames")
        targets = sorted({
            max(0, int(frame_count * 0.2) - 1),
            max(0, int(frame_count * 0.5) - 1),
            max(0, int(frame_count * 0.8) - 1),
        })
        hashes: list[str] = []
        for idx in targets:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if ok and frame is not None:
                hashes.append(_frame_to_hash(frame))
        cap.release()
        if not hashes:
            raise RuntimeError("Could not extract frames from video")
        return hashes


async def get_media_meta(bot: Bot, message: Message) -> MediaMeta:
    media_type, media = extract_media_handle(message)
    if not media_type or not media:
        raise ValueError("Message does not contain supported media")
    raw = await download_file_bytes(bot, media.file_id)
    digest = sha256_hex(raw)
    if media_type == "photo":
        return MediaMeta(
            media_type="photo",
            file_id=media.file_id,
            file_unique_id=media.file_unique_id,
            sha256=digest,
            phash=compute_photo_phash(raw),
        )
    return MediaMeta(
        media_type="video",
        file_id=media.file_id,
        file_unique_id=media.file_unique_id,
        sha256=digest,
        frame_hashes=compute_video_hashes(raw),
    )


def result_cache_key(meta: MediaMeta, command_hints: Optional[list[str]] = None) -> str:
    hint_key = ",".join(dedupe_commands(list(command_hints or [])))
    return f"{meta.media_type}|{meta.file_unique_id}|{meta.sha256}|{hint_key}"


async def measure_db_ping_ms() -> float:
    started = time.perf_counter()
    await db.command("ping")
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    await PERF.set_db_ping(elapsed_ms)
    return elapsed_ms


async def refresh_snapshot_loop() -> None:
    while True:
        await asyncio.sleep(max(5, SNAPSHOT_REFRESH_SECONDS))
        try:
            await SNAPSHOT.refresh()
        except Exception:
            logger.exception("snapshot refresh loop failed")


async def maybe_refresh_snapshot_on_miss() -> None:
    global LAST_MISS_REFRESH
    now = time.monotonic()
    if now - LAST_MISS_REFRESH < MISS_REFRESH_COOLDOWN_SECONDS:
        return
    LAST_MISS_REFRESH = now
    try:
        await SNAPSHOT.refresh()
    except Exception:
        logger.exception("miss-triggered snapshot refresh failed")


async def find_match(meta: MediaMeta, command_hints: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    normalized_hints = dedupe_commands(list(command_hints or []))
    cache_key = result_cache_key(meta, normalized_hints)
    cached = await RESULT_CACHE.get(cache_key)
    if cached is not None:
        return cached

    matched = SNAPSHOT.by_file_unique_id.get(meta.file_unique_id)
    if matched:
        await RESULT_CACHE.set(cache_key, matched)
        return matched

    matched = SNAPSHOT.by_sha256.get(meta.sha256)
    if matched:
        await RESULT_CACHE.set(cache_key, matched)
        return matched

    best_item: Optional[dict[str, Any]] = None

    if meta.media_type == "photo" and meta.phash:
        try:
            meta_hash_int = int(meta.phash, 16)
        except Exception:
            meta_hash_int = None
        if meta_hash_int is not None:
            candidate_lists = []
            for hint_cmd in normalized_hints:
                if SNAPSHOT.photos_by_command.get(hint_cmd):
                    candidate_lists.append(SNAPSHOT.photos_by_command[hint_cmd])
            candidate_lists.append(SNAPSHOT.photos)

            seen = set()
            best_score = 10**9
            for candidates in candidate_lists:
                obj_id = id(candidates)
                if obj_id in seen:
                    continue
                seen.add(obj_id)
                local_best = None
                local_score = 10**9
                for phash_int, row in candidates:
                    distance = hamming_distance_int(meta_hash_int, phash_int)
                    if distance < local_score:
                        local_score = distance
                        local_best = row
                if local_best and local_score < best_score:
                    best_score = local_score
                    best_item = local_best
                if best_item and best_score <= PHOTO_PHASH_THRESHOLD:
                    await RESULT_CACHE.set(cache_key, best_item)
                    return best_item

    if meta.media_type == "video" and meta.frame_hashes:
        try:
            meta_frame_ints = [int(h, 16) for h in meta.frame_hashes if h]
        except Exception:
            meta_frame_ints = []
        if meta_frame_ints:
            candidate_lists = []
            for hint_cmd in normalized_hints:
                if SNAPSHOT.videos_by_command.get(hint_cmd):
                    candidate_lists.append(SNAPSHOT.videos_by_command[hint_cmd])
            candidate_lists.append(SNAPSHOT.videos)

            seen = set()
            best_avg = 10**9
            best_item = None
            for candidates in candidate_lists:
                obj_id = id(candidates)
                if obj_id in seen:
                    continue
                seen.add(obj_id)
                for saved_hashes, row in candidates:
                    distances = [hamming_distance_int(a, b) for a, b in zip(meta_frame_ints, saved_hashes)]
                    if not distances:
                        continue
                    avg = sum(distances) / len(distances)
                    peak = max(distances)
                    if avg < best_avg and peak <= VIDEO_FRAME_THRESHOLD:
                        best_avg = avg
                        best_item = row
                if best_item and best_avg <= VIDEO_AVG_THRESHOLD:
                    await RESULT_CACHE.set(cache_key, best_item)
                    return best_item

    await maybe_refresh_snapshot_on_miss()
    matched = SNAPSHOT.by_file_unique_id.get(meta.file_unique_id) or SNAPSHOT.by_sha256.get(meta.sha256)
    await RESULT_CACHE.set(cache_key, matched)
    return matched

# -----------------------------------------------------
# Stats / status helpers
# -----------------------------------------------------
async def count_media_for_bot_key(key: str, commands: list[str]) -> int:
    return await items.count_documents({"$or": [{"source_bot_key": key}, {"command_name": {"$in": commands}}]})


async def build_status_text() -> str:
    total_media, total_users, total_groups, gapproved_count, blacklisted_count = await asyncio.gather(
        asyncio.sleep(0, result=SNAPSHOT.count or 0),
        known_users.count_documents({}),
        known_groups.count_documents({}),
        gapproved_groups.count_documents({}),
        blacklisted_users.count_documents({}),
    )

    bot_counts = await asyncio.gather(*[count_media_for_bot_key(key, commands) for key, _bot, commands in SUPPORTED_BOTS])
    saved_by_cmd_lines = [f"‣ {html_escape(commands[0])} : <b>{count}</b>" for count, (_key, _bot, commands) in zip(bot_counts, SUPPORTED_BOTS)]
    perf = await PERF.snapshot()

    lines = [
        "♻ <b>LOOKUP BOT STATUS</b>",
        f"‣ Total Media : <b>{total_media}</b>",
        f"‣ Known Users : <b>{total_users}</b>",
        f"‣ Known Groups : <b>{total_groups}</b>",
        f"‣ GApproved Groups : <b>{gapproved_count}</b>",
        f"‣ Blacklisted Users : <b>{blacklisted_count}</b>",
        f"‣ Force Join : <b>{'ON' if force_join_enabled() else 'OFF'}</b>",
        "",
        "⚡ <b>LOOKUP ENGINE</b>",
        f"‣ Snapshot Items : <b>{SNAPSHOT.count}</b>",
        f"‣ Snapshot Age : <b>{int(SNAPSHOT.age_seconds())}s</b>",
        f"‣ Result Cache : <b>{len(RESULT_CACHE.data)}</b>",
        f"‣ Avg Latency : <b>{perf['lookup_ema_ms']:.2f}ms</b>",
        f"‣ Cache Hit Rate : <b>{perf['lookup_hit_rate']:.2f}%</b>",
        "",
        "🎮 <b>Saved Media By Cmd</b>",
        *saved_by_cmd_lines,
    ]
    return "\n".join(lines)


# -----------------------------------------------------
# DB / auth helpers
# -----------------------------------------------------
async def ensure_indexes() -> None:
    await items.create_index("file_unique_id", unique=True, sparse=True)
    await items.create_index("sha256", unique=True, sparse=True)
    await items.create_index("media_type")
    await items.create_index("command_name")
    await items.create_index([("command_name", 1), ("media_type", 1)])
    await items.create_index("source_bot_key")
    await sudo_users.create_index("user_id", unique=True)
    await blacklisted_users.create_index("user_id", unique=True)
    await known_users.create_index("user_id", unique=True)
    await known_users.create_index("username")
    await known_groups.create_index("chat_id", unique=True)
    await known_groups.create_index("username")
    await gapproved_groups.create_index("chat_id", unique=True)


async def remember_user(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    await known_users.update_one(
        {"user_id": user.id},
        {"$set": {"user_id": user.id, "username": (user.username or "").lower(), "full_name": clean_value(user.full_name), "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def remember_chat(message: Message) -> None:
    await remember_user(message)
    chat = message.chat
    if not chat:
        return
    if getattr(chat, "type", "") in {"group", "supergroup"}:
        await known_groups.update_one(
            {"chat_id": chat.id},
            {"$set": {
                "chat_id": chat.id,
                "title": clean_value(getattr(chat, "title", "") or ""),
                "username": (getattr(chat, "username", "") or "").lower(),
                "type": getattr(chat, "type", ""),
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )


async def is_owner_user(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in OWNER_IDS)


async def is_sudo_user(user_id: Optional[int]) -> bool:
    return bool(user_id and await sudo_users.find_one({"user_id": user_id}, {"_id": 1}))


async def is_admin_user(user_id: Optional[int]) -> bool:
    return bool(user_id) and (await is_owner_user(user_id) or await is_sudo_user(user_id))


async def is_blacklisted_user(user_id: Optional[int]) -> bool:
    return bool(user_id and await blacklisted_users.find_one({"user_id": user_id}, {"_id": 1}))


async def is_group_approved(chat_id: Optional[int]) -> bool:
    return bool(chat_id and await gapproved_groups.find_one({"chat_id": chat_id}, {"_id": 1}))


async def set_group_approval(chat, approved_by: int, enabled: bool) -> None:
    if not chat or getattr(chat, "type", "") not in {"group", "supergroup"}:
        return
    doc = {
        "chat_id": chat.id,
        "title": clean_value(getattr(chat, "title", "") or ""),
        "username": (getattr(chat, "username", "") or "").lower(),
        "type": getattr(chat, "type", ""),
        "updated_at": datetime.now(timezone.utc),
        "updated_by": approved_by,
    }
    if enabled:
        doc["approved_at"] = datetime.now(timezone.utc)
        await gapproved_groups.update_one({"chat_id": chat.id}, {"$set": doc}, upsert=True)
    else:
        await gapproved_groups.delete_one({"chat_id": chat.id})


async def can_use_lookup(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else None
    return bool(user_id) and not await is_blacklisted_user(user_id)


async def require_lookup_access(message: Message) -> bool:
    if await can_use_lookup(message):
        return True
    await message.reply("You are blacklisted from using this bot.")
    return False


async def resolve_user_reference(message: Message, bot: Bot, raw_arg: Optional[str]) -> Optional[dict[str, Any]]:
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        return {"user_id": target.id, "username": (target.username or "").lower(), "full_name": clean_value(target.full_name)}
    arg = clean_value(raw_arg or "")
    if not arg:
        return None
    if arg.isdigit():
        known = await known_users.find_one({"user_id": int(arg)})
        return known or {"user_id": int(arg), "username": "", "full_name": ""}
    if arg.startswith("@"):
        username = arg.lstrip("@").lower()
        known = await known_users.find_one({"username": username})
        if known:
            return known
        try:
            chat = await bot.get_chat(arg)
            return {"user_id": chat.id, "username": (getattr(chat, "username", "") or "").lower(), "full_name": clean_value(getattr(chat, "full_name", "") or "")}
        except Exception:
            return None
    return None


def format_target_user(user_doc: dict[str, Any]) -> str:
    username = user_doc.get("username") or ""
    user_id = user_doc.get("user_id")
    full_name = clean_value(user_doc.get("full_name") or "")
    if username:
        return f"@{username} ({user_id})"
    if full_name:
        return f"{full_name} ({user_id})"
    return str(user_id)


async def set_access(collection, user_doc: dict[str, Any], added_by: int, enabled: bool) -> None:
    if enabled:
        await collection.update_one(
            {"user_id": user_doc["user_id"]},
            {"$set": {
                "user_id": user_doc["user_id"],
                "username": user_doc.get("username", ""),
                "full_name": user_doc.get("full_name", ""),
                "updated_at": datetime.now(timezone.utc),
                "updated_by": added_by,
            }},
            upsert=True,
        )
    else:
        await collection.delete_one({"user_id": user_doc["user_id"]})

# -----------------------------------------------------
# Lookup flow
# -----------------------------------------------------
async def send_found_result(message: Message, item: dict[str, Any], override_command_name: Optional[str] = None) -> None:
    name = clean_value(item.get("name") or "Unknown")
    command_name = clean_command_name(override_command_name or item.get("command_name") or DEFAULT_COMMAND)
    text = build_result_text(item, command_name=command_name)
    keyboard = build_copy_keyboard(command_name, name)
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True)


async def send_not_found(message: Message) -> None:
    await message.reply("Unknown!\nဒီ Media Name ကို owner က save မလုပ်ရသေးတာ ဖြစ်နိုင်ပါတယ်။")


async def lookup_and_reply(reply_message: Message, target_media_message: Message, bot: Bot, command_candidates: Optional[list[str]] = None) -> None:
    started = time.perf_counter()
    matched = None
    primary_command = clean_command_name(command_candidates[0]) if command_candidates else None
    try:
        meta = await get_media_meta(bot, target_media_message)
        matched = await find_match(meta, command_hints=command_candidates)
        if matched:
            await send_found_result(reply_message, matched, override_command_name=primary_command)
        else:
            await send_not_found(reply_message)
    finally:
        await PERF.observe_lookup((time.perf_counter() - started) * 1000.0, bool(matched))


async def should_auto_reply_media_in_chat(message: Message) -> bool:
    if is_private_chat(message):
        return True
    if not is_group_chat(message):
        return True
    if not await is_group_approved(getattr(message.chat, "id", None)):
        return False
    return is_group_auto_lookup_source_message(message)

async def handle_lookup_trigger(message: Message, bot: Bot) -> None:
    asyncio.create_task(remember_chat(message))
    if not await ensure_force_join_access(message, bot, group_prompt=True, dm_prompt=True):
        return
    if not await can_use_lookup(message):
        await require_lookup_access(message)
        return
    target = message.reply_to_message
    if not target:
        await message.reply("media message ကို reply ထောက်ပြီး /name, /waifu သို့ .wa သုံးပါ")
        return
    media_type, _media = extract_media_handle(target)
    if not media_type:
        await message.reply("photo/video message ကို reply ထောက်ပြီး /name, /waifu သို့ .wa သုံးပါ")
        return
    parsed_target = get_effective_parsed_message(target)
    command_candidates = get_effective_command_candidates_for_message(target, parsed_target)
    if not command_candidates and message.reply_to_message:
        command_candidates = get_effective_command_candidates_for_message(message, get_effective_parsed_message(message))
    try:
        await lookup_and_reply(message, target, bot, command_candidates=command_candidates)
    except Exception as exc:
        logger.exception("reply lookup failed")
        await message.reply(f"စစ်ဆေးရာမှာ error ဖြစ်နေပါတယ်: {exc}")

# -----------------------------------------------------
# Commands
# -----------------------------------------------------
@router.message(Command("start"))
async def start_handler(message: Message, command: CommandObject, bot: Bot) -> None:
    asyncio.create_task(remember_chat(message))
    if force_join_enabled() and not await ensure_force_join_access(message, bot, group_prompt=True, dm_prompt=True):
        return
    if not await can_use_lookup(message):
        await require_lookup_access(message)
        return

    keyboard = build_start_keyboard()
    await message.reply(
        "ဒီ bot က photo/video post တွေကို fast match စစ်ပြီး name ပြန်ထုတ်ပေးပါတယ်。\n\n"
        "• DM: photo/video ပို့လိုက်တာနဲ့ lookup လုပ်ပေးမယ်\n"
        "• Group: media ကို reply ထောက်ပြီး /name, /waifu, .wa, .name နဲ့မေးလို့ရမယ်\n"
        "• Auto lookup က /gapprove လုပ်ထားတဲ့ group တွေမှာ source post တွေအတွက်ပဲ အလုပ်လုပ်မယ်\n"
        "• Force join pass ဖြစ်ရင် normal user တွေလည်း သုံးလို့ရမယ်\n"
        "• /status: current database + engine status ကြည့်လို့ရမယ်",
        reply_markup=keyboard,
    )


@router.message(Command("status"))
async def status_command(message: Message, bot: Bot) -> None:
    asyncio.create_task(remember_chat(message))
    if not await ensure_force_join_access(message, bot, group_prompt=True, dm_prompt=True):
        return
    if not await can_use_lookup(message):
        await require_lookup_access(message)
        return
    await message.reply(await build_status_text(), parse_mode=ParseMode.HTML)


@router.message(Command("stats"))
async def stats_handler(message: Message) -> None:
    asyncio.create_task(remember_chat(message))
    user_id = message.from_user.id if message.from_user else None
    if not await is_admin_user(user_id):
        return

    db_ping_task = asyncio.create_task(measure_db_ping_ms())
    counts_task = asyncio.gather(
        asyncio.sleep(0, result=SNAPSHOT.count or 0),
        items.count_documents({"media_type": "photo"}),
        items.count_documents({"media_type": "video"}),
        sudo_users.count_documents({}),
        known_users.count_documents({}),
        known_groups.count_documents({}),
        blacklisted_users.count_documents({}),
        gapproved_groups.count_documents({}),
    )
    perf_task = asyncio.create_task(PERF.snapshot())

    total, photos, videos, sudos, users, groups, blacklisted, gapproved_count = await counts_task
    db_ping_ms = await db_ping_task
    perf = await perf_task

    await message.reply(
        f"📊 <b>Lookup Bot Stats</b>\n\n"
        f"‣ Total Media: <b>{total}</b>\n"
        f"‣ Photos: <b>{photos}</b>\n"
        f"‣ Videos: <b>{videos}</b>\n"
        f"‣ Total Users: <b>{users}</b>\n"
        f"‣ Total Groups: <b>{groups}</b>\n"
        f"‣ Sudo Users: <b>{sudos}</b>\n"
        f"‣ Blacklisted: <b>{blacklisted}</b>\n"
        f"‣ GApproved Groups: <b>{gapproved_count}</b>\n"
        f"‣ Force Join: <b>{'ON' if force_join_enabled() else 'OFF'}</b>\n"
        f"‣ Snapshot Age: <b>{int(SNAPSHOT.age_seconds())}s</b>\n\n"
        f"⚙️ <b>PERFORMANCE</b>\n"
        f"‣ Latency : <b>{perf['lookup_ema_ms']:.2f}ms</b>\n"
        f"‣ Last Lookup : <b>{perf['lookup_last_ms']:.2f}ms</b>\n"
        f"‣ Peak Lookup : <b>{perf['lookup_max_ms']:.2f}ms</b>\n"
        f"‣ Cache Hit Rate : <b>{perf['lookup_hit_rate']:.2f}%</b>\n"
        f"‣ DB Ping : <b>{db_ping_ms:.2f}ms</b>\n"
        f"‣ Cache Size : <b>{len(RESULT_CACHE.data)}</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("gapprove"))
async def gapprove_handler(message: Message) -> None:
    asyncio.create_task(remember_chat(message))
    user_id = message.from_user.id if message.from_user else None
    if not await is_admin_user(user_id):
        return
    if not is_group_chat(message):
        await message.reply("/gapprove ကို approve လုပ်ချင်တဲ့ group ထဲမှာပဲ သုံးပါ")
        return
    await set_group_approval(message.chat, user_id, True)
    await message.reply(
        f"Group approved for auto media lookup: <b>{html_escape(clean_value(getattr(message.chat, 'title', '') or str(message.chat.id)))}</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("grmapprove"))
async def grmapprove_handler(message: Message) -> None:
    asyncio.create_task(remember_chat(message))
    user_id = message.from_user.id if message.from_user else None
    if not await is_admin_user(user_id):
        return
    if not is_group_chat(message):
        await message.reply("/grmapprove ကို group ထဲမှာပဲ သုံးပါ")
        return
    await set_group_approval(message.chat, user_id, False)
    await message.reply(
        f"Group auto media lookup removed: <b>{html_escape(clean_value(getattr(message.chat, 'title', '') or str(message.chat.id)))}</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("gstatus"))
async def gstatus_handler(message: Message) -> None:
    asyncio.create_task(remember_chat(message))
    if not is_group_chat(message):
        return
    approved = await is_group_approved(message.chat.id)
    await message.reply(
        f"Group auto lookup: <b>{'ON' if approved else 'OFF'}</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("addsudo"))
async def addsudo_handler(message: Message, command: CommandObject, bot: Bot) -> None:
    asyncio.create_task(remember_chat(message))
    user_id = message.from_user.id if message.from_user else None
    if not await is_owner_user(user_id):
        return
    target = await resolve_user_reference(message, bot, command.args)
    if not target:
        await message.reply("အသုံးပြုပုံ:\nReply + /addsudo\n/addsudo @username\n/addsudo 123456789")
        return
    await set_access(sudo_users, target, user_id, True)
    await message.reply(f"Sudo added: <b>{html_escape(format_target_user(target))}</b>", parse_mode=ParseMode.HTML)


@router.message(Command("rmsudo"))
async def rmsudo_handler(message: Message, command: CommandObject, bot: Bot) -> None:
    asyncio.create_task(remember_chat(message))
    user_id = message.from_user.id if message.from_user else None
    if not await is_owner_user(user_id):
        return
    target = await resolve_user_reference(message, bot, command.args)
    if not target:
        await message.reply("အသုံးပြုပုံ:\nReply + /rmsudo\n/rmsudo @username\n/rmsudo 123456789")
        return
    await set_access(sudo_users, target, user_id, False)
    await message.reply(f"Sudo removed: <b>{html_escape(format_target_user(target))}</b>", parse_mode=ParseMode.HTML)


@router.message(Command("blacklist"))
async def blacklist_handler(message: Message, command: CommandObject, bot: Bot) -> None:
    asyncio.create_task(remember_chat(message))
    user_id = message.from_user.id if message.from_user else None
    if not await is_owner_user(user_id):
        return
    target = await resolve_user_reference(message, bot, command.args)
    if not target:
        await message.reply("အသုံးပြုပုံ:\nReply + /blacklist\n/blacklist @username\n/blacklist 123456789")
        return
    await set_access(blacklisted_users, target, user_id, True)
    await message.reply(f"Blacklisted: <b>{html_escape(format_target_user(target))}</b>", parse_mode=ParseMode.HTML)


@router.message(Command("unblacklist"))
async def unblacklist_handler(message: Message, command: CommandObject, bot: Bot) -> None:
    asyncio.create_task(remember_chat(message))
    user_id = message.from_user.id if message.from_user else None
    if not await is_owner_user(user_id):
        return
    target = await resolve_user_reference(message, bot, command.args)
    if not target:
        await message.reply("အသုံးပြုပုံ:\nReply + /unblacklist\n/unblacklist @username\n/unblacklist 123456789")
        return
    await set_access(blacklisted_users, target, user_id, False)
    await message.reply(f"Unblacklisted: <b>{html_escape(format_target_user(target))}</b>", parse_mode=ParseMode.HTML)


@router.message(F.text.regexp(NAME_TRIGGER_RE))
async def name_trigger_handler(message: Message, bot: Bot) -> None:
    await handle_lookup_trigger(message, bot)


@router.message(F.text.regexp(WAIFU_TRIGGER_RE))
async def waifu_trigger_handler(message: Message, bot: Bot) -> None:
    await handle_lookup_trigger(message, bot)


@router.callback_query(F.data == FORCE_JOIN_VERIFY_CALLBACK)
async def force_join_verify_handler(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    if not user:
        await callback.answer()
        return
    cjoin, gjoin = await refresh_force_join_flags(bot, user.id)
    if cjoin and gjoin:
        await callback.message.edit_text(
            build_force_join_success_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=build_start_keyboard(),
            disable_web_page_preview=True,
        )
        await callback.answer("Verified")
        return
    await callback.message.edit_text(
        build_force_join_dm_text(cjoin, gjoin),
        parse_mode=ParseMode.HTML,
        reply_markup=build_force_join_dm_keyboard(),
        disable_web_page_preview=True,
    )
    await callback.answer("Join required", show_alert=False)


@router.message(F.photo | F.video)
async def media_handler(message: Message, bot: Bot) -> None:
    asyncio.create_task(remember_chat(message))
    media_type, _media = extract_media_handle(message)
    if not media_type:
        return

    if is_group_chat(message):
        if not await ensure_force_join_access(message, bot, group_prompt=False, dm_prompt=False):
            return

    if not await should_auto_reply_media_in_chat(message):
        return

    if is_private_chat(message) and not await ensure_force_join_access(message, bot, group_prompt=False, dm_prompt=True):
        return

    if not await can_use_lookup(message):
        await require_lookup_access(message)
        return

    parsed = get_effective_parsed_message(message)
    try:
        command_candidates = get_effective_command_candidates_for_message(message, parsed)
        await lookup_and_reply(message, message, bot, command_candidates=command_candidates)
    except Exception as exc:
        logger.exception("lookup failed")
        await message.reply(f"စစ်ဆေးရာမှာ error ဖြစ်နေပါတယ်: {exc}")

# -----------------------------------------------------
# Startup / shutdown
# -----------------------------------------------------
async def on_startup(bot: Bot) -> None:
    await ensure_indexes()
    await SNAPSHOT.refresh()
    asyncio.create_task(refresh_snapshot_loop())
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="status", description="Show bot status"),
            BotCommand(command="stats", description="Admin stats"),
            BotCommand(command="gapprove", description="Approve group auto lookup"),
        ]
    )
    me = await bot.get_me()
    logger.info("Bot started as @%s", me.username)
    logger.info("Support channel: %s", SUPPORT_CHANNEL_USERNAME or "none")
    logger.info("Support group: %s", SUPPORT_GROUP_USERNAME or "none")
    logger.info("Forward source username commands: %s", FORWARD_SOURCE_USERNAME_COMMAND_MAP or "none")
    logger.info("Forward source title commands: %s", FORWARD_SOURCE_TITLE_COMMAND_MAP or "none")
    logger.info("Snapshot refresh seconds: %s", SNAPSHOT_REFRESH_SECONDS)
    logger.info("Result cache: max=%s ttl=%ss", RESULT_CACHE_MAX_ITEMS, RESULT_CACHE_TTL_SECONDS)


async def on_shutdown(bot: Bot) -> None:
    try:
        await bot.session.close()
    finally:
        client.close()


def should_use_webhook() -> bool:
    if MODE == "webhook":
        return True
    if MODE == "polling":
        return False
    return USE_WEBHOOK_ENV == "true" or bool(PUBLIC_URL and WEBHOOK_SECRET)


async def health_handler(_request: web.Request) -> web.Response:
    perf = await PERF.snapshot()
    return web.json_response(
        {
            "ok": True,
            "mode": "webhook" if should_use_webhook() else "polling",
            "snapshot_items": SNAPSHOT.count,
            "snapshot_age_seconds": int(SNAPSHOT.age_seconds()),
            "cache_size": len(RESULT_CACHE.data),
            "latency_ms": round(perf["lookup_ema_ms"], 2),
        }
    )


async def run_polling() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await on_startup(bot)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        logger.exception("delete_webhook failed")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await on_shutdown(bot)


async def run_webhook() -> None:
    if not PUBLIC_URL:
        raise RuntimeError("PUBLIC_URL is required for webhook mode")
    if not WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET is required for webhook mode")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/healthz", health_handler)

    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET)
    webhook_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    await bot.set_webhook(
        url=f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=False,
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=HOST, port=PORT)
    await site.start()

    logger.info("Webhook mode enabled")
    logger.info("Public URL: %s", PUBLIC_URL)
    logger.info("Webhook path: %s", WEBHOOK_PATH)
    logger.info("Listening on %s:%s", HOST, PORT)

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()


async def main() -> None:
    if should_use_webhook():
        await run_webhook()
    else:
        await run_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
