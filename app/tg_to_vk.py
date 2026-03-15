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


async def _upload_doc_to_vk(
    doc_bytes: bytes, filename: str, title: str | None = None
) -> str | None:
    """Upload a document/audio to VK and return attachment string like 'doc123_456'."""
    # Step 1: get upload URL
    resp = await _vk_api(
        "docs.getWallUploadServer",
        group_id=VK_TARGET_GROUP_ID,
    )
    upload_url = resp.get("response", {}).get("upload_url")
    if not upload_url:
        logger.error("Failed to get VK doc upload URL: %s", resp)
        return None

    # Step 2: upload the file
    form = aiohttp.FormData()
    form.add_field("file", doc_bytes, filename=filename)
    async with aiohttp.ClientSession() as session:
        async with session.post(upload_url, data=form) as upload_resp:
            upload_result = await upload_resp.json()

    file_field = upload_result.get("file")
    if not file_field:
        logger.error("VK doc upload failed: %s", upload_result)
        return None

    # Step 3: save the document
    save_params = {"file": file_field}
    if title:
        save_params["title"] = title
    save_resp = await _vk_api("docs.save", **save_params)
    doc_info = save_resp.get("response")
    if not doc_info:
        logger.error("VK doc save failed: %s", save_resp)
        return None

    # VK returns different structure for docs
    doc_type = doc_info.get("type")
    doc_obj = doc_info.get(doc_type, doc_info.get("doc"))
    if doc_obj:
        return f"doc{doc_obj['owner_id']}_{doc_obj['id']}"
    return None


async def _upload_video_to_vk(
    video_bytes: bytes, title: str = "Видео", description: str = ""
) -> str | None:
    """Upload a video to VK and return attachment string like 'video123_456'."""
    # Step 1: create video entry and get upload URL
    params = {
        "group_id": VK_TARGET_GROUP_ID,
        "name": title,
        "wallpost": 0,  # don't auto-post, we'll attach to our post
    }
    if description:
        params["description"] = description
    resp = await _vk_api("video.save", **params)
    video_info = resp.get("response")
    if not video_info or not video_info.get("upload_url"):
        logger.error("Failed to get VK video upload URL: %s", resp)
        return None

    upload_url = video_info["upload_url"]
    owner_id = video_info["owner_id"]
    video_id = video_info["video_id"]

    # Step 2: upload the video file
    form = aiohttp.FormData()
    form.add_field("video_file", video_bytes, filename="video.mp4", content_type="video/mp4")
    async with aiohttp.ClientSession() as session:
        async with session.post(upload_url, data=form) as upload_resp:
            upload_result = await upload_resp.json()

    if upload_result.get("size", 0) > 0 or upload_result.get("video_id"):
        return f"video{owner_id}_{video_id}"

    logger.error("VK video upload failed: %s", upload_result)
    return None


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

    # Handle documents (PDF, etc.)
    if message.document:
        try:
            file = await bot.get_file(message.document.file_id)
            doc_data = await bot.download_file(file.file_path)
            doc_bytes = doc_data.read()
            fname = message.document.file_name or "document"
            attachment = await _upload_doc_to_vk(doc_bytes, fname, fname)
            if attachment:
                attachments.append(attachment)
        except Exception as e:
            logger.error("Failed to download/upload document: %s", e)

    # Handle audio / voice
    audio_file = message.audio or message.voice
    if audio_file:
        try:
            file = await bot.get_file(audio_file.file_id)
            audio_data = await bot.download_file(file.file_path)
            audio_bytes = audio_data.read()
            fname = getattr(audio_file, "file_name", None) or "audio.ogg"
            attachment = await _upload_doc_to_vk(audio_bytes, fname, fname)
            if attachment:
                attachments.append(attachment)
        except Exception as e:
            logger.error("Failed to download/upload audio: %s", e)

    # Handle video
    if message.video:
        try:
            file = await bot.get_file(message.video.file_id)
            video_data = await bot.download_file(file.file_path)
            video_bytes = video_data.read()
            fname = message.video.file_name or "video.mp4"
            attachment = await _upload_video_to_vk(video_bytes, fname)
            if attachment:
                attachments.append(attachment)
        except Exception as e:
            logger.error("Failed to download/upload video: %s", e)
            text += "\n\n🎥 [видео — не удалось загрузить, см. оригинал в Telegram]"

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


# ---------------------------------------------------------------------------
#  Admin sends/forwards messages to bot in DM → post to VK
# ---------------------------------------------------------------------------


@router.message(
    F.chat.type == "private",
    # Match: forwarded messages OR messages with media (audio, doc, photo, video)
    F.forward_origin | F.audio | F.voice | F.document | F.photo | F.video,
)
async def handle_media_to_vk(message: Message, bot: Bot) -> None:
    """When admin sends or forwards a message with media to the bot, publish it to VK wall."""
    if not VK_TARGET_TOKEN or not VK_TARGET_GROUP_ID:
        return

    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return

    # Get text content
    text = message.text or message.caption or ""

    # Collect attachments
    attachments = []

    # Handle photos
    if message.photo:
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

    # Handle documents (PDF, etc.)
    if message.document:
        try:
            file = await bot.get_file(message.document.file_id)
            doc_data = await bot.download_file(file.file_path)
            doc_bytes = doc_data.read()
            fname = message.document.file_name or "document"
            attachment = await _upload_doc_to_vk(doc_bytes, fname, fname)
            if attachment:
                attachments.append(attachment)
        except Exception as e:
            logger.error("Failed to download/upload document: %s", e)

    # Handle audio / voice
    audio_file = message.audio or message.voice
    if audio_file:
        try:
            file = await bot.get_file(audio_file.file_id)
            audio_data = await bot.download_file(file.file_path)
            audio_bytes = audio_data.read()
            fname = getattr(audio_file, "file_name", None) or "audio.ogg"
            attachment = await _upload_doc_to_vk(audio_bytes, fname, fname)
            if attachment:
                attachments.append(attachment)
        except Exception as e:
            logger.error("Failed to download/upload audio: %s", e)

    # Handle video
    if message.video:
        try:
            file = await bot.get_file(message.video.file_id)
            video_data = await bot.download_file(file.file_path)
            video_bytes = video_data.read()
            fname = message.video.file_name or "video.mp4"
            attachment = await _upload_video_to_vk(video_bytes, fname)
            if attachment:
                attachments.append(attachment)
        except Exception as e:
            logger.error("Failed to download/upload video: %s", e)
            text += "\n\n🎥 [видео — не удалось загрузить, см. оригинал в Telegram]"

    if not text and not attachments:
        await message.reply("Не удалось извлечь контент из сообщения.")
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
        await message.reply(f"✅ Опубликовано на стене ВК (пост #{post_id})")
    else:
        error = result.get("error", {}).get("error_msg", "unknown")
        await message.reply(f"❌ Ошибка публикации: {error}")


