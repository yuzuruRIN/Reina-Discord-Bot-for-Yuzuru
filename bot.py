"""
bot.py — Discord Role Manager Bot v2
ฟีเจอร์:
  • ฟัง channel สลิป → AI อ่านยอด → ให้/ต่อยศ + บันทึก Sheet
  • เมื่อเครดิต API หมด → เงียบ + แจ้งเตือน admin ใน log channel
  • /addrole /removerole /checkrole /listroles (Admin commands)
  • Auto-expiry task ทุก 24 ชั่วโมง
"""

import sys
# Windows console (cp1252) แสดง emoji ไม่ได้ → บังคับ output เป็น UTF-8 กัน print แล้ว crash
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import discord
from discord import app_commands
from discord.ext import tasks
from datetime import datetime, date, time as dtime, timezone, timedelta
import os
from dotenv import load_dotenv
import anthropic

import config
import sheets
import supa
from slip_reader import read_slip_from_url, SlipResult

load_dotenv()

# ── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True  # จำเป็น: ใช้อ่านรูปสลิปที่ส่งเข้ามาในข้อความ
# หมายเหตุ: ไม่เปิด members intent — task เช็กยศหมดอายุใช้ fetch_member แทนได้


class RoleBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        check_expired_roles.start()
        print("✅ Slash commands synced | Auto-expiry task started")

    async def on_ready(self):
        print(f"🤖 Logged in as {self.user} (ID: {self.user.id})")


bot = RoleBot()


# ── Helper: ส่ง log ไปช่อง admin ────────────────────────────────────────────
async def log_to_admin(message: str):
    """ส่งข้อความแจ้งเตือนไปช่อง log ของ admin เงียบๆ"""
    channel = bot.get_channel(config.LOG_CHANNEL_ID)
    if channel:
        try:
            await channel.send(message)
        except Exception as e:
            print(f"[LOG ERROR] ส่ง log ไม่ได้: {e}")
    else:
        print(f"[LOG] ไม่พบ log channel (ID: {config.LOG_CHANNEL_ID}): {message}")


# ── Helper: แปลงเงิน → วัน ──────────────────────────────────────────────────
def amount_to_days(amount: float) -> int | None:
    """
    คิดยอดเงินเป็นรายเดือน ตามอัตรา PRICE_PER_MONTH บาท = 1 เดือน
    ปัดลงเป็นจำนวนเดือนเต็ม (90฿ = 3 เดือน, 50฿ = 1 เดือน)
    คืน จำนวนวัน หรือ None ถ้ายอดไม่ถึงราคา 1 เดือน
    """
    months = int(amount // config.PRICE_PER_MONTH)
    if months < 1:
        return None
    return months * config.DAYS_PER_MONTH


def package_list_text() -> str:
    return (
        f"• ขั้นต่ำ {config.PRICE_PER_MONTH:,} บาท = 1 เดือน ({config.DAYS_PER_MONTH} วัน)\n"
        f"• ทุกๆ {config.PRICE_PER_MONTH:,} บาท = เพิ่มอีก 1 เดือน"
    )


# ── Slip Channel Listener ────────────────────────────────────────────────────
@bot.event
async def on_message(message: discord.Message):
    # ไม่ตอบตัวเอง
    if message.author.bot:
        return

    # ตรวจเฉพาะใน channel ที่กำหนด
    if message.channel.id != config.SLIP_CHANNEL_ID:
        return

    # ต้องมีรูปภาพ
    images = [a for a in message.attachments if a.content_type and a.content_type.startswith("image/")]
    if not images:
        return

    # Processing indicator
    processing_msg = await message.reply("⏳ กำลังตรวจสอบสลิป...", mention_author=False)

    try:
        result: SlipResult = await read_slip_from_url(images[0].url, config.RECIPIENT_NAMES)

    except anthropic.AuthenticationError:
        # API Key ผิด
        await processing_msg.delete()
        await log_to_admin(
            "🔑 **[BOT ERROR] API Key ไม่ถูกต้อง**\n"
            "กรุณาตรวจสอบ `ANTHROPIC_API_KEY` ใน `.env`"
        )
        return

    except anthropic.RateLimitError:
        # เครดิตหมดหรือ rate limit
        await processing_msg.delete()
        await log_to_admin(
            f"🚨 **[BOT ALERT] เครดิต Anthropic API หมดหรือถึง rate limit!**\n"
            f"📌 เกิดจากสลิปของ: {message.author.mention} (`{message.author.id}`)\n"
            f"🕐 เวลา: <t:{int(datetime.now().timestamp())}:F>\n"
            f"💳 กรุณาเติมเครดิตที่ https://console.anthropic.com/settings/billing\n"
            f"⚠️ บอทจะไม่ตอบสลิปจนกว่าจะเติมเครดิต"
        )
        return

    except anthropic.APIStatusError as e:
        # API error อื่นๆ (500, 529 overloaded ฯลฯ)
        await processing_msg.delete()
        await log_to_admin(
            f"⚠️ **[BOT ERROR] Anthropic API มีปัญหา (status {e.status_code})**\n"
            f"📌 สลิปจาก: {message.author.mention}\n"
            f"🕐 เวลา: <t:{int(datetime.now().timestamp())}:F>\n"
            f"รายละเอียด: `{str(e)[:200]}`"
        )
        return

    except Exception as e:
        # error อื่นๆ ที่ไม่คาดคิด (network timeout ฯลฯ)
        await processing_msg.delete()
        await log_to_admin(
            f"❓ **[BOT ERROR] เกิดข้อผิดพลาดไม่ทราบสาเหตุ**\n"
            f"📌 สลิปจาก: {message.author.mention}\n"
            f"🕐 เวลา: <t:{int(datetime.now().timestamp())}:F>\n"
            f"รายละเอียด: `{str(e)[:200]}`"
        )
        print(f"[SlipReader Error] {e}")
        return

    member = message.author
    guild  = message.guild

    # ── ไม่ใช่สลิป ──────────────────────────────────────────────────────────
    if not result.is_slip:
        await processing_msg.edit(content=config.MSG_NOT_SLIP.format(mention=member.mention))
        return

    # ── ชื่อผู้รับไม่ตรง ──────────────────────────────────────────────────────
    if config.RECIPIENT_NAMES and not result.recipient_match:
        await processing_msg.edit(content=config.MSG_WRONG_RECIPIENT.format(mention=member.mention))
        return

    # ── อ่านยอดไม่ได้ ────────────────────────────────────────────────────────
    if result.amount is None:
        await processing_msg.edit(content=config.MSG_CANNOT_READ.format(mention=member.mention))
        return

    # ── จับ package ──────────────────────────────────────────────────────────
    days = amount_to_days(result.amount)
    if days is None:
        text = config.MSG_UNKNOWN_AMOUNT.format(
            mention=member.mention,
            amount=f"{result.amount:,.0f}",
            package_list=package_list_text(),
        )
        await processing_msg.edit(content=text)
        return

    # ── เช็กสลิปซ้ำ ──────────────────────────────────────────────────────────
    if result.ref and sheets.is_slip_used(result.ref):
        await processing_msg.edit(content=config.MSG_DUPLICATE_SLIP.format(mention=member.mention))
        return

    # ── หา Role ใน guild ─────────────────────────────────────────────────────
    role = discord.utils.get(guild.roles, name=config.ROLE_NAME)
    if role is None:
        await processing_msg.edit(
            content=f"❌ ไม่พบยศ **{config.ROLE_NAME}** ในเซิร์ฟเวอร์ค่ะ (ตรวจสอบชื่อใน config.py)"
        )
        return

    # ── Upsert Sheet + ให้/ต่ออายุยศ ────────────────────────────────────────
    # ชีตเก็บ: Name=username, Price=ยอดสะสม, Start Date=วันนี้ (End Date เป็นสูตรในชีต)
    is_new = sheets.upsert_member(member.name, member.id, result.amount)

    # ให้ยศ (กรณียังไม่มี)
    if role not in member.roles:
        try:
            await member.add_roles(role, reason=f"สลิป {result.amount:.0f}บ. ({days}วัน)")
        except discord.Forbidden:
            await processing_msg.edit(content="❌ เรย์นะไม่มีสิทธิ์ให้ยศนี้ค่ะ (ตรวจสอบลำดับยศของบอท)")
            return

    # ── บันทึกสลิปกันใช้ซ้ำ ───────────────────────────────────────────────────
    sheets.record_slip(result.ref, member.id, str(member.display_name), result.amount)

    # ── เขียน Supabase (best-effort: ถ้าพังไม่ให้กระทบการให้ยศ) ───────────────
    try:
        await supa.upsert_member(str(member.display_name), member.name)
    except Exception as e:
        await log_to_admin(
            f"⚠️ **[Supabase] เขียนข้อมูลไม่สำเร็จ**\n"
            f"📌 สมาชิก: {member.mention} (`{member.name}`)\n"
            f"รายละเอียด: `{str(e)[:200]}`"
        )
        print(f"[Supabase Error] {e}")

    # ── ตอบกลับ ──────────────────────────────────────────────────────────────
    fmt = dict(
        mention=member.mention,
        days=days,
        amount=f"{result.amount:,.0f}",
        role=config.ROLE_NAME,
    )

    if is_new:
        reply = config.MSG_NEW_MEMBER.format(**fmt)
    else:
        reply = config.MSG_RENEW_MEMBER.format(**fmt)

    await processing_msg.edit(content=reply)


# ── /addrole ─────────────────────────────────────────────────────────────────
@bot.tree.command(name="addrole", description="เพิ่มยศให้สมาชิก พร้อมกำหนดวันหมดอายุ")
@app_commands.describe(
    member="สมาชิกที่ต้องการเพิ่มยศ",
    role="ยศที่ต้องการให้",
    expires="วันหมดอายุ (YYYY-MM-DD)",
)
@app_commands.checks.has_permissions(manage_roles=True)
async def addrole(
    interaction: discord.Interaction,
    member: discord.Member,
    role: discord.Role,
    expires: str,
):
    try:
        expire_date = datetime.strptime(expires, "%Y-%m-%d").date()
    except ValueError:
        await interaction.response.send_message("❌ รูปแบบวันที่ไม่ถูกต้องค่ะ ใช้ **YYYY-MM-DD**", ephemeral=True)
        return

    if expire_date <= date.today():
        await interaction.response.send_message("❌ วันหมดอายุต้องเป็นวันในอนาคตค่ะ", ephemeral=True)
        return

    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(f"❌ เรย์นะจัดการยศ **{role.name}** ไม่ได้ค่ะ", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    await member.add_roles(role, reason=f"เพิ่มโดย {interaction.user}")

    # แปลงวันหมดอายุที่ขอ → จำนวนเดือน → ยอดเงิน (ชีตคำนวณ End Date เองจาก Price)
    days = (expire_date - date.today()).days
    months = max(1, round(days / config.DAYS_PER_MONTH))
    amount = months * config.PRICE_PER_MONTH

    sheets.upsert_member(member.name, member.id, amount)

    try:
        await supa.upsert_member(str(member.display_name), member.name)
    except Exception as e:
        print(f"[Supabase Error] {e}")

    embed = discord.Embed(title="✅ เพิ่มยศสำเร็จ", color=discord.Color.green())
    embed.add_field(name="สมาชิก", value=member.mention, inline=True)
    embed.add_field(name="ยศ", value=role.mention, inline=True)
    embed.add_field(name="ระยะเวลา", value=f"~{months} เดือน ({amount} บาท)", inline=True)
    await interaction.followup.send(embed=embed)


# ── /removerole ──────────────────────────────────────────────────────────────
@bot.tree.command(name="removerole", description="ปลดยศสมาชิกและลบออกจากระบบ")
@app_commands.describe(member="สมาชิกที่ต้องการปลดยศ", role="ยศที่ต้องการปลด")
@app_commands.checks.has_permissions(manage_roles=True)
async def removerole(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(f"❌ เรย์นะจัดการยศ **{role.name}** ไม่ได้ค่ะ", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    had_role = role in member.roles
    if had_role:
        await member.remove_roles(role, reason=f"ปลดโดย {interaction.user}")

    in_sheet = sheets.remove_record(member.name)

    try:
        await supa.set_blacklist(member.name, True)
    except Exception as e:
        print(f"[Supabase Error] {e}")

    if not had_role and not in_sheet:
        await interaction.followup.send(f"⚠️ {member.mention} ไม่มียศ **{role.name}** อยู่แล้วค่ะ", ephemeral=True)
        return

    embed = discord.Embed(title="🗑️ ปลดยศสำเร็จ", color=discord.Color.red())
    embed.add_field(name="สมาชิก", value=member.mention, inline=True)
    embed.add_field(name="ยศ", value=role.name, inline=True)
    await interaction.followup.send(embed=embed)


# ── /checkrole ───────────────────────────────────────────────────────────────
@bot.tree.command(name="checkrole", description="เช็ควันหมดอายุยศของตัวเองหรือสมาชิกคนอื่น")
@app_commands.describe(member="สมาชิกที่ต้องการเช็ค (ไม่ระบุ = เช็คตัวเอง)")
async def checkrole(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    await interaction.response.defer(ephemeral=True)

    record = sheets.get_record(target.name)
    if not record:
        await interaction.followup.send(f"📭 ไม่พบข้อมูลของ {target.mention} ในระบบค่ะ", ephemeral=True)
        return

    today = date.today()
    exp = sheets.parse_date(record["end"])
    if exp is None:
        status = "❓ ไม่ทราบ"
    else:
        days_left = (exp - today).days
        if days_left < 0:
            status = "⛔ หมดอายุแล้ว"
        elif days_left == 0:
            status = "⚠️ หมดอายุวันนี้!"
        elif days_left <= 7:
            status = f"⚠️ เหลือ {days_left} วัน"
        else:
            status = f"✅ เหลือ {days_left} วัน"

    embed = discord.Embed(title=f"📋 ข้อมูลยศของ {target.display_name}", color=discord.Color.blue())
    embed.add_field(name="ยศ", value=config.ROLE_NAME, inline=True)
    embed.add_field(name="หมดอายุ", value=f"`{record['end'] or '-'}`", inline=True)
    embed.add_field(name="สถานะ", value=status, inline=True)
    embed.add_field(name="ยอดชำระสะสม", value=f"{record['price']:,.0f} บาท", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /listroles ───────────────────────────────────────────────────────────────
@bot.tree.command(name="listroles", description="ดูรายการยศทั้งหมด (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def listroles(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    records = sheets.get_all_members()
    if not records:
        await interaction.followup.send("📭 ยังไม่มีสมาชิกในระบบค่ะ", ephemeral=True)
        return

    today = date.today()
    active, expired = [], []
    for r in records:
        exp = sheets.parse_date(r["end"])
        (active if (exp is None or exp >= today) else expired).append(r)

    embed = discord.Embed(
        title="📊 รายการสมาชิกทั้งหมด",
        color=discord.Color.gold(),
        description=f"✅ Active: **{len(active)}** | ⛔ Expired: **{len(expired)}**",
    )

    def fmt(r):
        exp = sheets.parse_date(r["end"])
        if exp is None:
            tag = "?"
        else:
            days = (exp - today).days
            tag = f"+{days}d" if days >= 0 else f"{days}d"
        return f"`{r['name']}` ({tag})"

    if active:
        text = "\n".join(fmt(r) for r in active[:20])
        if len(active) > 20:
            text += f"\n…และอีก {len(active) - 20} คน"
        embed.add_field(name="✅ ยังไม่หมดอายุ", value=text, inline=False)

    if expired:
        text = "\n".join(f"`{r['name']}` (หมดแล้ว)" for r in expired[:10])
        embed.add_field(name="⛔ หมดอายุแล้ว", value=text, inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Auto-expiry (วันละครั้ง เวลาไทยตายตัว) ──────────────────────────────────
# ไทย = UTC+7 (ไม่มี DST) → ใช้ offset คงที่ ไม่ต้องพึ่ง tzdata
THAI_TZ = timezone(timedelta(hours=7))
EXPIRY_CHECK_TIME = dtime(hour=3, minute=0, tzinfo=THAI_TZ)  # ← ตี 3 เวลาไทย (เปลี่ยน hour ได้)


@tasks.loop(time=EXPIRY_CHECK_TIME)
async def check_expired_roles():
    print(f"[{datetime.now(THAI_TZ)}] 🔍 Checking expired roles (เวลาไทย)...")
    members = sheets.get_all_members()
    today = date.today()

    for m in members:
        exp = sheets.parse_date(m["end"])
        if exp is None or exp >= today:
            continue

        # ── Supabase: blacklist = true (ใช้ username จากชีตได้เลย) ───────────
        try:
            await supa.set_blacklist(m["name"], True)
            print(f"  🚫 Supabase blacklist=true: {m['name']}")
        except Exception as e:
            print(f"  ❌ Supabase ไม่อัปเดต: {e}")

        # ── ปลดยศ Discord (ต้องมี Discord ID ในคอลัมน์ J) ───────────────────
        if not m["discord_id"]:
            continue
        for guild in bot.guilds:
            try:
                member = guild.get_member(int(m["discord_id"])) or await guild.fetch_member(int(m["discord_id"]))
            except Exception:
                continue
            role = discord.utils.get(guild.roles, name=config.ROLE_NAME)
            if role and member and role in member.roles:
                try:
                    await member.remove_roles(role, reason=f"ยศหมดอายุ ({m['end']})")
                    print(f"  ✂️  ปลด {role.name} จาก {member.display_name}")
                except Exception as e:
                    print(f"  ❌ ปลดไม่ได้: {e}")

        # หมายเหตุ: ไม่แก้ไขแถวในชีตตามที่กำหนด (ปล่อยไว้ถ้าไม่ต่ออายุ)


@check_expired_roles.before_loop
async def before_check():
    await bot.wait_until_ready()


# ── Error Handler ────────────────────────────────────────────────────────────
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    msg = "❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้ค่ะ" if isinstance(error, app_commands.MissingPermissions) else f"❌ เกิดข้อผิดพลาด: {error}"
    try:
        await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        await interaction.followup.send(msg, ephemeral=True)


# ── Run ──────────────────────────────────────────────────────────────────────
bot.run(os.getenv("DISCORD_TOKEN"))
