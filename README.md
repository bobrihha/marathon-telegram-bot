# Tilda Marathon Bot

Telegram/VK access bot for paid marathons.

## Flow

1. Prodamus/Tilda sends a payment webhook to `/webhooks/prodamus` or `/webhooks/tilda`.
2. The bot stores `order_id`, email/phone, status and product name in SQLite.
3. A buyer sends the same phone/email/order id to the Telegram bot or VK community bot.
4. The bot binds the payment to that person and returns the matching Telegram and VK access links by `product_tag`.
5. Telegram join requests are approved only for bound paid users.
6. VK closed-community join requests are approved only for bound paid VK users when the VK community token is configured.

## VK Setup

In Telegram admin menu, use `Установить ВК-группу`.

The bot asks for:

- VK invite/community link.
- Display name.
- `product_tag` matching the payment product name, or `нет` for fallback.
- VK community access token, or `нет` to save only the link.

When a token is provided, the bot tries to:

- Detect the VK community id.
- Get the Callback API confirmation code.
- Add this server as a VK Callback API server.
- Enable `message_new`, `group_join`, and `group_leave`.

The public callback URL is configured by `VK_CALLBACK_URL`.

## Environment

See `.env.example`.

Important variables:

- `BOT_TOKEN`
- `ADMIN_IDS`
- `DATABASE_URL`
- `WEBHOOK_TOKEN`
- `VK_CALLBACK_URL`
- `VK_COMMUNITY_TOKEN`, `VK_SECRET`, `VK_CONFIRMATION_STRING` for legacy single-community fallback.

## Notes

- A static VK invite link alone is not enough to identify the buyer's VK account. For protected VK access, the buyer must message the VK community bot and verify the payment there.
- When VK verification succeeds and the Telegram group `chat_id` is known, the bot tries to create a one-use Telegram invite link for that buyer.
- One payment can be linked to one Telegram user and one VK user.
