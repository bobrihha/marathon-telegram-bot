import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./db.sqlite3")
ADMIN_IDS = list(
    filter(
        None,
        [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()],
    )
)

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8080"))
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "")

# VK Bot settings
VK_COMMUNITY_TOKEN = os.getenv("VK_COMMUNITY_TOKEN", "")
VK_GROUP_ID = int(os.getenv("VK_GROUP_ID", "0"))
VK_SECRET = os.getenv("VK_SECRET", "")
VK_CONFIRMATION_STRING = os.getenv("VK_CONFIRMATION_STRING", "")
