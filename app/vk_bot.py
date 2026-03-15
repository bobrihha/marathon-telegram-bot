"""VK Community Bot — payment verification & group access."""

import json
import logging
from datetime import datetime

from aiohttp import web
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .config import (
    ADMIN_IDS,
    VK_COMMUNITY_TOKEN,
    VK_CONFIRMATION_STRING,
    VK_GROUP_ID,
    VK_SECRET,
)
from .db.dal import SessionLocal
from .db.models import AccessLog, Payment, User, VkGroup

import aiohttp

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  VK API helpers
# ---------------------------------------------------------------------------


async def vk_api(method: str, **params) -> dict:
    """Call VK API method."""
    params["access_token"] = VK_COMMUNITY_TOKEN
    params["v"] = "5.199"
    url = f"https://api.vk.com/method/{method}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=params) as resp:
            result = await resp.json()
            if "error" in result:
                logger.error("VK API error %s: %s", method, result["error"])
            return result


async def vk_send_message(user_id: int, message: str, keyboard: dict | None = None) -> None:
    """Send a message to a VK user."""
    import random

    params = {
        "user_id": user_id,
        "message": message,
        "random_id": random.randint(1, 2**31),
    }
    if keyboard:
        params["keyboard"] = json.dumps(keyboard)
    await vk_api("messages.send", **params)


async def vk_invite_to_group(user_id: int, group_id: int) -> bool:
    """Invite a user to a VK group. Returns True on success."""
    result = await vk_api("groups.invite", group_id=group_id, user_id=user_id)
    if "response" in result and result["response"] == 1:
        return True
    logger.warning("Failed to invite user %s to group %s: %s", user_id, group_id, result)
    return False


# ---------------------------------------------------------------------------
#  Keyboards
# ---------------------------------------------------------------------------


def main_keyboard() -> dict:
    """Main keyboard with action buttons."""
    return {
        "one_time": False,
        "buttons": [
            [
                {
                    "action": {"type": "text", "label": "Проверить оплату"},
                    "color": "primary",
                }
            ],
            [
                {
                    "action": {"type": "text", "label": "Поддержка"},
                    "color": "default",
                }
            ],
        ],
    }


# ---------------------------------------------------------------------------
#  Phone / email helpers (same logic as TG bot)
# ---------------------------------------------------------------------------


def normalize_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def phone_variants(value: str) -> tuple[str | None, str | None]:
    digits = normalize_phone(value)
    if not digits:
        return None, None
    last10 = digits[-10:] if len(digits) >= 10 else digits
    return digits, last10


# ---------------------------------------------------------------------------
#  Core payment check logic
# ---------------------------------------------------------------------------


async def handle_payment_check(vk_user_id: int, text: str) -> None:
    """Check payment by email/phone/order_id and grant access."""
    db: Session = SessionLocal()
    try:
        phone, phone_last10 = phone_variants(text)
        if "@" in text:
            email_lower = text.lower()
            payment = (
                db.query(Payment)
                .filter(
                    func.lower(Payment.email) == email_lower,
                    Payment.status == "paid",
                    Payment.used.is_(False),
                )
                .order_by(Payment.created_at.desc(), Payment.id.desc())
                .first()
            )
            used_payment = (
                db.query(Payment)
                .filter(func.lower(Payment.email) == email_lower, Payment.status == "paid")
                .order_by(Payment.created_at.desc(), Payment.id.desc())
                .first()
            )
        else:
            filters = [Payment.order_id == text]
            if phone:
                filters.append(Payment.phone == phone)
            if phone_last10 and len(phone_last10) >= 10:
                filters.append(Payment.phone.endswith(phone_last10))
            payment = (
                db.query(Payment)
                .filter(Payment.status == "paid")
                .filter(or_(*filters))
                .order_by(Payment.created_at.desc(), Payment.id.desc())
                .first()
            )
            used_payment = (
                db.query(Payment)
                .filter(Payment.status == "paid")
                .filter(or_(*filters))
                .order_by(Payment.created_at.desc(), Payment.id.desc())
                .first()
            )

        vk_id_str = str(vk_user_id)

        if not payment:
            if used_payment:
                existing_user = db.query(User).filter(User.payment_id == used_payment.id).first()
                if existing_user and existing_user.vk_id == vk_id_str:
                    payment = used_payment
                else:
                    await vk_send_message(
                        vk_user_id,
                        "Эта оплата уже использована другим пользователем.\n"
                        "Если вы оплатили новый поток, укажите новый email/телефон "
                        "или напишите в поддержку.",
                        keyboard=main_keyboard(),
                    )
                    return

            if not payment:
                await vk_send_message(
                    vk_user_id,
                    "Я не нашёл оплаченный заказ по этим данным.\n"
                    "Проверь, правильно ли ты ввёл данные, или напиши в поддержку.",
                    keyboard=main_keyboard(),
                )
                return

        # Check if payment is linked to another VK user
        existing_user = db.query(User).filter(User.payment_id == payment.id).first()
        if existing_user and existing_user.vk_id and existing_user.vk_id != vk_id_str:
            await vk_send_message(
                vk_user_id,
                "Эта оплата уже использована с другим ВК-аккаунтом.\n"
                "Если это ошибка, напиши в поддержку.",
                keyboard=main_keyboard(),
            )
            return

        # Find or create user
        user = db.query(User).filter(User.vk_id == vk_id_str).first()
        if not user:
            # Check if there's a user with same payment (linked via TG)
            if existing_user:
                existing_user.vk_id = vk_id_str
                user = existing_user
            else:
                user = User(
                    vk_id=vk_id_str,
                    payment_id=payment.id,
                )
                db.add(user)
        else:
            user.payment_id = payment.id

        db.commit()

        # Try to get VK group info
        vk_group = db.query(VkGroup).order_by(VkGroup.id.desc()).first()
        if not vk_group:
            await vk_send_message(
                vk_user_id,
                "Оплата подтверждена ✅\n"
                "Но пока не настроена ВК-группа для выдачи доступа.\n"
                "Свяжись с администратором марафона.",
                keyboard=main_keyboard(),
            )
            return

        # Try to invite user to the group
        target_group_id = int(vk_group.vk_group_id)
        invited = await vk_invite_to_group(vk_user_id, target_group_id)

        if invited:
            # Mark payment as used
            payment.used = True

            log = AccessLog(
                telegram_id=None,
                email=payment.email,
                order_id=payment.order_id,
                group_name=vk_group.group_name,
                group_id=vk_group.vk_group_id,
                action="granted_vk",
                timestamp=datetime.utcnow(),
                comment=f"VK invite sent to vk_id={vk_id_str}",
            )
            db.add(log)
            db.commit()

            await vk_send_message(
                vk_user_id,
                f"Оплата найдена ✅\n\n"
                f"Группа: {vk_group.group_name}\n"
                "Я отправил тебе приглашение в группу! "
                "Проверь уведомления ВК 👆",
                keyboard=main_keyboard(),
            )
        else:
            # If invite failed, try sending the link
            if vk_group.invite_link:
                await vk_send_message(
                    vk_user_id,
                    f"Оплата найдена ✅\n\n"
                    f"Группа: {vk_group.group_name}\n"
                    f"Вступи в группу по ссылке: {vk_group.invite_link}",
                    keyboard=main_keyboard(),
                )
            else:
                await vk_send_message(
                    vk_user_id,
                    "Оплата найдена ✅\n"
                    "Но не удалось отправить приглашение. "
                    "Напиши в поддержку, и мы добавим тебя вручную.",
                    keyboard=main_keyboard(),
                )
    finally:
        db.close()


# ---------------------------------------------------------------------------
#  VK Callback API handler
# ---------------------------------------------------------------------------


async def handle_vk_callback(request: web.Request) -> web.Response:
    """Handle VK Callback API events."""
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="bad request", status=400)

    event_type = data.get("type")
    group_id = data.get("group_id")

    # Validate secret
    if VK_SECRET and data.get("secret") != VK_SECRET:
        logger.warning("VK callback: invalid secret")
        return web.Response(text="bad secret", status=403)

    # Confirmation request
    if event_type == "confirmation" and group_id == VK_GROUP_ID:
        return web.Response(text=VK_CONFIRMATION_STRING)

    # New message
    if event_type == "message_new":
        obj = data.get("object", {})
        message = obj.get("message", obj)  # v5.199 wraps in "message"
        vk_user_id = message.get("from_id") or message.get("user_id")
        text = (message.get("text") or "").strip()

        if vk_user_id and text:
            await _process_message(vk_user_id, text)

    return web.Response(text="ok")


async def _process_message(vk_user_id: int, text: str) -> None:
    """Route incoming VK messages."""
    text_lower = text.lower()

    if text_lower in {"начать", "start", "привет", "здравствуйте"}:
        await vk_send_message(
            vk_user_id,
            "Привет! Я бот марафона 🏃‍♀️\n\n"
            "Я выдаю доступ в закрытую ВК-группу после оплаты.\n"
            "Нажми кнопку «Проверить оплату» и отправь email или "
            "номер телефона, который ты указал(а) при оплате.",
            keyboard=main_keyboard(),
        )
    elif text_lower == "проверить оплату":
        await vk_send_message(
            vk_user_id,
            "Введи email или номер телефона, который ты указал(а) при оплате.",
            keyboard=main_keyboard(),
        )
    elif text_lower == "поддержка":
        await _handle_support(vk_user_id)
    else:
        # Treat as payment data (email, phone, order_id)
        await handle_payment_check(vk_user_id, text)


async def _handle_support(vk_user_id: int) -> None:
    """Send support info and notify admins that user needs help."""
    # Get VK user info for the support message
    result = await vk_api("users.get", user_ids=vk_user_id)
    users = result.get("response", [])
    if users:
        user_info = users[0]
        name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}"
        user_label = f"{name} (vk.com/id{vk_user_id})"
    else:
        user_label = f"VK ID {vk_user_id}"

    await vk_send_message(
        vk_user_id,
        "Напиши свой вопрос одним сообщением — я передам администратору.\n"
        "Или напиши напрямую администратору марафона.",
        keyboard=main_keyboard(),
    )


def register_vk_routes(app: web.Application) -> None:
    """Register VK Callback API route on the aiohttp app."""
    app.router.add_post("/webhooks/vk", handle_vk_callback)
    app.router.add_post("/webhooks/vk/{token}", handle_vk_callback)
