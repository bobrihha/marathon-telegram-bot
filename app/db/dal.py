from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from ..config import DATABASE_URL
from .models import Base

engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _run_migrations()


def _run_migrations() -> None:
    """Apply manual schema migrations for columns that create_all won't add."""
    inspector = inspect(engine)

    # Add vk_id column to users table if missing
    if "users" in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns("users")]
        if "vk_id" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN vk_id VARCHAR"))

    # Add product_tag column to current_group if missing
    if "current_group" in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns("current_group")]
        if "product_tag" not in columns:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE current_group ADD COLUMN product_tag VARCHAR"))

    # Add product_tag column to vk_group if missing
    if "vk_group" in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns("vk_group")]
        new_columns = {
            "product_tag": "VARCHAR",
            "access_token": "TEXT",
            "callback_secret": "VARCHAR",
            "callback_confirmation": "VARCHAR",
            "callback_server_id": "VARCHAR",
            "callback_url": "VARCHAR",
            "callback_configured_at": "DATETIME",
        }
        with engine.begin() as conn:
            for column_name, column_type in new_columns.items():
                if column_name not in columns:
                    conn.execute(
                        text(f"ALTER TABLE vk_group ADD COLUMN {column_name} {column_type}")
                    )
