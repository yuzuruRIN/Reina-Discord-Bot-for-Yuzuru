"""
slip_reader.py — ใช้ Claude Vision อ่านสลิปโอนเงิน
คืน SlipResult(is_slip, amount) หรือ raise exception
"""

import anthropic
import base64
import httpx
import re
from dataclasses import dataclass


@dataclass
class SlipResult:
    is_slip: bool        # True = เป็นสลิปจริง
    amount: float | None # จำนวนเงินที่โอน (บาท), None ถ้าอ่านไม่ได้
    ref: str | None = None  # เลขที่อ้างอิง/รหัสรายการของสลิป (ใช้เช็กสลิปซ้ำ)
    recipient: str | None = None    # ชื่อผู้รับเงินในสลิป
    recipient_match: bool = True     # ชื่อผู้รับตรงกับที่กำหนดไหม (default True = ไม่ตรวจ)


async def read_slip_from_url(image_url: str, expected_names: list[str] | None = None) -> SlipResult:
    """ดาวน์โหลดรูปจาก URL แล้วส่ง Claude Vision วิเคราะห์"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(image_url, timeout=15)
        resp.raise_for_status()
        image_bytes = resp.content
        content_type = resp.headers.get("content-type", "image/jpeg")

    # รองรับ webp → ให้เป็น image/webp
    if "webp" in content_type:
        media_type = "image/webp"
    elif "png" in content_type:
        media_type = "image/png"
    else:
        media_type = "image/jpeg"

    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    return await _analyze(b64, media_type, expected_names)


async def _analyze(b64_image: str, media_type: str, expected_names: list[str] | None = None) -> SlipResult:
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

    prompt = f"""คุณเป็นผู้ตรวจสอบสลิปโอนเงินธนาคาร

วิเคราะห์รูปภาพนี้และตอบในรูปแบบ JSON เท่านั้น ไม่ต้องมีคำอธิบายเพิ่มเติม:

{{
  "is_slip": true/false,
  "amount": <จำนวนเงิน เป็นตัวเลขทศนิยม หรือ null ถ้าไม่ชัดเจน>,
  "ref": "<เลขที่อ้างอิง/รหัสรายการของสลิป หรือ null ถ้าไม่มี>",
  "recipient": "<ชื่อผู้รับเงิน/บัญชีปลายทาง หรือ null>",
  "recipient_match": true/false,
  "currency": "THB" หรือสกุลเงินอื่น,
  "confidence": "high"/"medium"/"low"
}}

กฎ:
- is_slip = true เฉพาะเมื่อเป็นสลิปโอนเงินจริง (มีข้อมูลธนาคาร, ผู้รับ, วันเวลา, จำนวนเงิน)
- is_slip = false ถ้าเป็นรูปอื่น, รูปสลิปปลอม, หรือไม่ชัดเจนพอ
- amount ให้เป็นตัวเลขล้วน ไม่มีเครื่องหมายคอมม่าหรือสัญลักษณ์สกุลเงิน
- ref คือเลขอ้างอิงเฉพาะของรายการ (เช่น "รหัสอ้างอิง", "เลขที่รายการ", "Transaction ID", "Reference No.") ให้ดึงเป็นข้อความตามที่เห็น ถ้าหาไม่เจอให้เป็น null
- recipient คือชื่อ "ผู้รับเงิน/บัญชีปลายทาง" (ไม่ใช่ผู้โอน) ให้ดึงตามที่เห็น
{recipient_rule}"""

    message = await client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
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

    raw = message.content[0].text.strip()

    # Parse JSON ที่ได้จาก Claude
    import json
    # ลบ markdown fence ถ้ามี
    raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
    data = json.loads(raw)

    is_slip = bool(data.get("is_slip", False))
    amount_raw = data.get("amount")
    ref_raw = data.get("ref")
    ref = str(ref_raw).strip() if ref_raw else None

    recipient_raw = data.get("recipient")
    recipient = str(recipient_raw).strip() if recipient_raw else None
    # ถ้าไม่ได้กำหนดชื่อผู้รับ ให้ผ่านเสมอ; ถ้ากำหนด ใช้คำตัดสินของ AI
    recipient_match = True if not (expected_names or []) else bool(data.get("recipient_match", False))

    if not is_slip:
        return SlipResult(is_slip=False, amount=None, ref=None,
                          recipient=recipient, recipient_match=recipient_match)

    if amount_raw is None:
        return SlipResult(is_slip=True, amount=None, ref=ref,
                          recipient=recipient, recipient_match=recipient_match)

    return SlipResult(is_slip=True, amount=float(amount_raw), ref=ref,
                      recipient=recipient, recipient_match=recipient_match)
