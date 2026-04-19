"""Forward content from TG marathon group to VK community wall."""

import asyncio
import logging
import mimetypes
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import aiohttp
from aiogram import Bot, F, Router
from aiogram.types import Message

from .config import (
    ADMIN_IDS,
    VK_TARGET_GROUP_ID,
    VK_TARGET_MEDIA_TOKEN,
    VK_TARGET_TOKEN,
    VK_TARGET_VIDEO_TOKEN,
)

logger = logging.getLogger(__name__)
router = Router()
_vk_docs_peer_id: int | None = None
VK_DOC_UPLOAD_LIMIT_BYTES = 12 * 1024 * 1024


# ---------------------------------------------------------------------------
#  VK API helpers
# ---------------------------------------------------------------------------


async def _vk_api(method: str, token: str | None = None, **params) -> dict:
    """Call VK API with given token (defaults to VK_TARGET_TOKEN)."""
    params["access_token"] = token or VK_TARGET_TOKEN
    params["v"] = "5.199"
    url = f"https://api.vk.com/method/{method}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=params) as resp:
            result = await resp.json()
            if "error" in result:
                logger.error("VK API error %s: %s", method, result["error"])
            return result


def _guess_content_type(filename: str, default: str = "application/octet-stream") -> str:
    """Guess MIME type for upload."""
    return mimetypes.guess_type(filename)[0] or default


def _append_note(text: str, note: str) -> str:
    """Append a human-readable note to post text."""
    return f"{text}\n\n{note}" if text else note


def _build_attachment(prefix: str, obj: dict) -> str:
    """Build VK attachment reference, preserving access_key when present."""
    attachment = f"{prefix}{obj['owner_id']}_{obj['id']}"
    access_key = obj.get("access_key")
    if access_key:
        attachment = f"{attachment}_{access_key}"
    return attachment


async def _upload_to_vk(
    upload_url: str,
    field_name: str,
    file_bytes: bytes,
    filename: str,
    content_type: str | None = None,
) -> dict | None:
    """Upload raw bytes to a VK upload server."""
    for attempt in range(1, 4):
        form = aiohttp.FormData()
        form.add_field(
            field_name,
            file_bytes,
            filename=filename,
            content_type=content_type or _guess_content_type(filename),
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(upload_url, data=form) as upload_resp:
                    if upload_resp.status >= 500 and attempt < 3:
                        logger.warning(
                            "VK upload HTTP %s for %s on attempt %s, retrying",
                            upload_resp.status,
                            filename,
                            attempt,
                        )
                        await asyncio.sleep(attempt)
                        continue
                    if upload_resp.status >= 400:
                        logger.error("VK upload HTTP %s for %s", upload_resp.status, filename)
                        return None
                    return await upload_resp.json(content_type=None)
        except aiohttp.ClientError as e:
            if attempt < 3:
                logger.warning("VK upload client error for %s on attempt %s: %s", filename, attempt, e)
                await asyncio.sleep(attempt)
                continue
            logger.error("VK upload client error for %s: %s", filename, e)
            return None

    return None


async def _get_vk_docs_peer_id() -> int | None:
    """Pick a stable peer_id for document uploads via VK messages API."""
    global _vk_docs_peer_id

    if _vk_docs_peer_id:
        return _vk_docs_peer_id

    resp = await _vk_api("messages.getConversations", token=VK_TARGET_TOKEN, count=20)
    items = resp.get("response", {}).get("items", [])
    if not items:
        logger.error("No VK conversations available for document uploads: %s", resp)
        return None

    for item in items:
        peer = item.get("conversation", {}).get("peer", {})
        if peer.get("type") == "chat" and peer.get("id"):
            _vk_docs_peer_id = peer["id"]
            return _vk_docs_peer_id

    peer = items[0].get("conversation", {}).get("peer", {})
    _vk_docs_peer_id = peer.get("id")
    return _vk_docs_peer_id


def _compress_video_for_vk(video_bytes: bytes, filename: str) -> tuple[bytes, str] | None:
    """Compress video to fit VK doc upload constraints."""
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg is not installed, cannot compress video %s", filename)
        return None

    output_name = f"{Path(filename).stem}_vk.mp4"
    attempts = [
        ("min(1280\\,iw):-2", "24", "24", "128k"),
        ("min(960\\,iw):-2", "24", "26", "128k"),
        ("min(854\\,iw):-2", "24", "28", "96k"),
        ("min(720\\,iw):-2", "22", "30", "96k"),
        ("min(640\\,iw):-2", "20", "32", "64k"),
    ]

    with tempfile.TemporaryDirectory(prefix="vk_video_") as tmpdir:
        input_path = Path(tmpdir) / filename
        output_path = Path(tmpdir) / output_name
        input_path.write_bytes(video_bytes)

        for scale, fps, crf, audio_bitrate in attempts:
            if output_path.exists():
                output_path.unlink()

            cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(input_path),
                "-vf",
                f"scale={scale}",
                "-r",
                fps,
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                crf,
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-c:a",
                "aac",
                "-b:a",
                audio_bitrate,
                str(output_path),
            ]

            try:
                subprocess.run(cmd, check=True, timeout=300)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                logger.warning("ffmpeg compression failed for %s: %s", filename, e)
                continue

            compressed_bytes = output_path.read_bytes()
            logger.info(
                "Compressed %s from %s bytes to %s bytes with scale=%s fps=%s crf=%s",
                filename,
                len(video_bytes),
                len(compressed_bytes),
                scale,
                fps,
                crf,
            )
            if len(compressed_bytes) <= VK_DOC_UPLOAD_LIMIT_BYTES:
                return compressed_bytes, output_name

    logger.warning(
        "Failed to compress %s under VK doc limit (%s bytes)",
        filename,
        VK_DOC_UPLOAD_LIMIT_BYTES,
    )
    return None


async def _upload_photo_to_vk(photo_bytes: bytes) -> str | None:
    """Upload a photo for wall.post and return VK attachment string."""
    media_token = VK_TARGET_MEDIA_TOKEN or VK_TARGET_TOKEN
    resp = await _vk_api(
        "photos.getWallUploadServer",
        token=media_token,
        group_id=VK_TARGET_GROUP_ID,
    )
    upload_url = resp.get("response", {}).get("upload_url")
    if not upload_url:
        logger.warning(
            "Falling back to doc upload for photo because wall photo upload is unavailable: %s",
            resp,
        )
        return await _upload_doc_to_vk(photo_bytes, "photo.jpg", "photo")

    upload_result = await _upload_to_vk(
        upload_url,
        "photo",
        photo_bytes,
        "photo.jpg",
        content_type="image/jpeg",
    )
    if not upload_result:
        return None
    if not upload_result.get("photo") or upload_result["photo"] == "[]":
        logger.error("VK photo upload failed: %s", upload_result)
        return None

    save_resp = await _vk_api(
        "photos.saveWallPhoto",
        token=media_token,
        group_id=VK_TARGET_GROUP_ID,
        photo=upload_result["photo"],
        server=upload_result["server"],
        hash=upload_result["hash"],
    )
    photos = save_resp.get("response", [])
    if not photos:
        logger.warning("Falling back to doc upload for photo after save failure: %s", save_resp)
        return await _upload_doc_to_vk(photo_bytes, "photo.jpg", "photo")

    p = photos[0]
    return _build_attachment("photo", p)


async def _upload_doc_to_vk(
    doc_bytes: bytes, filename: str, title: str | None = None
) -> str | None:
    """Upload a document/audio for wall.post."""
    for attempt in range(1, 4):
        wall_resp = await _vk_api(
            "docs.getWallUploadServer",
            token=VK_TARGET_TOKEN,
            group_id=VK_TARGET_GROUP_ID,
        )
        upload_url = wall_resp.get("response", {}).get("upload_url")
        if not upload_url:
            peer_id = await _get_vk_docs_peer_id()
            if not peer_id:
                logger.error("Failed to find VK peer_id for doc upload fallback")
                return None
            logger.warning(
                "Falling back to docs.getMessagesUploadServer for %s because wall upload is unavailable: %s",
                filename,
                wall_resp,
            )
            messages_resp = await _vk_api(
                "docs.getMessagesUploadServer",
                token=VK_TARGET_TOKEN,
                type="doc",
                peer_id=peer_id,
            )
            upload_url = messages_resp.get("response", {}).get("upload_url")
            if not upload_url:
                logger.error("Failed to get VK doc upload URL: %s", messages_resp)
                return None

        upload_result = await _upload_to_vk(upload_url, "file", doc_bytes, filename)
        if not upload_result:
            if attempt < 3:
                logger.warning("Retrying full VK doc upload cycle for %s after empty upload result", filename)
                await asyncio.sleep(attempt)
                continue
            return None

        file_field = upload_result.get("file")
        if not file_field:
            logger.error("VK doc upload failed: %s", upload_result)
            if attempt < 3:
                await asyncio.sleep(attempt)
                continue
            return None

        save_params = {}
        if title:
            save_params["title"] = title
        save_resp = await _vk_api("docs.save", token=VK_TARGET_TOKEN, file=file_field, **save_params)
        doc_info = save_resp.get("response")
        if not doc_info:
            logger.error("VK doc save failed: %s", save_resp)
            if attempt < 3:
                await asyncio.sleep(attempt)
                continue
            return None

        if isinstance(doc_info, list):
            doc_info = doc_info[0] if doc_info else None
        if not isinstance(doc_info, dict):
            logger.error("VK doc save returned unexpected payload: %s", save_resp)
            if attempt < 3:
                await asyncio.sleep(attempt)
                continue
            return None

        doc_type = doc_info.get("type")
        doc_obj = doc_info.get(doc_type, doc_info.get("doc"))
        if not doc_obj and {"owner_id", "id"} <= doc_info.keys():
            doc_obj = doc_info
        if doc_obj:
            return _build_attachment("doc", doc_obj)

    return None


async def _upload_video_to_vk(
    video_bytes: bytes,
    filename: str = "video.mp4",
    title: str = "Видео",
    description: str = "",
) -> str | None:
    """Upload a video for wall.post.

    VK rejects video.save with a community token, so we fall back to a
    document attachment unless an explicit user video token is configured.
    """
    if not VK_TARGET_VIDEO_TOKEN:
        upload_bytes = video_bytes
        upload_name = filename
        if len(upload_bytes) > VK_DOC_UPLOAD_LIMIT_BYTES:
            compressed = _compress_video_for_vk(upload_bytes, filename)
            if compressed:
                upload_bytes, upload_name = compressed
            else:
                logger.warning(
                    "Video %s is %s bytes and could not be compressed under %s bytes",
                    filename,
                    len(video_bytes),
                    VK_DOC_UPLOAD_LIMIT_BYTES,
                )
        return await _upload_doc_to_vk(upload_bytes, upload_name, title)

    video_token = VK_TARGET_VIDEO_TOKEN
    params = {
        "group_id": VK_TARGET_GROUP_ID,
        "name": title,
        "wallpost": 0,
    }
    if description:
        params["description"] = description
    resp = await _vk_api("video.save", token=video_token, **params)
    video_info = resp.get("response")
    if not video_info or not video_info.get("upload_url"):
        logger.warning(
            "Falling back to doc upload for video %s because video.save is unavailable: %s",
            filename,
            resp,
        )
        return await _upload_doc_to_vk(video_bytes, filename, title)

    upload_result = await _upload_to_vk(
        video_info["upload_url"],
        "video_file",
        video_bytes,
        filename,
        content_type=_guess_content_type(filename, "video/mp4"),
    )
    if not upload_result:
        logger.warning("Falling back to doc upload for video %s after empty upload result", filename)
        return await _upload_doc_to_vk(video_bytes, filename, title)
    if upload_result.get("error"):
        logger.warning(
            "Falling back to doc upload for video %s after VK video upload error: %s",
            filename,
            upload_result,
        )
        return await _upload_doc_to_vk(video_bytes, filename, title)

    if upload_result.get("size", 0) > 0 or upload_result.get("video_id") or upload_result.get("owner_id"):
        return _build_attachment(
            "video",
            {
                "owner_id": video_info["owner_id"],
                "id": video_info["video_id"],
                "access_key": video_info.get("access_key"),
            },
        )

    logger.warning(
        "Falling back to doc upload for video %s after unexpected payload: %s",
        filename,
        upload_result,
    )
    return await _upload_doc_to_vk(video_bytes, filename, title)


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


async def _prepare_vk_post(message: Message, bot: Bot, hashtag: str = "") -> tuple[str, list[str]]:
    """Convert a Telegram message into VK wall text and attachments."""
    text = message.text or message.caption or ""
    if hashtag:
        text = f"{hashtag}\n\n{text}" if text else hashtag

    attachments: list[str] = []
    fallback_labels: list[str] = []

    if message.photo:
        photo = message.photo[-1]
        try:
            file = await bot.get_file(photo.file_id)
            photo_data = await bot.download_file(file.file_path)
            photo_bytes = photo_data.read()
            attachment = await _upload_photo_to_vk(photo_bytes)
            if attachment:
                attachments.append(attachment)
                if attachment.startswith("doc"):
                    fallback_labels.append("🖼 Фото")
            else:
                text = _append_note(text, "🖼 Фото не удалось загрузить в VK.")
        except Exception as e:
            logger.error("Failed to download/upload photo: %s", e)
            text = _append_note(text, "🖼 Фото не удалось загрузить в VK.")

    if message.document:
        try:
            file = await bot.get_file(message.document.file_id)
            doc_data = await bot.download_file(file.file_path)
            doc_bytes = doc_data.read()
            fname = message.document.file_name or "document"
            attachment = await _upload_doc_to_vk(doc_bytes, fname, fname)
            if attachment:
                attachments.append(attachment)
                fallback_labels.append(f"📎 {fname}")
            else:
                text = _append_note(text, f"📎 Файл: {fname}")
        except Exception as e:
            logger.error("Failed to upload document: %s", e)
            text = _append_note(text, f"📎 Файл: {message.document.file_name or 'document'}")

    audio_file = message.audio or message.voice
    if audio_file:
        try:
            file = await bot.get_file(audio_file.file_id)
            audio_data = await bot.download_file(file.file_path)
            audio_bytes = audio_data.read()
            original_name = getattr(audio_file, "file_name", None) or "audio.ogg"
            upload_name = (
                original_name.rsplit(".", 1)[0] + ".dat"
                if "." in original_name
                else f"{original_name}.dat"
            )
            attachment = await _upload_doc_to_vk(audio_bytes, upload_name, original_name)
            if attachment:
                attachments.append(attachment)
                fallback_labels.append(f"🎵 {original_name}")
            else:
                text = _append_note(text, f"🎵 Аудио: {original_name}")
        except Exception as e:
            logger.error("Failed to upload audio: %s", e)
            text = _append_note(text, "🎵 Аудио не удалось загрузить в VK.")

    if message.video:
        try:
            file = await bot.get_file(message.video.file_id)
            video_data = await bot.download_file(file.file_path)
            video_bytes = video_data.read()
            filename = message.video.file_name or "video.mp4"
            attachment = await _upload_video_to_vk(video_bytes, filename=filename, title=filename)
            if attachment:
                attachments.append(attachment)
                if attachment.startswith("doc"):
                    fallback_labels.append(f"🎥 {filename}")
            else:
                text = _append_note(text, "🎥 Видео не удалось загрузить в VK.")
        except Exception as e:
            logger.error("Failed to download/upload video: %s", e)
            text = _append_note(text, "🎥 Видео не удалось загрузить в VK.")

    if not text and fallback_labels:
        text = "\n".join(fallback_labels)

    return text, attachments


async def _publish_to_vk_wall(text: str, attachments: list[str]) -> dict | None:
    """Publish prepared text and attachments to the target VK wall."""
    if not text and not attachments:
        logger.warning("Skipping VK wall post because message has no text and no attachments")
        return None

    params = {
        "owner_id": -VK_TARGET_GROUP_ID,
        "from_group": 1,
        "message": text,
    }
    if attachments:
        params["attachments"] = ",".join(attachments)
    return await _vk_api("wall.post", token=VK_TARGET_TOKEN, **params)


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

    text, attachments = await _prepare_vk_post(message, bot, hashtag=hashtag)
    result = await _publish_to_vk_wall(text, attachments)
    if result is None:
        return

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

    text, attachments = await _prepare_vk_post(message, bot)
    result = await _publish_to_vk_wall(text, attachments)
    if result is None:
        await message.reply("Не удалось извлечь контент из сообщения.")
        return

    if "response" in result:
        post_id = result["response"].get("post_id")
        await message.reply(f"✅ Опубликовано на стене ВК (пост #{post_id})")
    else:
        error = result.get("error", {}).get("error_msg", "unknown")
        await message.reply(f"❌ Ошибка публикации: {error}")
