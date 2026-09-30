"""
patreon_webhook.py — ตัวรับและประมวลผล Webhook จาก Patreon (API v2)

ฟังก์ชัน:
  • ตรวจสอบลายเซ็น HMAC-MD5 (Header X-Patreon-Signature)
  • ถอดรหัสโครงสร้าง JSON:API ของ Patreon
  • ดึง email, ชื่อ, ยอดเงิน, และชื่อ Tier
  • รัน Webhook HTTP Server (aiohttp) ทำงานคู่กับบอท Discord
"""

import os
import hmac
import hashlib
import json
from typing import Callable, Awaitable, Any
from aiohttp import web

# Callback type signature: async (event_type: str, patron_info: dict) -> None
EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


def verify_signature(body_bytes: bytes, signature_header: str | None, secret: str) -> bool:
    """ตรวจสอบ HMAC-MD5 Signature ที่ส่งมาจาก Patreon"""
    if not secret:
        # ถ้าไม่ได้ตั้ง Secret ไว้ อนุญาตให้ผ่าน (สำหรับกรณี dev/test)
        return True
    if not signature_header:
        return False

    computed = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.md5).hexdigest()
    return hmac.compare_digest(computed.lower(), signature_header.strip().lower())


def parse_patreon_payload(payload: dict) -> dict[str, Any]:
    """
    แกะข้อมูลจาก Patreon JSON:API:
    คืนค่า dict ที่มี:
      - email: str
      - full_name: str
      - patron_status: str (active_patron, former_patron, declined_patron)
      - tier_title: str (ชื่อ Tier ที่ผู้ใช้เลือก)
      - tier_id: str | None
      - amount_cents: int
      - discord_id: str | None
    """
    data = payload.get("data", {})
    attrs = data.get("attributes", {})
    relationships = data.get("relationships", {})
    included = payload.get("included", [])

    # ── 1) ข้อมูลพื้นฐานของผู้สนับสนุน ──────────────────────────────────────────
    email = attrs.get("email", "").strip().lower()
    full_name = attrs.get("full_name", "") or attrs.get("vanity", "") or "Patreon Member"
    patron_status = attrs.get("patron_status", "")
    amount_cents = attrs.get("pledge_amount_cents", 0) or attrs.get("currently_entitled_amount_cents", 0)

    # ── 2) ค้นหาชื่อ Tier จาก currently_entitled_tiers ────────────────────────
    tier_title = "None"
    tier_id = None
    tier_data = relationships.get("currently_entitled_tiers", {}).get("data", [])
    entitled_tier_ids = [t.get("id") for t in tier_data if t.get("id")] if isinstance(tier_data, list) else []

    # Map id -> tier attributes from included
    tiers_map = {}
    user_obj = None
    for item in included:
        itype = item.get("type")
        if itype == "tier":
            tiers_map[item.get("id")] = item.get("attributes", {})
        elif itype == "user":
            user_obj = item

    if entitled_tier_ids:
        # เลือก Tier ที่ราคาแพงที่สุด — ลำดับใน list ไม่การันตี และคนที่อัปเกรด
        # กลางรอบจะมีสิทธิ์หลาย Tier พร้อมกันจนสิ้นรอบบิล
        def _amount(tid):
            return (tiers_map.get(tid) or {}).get("amount_cents") or 0
        best_id = max(entitled_tier_ids, key=_amount)
        tier_id = best_id
        if best_id in tiers_map:
            tier_title = tiers_map[best_id].get("title", f"Tier {best_id}")
    elif amount_cents and amount_cents > 0:
        tier_title = f"Custom Pledge (${amount_cents / 100:.2f})"

    # ── 3) ตรวจสอบอีเมลจาก user object ใน included (ถ้าใน data ไม่มี) ────────────
    if not email and user_obj:
        user_attrs = user_obj.get("attributes", {})
        email = user_attrs.get("email", "").strip().lower()
        if not full_name or full_name == "Patreon Member":
            full_name = user_attrs.get("full_name") or full_name

    # ── 4) ดึง Discord User ID (ถ้ามีเชื่อมต่อไว้กับ Patreon) ───────────────────
    discord_id = None
    if user_obj:
        social_connections = user_obj.get("attributes", {}).get("social_connections", {})
        discord_conn = social_connections.get("discord")
        if discord_conn and isinstance(discord_conn, dict):
            discord_id = discord_conn.get("user_id")

    return {
        "email": email,
        "full_name": full_name,
        "patron_status": patron_status,
        "tier_title": tier_title,
        "tier_id": tier_id,
        "amount_cents": amount_cents,
        "discord_id": discord_id,
    }


class PatreonWebhookServer:
    def __init__(self, port: int, secret: str, event_handler: EventHandler):
        self.port = port
        self.secret = secret
        self.event_handler = event_handler
        self.app = web.Application()
        self.runner: web.AppRunner | None = None
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_post("/patreon/webhook", self._handle_webhook)
        self.app.router.add_get("/patreon/health", self._handle_health)
        self.app.router.add_get("/", self._handle_health)

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "service": "Reina Patreon Webhook Server"})

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        body_bytes = await request.read()
        sig = request.headers.get("X-Patreon-Signature")
        event_type = request.headers.get("X-Patreon-Event", "members:pledge:update")

        # ── ตรวจสอบ Signature ────────────────────────────────────────────────
        if not verify_signature(body_bytes, sig, self.secret):
            print(f"[Patreon Webhook] ❌ Invalid signature from {request.remote}")
            return web.Response(status=401, text="Invalid webhook signature")

        try:
            payload = json.loads(body_bytes.decode("utf-8"))
        except Exception as e:
            print(f"[Patreon Webhook] ❌ JSON decode error: {e}")
            return web.Response(status=400, text="Bad JSON format")

        # ── แกะข้อมูล ────────────────────────────────────────────────────────
        info = parse_patreon_payload(payload)
        print(f"[Patreon Webhook] 📥 Received event '{event_type}' for {info['email']} (Tier: {info['tier_title']})")

        # ── ส่งให้บอทประมวลผลต่อ ─────────────────────────────────────────────
        try:
            await self.event_handler(event_type, info)
        except Exception as e:
            print(f"[Patreon Webhook] ⚠️ Error in event handler: {e}")

        return web.Response(status=200, text="Webhook received")

    async def start(self):
        """เริ่มรัน Web Server"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", self.port)
        await site.start()
        print(f"🌐 Patreon Webhook server listening on http://0.0.0.0:{self.port}/patreon/webhook")

    async def stop(self):
        """หยุด Web Server"""
        if self.runner:
            await self.runner.cleanup()
            print("🛑 Patreon Webhook server stopped")
