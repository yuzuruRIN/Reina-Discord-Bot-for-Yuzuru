"""
supa.py — เขียนข้อมูลสมาชิกลง Supabase (ตาราง member_list)

ทำงานคู่กับ Google Sheets:
  • ผู้ใช้ใหม่      → INSERT record ใหม่ (blacklist = false)
  • ผู้ใช้เก่าสมัครใหม่ → UPDATE blacklist = false (ปลดแบน)
  • ยศหมดอายุ      → UPDATE blacklist = true

ใช้ REST API (PostgREST) ผ่าน httpx — ไม่ต้องลง library เพิ่ม

─── การจับคู่แถว (สำคัญ) ────────────────────────────────────────────────────
เดิมจับคู่ด้วย email สังเคราะห์ {discord_username}@donator.discord อย่างเดียว
แต่ระบบล็อกอินหน้าแรกของเกมต้องใช้ "อีเมลจริง" (ส่ง OTP ไปหาได้) ผู้โดเนทจึง
เปลี่ยน email ของตัวเองได้ผ่าน /linkemail → จับคู่ด้วย email อย่างเดียวไม่ได้อีก

ลำดับการค้นหาแถวจึงเป็น:
    1. discord_id         ← เสถียรที่สุด ไม่เปลี่ยนตลอดชีพ (ใช้หลังผูกอีเมลแล้ว)
    2. email สังเคราะห์    ← แถวเก่าที่ยังไม่ได้ผูกอีเมลจริง
    3. discord_username   ← เผื่อไว้ ข้อมูลจริงส่วนใหญ่เป็น null

ส่วนฟังก์ชันฝั่ง Patreon (upsert_patreon_member / get_member_by_email /
set_blacklist_by_email) ใช้อีเมลจริงอยู่แล้ว จึงไม่ต้องแก้

⚠️ ต้องรัน migrations/001_device_login.sql (ฝั่ง yuzuru_game_dev) ก่อนใช้ไฟล์นี้
   เพราะต้องมีคอลัมน์ discord_id / source / email_verified
"""

import os
import re
import httpx

TABLE = "member_list"
TIER = "Donator"
EMAIL_SUFFIX = "@donator.discord"  # อีเมลสังเคราะห์: {discord_username}{EMAIL_SUFFIX}

# ตรวจรูปแบบอีเมลแบบหลวม ๆ พอกันพิมพ์ผิดชัด ๆ — ตัวตัดสินจริงคือ OTP ส่งไปถึงหรือไม่
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")

# คอลัมน์ที่ดึงมาใช้ตัดสินใจ — ขอเท่าที่ใช้จริงเพื่อไม่ให้ payload บวม
SELECT_COLS = "email,username,tier,blacklist,discord_username,discord_id"


def _base_url() -> str:
    return os.getenv("SUPABASE_URL", "").rstrip("/")


def _headers(prefer: str | None = None) -> dict:
    key = os.getenv("SUPABASE_SERVICE_KEY", "")
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _enabled() -> bool:
    return bool(_base_url() and os.getenv("SUPABASE_SERVICE_KEY"))


def _email(discord_username: str) -> str:
    return f"{discord_username}{EMAIL_SUFFIX}"


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def is_placeholder_email(email: str | None) -> bool:
    """True ถ้าเป็นอีเมลสังเคราะห์ที่ส่ง OTP ไปไม่ถึง"""
    return normalize_email(email).endswith(EMAIL_SUFFIX)


def is_valid_email(email: str | None) -> bool:
    e = normalize_email(email)
    return bool(e) and len(e) <= 254 and bool(EMAIL_RE.match(e)) and not e.endswith(EMAIL_SUFFIX)


# ── Helper: ค้นหาแถว ─────────────────────────────────────────────────────────
async def _rows(client, params: dict) -> list[dict]:
    r = await client.get(
        f"{_base_url()}/rest/v1/{TABLE}",
        headers=_headers(),
        params={"select": SELECT_COLS, "limit": "20", **params},
    )
    r.raise_for_status()
    return r.json() or []


async def _find_by_email(client, email: str) -> dict | None:
    """
    ค้นด้วย email แบบไม่สนตัวพิมพ์ใหญ่เล็ก

    ข้อมูลจริงมี email ทั้งพิมพ์เล็กและพิมพ์ใหญ่ปนกัน (main.py::_get_member เจอ
    ปัญหาเดียวกัน) จึงลอง eq ก่อนแล้วค่อย ilike — และต้องเช็คซ้ำใน Python เพราะ
    "_" กับ "%" เป็น wildcard ของ LIKE และโผล่ในอีเมลจริงได้
    """
    target = normalize_email(email)
    if not target:
        return None

    rows = await _rows(client, {"email": f"eq.{target}"})
    if rows:
        return rows[0]

    rows = await _rows(client, {"email": f"ilike.{target}"})
    for row in rows:
        if normalize_email(row.get("email")) == target:
            return row
    return None


async def _find_row(client, discord_id, discord_username: str) -> dict | None:
    """หาแถวของผู้ใช้ Discord ตามลำดับ discord_id → email สังเคราะห์ → handle"""
    if discord_id:
        rows = await _rows(client, {"discord_id": f"eq.{discord_id}"})
        if rows:
            return rows[0]

    if discord_username:
        row = await _find_by_email(client, _email(discord_username))
        if row:
            return row

        rows = await _rows(client, {"discord_username": f"eq.{discord_username}"})
        if rows:
            return rows[0]

    return None


async def _drop_devices(client, email: str) -> None:
    """
    ล้างเครื่องที่ผูกไว้กับอีเมลเดิม

    เรียกตอนผู้เล่นเปลี่ยนอีเมล — ไม่งั้นแถวใน devices จะกลายเป็นขยะที่ยังกิน
    slot ของคนนั้นอยู่ แต่เจ้าตัวมองไม่เห็นและปลดเองไม่ได้

    ยอมให้ล้มเหลวเงียบ ๆ เผื่อยังไม่ได้รัน migration (ตาราง devices ยังไม่มี)
    """
    if not email:
        return
    try:
        await client.delete(
            f"{_base_url()}/rest/v1/devices",
            headers=_headers("return=minimal"),
            params={"email": f"eq.{normalize_email(email)}"},
        )
    except Exception as e:
        print(f"[Supabase] ล้าง devices ของ {email} ไม่สำเร็จ: {e}")


# ── อ่านข้อมูลสมาชิกจากผู้ใช้ Discord ────────────────────────────────────────
async def get_member(discord_id, discord_username: str) -> dict | None:
    """คืนแถวของผู้ใช้ Discord คนนี้ (None ถ้าไม่มี / ปิดใช้งาน Supabase)"""
    if not _enabled():
        return None
    async with httpx.AsyncClient(timeout=15) as client:
        return await _find_row(client, discord_id, discord_username)


# ── ผูกอีเมลจริง ─────────────────────────────────────────────────────────────
async def link_email(discord_id, discord_username: str, display_name: str, new_email: str) -> tuple[str, dict]:
    """
    ผูกอีเมลจริงของผู้โดเนท Discord เข้ากับแถวใน member_list

    คืน (status, info) โดย status เป็นหนึ่งใน:
      disabled      ไม่ได้ตั้งค่า Supabase
      invalid_email รูปแบบอีเมลไม่ถูกต้อง
      taken         อีเมลนี้เป็นของสมาชิกคนอื่นอยู่แล้ว
      same          ผูกอีเมลนี้ไว้อยู่แล้ว ไม่ได้แก้อะไร
      linked        ผูกสำเร็จครั้งแรก (เดิมเป็นอีเมลสังเคราะห์)
      changed       เปลี่ยนจากอีเมลจริงเดิมเป็นอีเมลใหม่ (ล้าง device เดิมแล้ว)
      created       ไม่เคยมีแถวมาก่อน จึงสร้างใหม่ให้

    ⚠️ ตัวเรียกต้องเช็คยศ Donator มาก่อนแล้ว — ฟังก์ชันนี้ไม่ตรวจสิทธิ์ให้
    """
    if not _enabled():
        return "disabled", {}

    email = normalize_email(new_email)
    if not is_valid_email(email):
        return "invalid_email", {}

    async with httpx.AsyncClient(timeout=15) as client:
        mine = await _find_row(client, discord_id, discord_username)

        # อีเมลนี้มีคนใช้อยู่ไหม (และไม่ใช่แถวของเราเอง)
        owner = await _find_by_email(client, email)
        if owner and (not mine or normalize_email(owner.get("email")) != normalize_email(mine.get("email"))):
            return "taken", {"owner_tier": owner.get("tier")}

        patch = {
            "email": email,
            "discord_id": str(discord_id),
            "discord_username": discord_username,
            "source": "discord",
            # ยังไม่นับว่ายืนยันแล้ว จนกว่าจะผ่าน OTP ในเกม
            "email_verified": False,
        }

        # ── ไม่เคยมีแถว → สร้างใหม่ (มียศ Donator = มีสิทธิ์อยู่แล้ว) ──────────
        if not mine:
            r = await client.post(
                f"{_base_url()}/rest/v1/{TABLE}",
                headers=_headers("resolution=merge-duplicates,return=minimal"),
                params={"on_conflict": "email"},
                json={**patch, "username": display_name, "tier": TIER, "blacklist": False},
            )
            r.raise_for_status()
            return "created", {"email": email}

        old_email = normalize_email(mine.get("email"))
        if old_email == email:
            return "same", {"email": email}

        r = await client.patch(
            f"{_base_url()}/rest/v1/{TABLE}",
            headers=_headers("return=minimal"),
            params={"email": f"eq.{mine.get('email')}"},
            json=patch,
        )
        r.raise_for_status()

        was_placeholder = is_placeholder_email(old_email)
        if not was_placeholder:
            # เปลี่ยนอีเมลจริง → เครื่องที่ผูกไว้ต้องถูกล้าง ให้ไปยืนยันใหม่
            await _drop_devices(client, old_email)

        return ("linked" if was_placeholder else "changed"), {"email": email, "old_email": old_email}


# ── ติดตามความคืบหน้าการย้ายไปใช้อีเมลจริง ───────────────────────────────────
async def link_stats() -> dict:
    """
    นับว่าผู้โดเนท Discord ผูกอีเมลจริงไปแล้วกี่คน

    ดึงมานับใน Python แทน count=exact เพราะข้อมูลมีแค่หลักร้อยแถว และต้องแยก
    อีเมลสังเคราะห์ออกด้วย logic เดียวกับที่อื่นอยู่แล้ว

    pending_active คือตัวเลขที่ต้องดู — ยศยังไม่หมดอายุ แต่ยังผูกอีเมลไม่ได้
    คนกลุ่มนี้คือคนที่จะเข้าเกมเวอร์ชันใหม่ไม่ได้
    """
    if not _enabled():
        return {}

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(
            f"{_base_url()}/rest/v1/{TABLE}",
            headers=_headers(),
            params={"select": "email,blacklist", "tier": f"eq.{TIER}", "limit": "5000"},
        )
        r.raise_for_status()
        rows = r.json() or []

    pending = [x for x in rows if is_placeholder_email(x.get("email"))]
    return {
        "total": len(rows),
        "linked": len(rows) - len(pending),
        "pending": len(pending),
        "pending_active": len([x for x in pending if not x.get("blacklist")]),
    }


async def upsert_member(display_name: str, discord_username: str, discord_id=None) -> str:
    """
    จับคู่แถวด้วย discord_id → email สังเคราะห์ แล้วทำตามสถานะ:
      • ไม่มีแถว           → INSERT (blacklist=false) → คืน "new"
      • มีแถว blacklist=true → UPDATE blacklist=false → คืน "reactivated"
      • มีแถว blacklist=false → ไม่แก้ไข               → คืน "active"

    เติม discord_id/discord_username ให้เสมอถ้ายังว่าง เพื่อให้แถวเก่าค่อย ๆ มี
    discord_id ครบโดยไม่ต้องไล่ backfill เอง

    ⚠️ ห้ามเขียนทับคอลัมน์ email — ผู้เล่นอาจผูกอีเมลจริงไว้แล้วผ่าน /linkemail

    คืน "disabled" ถ้าไม่ได้ตั้งค่า Supabase
    """
    if not _enabled():
        return "disabled"

    async with httpx.AsyncClient(timeout=15) as client:
        row = await _find_row(client, discord_id, discord_username)

        # ── ไม่มีแถว → สมาชิกใหม่ (ใช้ upsert on_conflict กัน race 409) ─────────
        if not row:
            r = await client.post(
                f"{_base_url()}/rest/v1/{TABLE}",
                headers=_headers("resolution=merge-duplicates,return=minimal"),
                params={"on_conflict": "email"},
                json={
                    "username": display_name,
                    "email": _email(discord_username),
                    "tier": TIER,
                    "blacklist": False,
                    "discord_username": discord_username,
                    "discord_id": str(discord_id) if discord_id else None,
                    "source": "discord",
                },
            )
            r.raise_for_status()
            return "new"

        patch = {}
        if not row.get("discord_username"):
            patch["discord_username"] = discord_username
        if discord_id and not row.get("discord_id"):
            patch["discord_id"] = str(discord_id)

        was_blacklisted = bool(row.get("blacklist"))
        if was_blacklisted:
            patch["blacklist"] = False

        if patch:
            r = await client.patch(
                f"{_base_url()}/rest/v1/{TABLE}",
                headers=_headers("return=minimal"),
                params={"email": f"eq.{row.get('email')}"},
                json=patch,
            )
            r.raise_for_status()

        return "reactivated" if was_blacklisted else "active"


async def set_blacklist_by_email(email: str, value: bool = True) -> None:
    """ตั้ง/ปลด blacklist ด้วยอีเมลจริง โดยคง tier เดิมไว้ (ใช้กับเคสยกเลิก Patreon)"""
    if not _enabled() or not email:
        return

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.patch(
            f"{_base_url()}/rest/v1/{TABLE}",
            headers=_headers("return=minimal"),
            params={"email": f"eq.{email.strip().lower()}"},
            json={"blacklist": value},
        )
        r.raise_for_status()


async def set_blacklist(discord_username: str, value: bool, discord_id=None) -> None:
    """
    อัปเดตค่า blacklist ของผู้ใช้ (ใช้ตอนยศหมดอายุ → true)

    ⚠️ ต้องส่ง discord_id มาด้วยทุกครั้งที่มี — ผู้เล่นที่ผูกอีเมลจริงแล้วจะหา
       ด้วย email สังเคราะห์ไม่เจอ ถ้าหาไม่เจอแล้วเงียบ คนที่ยศหมดอายุจะยังเล่น
       เกมได้ต่อไปเรื่อย ๆ
    """
    if not _enabled():
        return

    async with httpx.AsyncClient(timeout=15) as client:
        row = await _find_row(client, discord_id, discord_username)
        if not row:
            print(f"[Supabase] set_blacklist: ไม่พบแถวของ {discord_username} (id={discord_id})")
            return

        r = await client.patch(
            f"{_base_url()}/rest/v1/{TABLE}",
            headers=_headers("return=minimal"),
            params={"email": f"eq.{row.get('email')}"},
            json={"blacklist": value},
        )
        r.raise_for_status()


# ── Patreon Specific Supabase Operations ───────────────────────────────────

async def get_member_by_email(email: str) -> dict | None:
    """ดึงข้อมูลสมาชิกจาก Supabase โดยตรงด้วย email จริง"""
    if not _enabled() or not email:
        return None

    clean_email = email.strip().lower()
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{_base_url()}/rest/v1/{TABLE}",
            headers=_headers(),
            params={"email": f"eq.{clean_email}", "select": "username,email,tier,blacklist,discord_username"},
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None


async def upsert_patreon_member(email: str, new_tier: str, display_name: str = "") -> dict:
    """
    อัปเดต Tier สำหรับสมาชิก Patreon (ไม่แตะยศใน Discord):
      • ถ้ามีข้อมูลอยู่แล้ว → UPDATE tier = new_tier, blacklist = false
      • ถ้าเป็นสมาชิกใหม่ → INSERT record ใหม่
    คืนค่า dict เช่น {"status": "updated"|"inserted", "old_tier": "..."}
    """
    if not _enabled() or not email:
        return {"status": "disabled", "old_tier": None}

    clean_email = email.strip().lower()
    existing = await get_member_by_email(clean_email)

    async with httpx.AsyncClient(timeout=15) as client:
        if not existing:
            # ── สมาชิกใหม่ → INSERT ──────────────────────────────────────────
            name = display_name.strip() if display_name else clean_email.split("@")[0]
            r = await client.post(
                f"{_base_url()}/rest/v1/{TABLE}",
                headers=_headers("resolution=merge-duplicates,return=minimal"),
                params={"on_conflict": "email"},
                json={
                    "username": name,
                    "email": clean_email,
                    "tier": new_tier,
                    "blacklist": False,
                },
            )
            r.raise_for_status()
            return {"status": "inserted", "old_tier": None}

        # ── สมาชิกเดิม → UPDATE tier + ปลด blacklist ────────────────────────
        old_tier = existing.get("tier")
        patch = {
            "tier": new_tier,
            "blacklist": False,
        }
        if display_name and not existing.get("username"):
            patch["username"] = display_name.strip()

        r = await client.patch(
            f"{_base_url()}/rest/v1/{TABLE}",
            headers=_headers("return=minimal"),
            params={"email": f"eq.{clean_email}"},
            json=patch,
        )
        r.raise_for_status()
        return {"status": "updated", "old_tier": old_tier}
