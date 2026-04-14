import asyncio
import hashlib
import logging
import unicodedata
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from typing import Any, Optional

import cv2
import imagehash
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
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
logger = logging.getLogger("hallow-match-bot")

# -----------------------------------------------------
# Database
# -----------------------------------------------------
client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]
items = db.items
approved_users = db.approved_users
sudo_users = db.sudo_users
known_users = db.known_users
user_modes = db.user_modes
settings_col = db.settings

# -----------------------------------------------------
# Helpers
# -----------------------------------------------------
NAME_PATTERNS = [
    re.compile(r"^[^\n\r]*?Character\s*Name\s*[:：﹕꞉]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?\bNAME\s*[:：﹕꞉]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?\bName\s*[:：﹕꞉]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
]

ANIME_PATTERNS = [
    re.compile(r"^[^\n\r]*?Anime\s*Name\s*[:：﹕꞉]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?Anime\s*[:：﹕꞉]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
]

RARITY_PATTERNS = [
    re.compile(r"^[^\n\r]*?Rarity\s*[:：﹕꞉]\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
]

CARD_ID_PATTERNS = [
    re.compile(r"^[^\n\r]*?ID\s*[:：﹕꞉]\s*([0-9]+)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[^\n\r]*?Id\s*[:：﹕꞉]\s*([0-9]+)\s*$", re.IGNORECASE | re.MULTILINE),
]

COMMAND_PATTERNS = [
    re.compile(r"(?:using|use|hint|full).*?/\s*([A-Za-z0-9_]+)\b", re.IGNORECASE | re.DOTALL),
    re.compile(r"/\s*([A-Za-z0-9_]+)\s*(?:\[[^\]]*name[^\]]*\]|\([^\)]*name[^\)]*\)|\bname\b)", re.IGNORECASE | re.DOTALL),
    re.compile(r"/\s*([A-Za-z0-9_]+)\b", re.IGNORECASE),
]

NAME_TRIGGER_RE = re.compile(r"^(?:\.name|/name)(?:@\w+)?$", re.IGNORECASE)

router = Router()


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


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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


def parse_command_name(text: str) -> Optional[str]:
    raw = unicodedata.normalize("NFKC", normalize_parse_text(text or ""))

    for pattern in COMMAND_PATTERNS:
        match = pattern.search(raw)
        if match:
            return clean_command_name("/" + match.group(1))

    return None 


def parse_caption_text(text: Optional[str]) -> ParsedText:
    raw = normalize_parse_text(text)
    return ParsedText(
        name=parse_field(raw, NAME_PATTERNS),
        anime_name=parse_field(raw, ANIME_PATTERNS),
        rarity=parse_field(raw, RARITY_PATTERNS),
        card_id=parse_field(raw, CARD_ID_PATTERNS),
        command_name=parse_command_name(raw),
        raw_text=raw,
    )


def collect_candidate_texts(message: Message) -> list[str]:
    candidates: list[str] = []

    for value in [
        getattr(message, "caption", None),
        getattr(message, "text", None),
    ]:
        value = normalize_parse_text(value)
        if value and value not in candidates:
            candidates.append(value)

    ext = getattr(message, "external_reply", None)
    if ext is not None:
        for value in [
            getattr(ext, "caption", None),
            getattr(ext, "text", None),
        ]:
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
        f"<b>NAME :</b> {html_escape(name)}",
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
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 Copy Hint",
                    copy_text=CopyTextButton(text=hint_cmd[:256]),
                ),
                InlineKeyboardButton(
                    text="📋 Copy Full",
                    copy_text=CopyTextButton(text=full_cmd[:256]),
                ),
            ]
        ]
    )


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

        targets = sorted(
            {
                max(0, int(frame_count * 0.2) - 1),
                max(0, int(frame_count * 0.5) - 1),
                max(0, int(frame_count * 0.8) - 1),
            }
        )

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


def hash_distance(a: str, b: str) -> int:
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def is_forwarded_message(message: Message) -> bool:
    return bool(
        getattr(message, "forward_origin", None)
        or getattr(message, "forward_from_chat", None)
        or getattr(message, "forward_from", None)
        or getattr(message, "forward_sender_name", None)
    )


def get_forward_source_info(message: Message) -> dict[str, Any]:
    info: dict[str, Any] = {
        "chat_id": None,
        "username": "",
        "title": "",
        "origin_type": "",
    }

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


def is_allowed_forward_source(message: Message) -> bool:
    if not is_forwarded_message(message):
        return False

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


async def ensure_indexes() -> None:
    await items.create_index("file_unique_id", unique=True, sparse=True)
    await items.create_index("sha256", unique=True, sparse=True)
    await items.create_index("media_type")
    await items.create_index("normalized_name")
    await items.create_index("created_at")

    await approved_users.create_index("user_id", unique=True)
    await sudo_users.create_index("user_id", unique=True)
    await known_users.create_index("user_id", unique=True)
    await known_users.create_index("username")
    await user_modes.create_index("user_id", unique=True)
    await settings_col.create_index("key", unique=True)


async def remember_user(message: Message) -> None:
    user = message.from_user
    if not user:
        return
    await known_users.update_one(
        {"user_id": user.id},
        {
            "$set": {
                "user_id": user.id,
                "username": (user.username or "").lower(),
                "full_name": clean_value(user.full_name),
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


async def is_sudo_user(user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    return await sudo_users.find_one({"user_id": user_id}) is not None


async def is_approved_user(user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    return await approved_users.find_one({"user_id": user_id}) is not None


async def set_global_mode(enabled: bool, updated_by: int) -> None:
    await settings_col.update_one(
        {"key": "global_mode"},
        {
            "$set": {
                "key": "global_mode",
                "enabled": enabled,
                "updated_by": updated_by,
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


async def get_global_mode() -> bool:
    row = await settings_col.find_one({"key": "global_mode"})
    return bool(row and row.get("enabled"))


async def is_allowed_user(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return False

    if user_id in OWNER_IDS:
        return True

    if await is_sudo_user(user_id):
        return True

    if await get_global_mode():
        return True

    if await is_approved_user(user_id):
        return True

    return False


async def can_save(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if not user_id:
        return False
    if user_id in OWNER_IDS:
        return True
    return await is_sudo_user(user_id)


async def require_access(message: Message) -> bool:
    if await is_allowed_user(message):
        return True

    global_mode = await get_global_mode()
    if global_mode:
        return True

    await message.reply(
        "ဒီ bot ကိုသုံးဖို့ owner approval လိုပါတယ်။\n"
        "Owner ကို Ledengary 1 card ပေးပြီးမှ သုံးလို့ရပါမယ်။"
    )
    return False


async def set_autosave_mode(user_id: int, enabled: bool) -> None:
    await user_modes.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "user_id": user_id,
                "autosave_enabled": enabled,
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


async def get_autosave_mode(user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    row = await user_modes.find_one({"user_id": user_id})
    return bool(row and row.get("autosave_enabled"))


async def find_match(meta: MediaMeta) -> Optional[dict[str, Any]]:
    exact = await items.find_one({"file_unique_id": meta.file_unique_id})
    if exact:
        exact["match_reason"] = "file_unique_id"
        return exact

    exact = await items.find_one({"sha256": meta.sha256})
    if exact:
        exact["match_reason"] = "sha256"
        return exact

    if meta.media_type == "photo" and meta.phash:
        best_item = None
        best_score = 10**9
        cursor = items.find({"media_type": "photo", "phash": {"$exists": True, "$ne": None}})
        async for row in cursor:
            distance = hash_distance(meta.phash, row["phash"])
            if distance < best_score:
                best_score = distance
                best_item = row
        if best_item and best_score <= PHOTO_PHASH_THRESHOLD:
            best_item["match_reason"] = f"phash:{best_score}"
            return best_item

    if meta.media_type == "video" and meta.frame_hashes:
        best_item = None
        best_avg = 10**9
        cursor = items.find({"media_type": "video", "frame_hashes": {"$exists": True, "$ne": []}})
        async for row in cursor:
            saved_hashes = row.get("frame_hashes") or []
            if not saved_hashes:
                continue
            distances = [hash_distance(a, b) for a, b in zip(meta.frame_hashes, saved_hashes)]
            if not distances:
                continue
            avg = sum(distances) / len(distances)
            peak = max(distances)
            if avg < best_avg and peak <= VIDEO_FRAME_THRESHOLD:
                best_avg = avg
                best_item = row
        if best_item and best_avg <= VIDEO_AVG_THRESHOLD:
            best_item["match_reason"] = f"video_hash:{best_avg:.2f}"
            return best_item

    return None


async def upsert_item(*, meta: MediaMeta, parsed: ParsedText, saved_by: int) -> tuple[dict[str, Any], bool]:
    command_name = clean_command_name(parsed.command_name or DEFAULT_COMMAND)
    doc = {
        "name": clean_value(parsed.name or ""),
        "normalized_name": normalize_name(parsed.name or ""),
        "anime_name": clean_value(parsed.anime_name or ""),
        "rarity": clean_value(parsed.rarity or ""),
        "card_id": clean_value(parsed.card_id or ""),
        "command_name": command_name,
        "raw_text": parsed.raw_text,
        "media_type": meta.media_type,
        "file_id": meta.file_id,
        "file_unique_id": meta.file_unique_id,
        "sha256": meta.sha256,
        "phash": meta.phash,
        "frame_hashes": meta.frame_hashes,
        "saved_by": saved_by,
        "updated_at": datetime.now(timezone.utc),
    }

    existing = await items.find_one(
        {"$or": [{"file_unique_id": meta.file_unique_id}, {"sha256": meta.sha256}]}
    )

    if existing:
        await items.update_one({"_id": existing["_id"]}, {"$set": doc})
        existing.update(doc)
        return existing, False

    doc["created_at"] = datetime.now(timezone.utc)
    result = await items.insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc, True


async def send_found_result(
    message: Message,
    item: dict[str, Any],
    override_command_name: Optional[str] = None,
) -> None:
    name = clean_value(item.get("name") or "Unknown")

    #nodbcmd
    command_name = clean_command_name(override_command_name or DEFAULT_COMMAND)

    text = build_result_text(item, command_name=command_name)
    keyboard = build_copy_keyboard(command_name, name)
    await message.reply(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )

async def send_not_found(message: Message) -> None:
    await message.reply(
        "Unknown!\n"
        "ဒီ Media Name ကို owner က save မလုပ်ရသေးတာ ဖြစ်နိုင်ပါတယ်။"
    )


async def lookup_and_reply(
    reply_message: Message,
    target_media_message: Message,
    bot: Bot,
    override_command_name: Optional[str] = None,
) -> None:
    meta = await get_media_meta(bot, target_media_message)
    matched = await find_match(meta)
    if matched:
        await send_found_result(reply_message, matched, override_command_name=override_command_name)
    else:
        await send_not_found(reply_message)


async def resolve_user_reference(message: Message, bot: Bot, raw_arg: Optional[str]) -> Optional[dict[str, Any]]:
    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        return {
            "user_id": target.id,
            "username": (target.username or "").lower(),
            "full_name": clean_value(target.full_name),
        }

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
            return {
                "user_id": chat.id,
                "username": (getattr(chat, "username", "") or "").lower(),
                "full_name": clean_value(getattr(chat, "full_name", "") or ""),
            }
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
            {
                "$set": {
                    "user_id": user_doc["user_id"],
                    "username": user_doc.get("username", ""),
                    "full_name": user_doc.get("full_name", ""),
                    "updated_at": datetime.now(timezone.utc),
                    "updated_by": added_by,
                }
            },
            upsert=True,
        )
    else:
        await collection.delete_one({"user_id": user_doc["user_id"]})


# -----------------------------------------------------
# Commands
# -----------------------------------------------------
@router.message(Command("start"))
async def start_handler(message: Message) -> None:
    await remember_user(message)
    if await is_allowed_user(message):
        await message.reply(
            "ဒီ bot က photo/video post တွေကို match စစ်ပြီး name ပြန်ထုတ်ပေးပါတယ်。\n\n"
            "• Approved user: media ကို forward / upload လုပ်တာနဲ့ lookup လုပ်ပေးမယ်\n"
            "• Group: media ကို reply ထောက်ပြီး .name or /name နဲ့မေးလို့ရမယ်\n"
            "• Owner/Sudo: DM ထဲ /autosave on လုပ်ပြီး New post တွေကို Name save/update လုပ်လို့ရမယ်\n"
            "• Owner/Sudo: /save နဲ့ ပုံ+Name ကို manual save လည်း လုပ်လို့ရပါတယ်"
        )
        return

    global_mode = await get_global_mode()
    if global_mode:
        await message.reply("Global mode ON ဖြစ်နေပါတယ်။ ဘယ်သူမဆို သုံးလို့ရပါတယ်။")
        return

    await message.reply(
        "ဒီ bot ကိုသုံးဖို့ approval လိုပါတယ်。\n"
        "Owner ကို Legendary 1 card ပေးပြီး approve လုပ်ခိုင်းပါ။"
    )


@router.message(Command("global"))
async def global_handler(message: Message, command: CommandObject) -> None:
    await remember_user(message)

    if not message.from_user or message.from_user.id not in OWNER_IDS:
        return

    arg = clean_value(command.args or "").lower()

    if arg not in {"on", "off", "status"}:
        enabled = await get_global_mode()
        await message.reply(
            "အသုံးပြုပုံ:\n"
            "/global on\n"
            "/global off\n"
            "/global status\n\n"
            f"Current: <b>{'ON' if enabled else 'OFF'}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if arg == "status":
        enabled = await get_global_mode()
        await message.reply(
            f"Global mode: <b>{'ON' if enabled else 'OFF'}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    enabled = arg == "on"
    await set_global_mode(enabled, message.from_user.id)

    await message.reply(
        f"Global mode: <b>{'ON' if enabled else 'OFF'}</b>\n"
        f"{'အခု ဘယ်သူမဆို bot ကို သုံးလို့ရပါပြီ။' if enabled else 'အခု approve ထားတဲ့သူတွေပဲ bot ကို သုံးလို့ရပါမယ်။'}",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("autosave"))
async def autosave_handler(message: Message, command: CommandObject) -> None:
    await remember_user(message)

    if not is_private_chat(message):
        await message.reply("ဒီ command ကို DM/private chat ထဲမှာပဲ သုံးပါ။")
        return

    if not await can_save(message):
        return

    arg = clean_value(command.args or "").lower()
    if arg not in {"on", "off", "status"}:
        await message.reply("အသုံးပြုပုံ:\n/autosave on\n/autosave off\n/autosave status")
        return

    user_id = message.from_user.id

    if arg == "status":
        enabled = await get_autosave_mode(user_id)
        await message.reply(
            f"Auto-save mode: <b>{'ON' if enabled else 'OFF'}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    enabled = arg == "on"
    await set_autosave_mode(user_id, enabled)

    await message.reply(
        f"Auto-save mode: <b>{'ON' if enabled else 'OFF'}</b>\n"
        f"{'Forwarded post တွေကို save/update ပဲလုပ်ပါမယ်။' if enabled else 'Normal lookup mode ပြန်ဝင်ပါပြီ။'}",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("stats"))
async def stats_handler(message: Message) -> None:
    await remember_user(message)
    if not message.from_user or message.from_user.id not in OWNER_IDS:
        return

    total = await items.count_documents({})
    photos = await items.count_documents({"media_type": "photo"})
    videos = await items.count_documents({"media_type": "video"})
    approved = await approved_users.count_documents({})
    sudos = await sudo_users.count_documents({})
    global_mode = await get_global_mode()

    await message.reply(
        f"Total saved: <b>{total}</b>\n"
        f"Photos: <b>{photos}</b>\n"
        f"Videos: <b>{videos}</b>\n"
        f"Approved users: <b>{approved}</b>\n"
        f"Sudo users: <b>{sudos}</b>\n"
        f"Global mode: <b>{'ON' if global_mode else 'OFF'}</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("approve"))
async def approve_handler(message: Message, command: CommandObject, bot: Bot) -> None:
    await remember_user(message)
    if not message.from_user or message.from_user.id not in OWNER_IDS:
        return

    target = await resolve_user_reference(message, bot, command.args)
    if not target:
        await message.reply("အသုံးပြုပုံ:\nReply + /approve\n/approve @username\n/approve 123456789")
        return

    await set_access(approved_users, target, message.from_user.id, True)
    await message.reply(
        f"Approved: <b>{html_escape(format_target_user(target))}</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("addsudo"))
async def addsudo_handler(message: Message, command: CommandObject, bot: Bot) -> None:
    await remember_user(message)
    if not message.from_user or message.from_user.id not in OWNER_IDS:
        return

    target = await resolve_user_reference(message, bot, command.args)
    if not target:
        await message.reply("အသုံးပြုပုံ:\nReply + /addsudo\n/addsudo @username\n/addsudo 123456789")
        return

    await set_access(sudo_users, target, message.from_user.id, True)
    await set_access(approved_users, target, message.from_user.id, True)
    await message.reply(
        f"Sudo added: <b>{html_escape(format_target_user(target))}</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("rmsudo"))
async def rmsudo_handler(message: Message, command: CommandObject, bot: Bot) -> None:
    await remember_user(message)
    if not message.from_user or message.from_user.id not in OWNER_IDS:
        return

    target = await resolve_user_reference(message, bot, command.args)
    if not target:
        await message.reply("အသုံးပြုပုံ:\nReply + /rmsudo\n/rmsudo @username\n/rmsudo 123456789")
        return

    await set_access(sudo_users, target, message.from_user.id, False)
    await message.reply(
        f"Sudo removed: <b>{html_escape(format_target_user(target))}</b>",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("save"))
async def save_handler(message: Message, command: CommandObject, bot: Bot) -> None:
    await remember_user(message)
    if not await can_save(message):
        return

    target = message.reply_to_message or message
    media_type, _media = extract_media_handle(target)
    if not media_type:
        await message.reply("/save ကို media message ကို reply ပြီးသုံးပါ")
        return

    parsed = parse_caption_text_from_message(target)
    if command.args:
        parsed.name = clean_value(command.args)

    if not parsed.name:
        await message.reply("name မတွေ့ပါ。\nအသုံးပြုပုံ: replied media ပေါ်မှာ /save Nahida")
        return

    try:
        meta = await get_media_meta(bot, target)
        doc, created = await upsert_item(meta=meta, parsed=parsed, saved_by=message.from_user.id)
    except Exception as exc:
        logger.exception("save failed")
        await message.reply(f"save မအောင်မြင်ပါ: {exc}")
        return

    status = "Saved" if created else "Updated"
    await message.reply(
        f"{status}: <b>{html_escape(doc['name'])}</b>\n"
        f"Type: <b>{doc['media_type']}</b>\n"
        f"Cmd: <code>{html_escape(doc['command_name'])}</code>",
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text.regexp(NAME_TRIGGER_RE))
async def name_trigger_handler(message: Message, bot: Bot) -> None:
    await remember_user(message)

    if not await is_allowed_user(message):
        await require_access(message)
        return

    target = message.reply_to_message
    if not target:
        await message.reply("media message ကို reply ထောက်ပြီး .name သို့ /name သုံးပါ")
        return

    media_type, _media = extract_media_handle(target)
    if not media_type:
        await message.reply("photo/video message ကို reply ထောက်ပြီး .name သို့ /name သုံးပါ")
        return

    parsed_target = parse_caption_text_from_message(target)
    override_cmd = parsed_target.command_name

    try:
        await lookup_and_reply(message, target, bot, override_command_name=override_cmd)
    except Exception as exc:
        logger.exception("reply lookup failed")
        await message.reply(f"စစ်ဆေးရာမှာ error ဖြစ်နေပါတယ်: {exc}")


# -----------------------------------------------------
# Media messages
# -----------------------------------------------------
@router.message(F.photo | F.video)
async def media_handler(message: Message, bot: Bot) -> None:
    await remember_user(message)

    media_type, _media = extract_media_handle(message)
    if not media_type:
        return

    user_can_use = await is_allowed_user(message)
    user_can_save = await can_save(message)
    user_id = message.from_user.id if message.from_user else None
    autosave_enabled = await get_autosave_mode(user_id)

    parsed = parse_caption_text_from_message(message)

    # DM autosave mode: owner/sudo + /autosave on + forwarded message only
    if is_private_chat(message) and user_can_save and autosave_enabled and is_forwarded_message(message):
        if not is_allowed_forward_source(message):
            await message.reply("ဒီ forwarded source ကို auto-save ခွင့်မပြုထားသေးပါဘူး။")
            return

        if not parsed.name:
            await message.reply("name မတွေ့ပါ။ Character Name line ပါတဲ့ post ကို forward လုပ်ပါ။")
            return

        try:
            meta = await get_media_meta(bot, message)
            doc, created = await upsert_item(meta=meta, parsed=parsed, saved_by=user_id)

            status = "Saved" if created else "Updated"
            source_info = get_forward_source_info(message)
            source_label = (
                source_info.get("title")
                or source_info.get("username")
                or str(source_info.get("chat_id") or "forwarded source")
            )

            await message.reply(
                f"{status}: <b>{html_escape(doc['name'])}</b>\n"
                f"Mode: <b>auto-save</b>\n"
                f"Source: <b>{html_escape(str(source_label))}</b>\n"
                f"Cmd: <code>{html_escape(doc['command_name'])}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception as exc:
            logger.exception("auto-save failed")
            await message.reply(f"auto-save error: {exc}")
            return

    if not user_can_use:
        await require_access(message)
        return

    try:
        override_cmd = parsed.command_name
        await lookup_and_reply(message, message, bot, override_command_name=override_cmd)
    except Exception as exc:
        logger.exception("lookup failed")
        await message.reply(f"စစ်ဆေးရာမှာ error ဖြစ်နေပါတယ်: {exc}")


# -----------------------------------------------------
# Main
# -----------------------------------------------------
async def on_startup(bot: Bot) -> None:
    await ensure_indexes()
    me = await bot.get_me()
    logger.info("Bot started as @%s", me.username)
    logger.info("Configured source ids: %s", sorted(SOURCE_CHANNEL_IDS) if SOURCE_CHANNEL_IDS else "none")
    logger.info("Configured source usernames: %s", sorted(SOURCE_CHANNEL_USERNAMES) if SOURCE_CHANNEL_USERNAMES else "none")
    logger.info("Configured source titles: %s", sorted(SOURCE_CHANNEL_TITLES) if SOURCE_CHANNEL_TITLES else "none")


async def main() -> None:
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await on_startup(bot)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
