# 🤖 Discord Role Manager Bot v2 — ระบบสลิปอัตโนมัติ

---

## 📁 โครงสร้างไฟล์

```
discord-role-bot-v2/
├── bot.py            ← โค้ดหลัก (ไม่ต้องแตะ)
├── config.py         ← ✏️ แก้ไขที่นี่ที่เดียว
├── sheets.py         ← จัดการ Google Sheets
├── slip_reader.py    ← AI อ่านสลิป (Claude Vision)
├── credentials.json  ← Google Service Account key
├── .env              ← Tokens & IDs
└── requirements.txt
```

---

## ⚙️ ตั้งค่า (ทำครั้งเดียว)

### 1. Discord Bot
1. https://discord.com/developers/applications → New Application
2. แถบ **Bot** → Add Bot → คัดลอก Token
3. เปิด **Server Members Intent** และ **Message Content Intent**
4. OAuth2 → URL Generator:
   - Scopes: `bot`, `applications.commands`
   - Permissions: `Manage Roles`, `Read Messages`, `Send Messages`
5. เพิ่มบอทเข้าเซิร์ฟเวอร์

### 2. Google Sheets
1. สร้าง Sheet ใหม่ → ใส่ Header แถวแรก:
   ```
   UserID | Username | RoleName | RoleID | ExpiresAt | AssignedAt | TotalPaid
   ```
2. คัดลอก Spreadsheet ID จาก URL
3. สร้าง Service Account (ดูคู่มือ v1) → ดาวน์โหลด `credentials.json`
4. แชร์ Sheet ให้ Service Account email เป็น **Editor**

### 3. Anthropic API Key
1. https://console.anthropic.com → API Keys → สร้าง Key ใหม่
2. ใส่ใน `.env`

### 4. ตั้งค่า .env
```
cp .env.example .env
```
แก้ไขค่าทั้ง 3 บรรทัด

### 5. ตั้งค่า config.py
```python
ROLE_NAME = "Member"          # ชื่อยศใน Discord
SLIP_CHANNEL_ID = 123...      # ID ของช่องรับสลิป (คลิกขวา → Copy ID)

PACKAGES = {
    100: 30,   # 100 บาท = 30 วัน
    200: 60,
    ...
}
```

### 6. รัน
```bash
pip install -r requirements.txt
python bot.py
```

---

## 🔄 Flow การทำงานเมื่อมีคนส่งสลิป

```
ส่งรูปใน #slip-channel
        ↓
AI อ่านสลิป (Claude Vision)
        ↓
    เป็นสลิป?
   ✅ ใช่          ❌ ไม่ใช่ → แจ้งเตือน
        ↓
  จับ package
  ตรงแพ็กเกจ?
   ✅ ใช่          ❌ ไม่ → บอกแพ็กเกจที่มี
        ↓
  ตรวจ Sheet
  มีชื่ออยู่แล้ว?
 ✅ มี → ต่ออายุ    ❌ ไม่มี → เพิ่มใหม่
        ↓
   ให้/คง Role
        ↓
   ตอบกลับ
```

---

## 💬 ข้อความตอบกลับ

แก้ไขได้ใน `config.py` ส่วน `MSG_*`

| ตัวแปร | ความหมาย |
|--------|----------|
| `{mention}` | แท็กผู้ส่ง |
| `{amount}` | จำนวนเงินในสลิป |
| `{days}` | จำนวนวันที่ได้ |
| `{expires}` | วันหมดอายุ |
| `{role}` | ชื่อยศ |

---

## 🎮 Slash Commands

| คำสั่ง | สิทธิ์ | คำอธิบาย |
|--------|--------|----------|
| `/addrole @member @role YYYY-MM-DD` | Manage Roles | เพิ่มยศแบบ manual |
| `/removerole @member @role` | Manage Roles | ปลดยศ |
| `/checkrole [@member]` | ทุกคน | เช็ควันหมดอายุ + ยอดสะสม |
| `/listroles` | Administrator | ดูรายการทั้งหมด |
| `/linkemail [อีเมล]` | ผู้มียศ Donator | ผูกอีเมลจริงเพื่อล็อกอินเข้าเกม (ไม่ใส่อีเมล = ดูอีเมลที่ผูกไว้) |
| `/linkstats` | Administrator | ดูจำนวนคนที่ผูกอีเมลแล้ว/ยัง (🔴 = ยังไม่ผูกและยศยังไม่หมด) |
| `/checkslip message_id [channel]` | Manage Roles | สั่งตรวจสลิปย้อนหลังจาก ID ข้อความ |
| `/checkpatreon email` | Manage Roles | ดูข้อมูลสมาชิก Patreon ใน Supabase |
| `/forceexpiry` | เจ้าของเซิร์ฟเวอร์ | ปลดยศที่หมดอายุทันที |

### 📧 การผูกอีเมล (เกมเวอร์ชันใหม่ใช้อีเมลจริงล็อกอิน)
- ตอนส่งสลิปครั้งแรก ระบบสร้างอีเมลชั่วคราว `{username}@donator.discord` ซึ่งส่งรหัส OTP ไม่ถึง
- บอทจะต่อท้ายข้อความตอบสลิปให้ผูกอีเมลผ่าน `/linkemail` ในช่องผูกอีเมล (ID `1556549151381127168`) **เฉพาะคนที่ยังไม่ได้ผูก** (ข้อความแก้ที่ `MSG_LINK_EMAIL_HINT` ใน `config.py`)
- ก่อนปล่อยอัปเดตเกม ให้รัน `/linkstats` ดูกลุ่ม 🔴 แล้วประกาศตามให้ผูก
- ต้องรัน migration `001_device_login.sql` (ฝั่ง yuzuru_game_dev) ก่อน: คอลัมน์ `discord_id`, `source`, `email_verified` และตาราง `devices`

---

## ⚠️ หมายเหตุ

- ยศบอทต้องอยู่**สูงกว่า** Role ที่จะจัดการ
- AI มี tolerance ±5% (เช่น 100 บาท ยอม 95-105 บาท)
- ไฟล์ `credentials.json` และ `.env` ห้าม push ขึ้น Git
