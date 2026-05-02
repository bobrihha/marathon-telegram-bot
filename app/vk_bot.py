"""VK Community Bot — payment verification & group access."""

import json
import logging
import secrets
from datetime import datetime

from aiohttp import web
from aiogram import Bot
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .config import (
    ADMIN_IDS,
    BOT_TOKEN,
    VK_COMMUNITY_TOKEN,
    VK_CONFIRMATION_STRING,
    VK_CALLBACK_URL,
    VK_GROUP_ID,
    VK_SECRET,
)
from .db.dal import SessionLocal
from .db.models import AccessLog, CurrentGroup, Payment, User, VkGroup

import aiohttp

logger = logging.getLogger(__name__)
_PENDING_CALLBACK_CONFIRMATIONS: dict[str, str] = {}


# ---------------------------------------------------------------------------
#  VK API helpers
# ---------------------------------------------------------------------------


async def vk_api(method: str, access_token: str | None = None, **params) -> dict:
    """Call VK API method."""
    token = access_token or VK_COMMUNITY_TOKEN
    if not token:
        return {"error": {"error_msg": "VK token is not configured"}}
    params["access_token"] = token
    params["v"] = "5.199"
    url = f"https://api.vk.com/method/{method}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=params) as resp:
            result = await resp.json()
            logger.info("VK API response for %s: %s", method, result)
            if "error" in result:
                logger.error("VK API error %s: %s", method, result["error"])
            return result


async def vk_send_message(
    user_id: int,
    message: str,
    keyboard: dict | None = None,
    access_token: str | None = None,
) -> None:
    """Send a message to a VK user."""
    import random

    params = {
        "user_id": user_id,
        "message": message,
        "random_id": random.randint(1, 2**31),
    }
    if keyboard:
        params["keyboard"] = json.dumps(keyboard)
    await vk_api("messages.send", access_token=access_token, **params)


async def vk_invite_to_group(user_id: int, group_id: int, access_token: str | None = None) -> bool:
    """Invite a user to a VK group. Returns True on success."""
    result = await vk_api(
        "groups.invite",
        access_token=access_token,
        group_id=group_id,
        user_id=user_id,
    )
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


def _vk_token(vk_group: VkGroup | None = None) -> str | None:
    if vk_group and vk_group.access_token:
        return vk_group.access_token
    return VK_COMMUNITY_TOKEN or None


def _normalize_group_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.lstrip("-")


def find_vk_group_by_callback(db: Session, group_id: object) -> VkGroup | None:
    group_id_text = _normalize_group_id(group_id)
    if not group_id_text:
        return None
    return (
        db.query(VkGroup)
        .filter(VkGroup.vk_group_id == group_id_text)
        .order_by(VkGroup.id.desc())
        .first()
    )


def select_vk_group_for_payment(db: Session, payment: Payment) -> VkGroup | None:
    product = (payment.product_name or "").lower()
    vk_group = None
    if product:
        vk_group = (
            db.query(VkGroup)
            .filter(VkGroup.product_tag == product)
            .order_by(VkGroup.id.desc())
            .first()
        )
    if not vk_group:
        vk_group = (
            db.query(VkGroup)
            .filter(VkGroup.product_tag.is_(None))
            .order_by(VkGroup.id.desc())
            .first()
        )
    if not vk_group:
        vk_group = db.query(VkGroup).order_by(VkGroup.id.desc()).first()
    return vk_group


def select_tg_group_for_payment(db: Session, payment: Payment) -> CurrentGroup | None:
    product = (payment.product_name or "").lower()
    tg_group = None
    if product:
        tg_group = (
            db.query(CurrentGroup)
            .filter(CurrentGroup.product_tag == product)
            .order_by(CurrentGroup.id.desc())
            .first()
        )
    if not tg_group:
        tg_group = (
            db.query(CurrentGroup)
            .filter(CurrentGroup.product_tag.is_(None))
            .order_by(CurrentGroup.id.desc())
            .first()
        )
    if not tg_group:
        tg_group = db.query(CurrentGroup).order_by(CurrentGroup.id.desc()).first()
    return tg_group


def _vk_group_matches_payment(db: Session, vk_group: VkGroup | None, payment: Payment) -> bool:
    if not vk_group:
        return False
    expected = select_vk_group_for_payment(db, payment)
    return bool(expected and expected.id == vk_group.id)


def vk_message_url(vk_group: VkGroup) -> str:
    group_id = _normalize_group_id(vk_group.vk_group_id)
    if group_id and group_id != "chat":
        return f"https://vk.com/im?sel=-{group_id}"
    return vk_group.invite_link or ""


async def _tg_invite_link_for_vk_access(
    tg_group: CurrentGroup,
    payment: Payment,
    vk_user_id: int,
) -> str:
    if not tg_group.chat_id:
        return tg_group.invite_link or ""
    if not BOT_TOKEN:
        return tg_group.invite_link or ""
    tg_bot = Bot(token=BOT_TOKEN)
    try:
        invite = await tg_bot.create_chat_invite_link(
            chat_id=int(tg_group.chat_id),
            name=f"vk-{vk_user_id}-{payment.order_id or payment.id}",
            member_limit=1,
            creates_join_request=False,
        )
        return invite.invite_link
    except Exception as e:
        logger.warning("Cannot create one-use TG invite link: %s", e)
        return tg_group.invite_link or ""
    finally:
        await tg_bot.session.close()


def _first_vk_group(payload: dict) -> dict | None:
    response = payload.get("response")
    if isinstance(response, list) and response:
        first = response[0]
        return first if isinstance(first, dict) else None
    if isinstance(response, dict):
        groups = response.get("groups")
        if isinstance(groups, list) and groups:
            first = groups[0]
            return first if isinstance(first, dict) else None
        return response
    return None


async def get_vk_group_info(access_token: str) -> dict | None:
    """Return VK community info for a community token when VK allows it."""
    result = await vk_api("groups.getById", access_token=access_token)
    if "error" in result:
        logger.warning("Cannot get VK group info from token: %s", result["error"])
        return None
    return _first_vk_group(result)


async def configure_vk_callback_server(
    access_token: str,
    group_id: str,
    title: str,
    callback_url: str | None = None,
) -> dict:
    """Create a VK Callback API server and enable message/join events."""
    url = callback_url or VK_CALLBACK_URL
    secret_key = secrets.token_urlsafe(24)

    confirmation_result = await vk_api(
        "groups.getCallbackConfirmationCode",
        access_token=access_token,
        group_id=group_id,
    )
    if "error" in confirmation_result:
        raise RuntimeError(
            f"VK confirmation code error: {confirmation_result['error'].get('error_msg')}"
        )
    confirmation = str(confirmation_result.get("response", {}).get("code", ""))
    if not confirmation:
        raise RuntimeError("VK did not return a callback confirmation code")
    _PENDING_CALLBACK_CONFIRMATIONS[str(group_id)] = confirmation

    add_result = await vk_api(
        "groups.addCallbackServer",
        access_token=access_token,
        group_id=group_id,
        url=url,
        title=title,
        secret_key=secret_key,
    )
    if "error" in add_result:
        raise RuntimeError(
            f"VK callback server error: {add_result['error'].get('error_msg')}"
        )

    server_id = str(add_result.get("response", {}).get("server_id", ""))
    if not server_id:
        raise RuntimeError("VK did not return callback server_id")

    settings_result = await vk_api(
        "groups.setCallbackSettings",
        access_token=access_token,
        group_id=group_id,
        server_id=server_id,
        api_version="5.199",
        message_new=1,
        group_join=1,
        group_leave=1,
    )
    if "error" in settings_result:
        raise RuntimeError(
            f"VK callback settings error: {settings_result['error'].get('error_msg')}"
        )

    return {
        "server_id": server_id,
        "secret": secret_key,
        "confirmation": confirmation,
        "url": url,
    }


# ---------------------------------------------------------------------------
#  Core payment check logic
# ---------------------------------------------------------------------------


async def handle_payment_check(
    vk_user_id: int,
    text: str,
    source_group: VkGroup | None = None,
) -> None:
    """Check payment by email/phone/order_id and grant access."""
    db: Session = SessionLocal()
    access_token = _vk_token(source_group)
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
                    # Same VK user — allow re-access
                    payment = used_payment
                else:
                    await vk_send_message(
                        vk_user_id,
                        "Эта оплата уже использована другим пользователем.\n"
                        "Каждая оплата даёт доступ одному человеку.\n"
                        "Если это ошибка — напиши в поддержку.",
                        keyboard=main_keyboard(),
                        access_token=access_token,
                    )
                    return

            if not payment:
                await vk_send_message(
                    vk_user_id,
                    "Я не нашёл оплаченный заказ по этим данным.\n"
                    "Проверь, правильно ли ты ввёл данные, или напиши в поддержку.",
                    keyboard=main_keyboard(),
                    access_token=access_token,
                )
                return

        # Check if payment is linked to another VK user
        existing_user = db.query(User).filter(User.payment_id == payment.id).first()
        if existing_user and existing_user.vk_id != vk_id_str:
            await vk_send_message(
                vk_user_id,
                "Эта оплата уже использована с другим ВК-аккаунтом.\n"
                "Если это ошибка, напиши в поддержку.",
                keyboard=main_keyboard(),
                access_token=access_token,
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
            if existing_user and existing_user.id != user.id and not existing_user.vk_id:
                if existing_user.telegram_id and not user.telegram_id:
                    existing_telegram_id = existing_user.telegram_id
                    existing_username = existing_user.username
                    existing_full_name = existing_user.full_name
                    existing_user.telegram_id = None
                    existing_user.username = None
                    existing_user.full_name = None
                    db.flush()
                    user.telegram_id = existing_telegram_id
                    user.username = existing_username
                    user.full_name = existing_full_name
                existing_user.payment_id = None
            user.payment_id = payment.id

        payment.used = True
        db.commit()

        # Try to get VK group/chat info — match by product
        vk_group = select_vk_group_for_payment(db, payment)
        if not vk_group or not vk_group.invite_link:
            await vk_send_message(
                vk_user_id,
                "Оплата подтверждена ✅\n"
                "Но пока не настроен ВК-чат для выдачи доступа.\n"
                "Свяжись с администратором марафона.",
                keyboard=main_keyboard(),
                access_token=access_token,
            )
            return

        # Log access
        log = AccessLog(
            telegram_id=None,
            email=payment.email,
            order_id=payment.order_id,
            group_name=vk_group.group_name,
            group_id=vk_group.vk_group_id,
            action="granted_vk",
            timestamp=datetime.utcnow(),
            comment=f"VK chat link sent to vk_id={vk_id_str}",
        )
        db.add(log)
        db.commit()

        # Also find matching TG group to send both links
        tg_group = select_tg_group_for_payment(db, payment)

        tg_info = ""
        if tg_group and tg_group.invite_link:
            tg_invite_link = await _tg_invite_link_for_vk_access(
                tg_group=tg_group,
                payment=payment,
                vk_user_id=vk_user_id,
            )
            if tg_invite_link:
                tg_info = f"\n\nТелеграм-группа: {tg_group.group_name}\nСсылка: {tg_invite_link}"

        await vk_send_message(
            vk_user_id,
            f"Оплата найдена ✅\n\n"
            f"ВК-чат марафона: {vk_group.group_name}\n"
            f"Вступай по ссылке 👇\n{vk_group.invite_link}"
            f"{tg_info}",
            keyboard=main_keyboard(),
            access_token=access_token,
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
    logger.info("VK callback received: event_type=%s, group_id=%s, data=%s", event_type, group_id, data)

    db: Session = SessionLocal()
    try:
        source_group = find_vk_group_by_callback(db, group_id)
    finally:
        db.close()

    # Confirmation request — VK sends this WITHOUT secret, must check first
    if event_type == "confirmation":
        confirmation = (
            source_group.callback_confirmation
            if source_group and source_group.callback_confirmation
            else _PENDING_CALLBACK_CONFIRMATIONS.get(
                _normalize_group_id(group_id) or ""
            )
            or VK_CONFIRMATION_STRING
        )
        logger.info("Returning confirmation string for VK group %s", group_id)
        return web.Response(text=confirmation)

    # Validate secret (only for non-confirmation events)
    expected_secret = (
        source_group.callback_secret
        if source_group and source_group.callback_secret
        else VK_SECRET
    )
    if expected_secret and data.get("secret") != expected_secret:
        logger.warning("VK callback: invalid secret")
        return web.Response(text="bad secret", status=403)

    # New message
    if event_type == "message_new":
        obj = data.get("object", {})
        message = obj.get("message", obj)  # v5.199 wraps in "message"
        vk_user_id = message.get("from_id") or message.get("user_id")
        text = (message.get("text") or "").strip()

        if vk_user_id and text:
            await _process_message(vk_user_id, text, source_group=source_group)

    # Someone joined the VK group — check if they are approved
    elif event_type == "group_join":
        obj = data.get("object", {})
        vk_user_id = obj.get("user_id")
        join_type = (obj.get("join_type") or obj.get("type") or "").lower()
        if vk_user_id:
            if join_type == "request":
                await _handle_group_join_request(vk_user_id, source_group, group_id)
            else:
                await _check_group_join(vk_user_id, source_group, group_id)

    return web.Response(text="ok")


async def _process_message(
    vk_user_id: int,
    text: str,
    source_group: VkGroup | None = None,
) -> None:
    """Route incoming VK messages."""
    logger.info("Processing VK message from user %s: '%s'", vk_user_id, text)
    text_lower = text.lower()
    access_token = _vk_token(source_group)

    if text_lower in {"начать", "start", "привет", "здравствуйте"}:
        logger.info("Sending greeting to VK user %s", vk_user_id)
        await vk_send_message(
            vk_user_id,
            "Привет! Я бот марафона 🏃‍♀️\n\n"
            "Я выдаю доступ в закрытую ВК-группу после оплаты.\n"
            "Нажми кнопку «Проверить оплату» и отправь email или "
            "номер телефона, который ты указал(а) при оплате.",
            keyboard=main_keyboard(),
            access_token=access_token,
        )
    elif text_lower == "проверить оплату":
        await vk_send_message(
            vk_user_id,
            "Введи email или номер телефона, который ты указал(а) при оплате.",
            keyboard=main_keyboard(),
            access_token=access_token,
        )
    elif text_lower == "поддержка":
        await _handle_support(vk_user_id, source_group=source_group)
    else:
        # Treat as payment data (email, phone, order_id)
        await handle_payment_check(vk_user_id, text, source_group=source_group)


async def _handle_support(vk_user_id: int, source_group: VkGroup | None = None) -> None:
    """Send support info and notify admins that user needs help."""
    access_token = _vk_token(source_group)
    # Get VK user info for the support message
    result = await vk_api("users.get", access_token=access_token, user_ids=vk_user_id)
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
        access_token=access_token,
    )


async def _approve_vk_request(
    vk_user_id: int,
    vk_group: VkGroup | None,
    callback_group_id: object,
) -> bool:
    group_id = _normalize_group_id(callback_group_id or (vk_group.vk_group_id if vk_group else None))
    access_token = _vk_token(vk_group)
    if not group_id or not access_token:
        logger.warning("Cannot approve VK request: group_id/token missing")
        return False
    result = await vk_api(
        "groups.approveRequest",
        access_token=access_token,
        group_id=group_id,
        user_id=vk_user_id,
    )
    return "response" in result and result["response"] == 1


async def _remove_vk_user(
    vk_user_id: int,
    vk_group: VkGroup | None,
    callback_group_id: object,
) -> bool:
    group_id = _normalize_group_id(callback_group_id or (vk_group.vk_group_id if vk_group else None))
    access_token = _vk_token(vk_group)
    if not group_id or not access_token:
        logger.warning("Cannot remove VK user/request: group_id/token missing")
        return False
    result = await vk_api(
        "groups.removeUser",
        access_token=access_token,
        group_id=group_id,
        user_id=vk_user_id,
    )
    return "response" in result and result["response"] == 1


async def _handle_group_join_request(
    vk_user_id: int,
    source_group: VkGroup | None,
    callback_group_id: object,
) -> None:
    """Approve a closed-community VK join request only for paid users."""
    db: Session = SessionLocal()
    try:
        vk_id_str = str(vk_user_id)
        user = db.query(User).filter(User.vk_id == vk_id_str).first()
        payment = user.payment if user and user.payment_id else None

        if payment and payment.status == "paid" and (
            not source_group or _vk_group_matches_payment(db, source_group, payment)
        ):
            approved = await _approve_vk_request(vk_user_id, source_group, callback_group_id)
            if approved:
                log = AccessLog(
                    telegram_id=user.telegram_id if user else None,
                    email=payment.email,
                    order_id=payment.order_id,
                    group_name=source_group.group_name if source_group else "VK group",
                    group_id=_normalize_group_id(callback_group_id) or "",
                    action="approved_vk_request",
                    timestamp=datetime.utcnow(),
                    comment=f"VK join request approved for vk_id={vk_id_str}",
                )
                db.add(log)
                db.commit()
                logger.info("VK join request approved for user %s", vk_id_str)
            return

        removed = await _remove_vk_user(vk_user_id, source_group, callback_group_id)
        result_text = "заявка отклонена" if removed else "заявка не одобрена"
        await _notify_tg_admins(
            f"Заявка в ВК без подтвержденной оплаты\n\n"
            f"vk.com/id{vk_user_id}\n"
            f"Результат: {result_text}."
        )
    except Exception as e:
        logger.exception("Error handling VK join request: %s", e)
    finally:
        db.close()


async def _check_group_join(
    vk_user_id: int,
    source_group: VkGroup | None = None,
    callback_group_id: object = None,
) -> None:
    """Check if a new VK group member has a valid payment. Alert admins if not."""
    db: Session = SessionLocal()
    try:
        vk_id_str = str(vk_user_id)
        user = db.query(User).filter(User.vk_id == vk_id_str).first()

        if user and user.payment_id:
            payment = user.payment
            if payment and payment.status == "paid" and (
                not source_group or _vk_group_matches_payment(db, source_group, payment)
            ):
                logger.info("VK group_join: user %s is approved (order %s)", vk_id_str, payment.order_id)
                return

        # Stranger! Get their info and alert admins
        result = await vk_api("users.get", user_ids=vk_user_id)
        users_data = result.get("response", [])
        if users_data:
            info = users_data[0]
            name = f"{info.get('first_name', '')} {info.get('last_name', '')}"
            user_label = f"{name}\nvk.com/id{vk_user_id}"
        else:
            user_label = f"VK ID {vk_user_id}"

        logger.warning("VK group_join: STRANGER detected — %s", user_label)
        removed = await _remove_vk_user(vk_user_id, source_group, callback_group_id)
        action = "Удалён из ВК-группы." if removed else "Проверь вручную: удалить через API не удалось."

        # Notify TG admins
        await _notify_tg_admins(
            f"⚠️ Чужак вступил в ВК-группу!\n\n"
            f"{user_label}\n\n"
            f"Этот пользователь НЕ проверял оплату через бота.\n"
            f"Возможно, кто-то поделился ссылкой.\n\n"
            f"{action}"
        )
    except Exception as e:
        logger.exception("Error checking group_join: %s", e)
    finally:
        db.close()


async def _notify_tg_admins(text: str) -> None:
    """Send a notification to all TG admins via the Telegram bot."""
    if not ADMIN_IDS:
        return
    try:
        from .main import bot as tg_bot
        for admin_id in ADMIN_IDS:
            try:
                await tg_bot.send_message(admin_id, text)
            except Exception as e:
                logger.error("Failed to notify TG admin %s: %s", admin_id, e)
    except Exception as e:
        logger.error("Failed to import TG bot for admin notification: %s", e)


def get_approved_vk_ids() -> list[dict]:
    """Get list of approved VK users (who got links) for admin audit."""
    db: Session = SessionLocal()
    try:
        users = (
            db.query(User)
            .filter(User.vk_id.isnot(None))
            .all()
        )
        result = []
        for user in users:
            payment = user.payment
            result.append({
                "vk_id": user.vk_id,
                "telegram_id": user.telegram_id,
                "email": payment.email if payment else None,
                "phone": payment.phone if payment else None,
                "order_id": payment.order_id if payment else None,
            })
        return result
    finally:
        db.close()


def register_vk_routes(app: web.Application) -> None:
    """Register VK Callback API route on the aiohttp app."""
    app.router.add_post("/webhooks/vk", handle_vk_callback)
    app.router.add_post("/webhooks/vk/{token}", handle_vk_callback)
