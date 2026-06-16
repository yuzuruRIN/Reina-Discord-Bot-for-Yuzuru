# 🚀 Deploy บอทเรย์นะขึ้น Oracle Cloud (Always Free)

รันบอท 24 ชม. ฟรีถาวร บนเครื่อง Linux ของ Oracle

---

## ขั้นที่ 1 — สร้าง VM Instance
1. เข้า https://cloud.oracle.com → ล็อกอิน
2. เมนู ☰ → **Compute → Instances → Create instance**
3. ตั้งค่า:
   - **Image**: Canonical Ubuntu 22.04
   - **Shape**: กด Change shape → เลือก **VM.Standard.E2.1.Micro** (มีป้าย *Always Free eligible*)
   - **SSH keys**: เลือก *Generate a key pair for me* → **ดาวน์โหลด Private Key** เก็บไว้ (สำคัญมาก ไฟล์ `.key`)
4. กด **Create** → รอจน Running → จด **Public IP address**

> 📌 บอทไม่ต้องเปิด port ขาเข้าเลย (มันต่อออกหา Discord เอง) ไม่ต้องตั้ง ingress rule

---

## ขั้นที่ 2 — SSH เข้าเครื่อง
บนคอม Windows เปิด PowerShell:
```powershell
ssh -i "C:\path\to\your-key.key" ubuntu@<PUBLIC_IP>
```
(ครั้งแรกถาม yes/no → พิมพ์ `yes`)

---

## ขั้นที่ 3 — ติดตั้ง Python บนเครื่อง server
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv
mkdir ~/discord-bot
```

---

## ขั้นที่ 4 — ส่งไฟล์ขึ้น server
เปิด PowerShell ใหม่ "บนคอมตัวเอง" (ไม่ใช่ใน ssh) แล้วรัน:
```powershell
cd "C:\Users\Atipun.p\Documents\discord bot"
scp -i "C:\path\to\your-key.key" bot.py config.py sheets.py slip_reader.py supa.py requirements.txt .env credentials.json raina-bot.service ubuntu@<PUBLIC_IP>:~/discord-bot/
```

---

## ขั้นที่ 5 — ติดตั้ง dependencies + ทดสอบ
กลับไปที่หน้าต่าง ssh:
```bash
cd ~/discord-bot
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python bot.py        # ทดสอบ ถ้าขึ้น connected to Gateway = OK แล้วกด Ctrl+C
```

---

## ขั้นที่ 6 — ตั้งให้รัน 24 ชม. + รีสตาร์ทเองอัตโนมัติ (systemd)
```bash
sudo cp ~/discord-bot/raina-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable raina-bot      # ให้สตาร์ทเองตอนเครื่องรีบูต
sudo systemctl start raina-bot       # เริ่มรันเลย
sudo systemctl status raina-bot      # เช็กสถานะ (เห็น active running = สำเร็จ)
```

ดู log สดๆ:
```bash
journalctl -u raina-bot -f
```

---

## 🔧 คำสั่งที่ใช้บ่อย
| ต้องการ | คำสั่ง |
|---------|--------|
| หยุดบอท | `sudo systemctl stop raina-bot` |
| รีสตาร์ทบอท | `sudo systemctl restart raina-bot` |
| ดู log | `journalctl -u raina-bot -f` |
| อัปเดตโค้ด | scp ไฟล์ใหม่ขึ้นไป แล้ว `sudo systemctl restart raina-bot` |

---

## ⚠️ ความปลอดภัย
- ไฟล์ `.env` + `credentials.json` อยู่บน server แล้ว — ตั้งสิทธิ์ให้อ่านได้เฉพาะเจ้าของ:
  ```bash
  chmod 600 ~/discord-bot/.env ~/discord-bot/credentials.json
  ```
- เก็บ Private Key (`.key`) ให้ดี ห้ามแชร์
