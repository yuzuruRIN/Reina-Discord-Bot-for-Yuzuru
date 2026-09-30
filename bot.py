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

import asyncio
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
from patreon_webhook import PatreonWebhookServer

load_dotenv()

# ไทย = UTC+7 (ไม่มี DST) → ใช้ offset คงที่ ไม่ต้องพึ่ง tzdata
THAI_TZ = timezone(timedelta(hours=7))

# ── Bot Setup ────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True  # จำเป็น: ใช้อ่านรูปสลิปที่ส่งเข้ามาในข้อความ
# หมายเหตุ: ไม่เปิด members intent — task เช็กยศหมดอายุใช้ fetch_member แทนได้


class RoleBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.patreon_server: PatreonWebhookServer | None = None

    async def setup_hook(self):
        await self.tree.sync()
        check_expired_roles.start()
        print("✅ Slash commands synced | Auto-expiry task started")

        # ── Start Patreon Webhook Server ─────────────────────────────────────
        port = int(os.getenv("PATREON_WEBHOOK_PORT") or getattr(config, "PATREON_WEBHOOK_PORT", 8080))
        secret = os.getenv("PATREON_WEBHOOK_SECRET", "")
        self.patreon_server = PatreonWebhookServer(
            port=port,
            secret=secret,
            event_handler=on_patreon_webhook_event
        )
        try:
            await self.patreon_server.start()
        except Exception as e:
            print(f"❌ [Patreon Server Error] Failed to start server on port {port}: {e}")

    async def close(self):
        if self.patreon_server:
            await self.patreon_server.stop()
        await super().close()

    async def on_ready(self):
        print(f"🤖 Logged in as {self.user} (ID: {self.user.id})")
        # กวาดยศหมดอายุตอนเริ่มบอท (catch-up กันกรณีบอทไม่ได้ออนไลน์ตอน 03:00)
        if not getattr(self, "_startup_expiry_done", False):
            self._startup_expiry_done = True
            try:
                results = await run_expiry_check()
                await _log_expiry_summary(results, source="ตอนเริ่มบอท")
            except Exception as e:
                print(f"[Startup expiry error] {e}")


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


# ── Patreon UI View: ปุ่มยืนยัน / ปฏิเสธ + Auto-update Timer ────────────────
class PatreonTierView(discord.ui.View):
    def __init__(
        self,
        email: str,
        display_name: str,
        new_tier: str,
        old_tier: str,
        amount_cents: int,
        event_type: str,
        auto_update_hours: float = 3.0,
    ):
        super().__init__(timeout=None)
        self.email = email
        self.display_name = display_name
        self.new_tier = new_tier
        self.old_tier = old_tier
        self.amount_cents = amount_cents
        self.event_type = event_type
        self.auto_update_hours = auto_update_hours
        self.is_resolved = False
        self.message: discord.Message | None = None
        self._timer_task: asyncio.Task | None = None

        # เคสยกเลิก pledge: เปลี่ยนหน้าที่ปุ่มเป็น "Blacklist ทันที" / "ปล่อยตามรอบบิล"
        if event_type == "members:pledge:delete":
            self.confirm_button.label = "Blacklist ทันที"
            self.cancel_button.label = "ปล่อยตามรอบบิล"

    def start_timer(self):
        """เริ่มนับเวลาถอยหลัง 3 ชั่วโมงสำหรับการ auto-update"""
        self._timer_task = asyncio.create_task(self._auto_update_timer())

    async def _auto_update_timer(self):
        try:
            await asyncio.sleep(self.auto_update_hours * 3600)
            if self.is_resolved:
                return

            self.is_resolved = True
            # Auto-update Supabase
            try:
                if self.event_type == "members:pledge:delete":
                    # เคสยกเลิก: ไม่แตะฐานข้อมูล — สมาชิกยังมีสิทธิ์ถึงสิ้นรอบบิล
                    # ระบบ sync ฝั่ง Render จะ blacklist ให้เองเมื่อสิทธิ์หมด
                    status_note = (
                        f"🕒 **ปล่อยตามรอบบิลอัตโนมัติ** (ครบ {self.auto_update_hours:.0f} ชั่วโมง) — "
                        "ระบบ sync จะ blacklist ให้เองเมื่อสิทธิ์หมดรอบบิล"
                    )
                else:
                    await supa.upsert_patreon_member(
                        email=self.email,
                        new_tier=self.new_tier,
                        display_name=self.display_name
                    )
                    status_note = f"⚡ **อัปเดตฐานข้อมูลอัตโนมัติแล้ว** (ครบ {self.auto_update_hours:.0f} ชั่วโมง)"
            except Exception as e:
                status_note = f"⚠️ **เกิดข้อผิดพลาดในการ Auto-update**: `{e}`"

            for child in self.children:
                child.disabled = True

            if self.message:
                embed = self.message.embeds[0]
                embed.color = discord.Color.blue()
                embed.set_field_at(
                    -1,
                    name="📌 สถานะการดำเนินการ",
                    value=status_note,
                    inline=False
                )
                await self.message.edit(embed=embed, view=self)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[Patreon View Error] {e}")

    @discord.ui.button(label="ยืนยันการอัปเดต", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.is_resolved:
            await interaction.response.send_message("⚠️ รายการนี้ได้รับการจัดการแล้วค่ะ", ephemeral=True)
            return

        if not interaction.user.guild_permissions.manage_roles and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์กดยืนยันรายการนี้ค่ะ", ephemeral=True)
            return

        self.is_resolved = True
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()

        await interaction.response.defer()

        try:
            if self.event_type == "members:pledge:delete":
                if "Donator" in (self.old_tier or ""):
                    status_text = (
                        f"🛡️ **ไม่ blacklist** โดย {interaction.user.mention} — "
                        "สมาชิกถือยศ Donator จากการส่งสลิป"
                    )
                else:
                    await supa.set_blacklist_by_email(self.email, True)
                    status_text = (
                        f"⛔ **Blacklist ทันทีแล้ว** โดย {interaction.user.mention} (คง tier เดิมไว้) — "
                        "⚠️ ถ้า Patreon ยังนับเป็น active อยู่ รอบ sync ถัดไปอาจปลด blacklist คืน"
                    )
            else:
                await supa.upsert_patreon_member(
                    email=self.email,
                    new_tier=self.new_tier,
                    display_name=self.display_name
                )
                status_text = f"✅ **ยืนยันการอัปเดตแล้ว** โดย {interaction.user.mention}"
        except Exception as e:
            status_text = f"⚠️ **อัปเดต Supabase ไม่สำเร็จ**: `{e}`"

        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.set_field_at(
            -1,
            name="📌 สถานะการดำเนินการ",
            value=status_text,
            inline=False
        )
        await interaction.message.edit(embed=embed, view=self)

    @discord.ui.button(label="ปฏิเสธ", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.is_resolved:
            await interaction.response.send_message("⚠️ รายการนี้ได้รับการจัดการแล้วค่ะ", ephemeral=True)
            return

        if not interaction.user.guild_permissions.manage_roles and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ คุณไม่มีสิทธิ์ปฏิเสธรายการนี้ค่ะ", ephemeral=True)
            return

        self.is_resolved = True
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()

        for child in self.children:
            child.disabled = True

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.set_field_at(
            -1,
            name="📌 สถานะการดำเนินการ",
            value=(
                f"🕒 **ปล่อยตามรอบบิล** โดย {interaction.user.mention} — ระบบ sync จะ blacklist ให้เมื่อสิทธิ์หมด"
                if self.event_type == "members:pledge:delete"
                else f"❌ **ปฏิเสธรายการแล้ว** โดย {interaction.user.mention} (ไม่มีการแก้ไขฐานข้อมูล)"
            ),
            inline=False
        )
        await interaction.message.edit(embed=embed, view=self)


# ── Patreon Webhook Event Handler ───────────────────────────────────────────
async def on_patreon_webhook_event(event_type: str, info: dict):
    """ส่งการ์ดแจ้งเตือนเข้าช่อง Discord พร้อมปุ่มกดยืนยัน / ปฏิเสธ"""
    email = info.get("email")
    if not email:
        await log_to_admin(f"⚠️ **[Patreon Webhook]** ได้รับ Event `{event_type}` แต่ไม่พบ Email: `{info}`")
        return

    full_name = info.get("full_name") or "Patreon Member"
    new_tier = info.get("tier_title") or "Donator"
    amount_cents = info.get("amount_cents", 0)
    amount_usd = amount_cents / 100 if amount_cents else 0.0

    # ── กันการ์ดซ้ำตอนมีคนยกเลิก ────────────────────────────────────────
    # Patreon ยิงหลาย event ต่อการยกเลิก 1 ครั้ง: update (tier หลุด, ยอด $0)
    # ตามด้วย pledge:delete — ใบที่มีความหมายคือใบ delete จึงข้าม update เปล่า
    if event_type != "members:pledge:delete" and info.get("tier_title") == "None" and not amount_cents:
        print(f"[Patreon Webhook] ข้ามการ์ด '{event_type}' ของ {email} (tier หลุด/ยอด 0 — เป็นส่วนหนึ่งของการยกเลิก)")
        return

    # ดึง Tier ปัจจุบันจาก Supabase
    existing = await supa.get_member_by_email(email)
    old_tier = existing.get("tier") if existing else "ไม่มีในระบบ (สมาชิกใหม่)"

    # ── กรองการ์ด: ส่งเฉพาะเคสที่แอดมินต้องตรวจจริงๆ ────────────────────
    if event_type == "members:pledge:delete":
        # ยกเลิกโดยคนที่ไม่มีในระบบ (เช่น สมาชิก Free) → ไม่มีอะไรต้องทำ
        if not existing:
            print(f"[Patreon Webhook] ข้ามการ์ด: {email} ยกเลิกแต่ไม่มีในฐานข้อมูล")
            return
    else:
        # tier ฟรี → ฝั่ง Render ไม่บันทึกอยู่แล้ว
        if new_tier.strip().lower() == "free":
            print(f"[Patreon Webhook] ข้ามการ์ด: {email} เป็นสมาชิก Free")
            return
        # สมาชิกใหม่ → Render บันทึกให้อัตโนมัติแล้ว ไม่ต้องตรวจ
        if not existing:
            print(f"[Patreon Webhook] ข้ามการ์ด: สมาชิกใหม่ {email} ({new_tier}) — บันทึกอัตโนมัติแล้ว")
            return
        # ซัพต่อ tier เดิม → ไม่มีอะไรเปลี่ยน
        if (existing.get("tier") or "").strip() == new_tier.strip():
            print(f"[Patreon Webhook] ข้ามการ์ด: {email} ต่ออายุ tier เดิม ({new_tier})")
            return

    # หาช่องที่จะส่งข้อความแจ้งเตือน
    target_channel_id = getattr(config, "PATREON_LOG_CHANNEL_ID", None) or config.LOG_CHANNEL_ID
    channel = bot.get_channel(target_channel_id)
    if not channel:
        print(f"[Patreon Webhook] ไม่พบ Channel ID {target_channel_id}")
        return

    auto_hours = float(getattr(config, "PATREON_AUTO_UPDATE_HOURS", 3))
    due_timestamp = int((datetime.now(timezone.utc) + timedelta(hours=auto_hours)).timestamp())

    if event_type == "members:pledge:delete":
        embed = discord.Embed(
            title="🚫 [Patreon] แจ้งเตือนการยกเลิก Pledge / สิ้นสุดสมาชิกภาพ",
            color=discord.Color.dark_orange(),
            timestamp=datetime.now(THAI_TZ),
        )
        target_tier = "None (ยกเลิกแล้ว — สิทธิ์คงอยู่ถึงสิ้นรอบบิล)"
    elif event_type == "members:pledge:create":
        embed = discord.Embed(
            title="✨ [Patreon] แจ้งเตือนสมาชิกใหม่ / สมัคร Tier ครั้งแรก",
            color=discord.Color.gold(),
            timestamp=datetime.now(THAI_TZ),
        )
        target_tier = new_tier
    else:  # members:pledge:update
        embed = discord.Embed(
            title="🔄 [Patreon] แจ้งเตือนการเปลี่ยนแปลง Tier / ยอด Pledge",
            color=discord.Color.blue(),
            timestamp=datetime.now(THAI_TZ),
        )
        target_tier = new_tier

    embed.add_field(name="👤 สมาชิก", value=f"**{full_name}**", inline=True)
    embed.add_field(name="📧 Email", value=f"`{email}`", inline=True)
    embed.add_field(name="💰 ยอด Pledge", value=f"${amount_usd:.2f} USD", inline=True)
    embed.add_field(name="🏷️ การเปลี่ยนแปลง Tier", value=f"`{old_tier}` ➔ **{target_tier}**", inline=False)
    if event_type == "members:pledge:delete":
        auto_text = (
            f"ถ้าไม่กดปุ่มภายใน <t:{due_timestamp}:R> จะถือว่า **ปล่อยตามรอบบิล** "
            "(ไม่แก้ฐานข้อมูล — ระบบ sync จะ blacklist ให้เองเมื่อสิทธิ์หมด)"
        )
    else:
        auto_text = f"ระบบจะบันทึกข้อมูลลงฐานข้อมูลอัตโนมัติใน <t:{due_timestamp}:R> (<t:{due_timestamp}:T>)"
    embed.add_field(
        name="⏳ กำหนดการ Auto-update",
        value=auto_text,
        inline=False,
    )
    embed.add_field(
        name="📌 สถานะการดำเนินการ",
        value="⏳ รอดำเนินการ (กดปุ่มด้านล่างเพื่อยืนยันหรือปฏิเสธทันที)",
        inline=False,
    )
    embed.set_footer(text="Reina Bot • Patreon Tier Sync System (อัปเดตเฉพาะฐานข้อมูล)")

    view = PatreonTierView(
        email=email,
        display_name=full_name,
        new_tier=target_tier,
        old_tier=old_tier,
        amount_cents=amount_cents,
        event_type=event_type,
        auto_update_hours=auto_hours,
    )

    msg = await channel.send(embed=embed, view=view)
    view.message = msg
    view.start_timer()


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


def slip_images(message: discord.Message) -> list[discord.Attachment]:
    """คืนรูปภาพที่แนบมาในข้อความ (ใช้เป็นสลิป)"""
    return [a for a in message.attachments if a.content_type and a.content_type.startswith("image/")]


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
    if not slip_images(message):
        return

    await process_slip(message)


# ── ตรวจสลิป + ให้ยศ + บันทึกข้อมูล ──────────────────────────────────────────
# ใช้ร่วมกันระหว่าง on_message (สลิปใหม่) และ /checkslip (สั่งตรวจย้อนหลัง)
async def process_slip(message: discord.Message):
    """ตรวจสลิปจากรูปในข้อความ แล้ว 'ตอบกลับที่ข้อความนั้น'"""
    images = slip_images(message)
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

    # ข้อความที่ fetch มาทีหลัง (/checkslip) author อาจเป็น User ไม่ใช่ Member → แปลงก่อน
    # (ต้องเป็น Member ถึงจะอ่าน/ให้ยศได้)
    if guild and not isinstance(member, discord.Member):
        try:
            member = guild.get_member(member.id) or await guild.fetch_member(member.id)
        except Exception:
            await processing_msg.edit(content="❌ ไม่พบผู้ส่งสลิปคนนี้ในเซิร์ฟเวอร์แล้วค่ะ")
            return

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
    is_new_sheet = sheets.upsert_member(member.name, member.id, result.amount)

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
    supa_status = None
    try:
        supa_status = await supa.upsert_member(str(member.display_name), member.name, member.id)
    except Exception as e:
        await log_to_admin(
            f"⚠️ **[Supabase] เขียนข้อมูลไม่สำเร็จ**\n"
            f"📌 สมาชิก: {member.mention} (`{member.name}`)\n"
            f"รายละเอียด: `{str(e)[:200]}`"
        )
        print(f"[Supabase Error] {e}")

    # ── ตัดสินข้อความตอบกลับ ─────────────────────────────────────────────────
    # ใช้สถานะ Supabase เป็นหลัก (blacklist บอกได้ว่ายศหลุดมาก่อนไหม):
    #   new/reactivated → "สมัครใหม่"  |  active → "ต่ออายุ"
    # ถ้า Supabase ปิดหรือพัง → ใช้สถานะจาก Google Sheet แทน
    if supa_status in ("new", "reactivated"):
        is_new = True
    elif supa_status == "active":
        is_new = False
    else:  # "disabled" หรือ None (เขียน Supabase พัง)
        is_new = is_new_sheet

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

    # ── เตือนให้ผูกอีเมล เฉพาะคนที่ยังใช้อีเมลสังเคราะห์ (ส่ง OTP ไม่ถึง) ───────
    # ถ้า Supabase ปิด/พัง จะไม่รู้สถานะ → ไม่ต่อท้าย เพื่อไม่รบกวนคนที่ผูกแล้ว
    if supa_status in ("new", "reactivated", "active"):
        try:
            row = await supa.get_member(member.id, member.name)
            if row and supa.is_placeholder_email(row.get("email")):
                reply += config.MSG_LINK_EMAIL_HINT
        except Exception as e:
            print(f"[Supabase Error] link hint lookup: {e}")

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
        await supa.upsert_member(str(member.display_name), member.name, member.id)
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
        await supa.set_blacklist(member.name, True, member.id)
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


# ── /linkemail ───────────────────────────────────────────────────────────────
@bot.tree.command(
    name="linkemail",
    description="ผูกอีเมลจริงเพื่อใช้ล็อกอินเข้าเกม (เฉพาะผู้สนับสนุน)",
)
@app_commands.describe(email="อีเมลจริงที่ใช้รับรหัสยืนยัน (เว้นว่าง = ดูอีเมลที่ผูกไว้ตอนนี้)")
async def linkemail(interaction: discord.Interaction, email: str = None):
    """
    ผู้โดเนทผ่าน Discord ผูกอีเมลจริงของตัวเองเข้ากับสิทธิ์ในเกม

    เดิมระบบปั้นอีเมลปลอม {handle}@donator.discord ให้ ซึ่งส่ง OTP ไปไม่ถึง และ
    handle เป็นข้อมูลสาธารณะที่ใครก็เดาได้ → ต้องเปลี่ยนเป็นอีเมลจริงก่อนถึงจะ
    ใช้ระบบล็อกอินหน้าแรกของเกมได้

    ยศ Donator ใน Discord คือตัวพิสูจน์สิทธิ์ — ปลอมไม่ได้ จึงไม่ต้องใช้ claim code
    """
    # ต้องใช้ในเซิร์ฟเวอร์เท่านั้น ใน DM จะไม่มีข้อมูลยศให้ตรวจ
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ คำสั่งนี้ต้องใช้ในเซิร์ฟเวอร์นะคะ ใช้ใน DM ไม่ได้ค่ะ", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    user = interaction.user

    if discord.utils.get(user.roles, name=config.ROLE_NAME) is None:
        await interaction.followup.send(
            f"❌ คำสั่งนี้สำหรับผู้ที่มียศ **{config.ROLE_NAME}** เท่านั้นค่ะ\n"
            f"ถ้าเพิ่งโอนมาแล้วยังไม่ได้ยศ รบกวนส่งสลิปในห้องรับสลิปก่อนนะคะ",
            ephemeral=True,
        )
        return

    # ── ไม่ใส่อีเมล → แสดงสถานะปัจจุบัน ──────────────────────────────────────
    if not email:
        try:
            row = await supa.get_member(user.id, user.name)
        except Exception as e:
            print(f"[Supabase Error] linkemail lookup: {e}")
            await interaction.followup.send("❌ ระบบขัดข้องชั่วคราว ลองใหม่อีกครั้งนะคะ", ephemeral=True)
            return

        current = (row or {}).get("email")
        if not row or supa.is_placeholder_email(current):
            await interaction.followup.send(
                "📭 คุณยังไม่ได้ผูกอีเมลค่ะ\n"
                "พิมพ์ `/linkemail อีเมลของคุณ` เพื่อผูก\n"
                "ตัวอย่าง: `/linkemail somchai@gmail.com`",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ อีเมลที่ผูกไว้ตอนนี้: `{current}`\n"
                "ถ้าต้องการเปลี่ยน พิมพ์ `/linkemail อีเมลใหม่` ได้เลยค่ะ",
                ephemeral=True,
            )
        return

    # ── ผูกอีเมล ─────────────────────────────────────────────────────────────
    try:
        status, info = await supa.link_email(user.id, user.name, str(user.display_name), email)
    except Exception as e:
        print(f"[Supabase Error] linkemail: {e}")
        await log_to_admin(
            f"⚠️ **[Supabase] ผูกอีเมลไม่สำเร็จ**\n"
            f"📌 สมาชิก: {user.mention} (`{user.name}`)\n"
            f"รายละเอียด: `{str(e)[:200]}`"
        )
        await interaction.followup.send("❌ ระบบขัดข้องชั่วคราว ลองใหม่อีกครั้งนะคะ", ephemeral=True)
        return

    if status == "invalid_email":
        await interaction.followup.send(
            "❌ รูปแบบอีเมลไม่ถูกต้องค่ะ ตรวจสอบอีกครั้งนะคะ\n"
            "ตัวอย่างที่ถูกต้อง: `somchai@gmail.com`",
            ephemeral=True,
        )
        return

    if status == "taken":
        await interaction.followup.send(
            "❌ อีเมลนี้ถูกใช้กับบัญชีอื่นในระบบแล้วค่ะ\n"
            "ถ้าเป็นอีเมลของคุณเองจริง ๆ (เช่นสนับสนุนผ่าน Patreon ด้วย) "
            "รบกวนทักแอดมินเพื่อรวมบัญชีนะคะ",
            ephemeral=True,
        )
        return

    if status == "disabled":
        await interaction.followup.send("⚠️ ระบบยังไม่เปิดใช้งานค่ะ รบกวนแจ้งแอดมินนะคะ", ephemeral=True)
        return

    if status == "same":
        await interaction.followup.send(
            f"ℹ️ อีเมล `{info['email']}` ถูกผูกไว้อยู่แล้วค่ะ ไม่ต้องทำอะไรเพิ่มนะคะ",
            ephemeral=True,
        )
        return

    # ── สำเร็จ: linked / changed / created ───────────────────────────────────
    embed = discord.Embed(title="✅ ผูกอีเมลสำเร็จ", color=discord.Color.green())
    embed.add_field(name="อีเมลที่ใช้ล็อกอิน", value=f"`{info['email']}`", inline=False)
    embed.add_field(
        name="ขั้นตอนต่อไป",
        value=(
            "บันทึกอีเมลเรียบร้อยแล้ว — **ตอนนี้ยังไม่มีอีเมลส่งไปหานะคะ**\n"
            "รหัสยืนยันจะถูกส่งตอนที่คุณเปิดเกมแล้วกดขอรหัสในหน้าล็อกอินค่ะ"
        ),
        inline=False,
    )
    if status == "changed":
        embed.add_field(
            name="⚠️ หมายเหตุ",
            value="เปลี่ยนอีเมลแล้ว เครื่องที่เคยผูกไว้ถูกล้างทั้งหมด ต้องยืนยันใหม่นะคะ",
            inline=False,
        )
    embed.set_footer(text="กรอกอีเมลผิด? พิมพ์ /linkemail อีเมลใหม่ ทับได้เลยค่ะ")
    await interaction.followup.send(embed=embed, ephemeral=True)

    await log_to_admin(
        f"📧 **ผูกอีเมล** ({status})\n"
        f"📌 {user.mention} (`{user.name}`) → `{info['email']}`"
    )


# ── /linkstats ───────────────────────────────────────────────────────────────
@bot.tree.command(name="linkstats", description="ดูความคืบหน้าการผูกอีเมลของผู้สนับสนุน (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def linkstats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        s = await supa.link_stats()
    except Exception as e:
        print(f"[Supabase Error] linkstats: {e}")
        await interaction.followup.send(f"❌ ดึงข้อมูลไม่สำเร็จ: `{str(e)[:200]}`", ephemeral=True)
        return

    if not s:
        await interaction.followup.send("⚠️ ยังไม่ได้ตั้งค่า Supabase ค่ะ", ephemeral=True)
        return

    total = s["total"] or 1  # กัน ZeroDivision ตอนตารางยังว่าง
    pct = s["linked"] * 100 / total

    embed = discord.Embed(
        title="📧 ความคืบหน้าการผูกอีเมล",
        description=f"ผูกแล้ว **{s['linked']}/{s['total']}** คน ({pct:.0f}%)",
        color=discord.Color.blue(),
    )
    embed.add_field(name="✅ ผูกอีเมลจริงแล้ว", value=f"{s['linked']} คน", inline=True)
    embed.add_field(name="⏳ ยังไม่ผูก", value=f"{s['pending']} คน", inline=True)
    embed.add_field(name="🔴 ยังไม่ผูก + ยศยังไม่หมด", value=f"{s['pending_active']} คน", inline=True)
    embed.set_footer(text="กลุ่ม 🔴 คือคนที่จะเข้าเกมเวอร์ชันใหม่ไม่ได้ ควรตามให้ผูกก่อนปล่อยอัปเดต")
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


# ── /checkslip ───────────────────────────────────────────────────────────────
@bot.tree.command(name="checkslip", description="สั่งให้บอทตรวจสลิปจาก ID ข้อความ แล้วตอบกลับที่ข้อความนั้น")
@app_commands.describe(
    message_id="ID ของข้อความที่มีรูปสลิป (เปิด Developer Mode → คลิกขวาที่ข้อความ → Copy Message ID)",
    channel="ช่องที่ข้อความนั้นอยู่ (ไม่ระบุ = ช่องรับสลิป)",
)
@app_commands.checks.has_permissions(manage_roles=True)
async def checkslip(
    interaction: discord.Interaction,
    message_id: str,
    channel: discord.TextChannel = None,
):
    await interaction.response.defer(ephemeral=True)

    # ── หา channel ที่จะไปดึงข้อความ ─────────────────────────────────────────
    target = channel or bot.get_channel(config.SLIP_CHANNEL_ID)
    if target is None:
        await interaction.followup.send(
            f"❌ ไม่พบช่องรับสลิป (ID: `{config.SLIP_CHANNEL_ID}`) ค่ะ ลองระบุ `channel` มาด้วยนะคะ",
            ephemeral=True,
        )
        return

    # ── แปลง message_id ─────────────────────────────────────────────────────
    try:
        mid = int(message_id.strip())
    except ValueError:
        await interaction.followup.send("❌ Message ID ต้องเป็นตัวเลขเท่านั้นค่ะ", ephemeral=True)
        return

    # ── ดึงข้อความ ──────────────────────────────────────────────────────────
    try:
        msg = await target.fetch_message(mid)
    except discord.NotFound:
        await interaction.followup.send(
            f"❌ ไม่พบข้อความ ID `{mid}` ใน {target.mention} ค่ะ (ข้อความอาจอยู่ช่องอื่น)", ephemeral=True
        )
        return
    except discord.Forbidden:
        await interaction.followup.send(
            f"❌ เรย์นะไม่มีสิทธิ์อ่านข้อความใน {target.mention} ค่ะ", ephemeral=True
        )
        return
    except Exception as e:
        await interaction.followup.send(f"❌ ดึงข้อความไม่สำเร็จค่ะ: `{str(e)[:150]}`", ephemeral=True)
        return

    # ── ต้องมีรูปสลิป ────────────────────────────────────────────────────────
    if not slip_images(msg):
        await interaction.followup.send(
            f"❌ ข้อความนั้นไม่มีรูปภาพแนบมาค่ะ ([ไปที่ข้อความ]({msg.jump_url}))", ephemeral=True
        )
        return

    await interaction.followup.send(
        f"🔍 กำลังตรวจสลิปของ {msg.author.mention} ค่ะ — เรย์นะจะตอบกลับที่ข้อความนั้นเลยนะคะ\n"
        f"[ไปที่ข้อความ]({msg.jump_url})",
        ephemeral=True,
    )

    # ตรวจ + ตอบกลับที่ข้อความต้นทาง (ใช้ logic เดียวกับตอนส่งสลิปปกติ)
    await process_slip(msg)


# ── /forceexpiry ─────────────────────────────────────────────────────────────
@bot.tree.command(name="forceexpiry", description="ตรวจสอบและปลดยศสมาชิกที่หมดอายุทันที (เจ้าของเซิร์ฟเวอร์เท่านั้น)")
@app_commands.checks.has_permissions(administrator=True)
async def forceexpiry(interaction: discord.Interaction):
    if interaction.user.id != interaction.guild.owner_id:
        await interaction.response.send_message("❌ คำสั่งนี้ใช้ได้เฉพาะเจ้าของเซิร์ฟเวอร์เท่านั้นค่ะ", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    results = await run_expiry_check()

    embed = discord.Embed(
        title="🔍 ผลการตรวจสอบยศหมดอายุ",
        color=discord.Color.orange(),
        timestamp=datetime.now(THAI_TZ),
    )

    if results:
        lines = [f"`{r['name']}` — หมดอายุ {r['days_overdue']} วัน ({r['status']})" for r in results]
        text = "\n".join(lines[:20])
        if len(results) > 20:
            text += f"\n…และอีก {len(results) - 20} คน"
        embed.add_field(name=f"⛔ หมดอายุ ({len(results)} คน)", value=text, inline=False)
    else:
        embed.add_field(name="✅ ผลการตรวจสอบ", value="ไม่พบสมาชิกที่หมดอายุค่ะ", inline=False)

    errs = [f"`{r['name']}`: {'; '.join(r['errors'])}" for r in results if r["errors"]]
    if errs:
        embed.add_field(name="⚠️ ข้อผิดพลาด", value="\n".join(errs[:5]), inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /checkpatreon ────────────────────────────────────────────────────────────
@bot.tree.command(name="checkpatreon", description="เช็คข้อมูลสมาชิก Patreon ในระบบฐานข้อมูล (Supabase) ด้วย Email")
@app_commands.describe(email="Email ของสมาชิกที่ต้องการตรวจสอบ")
@app_commands.checks.has_permissions(manage_roles=True)
async def checkpatreon(interaction: discord.Interaction, email: str):
    await interaction.response.defer(ephemeral=True)
    member_data = await supa.get_member_by_email(email.strip())
    if not member_data:
        await interaction.followup.send(f"📭 ไม่พบข้อมูลสำหรับ Email `{email}` ในฐานข้อมูลค่ะ", ephemeral=True)
        return

    embed = discord.Embed(title="📋 ข้อมูลสมาชิก Patreon ในฐานข้อมูล", color=discord.Color.teal())
    embed.add_field(name="ชื่อในระบบ", value=member_data.get("username") or "-", inline=True)
    embed.add_field(name="Email", value=f"`{member_data.get('email')}`", inline=True)
    embed.add_field(name="Tier ปัจจุบัน", value=f"**{member_data.get('tier') or '-'}**", inline=True)
    is_blacklisted = member_data.get("blacklist")
    status_str = "⛔ Blacklisted (ระงับสิทธิ์)" if is_blacklisted else "✅ Active (ปกติ)"
    embed.add_field(name="สถานะ", value=status_str, inline=True)
    if member_data.get("discord_username"):
        embed.add_field(name="Discord Handle", value=f"`{member_data.get('discord_username')}`", inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Auto-expiry (วันละครั้ง เวลาไทยตายตัว) ──────────────────────────────────
EXPIRY_CHECK_TIME = dtime(hour=3, minute=0, tzinfo=THAI_TZ)  # ← ตี 3 เวลาไทย (เปลี่ยน hour ได้)


async def run_expiry_check() -> list[dict]:
    """
    กวาดสมาชิกหมดอายุ: set blacklist=true ใน Supabase + ปลดยศ Discord
    คืน list ผลลัพธ์ต่อคน [{name, days_overdue, status, errors}]
    (ไม่แก้แถวในชีตตามดีไซน์ — ปล่อยไว้ถ้าไม่ต่ออายุ)
    """
    members = sheets.get_all_members()
    today = date.today()
    results: list[dict] = []

    for m in members:
        exp = sheets.parse_date(m["end"])
        if exp is None or exp >= today:
            continue

        entry = {"name": m["name"], "days_overdue": (today - exp).days, "status": "", "errors": []}

        # ── Supabase: blacklist = true (จับคู่ด้วย discord_id ก่อน ดู supa.py) ─
        try:
            await supa.set_blacklist(m["name"], True, m.get("discord_id"))
        except Exception as e:
            entry["errors"].append(f"Supabase: {str(e)[:80]}")

        # ── ปลดยศ Discord — ต้องมี Discord ID (คอลัมน์ F) ───────────────────
        if not m["discord_id"]:
            entry["status"] = "ไม่มี Discord ID"
            results.append(entry)
            continue

        removed = found = False
        for guild in bot.guilds:
            try:
                member = guild.get_member(int(m["discord_id"])) or await guild.fetch_member(int(m["discord_id"]))
            except Exception:
                continue
            found = True
            role = discord.utils.get(guild.roles, name=config.ROLE_NAME)
            if role and member and role in member.roles:
                try:
                    await member.remove_roles(role, reason=f"ยศหมดอายุ ({m['end']})")
                    removed = True
                    print(f"  ✂️  ปลด {role.name} จาก {member.display_name}")
                except Exception as e:
                    entry["errors"].append(f"ปลดยศ: {str(e)[:80]}")

        entry["status"] = "ปลดยศแล้ว" if removed else ("ไม่มียศอยู่แล้ว" if found else "ไม่พบในเซิร์ฟ")
        results.append(entry)

    return results


async def _log_expiry_summary(results: list[dict], source: str):
    """พิมพ์ log + แจ้งสรุปเข้าช่อง admin (เฉพาะเมื่อมีคนหมดอายุหรือมี error)"""
    removed = sum(1 for r in results if r["status"] == "ปลดยศแล้ว")
    no_id = sum(1 for r in results if r["status"] == "ไม่มี Discord ID")
    errs = [f"`{r['name']}`: {'; '.join(r['errors'])}" for r in results if r["errors"]]
    print(f"[{datetime.now(THAI_TZ)}] expiry({source}): หมดอายุ {len(results)} | "
          f"ปลดยศ {removed} | ไม่มี ID {no_id} | error {len(errs)}")

    if not results and not errs:
        return  # ไม่มีอะไรต้องแจ้ง

    lines = [
        f"🔁 **[Auto-expiry • {source}]**",
        f"พบหมดอายุ **{len(results)}** คน — ปลดยศ **{removed}**, ไม่มี Discord ID **{no_id}**",
    ]
    if no_id:
        lines.append("ℹ️ คนที่ไม่มี Discord ID ปลดยศอัตโนมัติไม่ได้ (รอเขาส่งสลิปเพื่อบันทึก ID)")
    if errs:
        lines.append("⚠️ error:\n" + "\n".join(errs[:5]))
    await log_to_admin("\n".join(lines))


@tasks.loop(time=EXPIRY_CHECK_TIME)
async def check_expired_roles():
    print(f"[{datetime.now(THAI_TZ)}] 🔍 Checking expired roles (เวลาไทย)...")
    results = await run_expiry_check()
    await _log_expiry_summary(results, source="รายวัน 03:00")


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
if __name__ == "__main__":
    bot.run(os.getenv("DISCORD_TOKEN"))
