"""VK Community Bot — payment verification & group access."""

import asyncio
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
    VK_APP_ID,
    VK_APP_SECRET,
    VK_CALLBACK_URL,
    VK_COMMUNITY_TOKEN,
    VK_CONFIRMATION_STRING,
    VK_GROUP_ID,
    VK_SECRET,
    VK_USER_DIRECT_OK,
    VK_USER_PROXY,
)
from .db.dal import SessionLocal
from .db.models import (
    AccessLog,
    CurrentGroup,
    Payment,
    User,
    VkAdminAuth,
    VkGroup,
    VkJoinRequest,
)

import aiohttp

logger = logging.getLogger(__name__)
_PENDING_CALLBACK_CONFIRMATIONS: dict[str, str] = {}
_PENDING_SUPPORT: set[int] = set()  # VK user_ids waiting to send support message
# Recently processed VK callback event_ids to dedupe retries the bot already
# answered with "ok" (belt-and-suspenders against any leftover retry storms).
_RECENT_VK_EVENT_IDS: dict[str, float] = {}
_VK_EVENT_DEDUP_TTL = 300.0  # seconds


def _vk_event_already_seen(event_id: str | None) -> bool:
    """Return True if this event_id was seen recently (and refresh the cache)."""
    if not event_id:
        return False
    now = asyncio.get_event_loop().time()
    # Drop expired entries
    expired = [k for k, ts in _RECENT_VK_EVENT_IDS.items() if now - ts > _VK_EVENT_DEDUP_TTL]
    for k in expired:
        _RECENT_VK_EVENT_IDS.pop(k, None)
    if event_id in _RECENT_VK_EVENT_IDS:
        return True
    _RECENT_VK_EVENT_IDS[event_id] = now
    return False


def _spawn_vk_task(coro) -> None:
    """Run a VK event handler in the background and never let exceptions escape."""
    async def _runner():
        try:
            await coro
        except Exception:
            logger.exception("VK event handler crashed")

    asyncio.create_task(_runner())


# ---------------------------------------------------------------------------
#  VK API helpers
# ---------------------------------------------------------------------------


async def vk_api(
    method: str,
    access_token: str | None = None,
    proxy: str | None = None,
    timeout: float | None = None,
    **params,
) -> dict:
    """Call VK API method. ``proxy`` (http://user:pass@host:port) routes the
    request through a proxy — used to make personal-token calls originate from a
    Russian IP so VK doesn't flag the admin account as hijacked."""
    token = access_token or VK_COMMUNITY_TOKEN
    if not token:
        return {"error": {"error_msg": "VK token is not configured"}}
    params["access_token"] = token
    params["v"] = "5.199"
    url = f"https://api.vk.com/method/{method}"
    session_kwargs = {}
    if timeout:
        session_kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(**session_kwargs) as session:
        async with session.post(url, data=params, proxy=proxy) as resp:
            result = await resp.json()
            logger.info("VK API response for %s: %s", method, result)
            if "error" in result:
                logger.error("VK API error %s: %s", method, result["error"])
            return result


def _vk_user_proxies() -> list[str]:
    """Parse VK_USER_PROXY (a single URL or a comma-separated list of URLs)."""
    return [p.strip() for p in VK_USER_PROXY.split(",") if p.strip()]


async def _vk_user_api(method: str, **params) -> dict:
    """Call a VK method that REQUIRES the admin's personal (USER) token.

    VK treats a user token used from a foreign IP as an account hijack and
    blocks the admin's personal page. Such calls must originate from a Russian
    IP, so we route them through ``VK_USER_PROXY`` (one or more RU-exit proxy
    URLs). Residential proxy nodes flake out, so we fail over across the
    configured proxies until one returns a real VK response.

    If no proxy is configured (and direct is not explicitly allowed via
    ``VK_USER_DIRECT_OK``), the call is SKIPPED to protect the admin's account.
    """
    import random

    admin_token = _get_vk_admin_token()
    if not admin_token:
        logger.warning("User-token VK call %s skipped: no admin token", method)
        return {"error": {"error_code": 5, "error_msg": "no admin token"}}

    proxies = _vk_user_proxies()
    if not proxies:
        if VK_USER_DIRECT_OK:
            return await vk_api(method, access_token=admin_token, **params)
        logger.warning(
            "User-token VK call %s PAUSED: VK_USER_PROXY not set "
            "(protecting admin account from foreign-IP block)",
            method,
        )
        return {
            "error": {
                "error_code": -1,
                "error_msg": "user-token calls paused: no RU proxy configured",
            }
        }

    # Spread load and avoid always retrying a dead node first.
    random.shuffle(proxies)
    last_err: Exception | None = None
    for proxy in proxies:
        try:
            return await vk_api(
                method,
                access_token=admin_token,
                proxy=proxy,
                timeout=25,
                **params,
            )
        except Exception as e:  # proxy node down / 503 / timeout — try next
            last_err = e
            logger.warning(
                "VK user call %s via proxy failed (%s); trying next node", method, e
            )
    logger.error("VK user call %s: all %d proxies failed (last: %s)", method, len(proxies), last_err)
    return {"error": {"error_code": -2, "error_msg": f"all proxies failed: {last_err}"}}


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
        "inline": True,
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


def granted_keyboard() -> dict:
    """Keyboard shown after access has been granted — no «Проверить оплату»
    button so users don't reflexively re-tap it and get «уже использована»."""
    return {
        "inline": True,
        "buttons": [
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


def _looks_like_payment_data(text: str) -> bool:
    """Heuristic: is this string plausibly an email / phone / order_id?

    Anything else (greetings, questions, "спасибо", emoji, free-form text) is
    NOT routed through handle_payment_check, so the bot stops replying with
    "Я не нашла оплаченный заказ" to casual chat.
    """
    t = (text or "").strip()
    if not t or len(t) > 120:
        return False
    if "@" in t:
        return True
    if len(normalize_phone(t)) >= 10:
        return True
    # Order_id: short, single token, alphanumeric (+ - _), no spaces, no
    # cyrillic letters. Prodamus order_ids fit this shape.
    if " " not in t and len(t) <= 64:
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if all(ch in allowed for ch in t):
            return True
    return False


def _is_verified_vk_user(vk_user_id: int) -> bool:
    """Return True if this VK user already has a paid Payment linked."""
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.vk_id == str(vk_user_id)).first()
        if not user or not user.payment_id:
            return False
        payment = user.payment
        return bool(payment and payment.status == "paid")
    finally:
        db.close()


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
    # Last-resort "any group" only for payments WITHOUT a product tag.
    # A payment that names a product but matches no tagged/general group must
    # NOT be dumped into a random (e.g. a different marathon's) group.
    if not vk_group and not product:
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
    # Last-resort "any group" only for payments WITHOUT a product tag.
    if not tg_group and not product:
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
    # VK Callback secret_key accepts only Latin letters and digits — token_urlsafe
    # can emit "-"/"_" and VK rejects it as "invalid secret_key" (~64% of the time),
    # which is why setup used to succeed only intermittently. token_hex is always
    # alphanumeric (32 hex chars, well within VK's 50-char limit).
    secret_key = secrets.token_hex(16)

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
        # Marathon groups: NO message_new — the bot must NOT answer messages in
        # the marathon group itself (there clients write to a human admin). Only
        # join events, so the bot can approve join requests by payment. Payment
        # verification happens in the SEPARATE concierge community (VK_GROUP_ID).
        message_new=0,
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
                .filter(Payment.status == "paid", Payment.used.is_(False))
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
                if existing_user and existing_user.vk_id and existing_user.vk_id != vk_id_str:
                    # Payment genuinely used by a DIFFERENT VK user
                    await vk_send_message(
                        vk_user_id,
                        "Эта оплата уже использована другим пользователем.\n"
                        "Каждая оплата даёт доступ одному человеку.\n"
                        "Если это ошибка — напиши в поддержку.",
                        keyboard=main_keyboard(),
                        access_token=access_token,
                    )
                    return
                # Allow TG→VK bridge: user paid via TG, first time in VK
                if existing_user and existing_user.telegram_id and not existing_user.vk_id:
                    payment = used_payment
                elif existing_user and existing_user.vk_id == vk_id_str:
                    # Same VK user re-checking their own payment — resend the
                    # link instead of refusing, so they stop tapping in panic.
                    await _resend_granted_links(
                        vk_user_id=vk_user_id,
                        payment=used_payment,
                        db=db,
                        access_token=access_token,
                    )
                    return
                elif existing_user is None:
                    # Orphaned used payment (used=1, no user row points at it —
                    # seen in prod 2026-08-14). Re-attach to this VK user who
                    # just proved ownership, then resend the links, same
                    # recovery as the TG side.
                    logger.warning(
                        "VK check vk=%s: ORPHANED used payment %s (%s) — re-attaching",
                        vk_id_str, used_payment.order_id, used_payment.product_name,
                    )
                    orphan_owner = (
                        db.query(User).filter(User.vk_id == vk_id_str).first()
                    )
                    if orphan_owner:
                        orphan_owner.payment_id = used_payment.id
                    else:
                        db.add(User(vk_id=vk_id_str, payment_id=used_payment.id))
                    db.commit()
                    await _resend_granted_links(
                        vk_user_id=vk_user_id,
                        payment=used_payment,
                        db=db,
                        access_token=access_token,
                    )
                    return
                else:
                    # Payment already used — no re-issue
                    await vk_send_message(
                        vk_user_id,
                        "Эта оплата уже была использована ✅\n"
                        "Ссылка на группу выдаётся один раз.\n\n"
                        "Если вы не получили доступ — напишите в поддержку.",
                        keyboard=main_keyboard(),
                        access_token=access_token,
                    )
                    return

            if not payment:
                await vk_send_message(
                    vk_user_id,
                    "Я не нашла оплаченный заказ 🤔\n\n"
                    "Возможно, информация об оплате ещё не поступила.\n"
                    "Если вводили почту — попробуйте номер телефона, "
                    "и наоборот.\n\n"
                    "Если не получается — напишите в поддержку.",
                    keyboard=main_keyboard(),
                    access_token=access_token,
                )
                return

        # Check if payment is linked to another VK user
        existing_user = db.query(User).filter(User.payment_id == payment.id).first()
        if existing_user and existing_user.vk_id and existing_user.vk_id != vk_id_str:
            # Payment linked to a DIFFERENT real VK user
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
                logger.info(
                    "VK grant vk=%s: merging duplicate user_id=%s (its payment link cleared)",
                    vk_id_str, existing_user.id,
                )
                existing_user.payment_id = None
            logger.info(
                "VK grant vk=%s: payment %s (%s); previous linked payment_id=%s",
                vk_id_str, payment.order_id, payment.product_name, user.payment_id,
            )
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

        # Try to auto-approve any pending VK join request for this user
        try:
            approved = await _approve_vk_request(
                vk_user_id, vk_group, vk_group.vk_group_id
            )
            if approved:
                logger.info(
                    "Auto-approved pending VK join request for user %s after payment verification",
                    vk_id_str,
                )
        except Exception as e:
            logger.warning("Could not auto-approve VK join request for %s: %s", vk_id_str, e)

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
                tg_info = (
                    f"\n\n📌 Телеграм-группа: {tg_group.group_name}\n"
                    f"Перейдите по ссылке и подайте заявку в группу — "
                    f"она будет принята автоматически 👇\n{tg_invite_link}"
                )

        await vk_send_message(
            vk_user_id,
            f"Оплата найдена ✅\n\n"
            f"Вам доступны две площадки — они дублируют друг друга, "
            f"можно вступить в любую или в обе:\n\n"
            f"📌 ВК-сообщество: {vk_group.group_name}\n"
            f"Перейдите по ссылке и подайте заявку на вступление — "
            f"администратор одобрит её в ближайшее время 👇\n"
            f"{vk_group.invite_link}"
            f"{tg_info}",
            keyboard=granted_keyboard(),
            access_token=access_token,
        )
    finally:
        db.close()


async def _resend_granted_links(
    vk_user_id: int,
    payment: Payment,
    db: Session,
    access_token: str | None,
) -> None:
    """Re-send the same access links to a user who is already verified for this
    payment. No new bindings, no new TG one-use links, no new approvals — just
    repeat the info so the user stops tapping «Проверить оплату» in panic."""
    vk_group = select_vk_group_for_payment(db, payment)
    if not vk_group or not vk_group.invite_link:
        await vk_send_message(
            vk_user_id,
            "У тебя уже есть доступ ✅\n"
            "Если что-то не так — напиши в поддержку.",
            keyboard=granted_keyboard(),
            access_token=access_token,
        )
        return

    tg_group = select_tg_group_for_payment(db, payment)
    tg_info = ""
    if tg_group and tg_group.invite_link:
        tg_info = (
            f"\n\n📌 Телеграм-группа: {tg_group.group_name}\n"
            f"Ссылка для входа 👇\n{tg_group.invite_link}"
        )

    await vk_send_message(
        vk_user_id,
        f"У тебя уже есть доступ ✅\n\n"
        f"Высылаю ссылки ещё раз:\n\n"
        f"📌 ВК-сообщество: {vk_group.group_name}\n"
        f"{vk_group.invite_link}"
        f"{tg_info}\n\n"
        f"Если возник вопрос — нажми «Поддержка».",
        keyboard=granted_keyboard(),
        access_token=access_token,
    )


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

    # Dedupe: if VK already retried this event before we deployed the fast-ack
    # fix, drop the duplicate so we don't re-send the same DM.
    event_id = data.get("event_id")
    if _vk_event_already_seen(event_id):
        logger.info("VK callback: duplicate event_id=%s, skipping", event_id)
        return web.Response(text="ok")

    # IMPORTANT: schedule handlers as background tasks and reply "ok" immediately.
    # VK Callback API retries the event if it does not get "ok" within ~10s,
    # which previously caused the same DM to be sent multiple times in a row.
    if event_type == "message_new":
        obj = data.get("object", {})
        message = obj.get("message", obj)  # v5.199 wraps in "message"
        vk_user_id = message.get("from_id") or message.get("user_id")
        text = (message.get("text") or "").strip()

        if vk_user_id and text:
            _spawn_vk_task(
                _process_message(vk_user_id, text, source_group=source_group)
            )

    elif event_type == "group_join":
        obj = data.get("object", {})
        vk_user_id = obj.get("user_id")
        join_type = (obj.get("join_type") or obj.get("type") or "").lower()
        if vk_user_id:
            if join_type == "request":
                _spawn_vk_task(
                    _handle_group_join_request(vk_user_id, source_group, group_id)
                )
            else:
                _spawn_vk_task(
                    _check_group_join(vk_user_id, source_group, group_id)
                )

    elif event_type == "group_leave":
        obj = data.get("object", {})
        vk_user_id = obj.get("user_id")
        if vk_user_id:
            # User left / declined — drop from the pending checker list.
            _clear_vk_join_request(vk_user_id, group_id)
            # Evidentiary record of WHEN this happened — for refund disputes.
            _record_vk_leave(vk_user_id, group_id, bool(obj.get("self", 0)), source_group)

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
            "Нажми кнопку «Проверить оплату» под этим сообщением "
            "ИЛИ просто отправь мне текстом номер телефона (или email), "
            "который ты указал(а) при оплате.",
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
    elif vk_user_id in _PENDING_SUPPORT:
        # User is sending a support message
        _PENDING_SUPPORT.discard(vk_user_id)
        await _forward_support_message(vk_user_id, text, source_group=source_group)
    elif _looks_like_payment_data(text):
        await handle_payment_check(vk_user_id, text, source_group=source_group)
    elif _is_verified_vk_user(vk_user_id):
        await vk_send_message(
            vk_user_id,
            "Доступ у тебя уже есть ✅\n"
            "Если есть вопрос — нажми «Поддержка», и я передам админу.",
            keyboard=main_keyboard(),
            access_token=access_token,
        )
    else:
        await vk_send_message(
            vk_user_id,
            "Чтобы я нашла твою оплату, пришли email или номер телефона, "
            "который ты указал(а) при оплате.\n\n"
            "Если есть вопрос — нажми «Поддержка».",
            keyboard=main_keyboard(),
            access_token=access_token,
        )


async def _handle_support(vk_user_id: int, source_group: VkGroup | None = None) -> None:
    """Ask user for their question and remember they're in support mode."""
    access_token = _vk_token(source_group)
    _PENDING_SUPPORT.add(vk_user_id)
    await vk_send_message(
        vk_user_id,
        "Напиши свой вопрос одним сообщением — я передам администратору.",
        keyboard=main_keyboard(),
        access_token=access_token,
    )


async def _forward_support_message(
    vk_user_id: int, text: str, source_group: VkGroup | None = None
) -> None:
    """Forward user's support message to TG admins."""
    access_token = _vk_token(source_group)
    # Get VK user info
    result = await vk_api("users.get", access_token=access_token, user_ids=vk_user_id)
    users = result.get("response", [])
    if users:
        user_info = users[0]
        name = f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}"
        user_label = f"{name} (vk.com/id{vk_user_id})"
    else:
        user_label = f"VK ID {vk_user_id}"

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    reply_markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Ответить", callback_data=f"support_reply:vk:{vk_user_id}")]
        ]
    )

    await _notify_tg_admins(
        f"📩 Запрос поддержки из ВК:\n"
        f"{user_label}\n"
        f"Сообщение: {text}",
        reply_markup=reply_markup
    )
    await vk_send_message(
        vk_user_id,
        "Спасибо! Сообщение передано администратору. ✅",
        keyboard=main_keyboard(),
        access_token=access_token,
    )


def _get_vk_admin_token() -> str | None:
    """Get VK admin user token for group management (approve/remove)."""
    db: Session = SessionLocal()
    try:
        auth = db.query(VkAdminAuth).order_by(VkAdminAuth.id.desc()).first()
        return auth.access_token if auth else None
    finally:
        db.close()


def vk_oauth_url() -> str | None:
    """Generate VK ID OAuth authorization URL (implicit flow)."""
    if not VK_APP_ID:
        return None
    redirect_uri = VK_CALLBACK_URL.replace("/webhooks/vk", "/vk-auth/callback")
    return (
        f"https://id.vk.com/authorize?client_id={VK_APP_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=token&scope=groups"
        f"&state=marathon_bot"
    )


async def handle_vk_oauth_callback(request: web.Request) -> web.Response:
    """Serve HTML page that extracts token from URL fragment and saves it."""
    # If called with code param — try authorization code flow
    code = request.query.get("code")
    if code:
        return await _handle_code_flow(request, code)

    # Otherwise serve HTML for implicit flow (token in fragment)
    return web.Response(
        text=_OAUTH_CAPTURE_HTML,
        content_type="text/html",
    )


async def handle_vk_oauth_save(request: web.Request) -> web.Response:
    """Save VK admin token received from the capture page."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad request"}, status=400)

    token = data.get("access_token", "")
    vk_user_id = str(data.get("user_id", ""))

    if not token or len(token) < 20:
        return web.json_response({"ok": False, "error": "invalid token"}, status=400)

    db: Session = SessionLocal()
    try:
        auth = VkAdminAuth(
            vk_user_id=vk_user_id,
            access_token=token,
            created_at=datetime.utcnow(),
        )
        db.add(auth)
        db.commit()
    finally:
        db.close()

    logger.info("VK admin OAuth completed for user %s", vk_user_id)

    await _notify_tg_admins(
        f"✅ ВК-авторизация администратора получена!\n"
        f"VK user_id: {vk_user_id}\n"
        f"Теперь заявки в ВК-сообщества будут одобряться автоматически."
    )

    return web.json_response({"ok": True})


async def _handle_code_flow(request: web.Request, code: str) -> web.Response:
    """Handle authorization code flow as fallback."""
    redirect_uri = VK_CALLBACK_URL.replace("/webhooks/vk", "/vk-auth/callback")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://id.vk.com/oauth2/auth",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": VK_APP_ID,
                "client_secret": VK_APP_SECRET,
                "redirect_uri": redirect_uri,
                "device_id": "marathon_bot",
            },
        ) as resp:
            data = await resp.json(content_type=None)

    if "access_token" not in data:
        error = data.get("error_description", data.get("error", "unknown"))
        logger.error("VK OAuth code flow error: %s", data)
        return web.Response(
            text=f"<h2>Ошибка авторизации ВК</h2><p>{error}</p>",
            content_type="text/html",
        )

    token = data["access_token"]
    vk_user_id = str(data.get("user_id", ""))

    db: Session = SessionLocal()
    try:
        auth = VkAdminAuth(
            vk_user_id=vk_user_id,
            access_token=token,
            created_at=datetime.utcnow(),
        )
        db.add(auth)
        db.commit()
    finally:
        db.close()

    logger.info("VK admin OAuth (code flow) completed for user %s", vk_user_id)

    await _notify_tg_admins(
        f"✅ ВК-авторизация администратора получена!\n"
        f"VK user_id: {vk_user_id}\n"
        f"Теперь заявки в ВК-сообщества будут одобряться автоматически."
    )

    return web.Response(
        text=(
            "<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
            "<h2>✅ Авторизация успешна!</h2>"
            "<p>Бот получил доступ к управлению сообществами.</p>"
            "<p>Можете закрыть эту страницу.</p>"
            "</body></html>"
        ),
        content_type="text/html",
    )


_OAUTH_CAPTURE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Авторизация ВК</title></head>
<body style="font-family:sans-serif;text-align:center;padding:60px">
<div id="status"><h2>⏳ Сохраняю авторизацию...</h2></div>
<script>
(function() {
    var hash = window.location.hash.substring(1);
    if (!hash) {
        document.getElementById('status').innerHTML =
            '<h2>❌ Токен не получен</h2><p>Попробуйте ещё раз.</p>';
        return;
    }
    var params = {};
    hash.split('&').forEach(function(p) {
        var kv = p.split('=');
        params[kv[0]] = decodeURIComponent(kv[1] || '');
    });
    if (!params.access_token) {
        document.getElementById('status').innerHTML =
            '<h2>❌ Токен не найден в ответе</h2>';
        return;
    }
    fetch('/vk-auth/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            access_token: params.access_token,
            user_id: params.user_id || ''
        })
    }).then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.ok) {
            document.getElementById('status').innerHTML =
                '<h2>✅ Авторизация успешна!</h2>' +
                '<p>Бот получил доступ к управлению сообществами.</p>' +
                '<p>Можете закрыть эту страницу.</p>';
        } else {
            document.getElementById('status').innerHTML =
                '<h2>❌ Ошибка сохранения</h2><p>' + (d.error || '') + '</p>';
        }
    }).catch(function(e) {
        document.getElementById('status').innerHTML =
            '<h2>❌ Ошибка связи</h2><p>' + e.message + '</p>';
    });
})();
</script>
</body></html>"""


async def _approve_vk_request(
    vk_user_id: int,
    vk_group: VkGroup | None,
    callback_group_id: object,
) -> bool:
    group_id = _normalize_group_id(callback_group_id or (vk_group.vk_group_id if vk_group else None))
    if not group_id:
        logger.warning("Cannot approve VK request: group_id missing")
        return False
    # groups.approveRequest needs the admin USER token, routed via RU proxy
    # (paused when no proxy is configured — see _vk_user_api).
    result = await _vk_user_api(
        "groups.approveRequest",
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
    if not group_id:
        logger.warning("Cannot remove VK user/request: group_id missing")
        return False
    # groups.removeUser needs the admin USER token, routed via RU proxy
    # (paused when no proxy is configured — see _vk_user_api).
    result = await _vk_user_api(
        "groups.removeUser",
        group_id=group_id,
        user_id=vk_user_id,
    )
    return "response" in result and result["response"] == 1


async def _record_vk_join_request(
    vk_user_id: int,
    callback_group_id: object,
    source_group: VkGroup | None,
) -> None:
    """Store a pending VK join request for the admin «🔔 Заявки ВК» checker.
    Uses the community token (users.get) — no user token, so the admin account
    is never touched."""
    gid = _normalize_group_id(
        callback_group_id or (source_group.vk_group_id if source_group else None)
    )
    if not gid:
        return
    full_name = None
    try:
        info = await vk_api(
            "users.get", user_ids=vk_user_id, access_token=_vk_token(source_group)
        )
        arr = info.get("response", [])
        if arr:
            full_name = f"{arr[0].get('first_name', '')} {arr[0].get('last_name', '')}".strip()
    except Exception:
        pass
    db: Session = SessionLocal()
    try:
        existing = (
            db.query(VkJoinRequest)
            .filter(VkJoinRequest.vk_id == str(vk_user_id), VkJoinRequest.vk_group_id == gid)
            .first()
        )
        if existing:
            existing.status = "pending"
            existing.created_at = datetime.utcnow()
            if full_name:
                existing.full_name = full_name
        else:
            db.add(
                VkJoinRequest(
                    vk_id=str(vk_user_id),
                    vk_group_id=gid,
                    full_name=full_name,
                    created_at=datetime.utcnow(),
                    status="pending",
                )
            )
        db.commit()
    finally:
        db.close()


def _clear_vk_join_request(vk_user_id: int, callback_group_id: object) -> None:
    """Mark a pending VK join request as done (user approved / joined / left)."""
    gid = _normalize_group_id(callback_group_id)
    if not gid:
        return
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(VkJoinRequest)
            .filter(
                VkJoinRequest.vk_id == str(vk_user_id),
                VkJoinRequest.vk_group_id == gid,
                VkJoinRequest.status == "pending",
            )
            .all()
        )
        for r in rows:
            r.status = "done"
        if rows:
            db.commit()
    finally:
        db.close()


def _record_vk_leave(
    vk_user_id: int,
    callback_group_id: object,
    self_initiated: bool,
    source_group: VkGroup | None,
) -> None:
    """Log the moment a paid member LEAVES or is REMOVED from a marathon VK
    community — evidentiary record for refund disputes. VK's group_leave event
    carries no leave timestamp of its own, so we stamp it with the time OUR
    system received the callback (near-real-time) rather than "whenever an
    admin happened to notice"."""
    gid = _normalize_group_id(callback_group_id or (source_group.vk_group_id if source_group else None))
    db: Session = SessionLocal()
    try:
        vk_id_str = str(vk_user_id)
        user = db.query(User).filter(User.vk_id == vk_id_str).first()
        payment = user.payment if user and user.payment_id else None
        group_name = source_group.group_name if source_group else (gid or "VK group")
        action = "left_vk" if self_initiated else "kicked_vk"
        comment = (
            f"vk_id={vk_id_str} "
            f"{'вышел сам' if self_initiated else 'удалён/исключён администратором'} "
            f"из «{group_name}»"
        )
        log = AccessLog(
            telegram_id=user.telegram_id if user else None,
            email=payment.email if payment else None,
            order_id=payment.order_id if payment else None,
            group_name=group_name,
            group_id=gid or "",
            action=action,
            timestamp=datetime.utcnow(),
            comment=comment,
        )
        db.add(log)
        db.commit()
    finally:
        db.close()


def get_pending_paid_vk_requests(db: Session) -> list[dict]:
    """Pending VK join requests whose user has a PAID payment matching the
    community — exactly who the admin should approve. (Used by the TG checker.)"""
    out: list[dict] = []
    pending = (
        db.query(VkJoinRequest)
        .filter(VkJoinRequest.status == "pending")
        .order_by(VkJoinRequest.created_at)
        .all()
    )
    for req in pending:
        user = db.query(User).filter(User.vk_id == req.vk_id).first()
        if not user or not user.payment_id:
            continue
        payment = user.payment
        if not payment or payment.status != "paid":
            continue
        vk_group = (
            db.query(VkGroup)
            .filter(VkGroup.vk_group_id == req.vk_group_id)
            .order_by(VkGroup.id.desc())
            .first()
        )
        if not vk_group or not _vk_group_matches_payment(db, vk_group, payment):
            continue
        out.append(
            {
                "vk_id": req.vk_id,
                "full_name": req.full_name or f"id{req.vk_id}",
                "group_name": vk_group.group_name,
            }
        )
    return out


async def _handle_group_join_request(
    vk_user_id: int,
    source_group: VkGroup | None,
    callback_group_id: object,
) -> None:
    """Approve a closed-community VK join request only for paid users."""
    # Capture this pending join request for the admin «🔔 Заявки ВК» checker.
    await _record_vk_join_request(vk_user_id, callback_group_id, source_group)
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

        # User not found or no valid payment — leave request pending
        # and prompt the user to verify payment via VK bot DM.
        logger.info(
            "VK join request from user %s — no verified payment yet, leaving pending",
            vk_id_str,
        )
        try:
            await vk_send_message(
                vk_user_id,
                "Привет! Я получила твою заявку на вступление 🙌\n\n"
                "Чтобы попасть в сообщество — напиши мне сюда "
                "email или номер телефона, который ты указал(а) при оплате.\n\n"
                "После проверки оплаты администратор одобрит твою заявку.",
                keyboard=main_keyboard(),
                access_token=_vk_token(source_group),
            )
        except Exception as e:
            logger.warning(
                "Could not DM VK user %s with verification prompt: %s",
                vk_id_str, e,
            )
    except Exception as e:
        logger.exception("Error handling VK join request: %s", e)
    finally:
        db.close()


async def _is_group_manager(vk_user_id: int, group_id: object) -> bool:
    """Check if user is a manager/editor/admin of the VK group."""
    gid = _normalize_group_id(group_id)
    if not gid:
        return False
    # groups.getMembers(filter=managers) needs the admin USER token, routed via
    # RU proxy (paused when no proxy is configured — see _vk_user_api).
    result = await _vk_user_api(
        "groups.getMembers",
        group_id=gid,
        filter="managers",
    )
    managers = result.get("response", {}).get("items", [])
    return any(m.get("id") == vk_user_id for m in managers)


async def _check_group_join(
    vk_user_id: int,
    source_group: VkGroup | None = None,
    callback_group_id: object = None,
) -> None:
    """Check if a new VK group member has a valid payment. Alert admins if not."""
    # User is now IN the group — clear them from the pending checker list.
    _clear_vk_join_request(vk_user_id, callback_group_id)
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

        # Check if user is a group manager/editor — never remove them
        group_id = callback_group_id or (source_group.vk_group_id if source_group else None)
        if await _is_group_manager(vk_user_id, group_id):
            logger.info("VK group_join: user %s is a group manager, skipping stranger check", vk_id_str)
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

        # Notify TG admins (don't auto-remove — admin may have added them manually)
        await _notify_tg_admins(
            f"ℹ️ Новый участник в ВК-группе без оплаты через бота\n\n"
            f"{user_label}\n\n"
            f"Этот пользователь НЕ проверял оплату через бота.\n"
            f"Если вы добавили его вручную — всё ок.\n"
            f"Если нет — удалите его из группы."
        )
    except Exception as e:
        logger.exception("Error checking group_join: %s", e)
    finally:
        db.close()


async def _notify_tg_admins(text: str, reply_markup=None) -> None:
    """Send a notification to all TG admins via the Telegram bot."""
    if not ADMIN_IDS or not BOT_TOKEN:
        return
    try:
        tg_bot = Bot(token=BOT_TOKEN)
        for admin_id in ADMIN_IDS:
            try:
                await tg_bot.send_message(admin_id, text, reply_markup=reply_markup)
            except Exception as e:
                logger.error("Failed to notify TG admin %s: %s", admin_id, e)
        await tg_bot.session.close()
    except Exception as e:
        logger.error("Failed to create TG bot for admin notification: %s", e)


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
