from datetime import datetime

from aiogram import F, Router
from aiogram.types import ChatJoinRequest

from ..db.dal import SessionLocal
from ..db.models import AccessLog, CurrentGroup, User

router = Router()


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
                
        if not current_group:
            current_group = (
                db.query(CurrentGroup).order_by(CurrentGroup.id.desc()).first()
            )
            if current_group:
                logging.info("Using last resort group %s", current_group.id)

        if not current_group:
            logging.error("No groups found in database at all")
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
        
        # Mark payment as used ONLY when they actually join
        payment.used = True
        
        db.commit()
        await event.approve()
        logging.info("Successfully approved join request for %s", tg_id)
        
    except Exception as e:
        logging.exception("Error in approve_join_request: %s", e)
    finally:
        db.close()
