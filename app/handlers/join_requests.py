from datetime import datetime

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.types import ChatJoinRequest, ChatMemberUpdated

from ..db.dal import SessionLocal
from ..db.models import AccessLog, CurrentGroup, User

router = Router()

_WAS_IN_CHAT = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.CREATOR,
    ChatMemberStatus.RESTRICTED,
}
_NOW_LEFT = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}


@router.chat_join_request(F.chat)
async def approve_join_request(event: ChatJoinRequest) -> None:
    """Auto-approve join requests for users with paid access."""
    import logging
    db = SessionLocal()
    try:
        tg_id = str(event.from_user.id)
        chat_id = str(event.chat.id)
        chat_title = str(event.chat.title)
        
        logging.info("Processing join request for user %s to chat %s (%s)", tg_id, chat_id, chat_title)
        
        user = db.query(User).filter(User.telegram_id == tg_id).first()
        if not user:
            logging.warning("User %s not found in database", tg_id)
            return
        if not user.payment_id:
            logging.warning("User %s has no payment linked", tg_id)
            return

        payment = user.payment
        if not payment:
            logging.error("Linked payment not found for user %s", tg_id)
            return
            
        if payment.status != "paid":
            logging.warning("Payment %s for user %s status is %s, not 'paid'", payment.order_id, tg_id, payment.status)
            return

        # Match group by product tag
        product = (payment.product_name or "").lower()
        logging.info("User %s paid for product: '%s'", tg_id, product)
        
        current_group = None
        if product:
            current_group = db.query(CurrentGroup).filter(
                CurrentGroup.product_tag == product
            ).order_by(CurrentGroup.id.desc()).first()
            if current_group:
                logging.info("Found group %s by product tag '%s'", current_group.id, product)
                
        if not current_group:
            current_group = db.query(CurrentGroup).filter(
                CurrentGroup.product_tag.is_(None)
            ).order_by(CurrentGroup.id.desc()).first()
            if current_group:
                logging.info("Using untagged fallback group %s", current_group.id)
                
        # NO "last-resort: any group" fallback. Approving into the newest group
        # regardless of product let a returning buyer whose linked payment is for
        # an OLD/other product (or empty) slip into the current marathon unpaid.
        # If the payment product matches no configured group, do NOT approve.
        if not current_group:
            logging.warning(
                "NOT approving user %s: payment product '%s' (order %s) matches no configured group — leaving pending",
                tg_id, product, payment.order_id,
            )
            return

        # Check if this group record is already bound to a DIFFERENT chat
        if current_group.chat_id and current_group.chat_id != chat_id:
            logging.warning("Matched group %s is bound to chat %s, but user joined %s", 
                           current_group.id, current_group.chat_id, chat_id)
            # If we have multiple groups, maybe this payment matched the WRONG one?
            # We don't return here yet, we try to see if there's a better match
            # But for now, let's keep it strict or log it.
            return

        if not current_group.chat_id:
            logging.info("Binding group %s to chat_id %s", current_group.id, chat_id)
            current_group.chat_id = chat_id

        logging.info("Approving join request for user %s (order %s) to chat %s", 
                    tg_id, payment.order_id, chat_id)
                    
        log = AccessLog(
            telegram_id=tg_id,
            email=payment.email,
            order_id=payment.order_id,
            group_name=chat_title,
            group_id=chat_id,
            action="granted",
            timestamp=datetime.utcnow(),
            comment="Auto-approved join request",
        )
        db.add(log)
        
        # NOTE: We do NOT mark payment.used = True here anymore.
        # The user needs access to BOTH TG and VK groups with the same payment.
        # The `used` flag is only checked to prevent OTHER users from reusing
        # the same payment, which is handled by the User→Payment link check.
        
        db.commit()
        await event.approve()
        logging.info("Successfully approved join request for %s", tg_id)
        
    except Exception as e:
        logging.exception("Error in approve_join_request: %s", e)
    finally:
        db.close()


@router.chat_member()
async def track_member_leave(event: ChatMemberUpdated) -> None:
    """Log the exact moment a paid member LEAVES or is REMOVED from a marathon
    TG group. Evidentiary record for refund disputes: uses Telegram's own
    event timestamp (event.date), not "whenever we happened to notice", and
    records who initiated it (the member themselves vs an admin)."""
    import logging

    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    if not (old_status in _WAS_IN_CHAT and new_status in _NOW_LEFT):
        return

    member = event.new_chat_member.user
    tg_id = str(member.id)
    chat_id = str(event.chat.id)
    chat_title = str(event.chat.title or chat_id)

    actor = event.from_user
    self_initiated = bool(actor and str(actor.id) == tg_id)
    action = "left_tg" if self_initiated else "kicked_tg"

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == tg_id).first()
        payment = user.payment if user and user.payment_id else None

        actor_label = "себя (вышел сам)" if self_initiated else (
            f"{actor.full_name} (id {actor.id})" if actor else "неизвестно"
        )
        comment = (
            f"{member.full_name} (@{member.username or '-'}) статус стал "
            f"'{new_status}' в «{chat_title}»; инициатор: {actor_label}"
        )
        event_ts = event.date.replace(tzinfo=None) if event.date.tzinfo else event.date

        log = AccessLog(
            telegram_id=tg_id,
            email=payment.email if payment else None,
            order_id=payment.order_id if payment else None,
            group_name=chat_title,
            group_id=chat_id,
            action=action,
            timestamp=event_ts,
            comment=comment,
        )
        db.add(log)
        db.commit()
        logging.info(
            "Logged %s for user %s in chat %s at %s", action, tg_id, chat_id, event_ts
        )
    except Exception as e:
        logging.exception("Error in track_member_leave: %s", e)
    finally:
        db.close()
