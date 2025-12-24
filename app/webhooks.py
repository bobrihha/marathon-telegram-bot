import json
import logging
from datetime import datetime
from typing import Any, Mapping, Optional
from urllib.parse import parse_qsl

from aiohttp import web
from sqlalchemy.orm import Session

from .config import WEBHOOK_HOST, WEBHOOK_PORT, WEBHOOK_TOKEN
from .db.dal import SessionLocal
from .db.models import Payment


def _get_first(payload: Mapping[str, Any], keys: list[str]) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _parse_timestamp(value: Optional[str]) -> datetime:
    if not value:
        return datetime.utcnow()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.utcnow()


def _normalize_status(value: Optional[str]) -> str:
    if not value:
        return "paid"
    normalized = value.strip().lower()
    mapping = {
        "success": "paid",
        "succeeded": "paid",
        "paid": "paid",
        "completed": "paid",
        "cancelled": "cancelled",
        "canceled": "cancelled",
        "failed": "failed",
        "pending": "pending",
    }
    return mapping.get(normalized, normalized)


def _normalize_phone(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits or None


async def _read_payload(request: web.Request) -> dict[str, Any]:
    raw = await request.read()
    if not raw:
        return {}

    content_type = request.content_type or ""
    if content_type.startswith("application/json"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(data, dict):
            return data
        return {"data": data}

    text = raw.decode(errors="ignore")
    parsed = dict(parse_qsl(text, keep_blank_values=True))
    if parsed:
        return parsed

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return data
    return {"data": data}


def _is_authorized(request: web.Request, payload: Mapping[str, Any]) -> bool:
    if not WEBHOOK_TOKEN:
        return True

    header_token = request.headers.get("X-Webhook-Token", "")
    query_token = request.query.get("token", "")
    body_token = str(payload.get("token", "")).strip()
    path_token = request.match_info.get("token", "")
    return WEBHOOK_TOKEN in (header_token, query_token, body_token, path_token)


async def handle_prodamus(request: web.Request) -> web.Response:
    payload = await _read_payload(request)
    logging.info(f"Received webhook payload: {json.dumps(payload, ensure_ascii=False)}")
    if not _is_authorized(request, payload):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    order_id = _get_first(payload, ["order_id", "orderId", "order", "payment_id"])
    email = _get_first(payload, ["email", "customer_email", "client_email", "buyer_email"])
    phone_raw = _get_first(
        payload,
        [
            "phone",
            "phone_number",
            "customer_phone",
            "client_phone",
            "buyer_phone",
            "telephone",
            "tel",
        ],
    )
    status_raw = _get_first(payload, ["status", "payment_status", "paymentStatus"])
    product_name = _get_first(payload, ["product_name", "product", "title", "name"])
    created_raw = _get_first(payload, ["created_at", "createdAt", "date", "created"])

    phone = _normalize_phone(phone_raw)

    if not order_id or (not email and not phone):
        return web.json_response(
            {"ok": False, "error": "missing order_id or email/phone"},
            status=400,
        )

    status = _normalize_status(status_raw)
    created_at = _parse_timestamp(created_raw)

    db: Session = SessionLocal()
    try:
        payment = db.query(Payment).filter(Payment.order_id == order_id).first()
        if not payment:
            payment = Payment(
                order_id=order_id,
                email=email,
                phone=phone,
                status=status,
                product_name=product_name,
                created_at=created_at,
                used=False,
            )
            db.add(payment)
        else:
            if email:
                payment.email = email
            if phone:
                payment.phone = phone
            payment.status = status
            payment.product_name = product_name or payment.product_name
            payment.created_at = created_at
        db.commit()
    finally:
        db.close()

    return web.json_response({"ok": True})


async def handle_tilda(request: web.Request) -> web.Response:
    return await handle_prodamus(request)


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/webhooks/prodamus", handle_prodamus)
    app.router.add_post("/webhooks/prodamus/{token}", handle_prodamus)
    app.router.add_post("/webhooks/tilda", handle_tilda)
    app.router.add_post("/webhooks/tilda/{token}", handle_tilda)
    return app


async def start_webhook_server() -> web.AppRunner:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
    await site.start()
    return runner


async def stop_webhook_server(runner: web.AppRunner) -> None:
    await runner.cleanup()
