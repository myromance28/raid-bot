import discord
from discord.ext import commands, tasks
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread
import threading

# =========================
# 🔹 설정 및 초기화 (KST)
# =========================
KST = timezone(timedelta(hours=9))
BOSS_CHANNEL_ID = 1503420212794622073 
BOSS_TIMES = [3, 9, 15, 21]

conn = sqlite3.connect("data.db", check_same_thread=False)
cursor = conn.cursor()
db_lock = threading.Lock()

cursor.execute("CREATE TABLE IF NOT EXISTS attendance (date TEXT, time_slot TEXT, name TEXT)")
cursor.execute("CREATE TABLE IF NOT EXISTS members (name TEXT PRIMARY KEY, total INTEGER DEFAULT 0)")
cursor.execute("CREATE TABLE IF NOT EXISTS drops (item_name TEXT, winner TEXT, date TEXT, boss_name TEXT)")
cursor.execute("CREATE TABLE IF NOT EXISTS boss_list (boss_name TEXT PRIMARY KEY)")
conn.commit()

# =========================
# 🔹 Flask (유지용)
# =========================
app = Flask(__name__)
@app.route("/")
def home(): return "OK"
def run(): app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
def keep_alive(): Thread(target=run).start()

# =========================
# 🔹 핵심 로직 함수 (수정됨)
# =========================
def get_slot():
    now = datetime.now(KST)
    hour = now.hour
    if 0 <= hour < 6: return "03"
    elif 6 <= hour < 12: return "09"
    elif 12 <= hour < 18: return "15"
    else: return "21"

def attend(name, date, slot):
    with db_lock:
        cursor.execute("SELECT 1 FROM attendance WHERE date=? AND time_slot=? AND name=?", (date, slot, name))
        if cursor.fetchone(): return "already"
        cursor.execute("INSERT INTO attendance VALUES (?, ?, ?)", (date, slot, name))
        cursor.execute("INSERT INTO members(name, total) VALUES(?, 1) ON CONFLICT(name) DO UPDATE SET total = total + 1", (name,))
        conn.commit()
    return "ok"

def cancel_attend(name, date, slot):
    with db_lock:
        cursor.execute("DELETE FROM attendance WHERE date=? AND time_slot=? AND name=?", (date, slot, name))
        cursor.execute("UPDATE members SET total = CASE WHEN total > 0 THEN total - 1 ELSE 0 END WHERE name=?", (name,))
        conn.commit()

def is_attended(name, date, slot):
    with db_lock:
        cursor.execute("SELECT 1 FROM attendance WHERE date=? AND time_slot=? AND name=?", (date, slot, name))
        return cursor.fetchone() is not None

def get_next_boss_time():
    now = datetime.now(KST)
    today = now.date()
    candidates = [datetime.combine(today, datetime.min.time(), tzinfo=KST).replace(hour=h) for h in BOSS_TIMES]
    candidates.append(datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=KST).replace(hour=3))
    for t in candidates:
        if t > now: return t

# =========================
# 🔹 UI 컴포넌트
# =========================
class DropModal(discord.ui.Modal, title="💎 보스 득템 기록"):
    item_input = discord.ui.TextInput(label="아이템 이름", placeholder="예: 영웅 비기")
    winner_input = discord.ui.TextInput(label="획득자 이름", placeholder="예: 홍길동")

    def __init__(self, boss_name):
        super().__init__()
        self.boss_name = boss_name

    async def on_submit(self, interaction: discord.Interaction):
        item, winner = self.item_input.value, self.winner_input.value
        date = datetime.now(KST).strftime("%m-%d %H:%M")
        with db_lock:
            cursor.execute("INSERT INTO drops VALUES (?, ?, ?, ?)", (item, winner, date, self.boss_name))
            conn.commit()
        await interaction.response.send_message(f"✅ **[{self.boss_name}] 컷!** {winner}님 - {item} 획득!", ephemeral=False)

class ToggleAttendButton(discord.ui.Button):
    def __init__(self, name, target_date, target_slot):
        self.member_name, self.target_date, self.target_slot = name, target_date, target_slot
        done = is_attended(name, target_date, target_slot)
        super().__init__(label=name, style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.green)

    async def callback(self, interaction: discord.Interaction):
        if get_slot() != self.target_slot:
            await interaction.response.send_message(f"⚠️ {self.target_slot}시 타임 출석은 마감되었습니다.", ephemeral=True); return
        if is_attended(self.member_name, self.target_date, self.target_slot):
            cancel_attend(self.member_name, self.target_date, self.target_slot); self.style = discord.ButtonStyle.green
        else:
            attend(self.member_name, self.target_date, self.target_slot); self.style = discord.ButtonStyle.secondary
        await interaction.response.edit_message(view=self.view)

class ToggleAttendanceView(discord.ui.View):
    def __init__(self, members, target_date, target_slot, bosses, per_page=20):
        super().__init__(timeout=None)
        self.members, self.target_date, self.target_slot, self.bosses = members, target_date, target_slot, bosses
        self.current_page = 0
        self.total_pages = max(1, (len(members) + per_page - 1) // per_page)
        self.build_page()

    def build_page(self):
        self.clear_items()
        start = self.current_page * 20
        for name in self.members[start:start+20]:
            self.add_item(ToggleAttendButton(name, self.target_date, self.target_slot))
        
        if self.total_pages > 1:
            prev_btn = discord.ui.Button(label="◀ 이전", style=discord.ButtonStyle.gray, row=4)
            async def prev_cb(interaction):
                self.current_page = (self.current_page - 1) % self.total_pages
                self.build_page(); await interaction.response.edit_message(view=self)
            prev_btn.callback = prev_cb; self.add_item(prev_btn)
            self.add_item(discord.ui.Button(label=f"{self.current_page + 1}/{self.total_pages}", style=discord.ButtonStyle.blurple, disabled=True, row=4))
            next_btn = discord.ui.Button(label="다음 ▶", style=discord.ButtonStyle.gray, row=4)
            async def next_cb(interaction):
                self.current_page = (self.current_page + 1) % self.total_pages
                self.build_page(); await interaction.response.edit_message(view=self)
            next_btn.callback = next_cb; self.add_item(next_btn)

        self.add_item(discord.ui.Button(label="🔥 보스 득템 현황 🔥", style=discord.ButtonStyle.gray, disabled=True, row=5))
        current_row = 6
        for boss in self.bosses:
            if current_row > 12: break
            self.add_item(discord.ui.Button(label=boss, style=discord.ButtonStyle.secondary, disabled=True, row=current_row))
            mung_btn = discord.ui.Button(label="멍", style=discord.ButtonStyle.primary, row=current_row)
            async def mung_cb(interaction, b=boss): await interaction.response.send_message(f"💤 **[{b}]** 멍입니다.", ephemeral=False)
            mung_btn.callback = mung_cb; self.add_item(mung_btn)
            cut_btn = discord.ui.Button(label="컷", style=discord.ButtonStyle.danger, row=current_row)
            async def cut_cb(interaction, b=boss): await interaction.response.send_modal(DropModal(b))
            cut_btn.callback = cut_cb; self.add_item(cut_btn)
            current_row += 1

# =========================
# 🔹 봇 설정 및 명령어
# =========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.command()
async def 출석(ctx):
    with db_lock:
        cursor.execute("SELECT name FROM members ORDER BY name ASC"); m_list = [r[0] for r in cursor.fetchall()]
        cursor.execute("SELECT boss_name FROM boss_list ORDER BY boss_name ASC"); b_list = [r[0] for r in cursor.fetchall()]
    
    if not m_list:
        await ctx.send("❌ 등록된 인원이 없습니다. `!추가`로 인원을 먼저 등록해 주세요."); return
        
    now = datetime.now(KST)
    t_date = now.strftime("%Y-%m-%d")
    t_slot = get_slot()
    
    await ctx.send(f"⚔️ **{t_date} [{t_slot}:00] 보스타임 패널**", 
                   view=ToggleAttendanceView(m_list, t_date, t_slot, b_list))

@bot.command()
async def 점수수정(ctx, name: str, amount: int):
    with db_lock:
        cursor.execute("UPDATE members SET total = total + ? WHERE name = ?", (amount, name))
        conn.commit()
    await ctx.send(f"📊 **{name}**님의 누적 점수가 {amount}만큼 수정되었습니다.")

@bot.command()
async def 초기화(ctx):
    with db_lock:
        cursor.execute("DELETE FROM attendance")
        cursor.execute("UPDATE members SET total = 0")
        conn.commit()
    await ctx.send("♻️ 모든 출석 기록과 누적 점수가 초기화되었습니다. (명단은 유지됩니다)")

@bot.command()
async def 추가(ctx, *, names: str):
    for name in names.replace(" ", "").split(","):
        with db_lock: cursor.execute("INSERT OR IGNORE INTO members(name, total) VALUES(?, 0)", (name,)); conn.commit()
    await ctx.send(f"✅ {names} 추가 완료")

@bot.command()
async def 삭제(ctx, name: str):
    with db_lock:
        cursor.execute("DELETE FROM members WHERE name=?", (name,))
        conn.commit()
    await ctx.send(f"✅ **{name}**님이 명단에서 삭제되었습니다. 다음 패널부터 나타나지 않습니다.")

@bot.command()
async def 명단(ctx):
    with db_lock: cursor.execute("SELECT name FROM members ORDER BY name ASC"); members = [r[0] for r in cursor.fetchall()]
    if not members: await ctx.send("등록된 인원 없음"); return
    await ctx.send("📋 **등록된 명단**\n" + "\n".join(members))

@bot.command()
async def 조회(ctx, start_date: str, end_date: str):
    with db_lock:
        cursor.execute("SELECT name, COUNT(*) FROM attendance WHERE date BETWEEN ? AND ? GROUP BY name ORDER BY COUNT(*) DESC", (start_date, end_date))
        rows = cursor.fetchall()
    if not rows: await ctx.send(f"🔎 {start_date} ~ {end_date} 데이터 없음"); return
    text = f"📊 점수 ({start_date} ~ {end_date})\n"
    for i, r in enumerate(rows, 1): text += f"{i}. {r[0]} - {r[1]}점\n"
    await ctx.send(text)

@bot.command()
async def 주간(ctx):
    now = datetime.now(KST)
    monday = now - timedelta(days=now.weekday())
    week_start = monday.strftime("%Y-%m-%d")
    week_end = (monday + timedelta(days=6)).strftime("%Y-%m-%d")
    await 조회(ctx, week_start, week_end)

@bot.command()
async def 보스추가(ctx, name: str):
    with db_lock: cursor.execute("INSERT OR IGNORE INTO boss_list VALUES (?)", (name,)); conn.commit()
    await ctx.send(f"📌 보스 **[{name}]** 추가되었습니다.")

@bot.command()
async def 보스삭제(ctx, name: str):
    with db_lock: cursor.execute("DELETE FROM boss_list WHERE boss_name=?", (name,)); conn.commit()
    await ctx.send(f"🗑️ 보스 **[{name}]** 삭제되었습니다.")

@bot.command()
async def 득템현황(ctx):
    with db_lock: cursor.execute("SELECT boss_name, winner, item_name, date FROM drops ORDER BY date DESC LIMIT 15"); rows = cursor.fetchall()
    if not rows: await ctx.send("💎 기록된 득템이 없습니다."); return
    text = "💎 **최근 보스 득템 현황**\n"
    for b, w, i, d in rows: text += f"• [{d}] **{b}** : {w} ({i})\n"
    await ctx.send(text)

@tasks.loop(minutes=1)
async def auto_boss_panel():
    now = datetime.now(KST)
    next_boss = get_next_boss_time()
    target = next_boss - timedelta(minutes=10)
    if now.hour == target.hour and now.minute == target.minute:
        channel = bot.get_channel(BOSS_CHANNEL_ID)
        if not channel: return
        with db_lock:
            cursor.execute("SELECT name FROM members ORDER BY name ASC"); m_list = [r[0] for r in cursor.fetchall()]
            cursor.execute("SELECT boss_name FROM boss_list ORDER BY boss_name ASC"); b_list = [r[0] for r in cursor.fetchall()]
        if not m_list: return
        t_date, t_slot = next_boss.strftime("%Y-%m-%d"), f"{next_boss.hour:02d}"
        await channel.send(f"⚔️ **{t_date} [{t_slot}:00] 보스타임 패널**", view=ToggleAttendanceView(m_list, t_date, t_slot, b_list))

@bot.event
async def on_ready():
    if not auto_boss_panel.is_running(): auto_boss_panel.start()
    print(f"로그인 완료 : {bot.user}")

keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))