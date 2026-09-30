"""
sheets.py — จัดการข้อมูลใน Google Sheets

Header ที่ต้องมีในแถวแรกของ Sheet:
UserID | Username | RoleName | RoleID | ExpiresAt | AssignedAt | TotalPaid
"""

import gspread
from google.oauth2.service_account import Credentials
from datetime import date, datetime
import os
import re

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_spreadsheet = None
_sheet = None
_slips_sheet = None


def get_spreadsheet():
    global _spreadsheet
    if _spreadsheet is None:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
        gc = gspread.authorize(creds)
        _spreadsheet = gc.open_by_key(os.getenv("SPREADSHEET_ID"))
    return _spreadsheet


def get_sheet():
    global _sheet
    if _sheet is None:
        _sheet = get_spreadsheet().sheet1
    return _sheet


def get_slips_sheet():
    """แท็บเก็บประวัติสลิปที่ใช้แล้ว (สร้างให้อัตโนมัติถ้ายังไม่มี)"""
    global _slips_sheet
    if _slips_sheet is None:
        ss = get_spreadsheet()
        try:
            _slips_sheet = ss.worksheet("UsedSlips")
        except gspread.WorksheetNotFound:
            _slips_sheet = ss.add_worksheet(title="UsedSlips", rows=1000, cols=5)
            _slips_sheet.append_row(["Ref", "UserID", "Username", "Amount", "UsedAt"])
    return _slips_sheet


# ── Columns (1-indexed) ──────────────────────────────────────────────────────
# Name | Price | Start Date | End Date(สูตร) | Status | สถานะยศ | Insert Name | Insert Price | Insert Date
COL_NAME       = 1   # A — username Discord (handle)
COL_PRICE      = 2   # B — ยอดเงินสะสม
COL_START      = 3   # C — Start Date (D/M/YYYY)
COL_END        = 4   # D — End Date (คอลัมน์สูตร — บอทไม่เขียนทับ)
COL_DISCORD_ID = 6   # F — เก็บ Discord ID (บอทใช้สำหรับปลดยศอัตโนมัติ)


def _fmt_date(d: date) -> str:
    """แปลงเป็นรูปแบบ D/M/YYYY (ไม่ปัด 0 นำหน้า) เช่น 16/6/2026"""
    return f"{d.day}/{d.month}/{d.year}"


def parse_date(s: str) -> date | None:
    """อ่านวันที่จากชีต รองรับทั้ง D/M/YYYY และ YYYY-MM-DD"""
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def _find_row(discord_username: str) -> int | None:
    """คืน row index (1-based) ของ user (เทียบคอลัมน์ Name) หรือ None"""
    values = get_sheet().col_values(COL_NAME)
    for i, v in enumerate(values[1:], start=2):  # skip header
        if v == discord_username:
            return i
    return None


def _find_row_by_id(discord_id: int) -> int | None:
    """
    คืน row index (1-based) ของ user โดยเทียบ Discord ID (คอลัมน์ F)
    เชื่อถือได้กว่าชื่อ เพราะ ID ไม่เปลี่ยน แม้ผู้ใช้เปลี่ยน username
    """
    if not discord_id:
        return None
    values = get_sheet().col_values(COL_DISCORD_ID)
    target = str(discord_id)
    for i, v in enumerate(values[1:], start=2):  # skip header
        if v and v.strip() == target:
            return i
    return None


def get_record(discord_username: str) -> dict | None:
    """ดึงข้อมูล user จากชีต คืน dict หรือ None"""
    row = _find_row(discord_username)
    if row is None:
        return None
    data = get_sheet().row_values(row)
    while len(data) < 10:
        data.append("")
    return {
        "row":        row,
        "name":       data[0],
        "price":      float(data[1]) if data[1] else 0.0,
        "start":      data[2],
        "end":        data[3],  # คำนวณจากสูตร
        "discord_id": data[5],  # คอลัมน์ F
    }


def _append_new_row(sheet, discord_username: str, discord_id: int, amount: float) -> bool:
    """append แถวใหม่ (A,B,C) ไม่แตะ D (สูตร) แล้วเขียน Discord ID ลงแถวที่เพิ่งสร้างเท่านั้น"""
    resp = sheet.append_row(
        [discord_username, amount, _fmt_date(date.today())],
        value_input_option="USER_ENTERED",
    )
    # อ่านเลขแถวจาก response (เช่น 'Sheet1!A123:C123') — เชื่อถือได้แม้ชื่อจะซ้ำกับแถวเก่า
    new_row = None
    try:
        rng = resp["updates"]["updatedRange"].split("!")[-1]  # 'A123:C123'
        m = re.search(r"\d+", rng)
        new_row = int(m.group()) if m else None
    except Exception:
        new_row = None
    if new_row is None:  # fallback (กรณีไม่ได้ response ที่คาดไว้)
        new_row = _find_row_by_id(discord_id) or _find_row(discord_username)
    if new_row:
        sheet.update_cell(new_row, COL_DISCORD_ID, str(discord_id))
    return True


def upsert_member(discord_username: str, discord_id: int, amount: float) -> bool:
    """
    คืน is_new (True = สร้างแถวใหม่, False = บวกเงินเข้าแถวเดิม)

    ลำดับการจับคู่แถว:
      1) หาด้วย Discord ID (เสถียรสุด — สมาชิกที่บอทเคยบันทึก ID ไว้แล้ว)
      2) ถ้าไม่เจอ หาด้วย username (แถวที่กรอกมือ ยังไม่มี Discord ID):
         • ถ้า "ยังไม่หมดอายุ" → ใช้แถวนั้น (เติม Discord ID + บวกเงิน)
         • ถ้า "หมดอายุแล้ว"   → สร้างแถวใหม่ (ปล่อยแถวเก่าไว้)
      3) ไม่เจอเลย → สร้างแถวใหม่
    """
    sheet = get_sheet()
    today = date.today()

    # 1) หาด้วย Discord ID ก่อน
    row = _find_row_by_id(discord_id)
    existing = sheet.row_values(row) if row else None

    # 2) ไม่เจอ ID → ลองชื่อ (เฉพาะแถวที่ยังไม่หมดอายุ)
    if row is None:
        name_row = _find_row(discord_username)
        if name_row is not None:
            name_vals = sheet.row_values(name_row)
            end_str = name_vals[COL_END - 1] if len(name_vals) >= COL_END else ""
            exp = parse_date(end_str)
            expired = exp is not None and exp < today
            if not expired:
                row, existing = name_row, name_vals
            # ถ้าหมดอายุ → ปล่อย row = None เพื่อไปสร้างแถวใหม่

    # 3) ไม่มีแถวที่ใช้ได้ (ใหม่จริง หรือ คนเก่าที่หมดอายุ) → สร้างแถวใหม่
    if row is None:
        return _append_new_row(sheet, discord_username, discord_id, amount)

    # ── สมาชิกเก่าที่ยัง active — บวกเงินเข้า Price เดิม ──────────────────────
    cur_price = 0.0
    if len(existing) >= COL_PRICE and existing[COL_PRICE - 1]:
        try:
            cur_price = float(str(existing[COL_PRICE - 1]).replace(",", ""))
        except ValueError:
            cur_price = 0.0
    sheet.update_cell(row, COL_PRICE, cur_price + amount)

    # เติม Discord ID ถ้าแถวเดิมยังไม่มี (เช่น แถวที่กรอกมือไว้ และยังไม่หมดอายุ)
    cur_id = existing[COL_DISCORD_ID - 1] if len(existing) >= COL_DISCORD_ID else ""
    if not cur_id:
        sheet.update_cell(row, COL_DISCORD_ID, str(discord_id))
    return False


def get_all_members() -> list[dict]:
    """อ่านสมาชิกทั้งหมด (ใช้ get_all_values เพื่อเลี่ยงปัญหา header คอลัมน์ J ไม่มีชื่อ)"""
    values = get_sheet().get_all_values()
    members = []
    for row in values[1:]:  # skip header
        row = row + [""] * (10 - len(row))
        if not row[0]:
            continue
        members.append({
            "name":       row[0],
            "price":      row[1],
            "start":      row[2],
            "end":        row[3],
            "discord_id": row[5],  # คอลัมน์ F
        })
    return members


def remove_record(discord_username: str) -> bool:
    """ลบ row ของ user (เทียบคอลัมน์ Name) คืน True ถ้าลบสำเร็จ"""
    values = get_sheet().get_all_values()
    for i, row in enumerate(values[1:], start=2):
        if row and row[0] == discord_username:
            get_sheet().delete_rows(i)
            return True
    return False


# ── เช็กสลิปซ้ำ ───────────────────────────────────────────────────────────────
def is_slip_used(ref: str) -> bool:
    """คืน True ถ้าเลขอ้างอิงสลิปนี้เคยถูกใช้แล้ว"""
    if not ref:
        return False
    used_refs = get_slips_sheet().col_values(1)  # คอลัมน์ Ref
    return ref in used_refs[1:]  # ข้าม header


def record_slip(ref: str, user_id: int, username: str, amount: float) -> None:
    """บันทึกเลขอ้างอิงสลิปที่ใช้ไปแล้ว"""
    if not ref:
        return
    get_slips_sheet().append_row([
        ref,
        str(user_id),
        username,
        amount,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ])
