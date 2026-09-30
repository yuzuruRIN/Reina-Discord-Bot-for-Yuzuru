"""
send_test_webhook.py — ยิง Webhook ปลอมเลียนแบบ Patreon เพื่อทดสอบระบบ

ปลายทางคือ FastAPI ใน repo yuzuru_game_dev ที่ deploy อยู่บน Render
(endpoint: POST /webhook/patreon)

สคริปต์จะเซ็น HMAC-MD5 ด้วย PATREON_WEBHOOK_SECRET ใน .env ให้เหมือน Patreon ของจริง
ดูผลได้จาก JSON ที่ตอบกลับมาเลย ไม่ต้องไปเปิด Supabase:
    action = upserted    -> เขียน/อัปเดต tier สำเร็จ
    action = blacklisted -> ยกเลิก pledge สำเร็จ
    action = skipped     -> ไม่มี tier ที่เสียเงิน (Free) เลยไม่บันทึก
    action = skipped_dev -> อีเมลอยู่ใน DEV_EMAILS

ตัวอย่างการใช้:
  # อัปเกรด Tier (ค่าเริ่มต้น = ยิงขึ้น Render จริง)
  python send_test_webhook.py --tier "Gold"

  # สมาชิกใหม่ / ยกเลิก
  python send_test_webhook.py --event create --tier "Bronze" --amount 300
  python send_test_webhook.py --event delete

  # ยืนยันว่ายิงซ้ำแล้วผลเหมือนเดิม (idempotent)
  python send_test_webhook.py --repeat 2

  # ยิงใส่เครื่องตัวเองแทน (uvicorn main:app --port 8000)
  python send_test_webhook.py --url http://127.0.0.1:8000/webhook/patreon

⚠️ การยิงใส่ Render จะเขียนแถวจริงลงตาราง member_list ใน Supabase
   ทดสอบเสร็จอย่าลืมลบแถวที่ email = tester@example.com ทิ้ง
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
import urllib.error

from dotenv import load_dotenv

# Windows console เป็น cp874/cp1252 พิมพ์ emoji/ภาษาไทยไม่ได้ → บังคับเป็น UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

load_dotenv()

EVENT_MAP = {
    "create": "members:pledge:create",
    "update": "members:pledge:update",
    "delete": "members:pledge:delete",
}


def build_payload(email, name, tier_title, tier_id, amount_cents, patron_status, discord_id):
    """สร้าง JSON:API payload หน้าตาเหมือนที่ Patreon ส่งมาจริง"""
    return {
        "data": {
            "type": "member",
            "id": "11111111-2222-3333-4444-555555555555",
            "attributes": {
                "email": email,
                "full_name": name,
                "patron_status": patron_status,
                "currently_entitled_amount_cents": amount_cents,
                "pledge_amount_cents": amount_cents,
                "lifetime_support_cents": amount_cents * 3,
                "last_charge_status": "Paid",
            },
            "relationships": {
                "currently_entitled_tiers": {
                    "data": ([{"type": "tier", "id": tier_id}] if tier_id else [])
                },
                "user": {"data": {"type": "user", "id": "99999999"}},
            },
        },
        "included": [
            {
                "type": "tier",
                "id": tier_id or "0",
                "attributes": {"title": tier_title, "amount_cents": amount_cents},
            },
            {
                "type": "user",
                "id": "99999999",
                "attributes": {
                    "email": email,
                    "full_name": name,
                    "social_connections": (
                        {"discord": {"user_id": discord_id}} if discord_id else {}
                    ),
                },
            },
        ],
    }


def send(url, secret, event_header, payload, timeout):
    # ต้องเซ็นจาก "bytes ชุดเดียวกัน" กับที่ส่งออกไปเป๊ะๆ ห้าม re-serialize
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.md5).hexdigest()

    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Patreon-Event": event_header,
            "X-Patreon-Signature": signature,
            "User-Agent": "Patreon HTTP Robot",
        },
    )

    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = time.time() - started
            print(f"✅ {resp.status} {resp.reason}  ({elapsed:.1f}s)")
            print(f"   {resp.read().decode('utf-8', 'replace')[:400]}")
            return resp.status
    except urllib.error.HTTPError as e:
        elapsed = time.time() - started
        print(f"❌ {e.code} {e.reason}  ({elapsed:.1f}s)")
        print(f"   {e.read().decode('utf-8', 'replace')[:400]}")
        return e.code
    except Exception as e:
        elapsed = time.time() - started
        print(f"💥 ส่งไม่สำเร็จ ({elapsed:.1f}s): {type(e).__name__}: {e}")
        return None


def main():
    p = argparse.ArgumentParser(description="ยิง Patreon webhook ปลอมเพื่อทดสอบ")
    p.add_argument("--url", default="https://yuzuru-game-dev.onrender.com/webhook/patreon",
                   help="ปลายทางที่จะยิง (ค่าเริ่มต้น = เซิร์ฟเวอร์จริงบน Render)")
    p.add_argument("--event", default="update", choices=list(EVENT_MAP),
                   help="ชนิด event: create / update / delete")
    p.add_argument("--email", default="tester@example.com")
    p.add_argument("--name", default="ผู้ทดสอบ เรย์นะ")
    p.add_argument("--tier", default="Gold", help="ชื่อ Tier ใหม่หลังอัปเกรด")
    p.add_argument("--tier-id", default="1234567", help="ID ของ Tier")
    p.add_argument("--amount", type=int, default=1000, help="ยอด pledge หน่วยเซนต์ (1000 = $10.00)")
    p.add_argument("--discord-id", default=None, help="Discord user id ที่ผูกไว้ (ถ้ามี)")
    p.add_argument("--secret", default=None, help="ทับค่า PATREON_WEBHOOK_SECRET ใน .env")
    p.add_argument("--repeat", type=int, default=1, help="ส่ง payload เดิมซ้ำกี่ครั้ง (ทดสอบ dedup)")
    p.add_argument("--timeout", type=float, default=120, help="วินาทีที่รอ (Render ตื่นจาก sleep ใช้ ~1 นาที)")
    p.add_argument("--bad-signature", action="store_true", help="ส่งลายเซ็นผิด เพื่อเช็คว่าโดนปฏิเสธจริงไหม")
    args = p.parse_args()

    secret = args.secret or os.getenv("PATREON_WEBHOOK_SECRET", "")
    if not secret:
        print("⚠️  ไม่พบ PATREON_WEBHOOK_SECRET ใน .env — ลายเซ็นจะไม่ตรงกับของจริง")
    if args.bad_signature:
        secret = secret + "_WRONG"

    is_delete = args.event == "delete"
    payload = build_payload(
        email=args.email,
        name=args.name,
        tier_title=("None" if is_delete else args.tier),
        tier_id=(None if is_delete else args.tier_id),
        amount_cents=(0 if is_delete else args.amount),
        patron_status=("former_patron" if is_delete else "active_patron"),
        discord_id=args.discord_id,
    )

    event_header = EVENT_MAP[args.event]
    print(f"🚀 ยิง '{event_header}' ไปที่ {args.url}")
    print(f"   email={args.email}  tier={'None' if is_delete else args.tier}  "
          f"amount=${(0 if is_delete else args.amount)/100:.2f}\n")

    for i in range(args.repeat):
        if args.repeat > 1:
            print(f"── ครั้งที่ {i + 1}/{args.repeat} ──")
        send(args.url, secret, event_header, payload, args.timeout)


if __name__ == "__main__":
    main()
