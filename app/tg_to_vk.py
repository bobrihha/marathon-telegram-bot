"""Forward content from TG marathon group to VK community wall."""

import io
import logging
import re

import aiohttp
from aiogram import Bot, F, Router
from aiogram.types import Message

from .config import ADMIN_IDS, VK_TARGET_GROUP_ID, VK_TARGET_TOKEN

logger = logging.getLogger(__name__)
router = Router()


# ---------------------------------------------------------------------------
#  VK API helpers (use target community token)
# ---------------------------------------------------------------------------


async def _vk_api(method: str, **params) -> dict:
    """Call VK API with target community token."""
    params["access_token"] = VK_TARGET_TOKEN
    params["v"] = "5.199"
    url = f"https://api.vk.com/method/{method}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=params) as resp:
            result = await resp.json()
            if "error" in result:
                logger.error("VK API error %s: %s", method, result["error"])
            return result


async def _upload_photo_to_vk(photo_bytes: bytes) -> str | None:
    """Upload a photo to VK and return attachment string like 'photo123_456'."""
    # Step 1: get upload URL
    resp = await _vk_api(
        "photos.getWallUploadServer",
        group_id=VK_TARGET_GROUP_ID,
    )
    upload_url = resp.get("response", {}).get("upload_url")
    if not upload_url:
        logger.error("Failed to get VK upload URL: %s", resp)
        return None

    # Step 2: upload the photo
    form = aiohttp.FormData()
    form.add_field("photo", photo_bytes, filename="photo.jpg", content_type="image/jpeg")
    async with aiohttp.ClientSession() as session:
        async with session.post(upload_url, data=form) as upload_resp:
            upload_result = await upload_resp.json()

    if not upload_result.get("photo") or upload_result["photo"] == "[]":
        logger.error("VK photo upload failed: %s", upload_result)
        return None

    # Step 3: save the photo
    save_resp = await _vk_api(
        "photos.saveWallPhoto",
        group_id=VK_TARGET_GROUP_ID,
        photo=upload_result["photo"],
        server=upload_result["server"],
        hash=upload_result["hash"],
    )
    photos = save_resp.get("response", [])
    if not photos:
        logger.error("VK photo save failed: %s", save_resp)
        return None

    p = photos[0]
    return f"photo{p['owner_id']}_{p['id']}"


# ---------------------------------------------------------------------------
#  Topic name → hashtag
# ---------------------------------------------------------------------------


def _topic_to_hashtag(topic_name: str | None) -> str:
    """Convert topic name to a VK hashtag."""
    if not topic_name:
        return ""
    # Remove special chars, replace spaces with underscores
    clean = re.sub(r"[^\w\sа-яА-ЯёЁ]", "", topic_name).strip()
    clean = re.sub(r"\s+", "_", clean)
    if clean:
        return f"#{clean}"
    return ""


# ---------------------------------------------------------------------------
#  Main forwarding handler
# ---------------------------------------------------------------------------


# Cache topic names: message_thread_id → topic name
_topic_names: dict[int, str] = {}


@router.message(F.forum_topic_created)
async def on_topic_created(message: Message) -> None:
    """Cache topic name when a new forum topic is created."""
    if message.forum_topic_created:
        _topic_names[message.message_thread_id] = message.forum_topic_created.name
        logger.info("Cached topic %s: %s", message.message_thread_id, message.forum_topic_created.name)


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
)
async def forward_to_vk(message: Message, bot: Bot) -> None:
    """Forward admin messages from TG group to VK wall."""
    # Only forward if VK target is configured
    if not VK_TARGET_TOKEN or not VK_TARGET_GROUP_ID:
        return

    # Only forward messages from admins
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return

    # Skip service messages
    if message.forum_topic_created or message.forum_topic_closed or message.forum_topic_reopened:
        return
    if message.new_chat_members or message.left_chat_member:
        return

    # Build hashtag from topic
    topic_name = None
    if message.message_thread_id:
        topic_name = _topic_names.get(message.message_thread_id)
        # Try to get from message if not cached
        if not topic_name and message.reply_to_message and message.reply_to_message.forum_topic_created:
            topic_name = message.reply_to_message.forum_topic_created.name
            _topic_names[message.message_thread_id] = topic_name

    hashtag = _topic_to_hashtag(topic_name)

    # Get text content
    text = message.text or message.caption or ""
    if hashtag:
        text = f"{hashtag}\n\n{text}" if text else hashtag

    # Collect attachments
    attachments = []

    # Handle photos
    if message.photo:
        # Get the largest photo
        photo = message.photo[-1]
        try:
            file = await bot.get_file(photo.file_id)
            photo_data = await bot.download_file(file.file_path)
            photo_bytes = photo_data.read()
            attachment = await _upload_photo_to_vk(photo_bytes)
            if attachment:
                attachments.append(attachment)
        except Exception as e:
            logger.error("Failed to download/upload photo: %s", e)

    # Post to VK wall
    if not text and not attachments:
        return

    params = {
        "owner_id": -VK_TARGET_GROUP_ID,
        "from_group": 1,
        "message": text,
    }
    if attachments:
        params["attachments"] = ",".join(attachments)

    result = await _vk_api("wall.post", **params)
    if "response" in result:
        post_id = result["response"].get("post_id")
        logger.info("Posted to VK wall: post_id=%s, topic=%s", post_id, topic_name)
    else:
        logger.error("Failed to post to VK wall: %s", result)
