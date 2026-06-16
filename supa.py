"""
supa.py — เขียนข้อมูลสมาชิกลง Supabase (ตาราง member_list)

ทำงานคู่กับ Google Sheets:
  • ผู้ใช้ใหม่      → INSERT record ใหม่ (blacklist = false)
  • ผู้ใช้เก่าสมัครใหม่ → UPDATE blacklist = false (ปลดแบน)
  • ยศหมดอายุ      → UPDATE blacklist = true

ใช้ REST API (PostgREST) ผ่าน httpx — ไม่ต้องลง library เพิ่ม
ค่า key ใช้ discord_username (handle ของ Discord ซึ่งไม่ซ้ำกัน)
"""

import os
import httpx

TABLE = "member_list"
TIER = "Donator"
EMAIL_SUFFIX = "@donator.discord"  # อีเมลสังเคราะห์: {discord_username}{EMAIL_SUFFIX}


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


async def _exists(client: httpx.AsyncClient, discord_username: str) -> bool:
    r = await client.get(
        f"{_base_url()}/rest/v1/{TABLE}",
        headers=_headers(),
        params={"discord_username": f"eq.{discord_username}", "select": "discord_username"},
    )
    r.raise_for_status()
    return len(r.json()) > 0


async def upsert_member(display_name: str, discord_username: str) -> None:
    """
    ผู้ใช้ใหม่ → INSERT (blacklist=false)
    ผู้ใช้เก่า → UPDATE blacklist=false + อัปเดตชื่อแสดงผลเผื่อเปลี่ยน
    """
    if not _enabled():
        return  # ไม่ได้ตั้งค่า Supabase → ข้ามเงียบๆ

    async with httpx.AsyncClient(timeout=15) as client:
        if await _exists(client, discord_username):
            r = await client.patch(
                f"{_base_url()}/rest/v1/{TABLE}",
                headers=_headers("return=minimal"),
                params={"discord_username": f"eq.{discord_username}"},
                json={"blacklist": False, "username": display_name},
            )
        else:
            r = await client.post(
                f"{_base_url()}/rest/v1/{TABLE}",
                headers=_headers("return=minimal"),
                json={
                    "username": display_name,
                    "email": f"{discord_username}{EMAIL_SUFFIX}",
                    "tier": TIER,
                    "blacklist": False,
                    "discord_username": discord_username,
                },
            )
        r.raise_for_status()


async def set_blacklist(discord_username: str, value: bool) -> None:
    """อัปเดตค่า blacklist ของผู้ใช้ (ใช้ตอนยศหมดอายุ → true)"""
    if not _enabled():
        return

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.patch(
            f"{_base_url()}/rest/v1/{TABLE}",
            headers=_headers("return=minimal"),
            params={"discord_username": f"eq.{discord_username}"},
            json={"blacklist": value},
        )
        r.raise_for_status()
