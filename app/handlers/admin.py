import csv
import os
import tempfile
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup
from sqlalchemy import Integer, func, or_
from sqlalchemy.orm import Session

from ..config import ADMIN_IDS
from ..db.dal import SessionLocal
from ..db.models import AccessLog, CurrentGroup, Payment, User, VkAdminAuth, VkGroup

router = Router()

ADMIN_MENU = "Админ-меню"
ADMIN_SET_GROUP = "Установить группу"
ADMIN_SET_VK_GROUP = "Установить ВК-группу"
ADMIN_EXPORT_LOGS = "Выгрузить логи"
ADMIN_FIND_PAYMENT = "Найти оплату"
ADMIN_REBIND_PAYMENT = "Перепривязать оплату"
ADMIN_REMOVE_USER = "Удалить участника"
ADMIN_UNBAN_USER = "Разбанить участника"
ADMIN_STATS = "📊 Статистика"
ADMIN_VK_AUDIT = "🔍 Аудит ВК"
ADMIN_VK_AUTH = "🔑 Авторизация ВК"
ADMIN_CANCEL = "Отмена"

ADMIN_MENU_BUTTONS = {
    ADMIN_MENU,
    ADMIN_SET_GROUP,
    ADMIN_SET_VK_GROUP,
    ADMIN_EXPORT_LOGS,
    ADMIN_FIND_PAYMENT,
    ADMIN_REBIND_PAYMENT,
    ADMIN_REMOVE_USER,
    ADMIN_UNBAN_USER,
    ADMIN_STATS,
    ADMIN_VK_AUDIT,
    ADMIN_VK_AUTH,
    ADMIN_CANCEL,
}

ADMIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=ADMIN_STATS), KeyboardButton(text=ADMIN_VK_AUDIT)],
        [KeyboardButton(text=ADMIN_SET_GROUP)],
        [KeyboardButton(text=ADMIN_SET_VK_GROUP)],
        [KeyboardButton(text=ADMIN_VK_AUTH)],
        [KeyboardButton(text=ADMIN_FIND_PAYMENT)],
        [KeyboardButton(text=ADMIN_EXPORT_LOGS)],
        [KeyboardButton(text=ADMIN_REBIND_PAYMENT)],
        [KeyboardButton(text=ADMIN_REMOVE_USER)],
        [KeyboardButton(text=ADMIN_UNBAN_USER)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие",
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=ADMIN_CANCEL)]],
    resize_keyboard=True,
    input_field_placeholder="Можно отменить",
)


class AdminStates(StatesGroup):
    set_group_invite = State()
    set_group_name = State()
    export_start = State()
    export_end = State()
    export_group = State()
    find_payment = State()
    rebind_key = State()
    rebind_telegram = State()
    remove_user = State()
    unban_user = State()
    set_vk_group_id = State()
    set_vk_group_name = State()
    set_group_product = State()
    set_vk_group_product = State()
    set_vk_group_token = State()
    set_vk_admin_token = State()


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_IDS)


def normalize_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def phone_variants(value: str) -> tuple[str | None, str | None]:
    digits = normalize_phone(value)
    if not digits:
        return None, None
    last10 = digits[-10:] if len(digits) >= 10 else digits
    return digits, last10


async def send_admin_menu(message: Message) -> None:
    await message.answer("Админ-меню:", reply_markup=ADMIN_MENU_KEYBOARD)


async def send_payment_info(message: Message, query: str) -> None:
    db: Session = SessionLocal()
    try:
        phone, phone_last10 = phone_variants(query)
        filters = [Payment.email == query, Payment.order_id == query]
        if phone:
            filters.append(Payment.phone == phone)
        if phone_last10 and len(phone_last10) >= 10:
            filters.append(Payment.phone.endswith(phone_last10))
        payment = (
            db.query(Payment)
            .filter(or_(*filters))
            .order_by(Payment.id.desc())
            .first()
        )
        if not payment:
            await message.answer("Оплата не найдена.", reply_markup=ADMIN_MENU_KEYBOARD)
            return

        user = payment.user
        logs = (
            db.query(AccessLog)
            .filter(
                or_(
                    AccessLog.email == payment.email,
                    AccessLog.order_id == payment.order_id,
                )
            )
            .order_by(AccessLog.timestamp.desc())
            .limit(5)
            .all()
        )

        lines = [
            "Найденная оплата:",
            f"order_id: {payment.order_id}",
            f"email: {payment.email or '-'}",
            f"phone: {payment.phone or '-'}",
            f"status: {payment.status}",
            f"used: {payment.used}",
            f"created_at: {payment.created_at}",
        ]

        if user:
            lines.extend(
                [
                    "",
                    "Связанный пользователь:",
                    f"telegram_id: {user.telegram_id}",
                    f"username: {user.username}",
                    f"full_name: {user.full_name}",
                ]
            )
        else:
            lines.extend(["", "Связанный пользователь: отсутствует"])

        if logs:
            lines.append("")
            lines.append("Последние логи доступа:")
            for log in logs:
                timestamp = log.timestamp.isoformat(sep=" ", timespec="seconds")
                lines.append(
                    f"{timestamp} | {log.action} | {log.group_name} | {log.comment}"
                )

        await message.answer("\n".join(lines), reply_markup=ADMIN_MENU_KEYBOARD)
    finally:
        db.close()


async def export_logs_report(
    message: Message,
    start_dt: datetime,
    end_dt: datetime,
    group_name: str | None,
    start_raw: str,
    end_raw: str,
) -> None:
    db: Session = SessionLocal()
    try:
        query = db.query(AccessLog).filter(
            AccessLog.timestamp >= start_dt, AccessLog.timestamp < end_dt
        )
        if group_name:
            query = query.filter(AccessLog.group_name == group_name)

        logs = query.order_by(AccessLog.timestamp.asc()).all()
        if not logs:
            await message.answer("Записей за этот период нет.", reply_markup=ADMIN_MENU_KEYBOARD)
            return

        with tempfile.NamedTemporaryFile(
            "w", newline="", suffix=".csv", delete=False, encoding="utf-8-sig"
        ) as tmp:
            writer = csv.writer(tmp)
            writer.writerow(
                [
                    "id",
                    "telegram_id",
                    "email",
                    "order_id",
                    "group_name",
                    "group_id",
                    "action",
                    "timestamp",
                    "comment",
                ]
            )
            for log in logs:
                timestamp = log.timestamp.isoformat(sep=" ", timespec="seconds")
                writer.writerow(
                    [
                        log.id,
                        log.telegram_id,
                        log.email,
                        log.order_id,
                        log.group_name,
                        log.group_id,
                        log.action,
                        timestamp,
                        log.comment,
                    ]
                )
            tmp_path = tmp.name

        caption = f"Логи с {start_raw} по {end_raw}"
        if group_name:
            caption = f"{caption} ({group_name})"

        try:
            await message.answer_document(FSInputFile(tmp_path), caption=caption)
        finally:
            os.remove(tmp_path)
    finally:
        db.close()


async def rebind_payment_to_user(
    message: Message,
    payment_key: str,
    telegram_id: str,
) -> None:
    db: Session = SessionLocal()
    try:
        phone, phone_last10 = phone_variants(payment_key)
        filters = [Payment.email == payment_key, Payment.order_id == payment_key]
        if phone:
            filters.append(Payment.phone == phone)
        if phone_last10 and len(phone_last10) >= 10:
            filters.append(Payment.phone.endswith(phone_last10))
        payment = (
            db.query(Payment)
            .filter(or_(*filters))
            .order_by(Payment.id.desc())
            .first()
        )
        if not payment:
            await message.answer("Оплата не найдена.", reply_markup=ADMIN_MENU_KEYBOARD)
            return

        new_user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if not new_user:
            new_user = User(telegram_id=telegram_id)
            db.add(new_user)

        old_user = db.query(User).filter(User.payment_id == payment.id).first()
        if old_user and old_user.id != new_user.id:
            old_user.payment_id = None

        new_user.payment_id = payment.id
        payment.used = True

        db.commit()
        await message.answer(
            f"Оплата {payment.order_id} привязана к Telegram ID {telegram_id}.",
            reply_markup=ADMIN_MENU_KEYBOARD,
        )
    finally:
        db.close()


async def create_current_group(
    message: Message,
    invite_link: str,
    group_name: str,
    product_tag: str | None = None,
) -> None:
    db: Session = SessionLocal()
    try:
        current = CurrentGroup(
            chat_id=None,
            group_name=group_name,
            invite_link=invite_link,
            product_tag=product_tag,
        )
        db.add(current)
        db.commit()
        tag_info = f"\nПродукт: {product_tag}" if product_tag else ""
        await message.answer(
            f"Текущая группа установлена:\n{group_name}\n{invite_link}{tag_info}",
            reply_markup=ADMIN_MENU_KEYBOARD,
        )
    finally:
        db.close()


@router.message(Command("admin"))
async def admin_menu(message: Message) -> None:
    if not is_admin(message):
        return
    await send_admin_menu(message)


@router.message(Command("admin_help"))
async def admin_help(message: Message) -> None:
    if not is_admin(message):
        return

    await message.answer(
        "Доступные команды администратора:\n"
        "/admin — открыть меню кнопок\n"
        "/find_payment <email, телефон или order_id> — найти оплату и связки\n"
        "/export_logs <YYYY-MM-DD> <YYYY-MM-DD> [название группы] — CSV выгрузка\n"
        "/rebind_payment <email|телефон|order_id> <telegram_id> — перепривязать оплату\n"
        "В меню есть кнопка «Удалить участника»\n"
        "«Установить ВК-группу» — добавить ссылку, тег продукта и токен сообщества"
    )


@router.message(F.text == ADMIN_MENU)
async def admin_menu_button(message: Message) -> None:
    if not is_admin(message):
        return
    await send_admin_menu(message)


@router.message(Command("cancel"))
@router.message(F.text == ADMIN_CANCEL)
async def admin_cancel(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.clear()
    await send_admin_menu(message)


@router.message(F.text == ADMIN_STATS)
async def admin_stats(message: Message) -> None:
    if not is_admin(message):
        return

    db: Session = SessionLocal()
    try:
        # Total paid payments
        total_paid = db.query(func.count(Payment.id)).filter(
            Payment.status == "paid"
        ).scalar() or 0

        # Links issued (payment verified by user)
        total_used = db.query(func.count(Payment.id)).filter(
            Payment.status == "paid",
            Payment.used.is_(True),
        ).scalar() or 0

        # Unused payments (paid but never verified)
        total_unused = total_paid - total_used

        # TG joins (from access_logs)
        tg_joins = db.query(func.count(AccessLog.id)).filter(
            AccessLog.action == "granted"
        ).scalar() or 0

        # VK links sent (from access_logs)
        vk_joins = db.query(func.count(AccessLog.id)).filter(
            AccessLog.action == "granted_vk"
        ).scalar() or 0

        # Breakdown by product
        product_stats = (
            db.query(
                Payment.product_name,
                func.count(Payment.id),
                func.sum(func.cast(Payment.used, Integer)),
            )
            .filter(Payment.status == "paid")
            .group_by(Payment.product_name)
            .all()
        )

        lines = [
            "📊 Статистика бота\n",
            f"💰 Всего оплат: {total_paid}",
            f"🔗 Ссылки выданы: {total_used}",
            f"⏳ Не забрали ссылку: {total_unused}",
            f"✅ Вступили в ТГ-группу: {tg_joins}",
            f"✅ Получили ВК-ссылку: {vk_joins}",
        ]

        if product_stats:
            lines.append("\n📦 По продуктам:")
            for product_name, count, used_count in product_stats:
                name = product_name or "(без тега)"
                used_count = used_count or 0
                lines.append(f"  • {name}: {count} оплат, {used_count} выдано")

        await message.answer("\n".join(lines), reply_markup=ADMIN_MENU_KEYBOARD)
    finally:
        db.close()

@router.message(F.text == ADMIN_VK_AUDIT)
async def admin_vk_audit(message: Message) -> None:
    if not is_admin(message):
        return

    from ..vk_bot import get_approved_vk_ids

    approved = get_approved_vk_ids()
    if not approved:
        await message.answer(
            "🔍 Аудит ВК\n\nНет одобренных ВК-пользователей в базе.",
            reply_markup=ADMIN_MENU_KEYBOARD,
        )
        return

    lines = [
        f"🔍 Аудит ВК — одобренные пользователи ({len(approved)}):\n",
    ]
    for i, u in enumerate(approved, 1):
        vk_link = f"vk.com/id{u['vk_id']}"
        email = u.get("email") or "-"
        phone = u.get("phone") or "-"
        tg = f"TG: {u['telegram_id']}" if u.get("telegram_id") else "TG: нет"
        lines.append(f"{i}. {vk_link} | {email} | {phone} | {tg}")

    lines.append(
        "\n💡 Сравните этот список с участниками ВК-группы.\n"
        "Кого нет в этом списке — тот чужак."
    )

    text = "\n".join(lines)
    if len(text) > 4000:
        for chunk_start in range(0, len(text), 4000):
            chunk = text[chunk_start:chunk_start + 4000]
            await message.answer(chunk, reply_markup=ADMIN_MENU_KEYBOARD)
    else:
        await message.answer(text, reply_markup=ADMIN_MENU_KEYBOARD)


@router.message(F.text == ADMIN_VK_AUTH)
async def admin_vk_auth(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return

    # Check current auth status
    db: Session = SessionLocal()
    try:
        auth = db.query(VkAdminAuth).order_by(VkAdminAuth.id.desc()).first()
        if auth:
            status = (
                f"Текущая авторизация: VK user_id {auth.vk_user_id}\n"
                f"Получена: {auth.created_at.strftime('%d.%m.%Y %H:%M') if auth.created_at else '—'}\n\n"
            )
        else:
            status = "⚠️ Авторизация ВК не настроена.\n\n"
    finally:
        db.close()

    token_url = (
        "https://oauth.vk.com/authorize?client_id=6121396"
        "&display=page&redirect_uri=https://oauth.vk.com/blank.html"
        "&scope=groups&response_type=token&v=5.199"
    )

    await state.set_state(AdminStates.set_vk_admin_token)
    await message.answer(
        f"🔑 Авторизация ВК\n\n{status}"
        "Чтобы бот мог одобрять заявки в сообществах, "
        "нужен пользовательский токен ВК.\n\n"
        "📋 Инструкция:\n"
        f"1. Перейдите по ссылке:\n{token_url}\n\n"
        "2. Нажмите «Разрешить»\n"
        "3. Скопируйте из адресной строки всё "
        "между access_token= и &expires\n"
        "4. Отправьте этот токен сюда\n\n"
        "Или напишите 'Отмена' для выхода.",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.set_vk_admin_token)
async def admin_set_vk_admin_token(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    # Accept full URL or just the token
    token = text
    if "access_token=" in token:
        # Extract token from URL
        import re
        match = re.search(r'access_token=([^&]+)', token)
        if match:
            token = match.group(1)

    if len(token) < 20:
        await message.answer(
            "Это не похоже на токен ВК. Попробуйте ещё раз.",
            reply_markup=CANCEL_KEYBOARD,
        )
        return

    # Verify the token works
    from ..vk_bot import vk_api
    result = await vk_api("users.get", access_token=token)
    users = result.get("response", [])
    if not users:
        error = result.get("error", {}).get("error_msg", "неизвестная ошибка")
        await message.answer(
            f"❌ Токен не работает: {error}\nПопробуйте получить новый.",
            reply_markup=CANCEL_KEYBOARD,
        )
        return

    vk_user = users[0]
    vk_user_id = str(vk_user.get("id", ""))
    vk_name = f"{vk_user.get('first_name', '')} {vk_user.get('last_name', '')}"

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

    await state.clear()
    await message.answer(
        f"✅ Авторизация ВК сохранена!\n\n"
        f"Пользователь: {vk_name} (id{vk_user_id})\n"
        f"Теперь заявки в ВК-сообщества будут одобряться автоматически.",
        reply_markup=ADMIN_MENU_KEYBOARD,
    )


@router.message(F.text == ADMIN_SET_GROUP)
async def admin_set_group_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.set_state(AdminStates.set_group_invite)
    await message.answer(
        "Пришли invite-link для группы (t.me/...).",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.set_group_invite)
async def admin_set_group_invite(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    await state.update_data(invite_link=text)
    await state.set_state(AdminStates.set_group_name)
    await message.answer(
        "Теперь пришли название группы.",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.set_group_name)
async def admin_set_group_name(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    group_name = message.text.strip()
    if group_name == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    await state.update_data(group_name=group_name)
    await state.set_state(AdminStates.set_group_product)
    await message.answer(
        "Введи тег продукта для этой группы\n"
        "(например: pechen, gormony, zhkt)\n\n"
        "Этот тег должен совпадать с названием товара в Продамусе.\n"
        "Если хочешь группу без привязки к продукту — напиши 'нет'.",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.set_group_product)
async def admin_set_group_product(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    product_tag = None if text.lower() in ("нет", "no", "-") else text.lower()

    data = await state.get_data()
    invite_link = data.get("invite_link")
    group_name = data.get("group_name")
    if not invite_link or not group_name:
        await message.answer(
            "Не вижу данные, начни заново.",
            reply_markup=ADMIN_MENU_KEYBOARD,
        )
        await state.clear()
        return

    await create_current_group(message, invite_link, group_name, product_tag)
    await state.clear()


@router.message(F.text == ADMIN_EXPORT_LOGS)
async def admin_export_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.set_state(AdminStates.export_start)
    await message.answer(
        "Дата начала в формате YYYY-MM-DD.",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.export_start)
async def admin_export_start_date(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await message.answer("Неверный формат даты. Пример: 2025-01-15")
        return

    await state.update_data(export_start=text)
    await state.set_state(AdminStates.export_end)
    await message.answer(
        "Дата окончания в формате YYYY-MM-DD.",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.export_end)
async def admin_export_end_date(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await message.answer("Неверный формат даты. Пример: 2025-01-31")
        return

    await state.update_data(export_end=text)
    await state.set_state(AdminStates.export_group)
    await message.answer(
        "Название группы (или '-' если все).",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.export_group)
async def admin_export_group_name(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    data = await state.get_data()
    start_raw = data.get("export_start")
    end_raw = data.get("export_end")
    if not start_raw or not end_raw:
        await message.answer("Не вижу даты, начни заново.", reply_markup=ADMIN_MENU_KEYBOARD)
        await state.clear()
        return

    group_name = None
    if text:
        marker = text.strip().lower()
        if marker not in {"-", "все", "все", "all"}:
            group_name = text

    start_dt = datetime.strptime(start_raw, "%Y-%m-%d")
    end_dt = datetime.strptime(end_raw, "%Y-%m-%d") + timedelta(days=1)

    await export_logs_report(message, start_dt, end_dt, group_name, start_raw, end_raw)
    await state.clear()


@router.message(F.text == ADMIN_FIND_PAYMENT)
async def admin_find_payment_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.set_state(AdminStates.find_payment)
    await message.answer(
        "Введи email, телефон или order_id.",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.find_payment)
async def admin_find_payment_query(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    await send_payment_info(message, text)
    await state.clear()


@router.message(F.text == ADMIN_REBIND_PAYMENT)
async def admin_rebind_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.set_state(AdminStates.rebind_key)
    await message.answer(
        "Введи email, телефон или order_id для перепривязки.",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(F.text == ADMIN_REMOVE_USER)
async def admin_remove_user_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.set_state(AdminStates.remove_user)
    await message.answer(
        "Введи email, телефон или order_id участника для удаления из группы.",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.rebind_key)
async def admin_rebind_key(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    await state.update_data(rebind_key=text)
    await state.set_state(AdminStates.rebind_telegram)
    await message.answer(
        "Введи Telegram ID пользователя.",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.rebind_telegram)
async def admin_rebind_telegram(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    telegram_id = message.text.strip()
    if telegram_id == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    if not telegram_id.isdigit():
        await message.answer("Telegram ID должен быть числом.")
        return

    data = await state.get_data()
    payment_key = data.get("rebind_key")
    if not payment_key:
        await message.answer("Не вижу оплату, начни заново.", reply_markup=ADMIN_MENU_KEYBOARD)
        await state.clear()
        return

    await rebind_payment_to_user(message, payment_key, telegram_id)
    await state.clear()


@router.message(AdminStates.remove_user)
async def admin_remove_user(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    db: Session = SessionLocal()
    try:
        phone, phone_last10 = phone_variants(text)
        filters = [Payment.email == text, Payment.order_id == text]
        if phone:
            filters.append(Payment.phone == phone)
        if phone_last10 and len(phone_last10) >= 10:
            filters.append(Payment.phone.endswith(phone_last10))

        payment = (
            db.query(Payment)
            .filter(or_(*filters))
            .order_by(Payment.id.desc())
            .first()
        )
        if not payment:
            await message.answer("Оплата не найдена.", reply_markup=ADMIN_MENU_KEYBOARD)
            await state.clear()
            return

        user = payment.user
        if not user or not user.telegram_id or not user.telegram_id.isdigit():
            await message.answer(
                "Пользователь не привязан к этой оплате.",
                reply_markup=ADMIN_MENU_KEYBOARD,
            )
            await state.clear()
            return

        if message.from_user and str(message.from_user.id) == user.telegram_id:
            await message.answer(
                "Нельзя удалить самого себя.",
                reply_markup=ADMIN_MENU_KEYBOARD,
            )
            await state.clear()
            return

        current_group = db.query(CurrentGroup).order_by(CurrentGroup.id.desc()).first()
        if not current_group or not current_group.chat_id:
            await message.answer(
                "Не вижу chat_id группы. Отправьте тестовую заявку на вступление,"
                " чтобы бот сохранил chat_id.",
                reply_markup=ADMIN_MENU_KEYBOARD,
            )
            await state.clear()
            return

        try:
            await message.bot.ban_chat_member(
                chat_id=int(current_group.chat_id),
                user_id=int(user.telegram_id),
            )
        except Exception:
            await message.answer(
                "Не удалось удалить пользователя. Проверь права бота.",
                reply_markup=ADMIN_MENU_KEYBOARD,
            )
            await state.clear()
            return

        log = AccessLog(
            telegram_id=user.telegram_id,
            email=payment.email or "unknown",
            order_id=payment.order_id or "unknown",
            group_name=current_group.group_name,
            group_id=current_group.chat_id,
            action="revoked",
            timestamp=datetime.utcnow(),
            comment="Removed by admin",
        )
        db.add(log)
        db.commit()

        await message.answer(
            "Пользователь удалён из группы и заблокирован.",
            reply_markup=ADMIN_MENU_KEYBOARD,
        )
    finally:
        await state.clear()
        db.close()


@router.message(F.text == ADMIN_UNBAN_USER)
async def admin_unban_user_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.set_state(AdminStates.unban_user)
    await message.answer(
        "Введи email, телефон или order_id участника для разбана.",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.unban_user)
async def admin_unban_user(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    db: Session = SessionLocal()
    try:
        phone, phone_last10 = phone_variants(text)
        filters = [Payment.email == text, Payment.order_id == text]
        if phone:
            filters.append(Payment.phone == phone)
        if phone_last10 and len(phone_last10) >= 10:
            filters.append(Payment.phone.endswith(phone_last10))

        payment = (
            db.query(Payment)
            .filter(or_(*filters))
            .order_by(Payment.id.desc())
            .first()
        )
        if not payment:
            await message.answer("Оплата не найдена.", reply_markup=ADMIN_MENU_KEYBOARD)
            await state.clear()
            return

        user = payment.user
        if not user or not user.telegram_id or not user.telegram_id.isdigit():
            await message.answer(
                "Пользователь не привязан к этой оплате.",
                reply_markup=ADMIN_MENU_KEYBOARD,
            )
            await state.clear()
            return

        current_group = db.query(CurrentGroup).order_by(CurrentGroup.id.desc()).first()
        if not current_group or not current_group.chat_id:
            await message.answer(
                "Не вижу chat_id группы. Отправьте тестовую заявку на вступление,"
                " чтобы бот сохранил chat_id.",
                reply_markup=ADMIN_MENU_KEYBOARD,
            )
            await state.clear()
            return

        try:
            await message.bot.unban_chat_member(
                chat_id=int(current_group.chat_id),
                user_id=int(user.telegram_id),
                only_if_banned=True,
            )
        except Exception:
            await message.answer(
                "Не удалось разбанить пользователя. Проверь права бота.",
                reply_markup=ADMIN_MENU_KEYBOARD,
            )
            await state.clear()
            return

        log = AccessLog(
            telegram_id=user.telegram_id,
            email=payment.email or "unknown",
            order_id=payment.order_id or "unknown",
            group_name=current_group.group_name,
            group_id=current_group.chat_id,
            action="unbanned",
            timestamp=datetime.utcnow(),
            comment="Unbanned by admin",
        )
        db.add(log)
        db.commit()

        await message.answer(
            f"Пользователь разбанен. Теперь он может снова подать заявку на вступление в группу.",
            reply_markup=ADMIN_MENU_KEYBOARD,
        )
    finally:
        await state.clear()
        db.close()


@router.message(Command("find_payment"))
async def find_payment(message: Message) -> None:
    if not is_admin(message) or not message.text:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Формат: /find_payment <email, телефон или order_id>")
        return

    query = parts[1].strip()
    if not query:
        await message.answer("Формат: /find_payment <email, телефон или order_id>")
        return

    await send_payment_info(message, query)


@router.message(Command("export_logs"))
async def export_logs(message: Message) -> None:
    if not is_admin(message) or not message.text:
        return

    parts = message.text.split(maxsplit=3)
    if len(parts) < 3:
        await message.answer(
            "Формат: /export_logs <YYYY-MM-DD> <YYYY-MM-DD> [название группы]"
        )
        return

    start_raw = parts[1].strip()
    end_raw = parts[2].strip()
    
    group_name = None
    if len(parts) > 3:
        raw_group = parts[3].strip()
        if raw_group.lower() not in {"-", "все", "все", "all"}:
            group_name = raw_group

    try:
        start_dt = datetime.strptime(start_raw, "%Y-%m-%d")
        end_dt = datetime.strptime(end_raw, "%Y-%m-%d") + timedelta(days=1)
    except ValueError:
        await message.answer("Дата должна быть в формате YYYY-MM-DD.")
        return

    await export_logs_report(message, start_dt, end_dt, group_name, start_raw, end_raw)


@router.message(Command("rebind_payment"))
async def rebind_payment(message: Message) -> None:
    if not is_admin(message) or not message.text:
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Формат: /rebind_payment <email|телефон|order_id> <telegram_id>"
        )
        return

    payment_key = parts[1].strip()
    telegram_id = parts[2].strip()
    if not payment_key or not telegram_id.isdigit():
        await message.answer(
            "Формат: /rebind_payment <email|телефон|order_id> <telegram_id>"
        )
        return

    await rebind_payment_to_user(message, payment_key, telegram_id)


# ---------------------------------------------------------------------------
#  VK group management
# ---------------------------------------------------------------------------


@router.message(F.text == ADMIN_SET_VK_GROUP)
async def admin_set_vk_group_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message):
        return
    await state.set_state(AdminStates.set_vk_group_id)
    await message.answer(
        "Пришли ссылку-приглашение на ВК-сообщество или чат марафона\n"
        "(например https://vk.me/join/...).",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.set_vk_group_id)
async def admin_set_vk_group_link(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    if not any(domain in text for domain in ("vk.ru", "vk.com", "vk.me")):
        await message.answer(
            "Это не похоже на ссылку ВК. Пришли ссылку на сообщество или приглашение ВК."
        )
        return

    await state.update_data(vk_invite_link=text)
    await state.set_state(AdminStates.set_vk_group_name)
    await message.answer(
        "Теперь пришли название ВК-чата (например «МАРАФОН С 15 АПРЕЛЯ»).",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.set_vk_group_name)
async def admin_set_vk_group_name(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    group_name = message.text.strip()
    if group_name == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    await state.update_data(vk_group_name=group_name)
    await state.set_state(AdminStates.set_vk_group_product)
    await message.answer(
        "Введи тег продукта для этой ВК-группы\n"
        "(например: pechen, gormony, zhkt)\n\n"
        "Этот тег должен совпадать с названием товара в Продамусе.\n"
        "Если хочешь группу без привязки к продукту — напиши 'нет'.",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.set_vk_group_product)
async def admin_set_vk_group_product(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    product_tag = None if text.lower() in ("нет", "no", "-") else text.lower()
    await state.update_data(vk_product_tag=product_tag)
    await state.set_state(AdminStates.set_vk_group_token)
    await message.answer(
        "Теперь пришли ключ доступа сообщества ВК.\n\n"
        "Он нужен, чтобы бот мог сам подключить Callback API и проверять заявки.\n"
        "Если сейчас нужно только сохранить ссылку без автосторожа — напиши 'нет'.",
        reply_markup=CANCEL_KEYBOARD,
    )


@router.message(AdminStates.set_vk_group_token)
async def admin_set_vk_group_token(message: Message, state: FSMContext) -> None:
    if not is_admin(message) or not message.text:
        return

    text = message.text.strip()
    if text == ADMIN_CANCEL:
        await admin_cancel(message, state)
        return

    access_token = None if text.lower() in ("нет", "no", "-") else text

    data = await state.get_data()
    invite_link = data.get("vk_invite_link")
    group_name = data.get("vk_group_name")
    product_tag = data.get("vk_product_tag")
    if not invite_link or not group_name:
        await message.answer(
            "Не вижу данные, начни заново.",
            reply_markup=ADMIN_MENU_KEYBOARD,
        )
        await state.clear()
        return

    vk_group_id = "chat"
    callback_data = None
    callback_error = None

    if access_token:
        from ..vk_bot import configure_vk_callback_server, get_vk_group_info

        info = await get_vk_group_info(access_token)
        if info and info.get("id"):
            vk_group_id = str(info["id"])
        else:
            callback_error = "не смог определить ID сообщества по токену"

        if vk_group_id != "chat":
            try:
                callback_data = await configure_vk_callback_server(
                    access_token=access_token,
                    group_id=vk_group_id,
                    title="MarathonBot",
                )
            except Exception as e:
                callback_error = str(e)

    db: Session = SessionLocal()
    try:
        vk_group = VkGroup(
            vk_group_id=vk_group_id,
            group_name=group_name,
            invite_link=invite_link,
            product_tag=product_tag,
            access_token=access_token,
        )
        if callback_data:
            vk_group.callback_secret = callback_data["secret"]
            vk_group.callback_confirmation = callback_data["confirmation"]
            vk_group.callback_server_id = callback_data["server_id"]
            vk_group.callback_url = callback_data["url"]
            vk_group.callback_configured_at = datetime.utcnow()
        db.add(vk_group)
        db.commit()
        tag_info = f"\nПродукт: {product_tag}" if product_tag else ""
        callback_info = "\nCallback API: подключен ✅" if callback_data else "\nCallback API: не подключен"
        if callback_error:
            callback_info += f"\nПричина: {callback_error}"
        await message.answer(
            f"ВК-чат установлен ✅\n"
            f"Название: {group_name}\n"
            f"ID сообщества: {vk_group_id}\n"
            f"Ссылка: {invite_link}{tag_info}"
            f"{callback_info}",
            reply_markup=ADMIN_MENU_KEYBOARD,
        )
    finally:
        db.close()
        await state.clear()
