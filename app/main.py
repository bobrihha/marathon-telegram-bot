import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .config import ADMIN_IDS, BOT_TOKEN, SUPPORT_CONTACT
from .db.dal import SessionLocal, init_db
from .db.models import CurrentGroup, Payment, User
from .handlers.admin import ADMIN_MENU, ADMIN_MENU_BUTTONS, ADMIN_MENU_KEYBOARD, router as admin_router
from .handlers.join_requests import router as join_router
from .webhooks import start_webhook_server, stop_webhook_server

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required to start the bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(join_router)
dp.include_router(admin_router)

BUTTON_CHECK_PAYMENT = "Проверить оплату"
BUTTON_SUPPORT = "Поддержка"
SUPPORT_CANCEL = "Отмена"
SUPPORT_REPLY_BUTTON = "Ответить"
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BUTTON_CHECK_PAYMENT)],
        [KeyboardButton(text=BUTTON_SUPPORT)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Введите email или телефон для проверки оплаты",
)

SUPPORT_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=SUPPORT_CANCEL)],
        [KeyboardButton(text=BUTTON_CHECK_PAYMENT)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Опишите проблему",
)

ADMIN_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=SUPPORT_CANCEL)],
        [KeyboardButton(text=ADMIN_MENU)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Введите ответ пользователю",
)


class SupportStates(StatesGroup):
    waiting_message = State()


class AdminReplyStates(StatesGroup):
    waiting_reply = State()


def normalize_phone(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def phone_variants(value: str) -> tuple[str | None, str | None]:
    digits = normalize_phone(value)
    if not digits:
        return None, None
    last10 = digits[-10:] if len(digits) >= 10 else digits
    return digits, last10


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я бот марафона.\n\n"
        "Я буду выдавать доступ в закрытую группу после оплаты.\n"
        "Нажми кнопку ниже и отправь свой email или телефон для проверки оплаты.",
        reply_markup=MAIN_KEYBOARD,
    )


@dp.message(Command("add_test_payment"))
async def add_test_payment(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return

    if not message.text:
        return

    parts = message.text.split()
    if len(parts) not in {3, 4}:
        await message.answer(
            "Формат: /add_test_payment <order_id> <email> [телефон]"
        )
        return

    order_id = parts[1]
    email = parts[2]
    phone = normalize_phone(parts[3]) if len(parts) == 4 else None

    db: Session = SessionLocal()
    try:
        payment = Payment(
            order_id=order_id,
            email=email,
            phone=phone,
            status="paid",
            created_at=datetime.utcnow(),
            used=False,
        )
        db.add(payment)
        db.commit()
        await message.answer(f"Тестовая оплата добавлена: {order_id} / {email}")
    finally:
        db.close()


@dp.message(Command("set_group"))
async def set_group(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return

    if not message.text:
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer(
            "Формат: /set_group <invite_link> <название группы одной строкой>"
        )
        return

    invite_link = parts[1]
    group_name = parts[2]

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
            f"Текущая группа установлена:\n{group_name}\n{invite_link}"
        )
    finally:
        db.close()


@dp.message(F.chat.type == "private", F.text == BUTTON_CHECK_PAYMENT)
async def prompt_payment_check(message: Message) -> None:
    await message.answer(
        "Введите email или телефон, который вы указали при оплате.",
        reply_markup=MAIN_KEYBOARD,
    )


@dp.message(F.chat.type == "private", F.text == BUTTON_SUPPORT)
async def show_support(message: Message, state: FSMContext) -> None:
    await state.set_state(SupportStates.waiting_message)
    lines = [
        "Опишите проблему одним сообщением — я передам администратору.",
        "Если хотите проверить оплату, нажмите «Проверить оплату».",
    ]
    if SUPPORT_CONTACT:
        lines.append(f"Можно написать напрямую: {SUPPORT_CONTACT}")
    await message.answer("\n".join(lines), reply_markup=SUPPORT_KEYBOARD)


@dp.message(SupportStates.waiting_message)
async def handle_support_message(message: Message, state: FSMContext) -> None:
    if not message.text:
        await message.answer("Пожалуйста, напишите сообщение текстом.")
        return

    text = message.text.strip()
    if text == SUPPORT_CANCEL:
        await state.clear()
        await message.answer("Ок, отменено.", reply_markup=MAIN_KEYBOARD)
        return
    if text == BUTTON_CHECK_PAYMENT:
        await state.clear()
        await prompt_payment_check(message)
        return

    user = message.from_user
    if user:
        if user.username:
            user_label = f"{user.full_name} (@{user.username}, id {user.id})"
        else:
            # Create tg://user link for users without username
            user_label = f'<a href="tg://user?id={user.id}">{user.full_name}</a> (id {user.id})'
    else:
        user_label = "Неизвестный пользователь"

    if ADMIN_IDS:
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=SUPPORT_REPLY_BUTTON,
                        callback_data=f"support_reply:{user.id}",
                    )
                ]
            ]
        )
        for admin_id in ADMIN_IDS:
            await bot.send_message(
                admin_id,
                "Новый запрос в поддержку:\n"
                f"{user_label}\n"
                f"Сообщение: {text}",
                reply_markup=reply_markup,
                parse_mode="HTML",
            )

    await state.clear()
    await message.answer(
        "Спасибо! Сообщение отправлено администратору.",
        reply_markup=MAIN_KEYBOARD,
    )


@dp.callback_query(F.data.startswith("support_reply:"))
async def support_reply_callback(query: CallbackQuery, state: FSMContext) -> None:
    if not query.from_user or query.from_user.id not in ADMIN_IDS:
        await query.answer()
        return

    data = query.data.split(":", 1)
    if len(data) != 2 or not data[1].isdigit():
        await query.answer("Некорректный запрос", show_alert=True)
        return

    await state.set_state(AdminReplyStates.waiting_reply)
    await state.update_data(reply_user_id=int(data[1]))
    await query.message.answer(
        "Введите ответ для пользователя.",
        reply_markup=ADMIN_REPLY_KEYBOARD,
    )
    await query.answer()


@dp.message(AdminReplyStates.waiting_reply)
async def handle_admin_reply(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in ADMIN_IDS:
        return

    if not message.text:
        await message.answer("Пожалуйста, напишите ответ текстом.")
        return

    text = message.text.strip()
    if text in {SUPPORT_CANCEL, ADMIN_MENU}:
        await state.clear()
        await message.answer("Ок, отменено.", reply_markup=ADMIN_MENU_KEYBOARD)
        return

    data = await state.get_data()
    reply_user_id = data.get("reply_user_id")
    if not reply_user_id:
        await state.clear()
        await message.answer("Не вижу получателя, начни заново.", reply_markup=ADMIN_MENU_KEYBOARD)
        return

    try:
        await bot.send_message(
            reply_user_id,
            f"Ответ поддержки:\n{text}",
        )
        await message.answer("Ответ отправлен пользователю.", reply_markup=ADMIN_MENU_KEYBOARD)
    except Exception:
        await message.answer(
            "Не удалось отправить ответ пользователю.",
            reply_markup=ADMIN_MENU_KEYBOARD,
        )
    finally:
        await state.clear()


@dp.message(
    StateFilter(None),
    F.chat.type == "private",
    F.text & ~F.text.startswith("/") & ~F.text.in_(ADMIN_MENU_BUTTONS),
)
async def handle_email_or_order(message: Message) -> None:
    if not message.from_user or not message.text:
        return

    text = message.text.strip()
    if text in {BUTTON_CHECK_PAYMENT, BUTTON_SUPPORT}:
        return
    if not text:
        await message.answer("Отправь, пожалуйста, email, телефон или номер заказа.")
        return

    db: Session = SessionLocal()
    try:
        phone, phone_last10 = phone_variants(text)
        if "@" in text:
            payment = (
                db.query(Payment)
                .filter(
                    Payment.email == text,
                    Payment.status == "paid",
                .order_by(Payment.created_at.desc(), Payment.id.desc())
                .first()
            )
            # Find any paid payment (even if used) to check if it's already linked to THIS user
            used_payment = (
                db.query(Payment)
                .filter(Payment.email == text, Payment.status == "paid")
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
            # Find any paid payment (even if used) to check if it's already linked to THIS user
            used_payment = (
                db.query(Payment)
                .filter(Payment.status == "paid")
                .filter(or_(*filters))
                .order_by(Payment.created_at.desc(), Payment.id.desc())
                .first()
            )

        if not payment:
            if used_payment:
                # Check if this used payment BELONGS TO THE CURRENT USER
                # If so, we should allow them to see the link again!
                existing_user = db.query(User).filter(User.payment_id == used_payment.id).first()
                if existing_user and existing_user.telegram_id == str(message.from_user.id):
                    payment = used_payment  # Treat it as valid for this specific user
                else:
                    await message.answer(
                        "Эта оплата уже использована другим пользователем или для доступа.\n"
                        "Если вы оплатили новый поток, пожалуйста, укажите "
                        "новый email/телефон или напишите в поддержку."
                    )
                    return

            await message.answer(
                "Я не нашёл оплаченный заказ по этим данным.\n"
                "Проверь, пожалуйста, правильно ли ты ввёл адрес, "
                "или напиши в поддержку."
            )
            return

        existing_user = db.query(User).filter(User.payment_id == payment.id).first()
        if existing_user and existing_user.telegram_id != str(message.from_user.id):
            await message.answer(
                "Эта оплата уже использована с другим Telegram-аккаунтом.\n"
                "Если это ошибка, напиши, пожалуйста, в поддержку."
            )
            return

        user = db.query(User).filter(User.telegram_id == str(message.from_user.id)).first()
        if not user:
            user = User(
                telegram_id=str(message.from_user.id),
                username=message.from_user.username,
                full_name=message.from_user.full_name,
                payment_id=payment.id,
            )
            db.add(user)
        else:
            user.payment_id = payment.id

        else:
            user.payment_id = payment.id

        # payment.used = True  <-- MOVED to join_requests.py (only when they actually join)

        current_group = db.query(CurrentGroup).order_by(CurrentGroup.id.desc()).first()
        if not current_group:
            await message.answer(
                "Оплата подтверждена, но пока не настроена группа для выдачи доступа.\n"
                "Свяжись с администратором марафона."
            )
            db.commit()
            return

        db.commit()

        join_url = current_group.invite_link
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Вступить в группу 🔐",
                        url=join_url,
                    )
                ]
            ]
        )

        await message.answer(
            "Оплата найдена ✅\n\n"
            f"Группа: {current_group.group_name}\n"
            "Нажми кнопку ниже и отправь заявку на вступление 👇",
            reply_markup=kb,
        )
    finally:
        db.close()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    init_db()
    runner = await start_webhook_server()
    try:
        await dp.start_polling(bot)
    finally:
        await stop_webhook_server(runner)


if __name__ == "__main__":
    asyncio.run(main())
