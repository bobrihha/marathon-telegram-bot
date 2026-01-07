from datetime import datetime

from aiogram import F, Router
from aiogram.types import ChatJoinRequest

from ..db.dal import SessionLocal
from ..db.models import AccessLog, CurrentGroup, User

router = Router()


@router.chat_join_request(F.chat)
async def approve_join_request(event: ChatJoinRequest) -> None:
    """Auto-approve join requests for users with paid access."""
    db = SessionLocal()
    try:
        tg_id = str(event.from_user.id)
        user = db.query(User).filter(User.telegram_id == tg_id).first()
        if not user or not user.payment_id:
            return

        payment = user.payment
        if not payment or payment.status != "paid":
            return

        current_group = (
            db.query(CurrentGroup).order_by(CurrentGroup.id.desc()).first()
        )
        if current_group:
            if current_group.chat_id and current_group.chat_id != str(event.chat.id):
                return
            if not current_group.chat_id:
                current_group.chat_id = str(event.chat.id)

        log = AccessLog(
            telegram_id=tg_id,
            email=payment.email,
            order_id=payment.order_id,
            group_name=str(event.chat.title),
            group_id=str(event.chat.id),
            action="granted",
            timestamp=datetime.utcnow(),
            comment="Auto-approved join request",
        )
        db.add(log)
        
        # Mark payment as used ONLY when they actually join
        payment.used = True
        
        db.commit()

        await event.approve()
    finally:
        db.close()
