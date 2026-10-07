"""
slip_reader.py — ใช้ Claude Vision (AI) อ่าน/ตรวจสอบสลิปโอนเงิน

ตรวจ 3 อย่าง: ชื่อผู้รับตรงไหม, จำนวนเงิน, และเลขอ้างอิง (ref) ไว้กันสลิปซ้ำ
คืน SlipResult เสมอ (ยกเว้น error ระดับ API ของ Claude ที่ raise ออกไปให้ bot.py จัดการ)

หมายเหตุ: AI อ่านยอด/ชื่อจากรูป ป้องกันสลิปตัดต่อ 100% ไม่ได้ (ยอดในรูปแก้ได้)
"""

import anthropic
import base64
import httpx
import re
import json
from dataclasses import dataclass

# โมเดลอ่านสลิป — Haiku สลับ "จาก"/"ไปที่" และอ่านเลขอ้างอิงยาวผิดหลักได้ จึงใช้รุ่นที่แม่นกว่า
SLIP_MODEL = "claude-sonnet-5-5"


@dataclass
class SlipResult:
    is_slip: bool                    # True = เป็นสลิปจริง
    amount: float | None             # จำนวนเงินที่โอน (บาท), None ถ้าอ่านไม่ได้
    ref: str | None = None           # เลขอ้างอิงรายการ (ใช้เช็กสลิปซ้ำ)
    recipient: str | None = None     # ชื่อผู้รับเงินในสลิป
    recipient_match: bool = True     # ชื่อผู้รับตรงกับที่กำหนดไหม (default True = ไม่ตรวจ)


# ── Entry point ───────────────────────────────────────────────────────────────
async def read_slip_from_url(image_url: str, expected_names: list[str] | None = None) -> SlipResult:
    """ดาวน์โหลดรูปจาก URL แล้วส่ง Claude Vision วิเคราะห์"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(image_url, timeout=15)
        resp.raise_for_status()
        image_bytes = resp.content
        content_type = resp.headers.get("content-type", "image/jpeg")

    if "webp" in content_type:
        media_type = "image/webp"
    elif "png" in content_type:
        media_type = "image/png"
    else:
        media_type = "image/jpeg"

    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    return await _analyze(b64, media_type, expected_names)


# ── ดึง JSON ก้อนแรกจากคำตอบของ AI ────────────────────────────────────────────
def _extract_json(text: str) -> dict:
    """
    AI บางครั้งพ่วงข้อความก่อน/หลัง JSON (หรือใส่ markdown fence) ทำให้ json.loads พัง
    ("Extra data: ...") → ฟังก์ชันนี้ตัด fence แล้วอ่านเฉพาะ JSON object ก้อนแรก
    ส่วนที่เหลือต่อท้ายจะถูกมองข้าม
    """
    raw = re.sub(r"```[a-zA-Z]*", "", text).strip()  # ตัด markdown fence
    start = raw.find("{")
    if start == -1:
        raise ValueError(f"ไม่พบ JSON ในคำตอบของ AI: {raw[:200]}")
    # raw_decode อ่าน JSON ก้อนแรกจากตำแหน่ง start แล้วหยุด (ไม่สนข้อความต่อท้าย)
    obj, _end = json.JSONDecoder().raw_decode(raw, start)
    if not isinstance(obj, dict):
        raise ValueError(f"คำตอบของ AI ไม่ใช่ JSON object: {raw[:200]}")
    return obj


# ── ตรวจชื่อผู้รับด้วยโค้ดเอง (ไม่เชื่อ boolean ของ AI อย่างเดียว) ─────────────
_TITLES = ("นางสาว", "น.ส.", "นาย", "นาง", "mr.", "mrs.", "miss", "ms.")


def _norm_name(name: str) -> str:
    n = re.sub(r"\s+", "", (name or "").lower())
    for t in _TITLES:
        if n.startswith(t):
            n = n[len(t):]
            break
    return n


def name_matches(recipient: str | None, expected_names: list[str]) -> bool:
    """
    ชื่อที่อ่านได้ 'ตรง' ชื่อที่ยอมรับไหม — ยืดหยุ่นคำนำหน้า/เว้นวรรค และชื่อถูกตัด/ปิดบังบางส่วน
    (เช่น "อธิพันธ์ พ." ตรงกับ "อธิพันธ์ พงษ์มั่น") แต่ต้องตรงชื่อจริงทั้งก้อน ไม่ใช่แค่สั้นเกินไป
    """
    r = _norm_name(recipient or "")
    if len(r) < 4:
        return False
    for e in expected_names:
        e = _norm_name(e)
        if r == e or e.startswith(r.rstrip(".")) or r.startswith(e):
            return True
    return False


# ── Claude Vision analysis ────────────────────────────────────────────────────
async def _analyze_once(b64_image: str, media_type: str, expected_names: list[str] | None = None) -> SlipResult:
    client = anthropic.AsyncAnthropic()

    names = expected_names or []
    if names:
        names_text = " หรือ ".join(f'"{n}"' for n in names)
        recipient_rule = (
            f'- recipient_match = true ถ้าชื่อ "ผู้รับเงิน" ตรงกับ {names_text} '
            'โดยยืดหยุ่นกับคำนำหน้า (นาย/นางสาว/นาง), การเว้นวรรค และการปิดบังบางส่วน '
            '(เช่น "อธิพันธ์ พ." หรือ "นาย อธิพันธ์ พงษ์มั่น" ให้ถือว่าตรง)\n'
            '- recipient_match = false ถ้าผู้รับเป็นคนอื่นชัดเจน หรืออ่านชื่อผู้รับไม่ได้'
        )
    else:
        recipient_rule = '- recipient_match = true เสมอ (ไม่มีการกำหนดชื่อผู้รับ)'

    prompt = f"""คุณเป็นผู้ตรวจสอบสลิปโอนเงิน (รองรับทั้งสลิปธนาคารและ TrueMoney Wallet)

วิเคราะห์รูปภาพนี้และตอบในรูปแบบ JSON เท่านั้น ไม่ต้องมีคำอธิบายเพิ่มเติม:

{{
  "is_slip": true/false,
  "amount": <จำนวนเงิน เป็นตัวเลขทศนิยม หรือ null ถ้าไม่ชัดเจน>,
  "ref": "<เลขที่อ้างอิง/รหัสรายการของสลิป หรือ null ถ้าไม่มี>",
  "sender": "<ชื่อผู้โอน/ผู้ส่งเงิน (ช่อง 'จาก') หรือ null>",
  "recipient": "<ชื่อผู้รับเงิน/บัญชีปลายทาง (ช่อง 'ไปที่') หรือ null>",
  "recipient_match": true/false,
  "currency": "THB" หรือสกุลเงินอื่น,
  "confidence": "high"/"medium"/"low"
}}

กฎ:
- is_slip = true เฉพาะเมื่อเป็นสลิปโอนเงินจริง (มีข้อมูลผู้รับ, วันเวลา, จำนวนเงิน)
- is_slip = false ถ้าเป็นรูปอื่น, รูปสลิปปลอม, หรือไม่ชัดเจนพอ
- amount ให้เป็นตัวเลขล้วน ไม่มีเครื่องหมายคอมม่าหรือสัญลักษณ์สกุลเงิน
- ref คือเลขอ้างอิงเฉพาะของรายการ (เช่น "รหัสอ้างอิง", "เลขที่รายการ", "เลขที่อ้างอิง", "หมายเลขการทำรายการ", "Transaction ID", "Reference No.") ให้ดึงเป็นข้อความตามที่เห็น ถ้าหาไม่เจอให้เป็น null
  ถ้าสลิปมีเลขอ้างอิงหลายชุด (เช่น "หมายเลขอ้างอิง" สั้น ๆ กับ "เลขที่อ้างอิง" ยาว ๆ) ให้เลือกชุดที่ **ยาวที่สุด** และคัดลอกทีละหลักอย่างระวัง
- สลิปธนาคารมักมี 2 บล็อก: "จาก" = ผู้โอน (sender) และ "ไปที่" = ผู้รับ (recipient) อย่าสลับกันเด็ดขาด
- recipient คือชื่อใต้ป้าย "ไปที่"/"ผู้รับ"/"โอนไปยัง" (ไม่ใช่ผู้โอน) ให้ดึงตามที่เห็น
{recipient_rule}"""

    message = await client.messages.create(
        model=SLIP_MODEL,
        max_tokens=2048,  # เผื่อบล็อก thinking ของโมเดล
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64_image,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    # โมเดลบางรุ่นตอบบล็อก thinking นำหน้า → อ่านเฉพาะบล็อกข้อความ
    text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
    data = _extract_json(text)

    is_slip = bool(data.get("is_slip", False))
    amount_raw = data.get("amount")
    ref_raw = data.get("ref")
    ref = str(ref_raw).strip() if ref_raw else None

    recipient_raw = data.get("recipient")
    recipient = str(recipient_raw).strip() if recipient_raw else None
    if not (expected_names or []):
        recipient_match = True
    else:
        # ตรงถ้า AI บอกว่าตรง หรือชื่อที่อ่านได้ตรงชื่อที่ยอมรับตามการเทียบของโค้ดเอง
        recipient_match = bool(data.get("recipient_match", False)) or name_matches(recipient, expected_names)

    if not is_slip:
        return SlipResult(is_slip=False, amount=None, ref=None,
                          recipient=recipient, recipient_match=recipient_match)

    amount = float(amount_raw) if amount_raw is not None else None
    return SlipResult(is_slip=True, amount=amount, ref=ref,
                      recipient=recipient, recipient_match=recipient_match)


# ── อ่านซ้ำถ้าผู้รับไม่ตรง ──────────────────────────────────────────────────────
MAX_ATTEMPTS = 3


async def _analyze(b64_image: str, media_type: str, expected_names: list[str] | None = None) -> SlipResult:
    """
    AI อ่านสลิปบางใบพลาดแบบสุ่ม (เช่น สลับช่อง 'จาก'/'ไปที่' จนเอาชื่อผู้โอนมาเป็นผู้รับ)
    จึงอ่านซ้ำได้สูงสุด MAX_ATTEMPTS ครั้งเมื่อ 'เป็นสลิปแต่ผู้รับไม่ตรง' แล้วใช้ผลที่ผ่านก่อน
    (ถ้าอ่านครบแล้วยังไม่ตรงทุกครั้ง ถือว่าไม่ตรงจริง) — error ระดับ API raise ออกไปตามเดิม
    """
    result = await _analyze_once(b64_image, media_type, expected_names)
    for _ in range(MAX_ATTEMPTS - 1):
        if not (result.is_slip and not result.recipient_match):
            break
        result = await _analyze_once(b64_image, media_type, expected_names)
    return result
