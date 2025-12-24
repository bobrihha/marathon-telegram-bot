from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String, unique=True, index=True)
    email = Column(String, index=True)
    phone = Column(String, index=True)
    status = Column(String, default="paid")
    product_name = Column(String, nullable=True)
    created_at = Column(DateTime)
    used = Column(Boolean, default=False)

    user = relationship("User", back_populates="payment", uselist=False)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(String, unique=True, index=True)
    username = Column(String, nullable=True)
    full_name = Column(String, nullable=True)

    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True)
    payment = relationship("Payment", back_populates="user")

    __table_args__ = (UniqueConstraint("telegram_id", name="uq_users_telegram_id"),)


class AccessLog(Base):
    __tablename__ = "access_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(String, index=True)
    email = Column(String, index=True)
    order_id = Column(String, index=True)
    group_name = Column(String)
    group_id = Column(String)
    action = Column(String)
    timestamp = Column(DateTime)
    comment = Column(Text, nullable=True)


class CurrentGroup(Base):
    __tablename__ = "current_group"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(String)
    group_name = Column(String)
    invite_link = Column(String)
