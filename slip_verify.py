"""
slip_verify.py — ตรวจสอบสลิปกับธนาคารจริงผ่าน SlipOK API

ทำไมต้องใช้: ตัวเลขยอดเงินในรูปสลิปแก้ไข/ตัดต่อได้ (เช่น 30 → 300)
แต่ QR code ในสลิปเก็บ "เลขอ้างอิงรายการ" ที่ผูกกับธนาคาร SlipOK เอา QR ไปถาม
ธนาคารว่ายอดจริงเท่าไหร่ → ได้ยอดจริงกลับมา ตัดต่อรูปจึงไม่มีผล

สมัครฟรี (แพ็กเกจ OK Basic = 100 สลิป/เดือน) ที่ https://slipok.com
แล้วใส่ค่าใน .env:
    SLIPOK_API_KEY=...        ← จากหน้า API Key ใน SlipOK
    SLIPOK_BRANCH_ID=...      ← เลข Branch ID ของร้าน
"""

import os
import httpx


# ── Exceptions ────────────────────────────────────────────────────────────────
class SlipVerifyError(Exception):
    """base error — พก code/data จริงจาก SlipOK ติดมาด้วยเพื่อ debug/log"""
    def __init__(self, message: str = "", code=None, data=None):
        super().__init__(message)
        self.code = code
        self.data = data


class SlipNotConfigured(SlipVerifyError):
    """ยังไม่ได้ตั้งค่า SLIPOK_API_KEY / SLIPOK_BRANCH_ID"""


class SlipInvalid(SlipVerifyError):
    """สลิปอ่าน QR ไม่ได้ / ไม่ใช่สลิปจริง / QR หมดอายุ → ปฏิเสธ (codes 1005-1008, 1011)"""


class SlipDuplicate(SlipVerifyError):
    """สลิปนี้ถูกใช้ไปแล้ว (SlipOK ตรวจซ้ำฝั่ง server) (code 1012)"""


class SlipWrongReceiver(SlipVerifyError):
    """โอนเข้าบัญชีที่ไม่ตรงกับบัญชีหลักของร้านที่ลงทะเบียนไว้ (code 1014)"""


class SlipPending(SlipVerifyError):
    """ธนาคารขัดข้องชั่วคราว / สลิปธนาคารดีเลย์ (BBL,SCB) → ให้ผู้ใช้ลองใหม่ ไม่ใช่ของปลอม (codes 1009, 1010)"""


class SlipQuotaExceeded(SlipVerifyError):
    """แพ็กเกจหมดอายุ / เกินโควตา → แจ้ง admin (codes 1003, 1004, 1015)"""


class SlipAuthError(SlipVerifyError):
    """API Key / Branch ID ผิด → แจ้ง admin (codes 1001, 1002 / HTTP 401)"""


# ── Config ────────────────────────────────────────────────────────────────────
def _api_key() -> str | None:
    return os.getenv("SLIPOK_API_KEY") or None


def _branch_id() -> str | None:
    return os.getenv("SLIPOK_BRANCH_ID") or None


def is_configured() -> bool:
    """True ถ้าตั้งค่า SlipOK ครบ (มีทั้ง api key และ branch id)"""
    return bool(_api_key() and _branch_id())


# ── Verify ────────────────────────────────────────────────────────────────────
async def verify_by_url(image_url: str) -> dict:
    """
    ส่ง URL รูปสลิปให้ SlipOK ตรวจกับธนาคาร
    คืน dict: {amount, ref, receiver_name, sender_name, receiving_bank, raw}
    หรือ raise: SlipInvalid / SlipDuplicate / SlipQuotaExceeded / SlipAuthError
    """
    if not is_configured():
        raise SlipNotConfigured()

    endpoint = f"https://api.slipok.com/api/line/apikey/{_branch_id()}"
    headers = {
        "x-authorization": _api_key(),
        "Content-Type": "application/json",
    }
    # log=true → ให้ SlipOK บันทึก + ตรวจสลิปซ้ำให้ฝั่ง server ด้วย
    body = {"url": image_url, "log": True}

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(endpoint, headers=headers, json=body)

    # พยายาม parse JSON ไม่ว่าจะ status ไหน (SlipOK ส่ง error เป็น JSON)
    try:
        payload = resp.json()
    except Exception:
        raise SlipVerifyError(f"SlipOK ตอบกลับผิดรูปแบบ (HTTP {resp.status_code})")

    if resp.status_code == 200 and payload.get("success"):
        return _normalize(payload.get("data", {}))

    # ── จัดการ error codes ของ SlipOK (อ้างอิงตาม API Guide v1.13) ───────────
    code = payload.get("code")
    msg = payload.get("message", "")
    data = payload.get("data")  # บาง error (1010,1012,1014) แนบ data มาด้วย

    # ตั้งค่า/บัญชีระบบผิด → แจ้ง admin
    if code in (1001, 1002) or resp.status_code in (401, 403):
        raise SlipAuthError(msg or "invalid api key / branch id", code=code, data=data)

    # แพ็กเกจหมดอายุ / เกินโควตา / ไม่พบแพ็กเกจ → แจ้ง admin
    if code in (1003, 1004, 1015):
        raise SlipQuotaExceeded(msg or "package expired / quota exceeded", code=code, data=data)

    # ขัดข้องชั่วคราว (ธนาคารล่ม / สลิป BBL,SCB ดีเลย์) → ให้ผู้ใช้ลองใหม่ ไม่ใช่ของปลอม
    if code in (1009, 1010):
        raise SlipPending(msg or "verification pending, retry later", code=code, data=data)

    # โอนผิดบัญชี (ไม่ตรงบัญชีหลักของร้านที่ลงทะเบียนใน SlipOK)
    if code == 1014:
        raise SlipWrongReceiver(msg or "receiver account mismatch", code=code, data=data)

    # สลิปซ้ำ
    if code == 1012:
        raise SlipDuplicate(msg or "duplicate slip", code=code, data=data)

    # ที่เหลือ (1000, 1005-1008, 1011, อื่นๆ) = QR เสีย/หมดอายุ/ไม่ใช่สลิปจริง → ปฏิเสธ
    raise SlipInvalid(msg or f"verify failed (code {code})", code=code, data=data)


async def check_quota() -> dict:
    """
    เช็คโควตาคงเหลือ (GET .../quota) — ใช้ทดสอบว่า key/branch ถูกต้อง
    คืน dict ของ data หรือ raise SlipAuthError / SlipVerifyError
    """
    if not is_configured():
        raise SlipNotConfigured()

    endpoint = f"https://api.slipok.com/api/line/apikey/{_branch_id()}/quota"
    headers = {"x-authorization": _api_key()}

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(endpoint, headers=headers)

    try:
        payload = resp.json()
    except Exception:
        raise SlipVerifyError(f"SlipOK ตอบกลับผิดรูปแบบ (HTTP {resp.status_code})")

    if resp.status_code == 200 and payload.get("success"):
        return payload.get("data", {})

    code = payload.get("code")
    msg = payload.get("message", "")
    if code in (1001, 1002) or resp.status_code in (401, 403):
        raise SlipAuthError(msg or "invalid api key / branch id")
    raise SlipVerifyError(msg or f"quota check failed (code {code})")


def _normalize(data: dict) -> dict:
    """แปลง response ของ SlipOK เป็น dict มาตรฐานที่บอทใช้"""
    receiver = data.get("receiver") or {}
    sender = data.get("sender") or {}

    amount = data.get("amount")
    try:
        amount = float(amount) if amount is not None else None
    except (TypeError, ValueError):
        amount = None

    return {
        "amount": amount,
        "ref": data.get("transRef") or None,
        "receiver_name": (receiver.get("displayName") or receiver.get("name") or "").strip() or None,
        "sender_name": (sender.get("displayName") or sender.get("name") or "").strip() or None,
        "receiving_bank": data.get("receivingBank"),
        "trans_date": data.get("transDate"),
        "trans_time": data.get("transTime"),
        "raw": data,
    }
