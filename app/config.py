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
VK_CALLBACK_URL = os.getenv(
    "VK_CALLBACK_URL",
    "https://bot.bosforovna-klub.ru/webhooks/vk",
)
SUPPORT_CONTACT = os.getenv("SUPPORT_CONTACT", "")


# VK personal (user) token routing.
# approveRequest / removeUser / getMembers(managers) require the admin's USER
# token. VK treats that token used from a FOREIGN IP as an account hijack and
# blocks the admin's personal page (the bot server is in Amsterdam). Route those
# calls through a Russian proxy. When VK_USER_PROXY is empty, such calls are
# SKIPPED (paused) to protect the admin's account — unless VK_USER_DIRECT_OK=1
# explicitly allows direct (e.g. when the bot itself runs on a RU server).
VK_USER_PROXY = os.getenv("VK_USER_PROXY", "").strip()
VK_USER_DIRECT_OK = os.getenv("VK_USER_DIRECT_OK", "").strip().lower() in {"1", "true", "yes"}

# VK Bot settings
VK_COMMUNITY_TOKEN = os.getenv("VK_COMMUNITY_TOKEN", "")
VK_GROUP_ID = int(os.getenv("VK_GROUP_ID", "0"))
VK_SECRET = os.getenv("VK_SECRET", "")
VK_CONFIRMATION_STRING = os.getenv("VK_CONFIRMATION_STRING", "")
VK_APP_ID = os.getenv("VK_APP_ID", "")
VK_APP_SECRET = os.getenv("VK_APP_SECRET", "")

# VK Target community (for TG→VK forwarding)
VK_TARGET_TOKEN = os.getenv("VK_TARGET_TOKEN", "")
VK_TARGET_GROUP_ID = int(os.getenv("VK_TARGET_GROUP_ID", "0"))
VK_TARGET_MEDIA_TOKEN = os.getenv("VK_TARGET_MEDIA_TOKEN", "")
VK_TARGET_VIDEO_TOKEN = os.getenv("VK_TARGET_VIDEO_TOKEN", "")
