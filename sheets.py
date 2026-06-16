"""
sheets.py — จัดการข้อมูลใน Google Sheets

Header ที่ต้องมีในแถวแรกของ Sheet:
UserID | Username | RoleName | RoleID | ExpiresAt | AssignedAt | TotalPaid
"""

import gspread
from google.oauth2.service_account import Credentials
from datetime import date, datetime
import os

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


def upsert_member(discord_username: str, discord_id: int, amount: float) -> bool:
    """
    สมาชิกใหม่ → append แถวใหม่ (Name, Price, Start Date=วันนี้) + เก็บ Discord ID คอลัมน์ J
    สมาชิกเก่า → บวกเงินเข้า Price เดิม (End Date คำนวณเองจากสูตร ไม่แตะ)
    คืน is_new
    """
    sheet = get_sheet()
    record = get_record(discord_username)

    if record is None:
        # เขียนเฉพาะ A,B,C — ไม่แตะ D (สูตร) หรือคอลัมน์อื่น
        sheet.append_row(
            [discord_username, amount, _fmt_date(date.today())],
            value_input_option="USER_ENTERED",
        )
        row = _find_row(discord_username)
        if row:
            sheet.update_cell(row, COL_DISCORD_ID, str(discord_id))
        return True
    else:
        new_price = record["price"] + amount
        sheet.update_cell(record["row"], COL_PRICE, new_price)
        if not record["discord_id"]:
            sheet.update_cell(record["row"], COL_DISCORD_ID, str(discord_id))
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
