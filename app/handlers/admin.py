import csv
import os
import tempfile
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..config import ADMIN_IDS
from ..db.dal import SessionLocal
from ..db.models import AccessLog, CurrentGroup, Payment, User

router = Router()

ADMIN_MENU = "Админ-меню"
ADMIN_SET_GROUP = "Установить группу"
ADMIN_EXPORT_LOGS = "Выгрузить логи"
ADMIN_FIND_PAYMENT = "Найти оплату"
ADMIN_REBIND_PAYMENT = "Перепривязать оплату"
ADMIN_REMOVE_USER = "Удалить участника"
ADMIN_UNBAN_USER = "Разбанить участника"
ADMIN_CANCEL = "Отмена"

ADMIN_MENU_BUTTONS = {
    ADMIN_MENU,
    ADMIN_SET_GROUP,
    ADMIN_EXPORT_LOGS,
    ADMIN_FIND_PAYMENT,
    ADMIN_REBIND_PAYMENT,
    ADMIN_REMOVE_USER,
    ADMIN_UNBAN_USER,
    ADMIN_CANCEL,
}

ADMIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=ADMIN_SET_GROUP)],
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
) -> None:
    db: Session = SessionLocal()
    try:
        current = CurrentGroup(
            chat_id=None,
            group_name=group_name,
            invite_link=invite_link,
        )
        db.add(current)
        db.commit()
        await message.answer(
            f"Текущая группа установлена:\n{group_name}\n{invite_link}",
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
        "В меню есть кнопка «Удалить участника»"
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

    data = await state.get_data()
    invite_link = data.get("invite_link")
    if not invite_link:
        await message.answer("Не вижу invite-link, начни заново.", reply_markup=ADMIN_MENU_KEYBOARD)
        await state.clear()
        return

    await create_current_group(message, invite_link, group_name)
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
