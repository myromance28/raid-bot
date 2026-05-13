import discord
from discord.ext import commands, tasks
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask
from threading import Thread
import threading

# =========================
# 🔹 설정 및 초기화
# =========================
KST = timezone(timedelta(hours=9))
BOSS_CHANNEL_ID = 1503420212794622073 
LOG_CHANNEL_ID = 1495580902787514508 
BOSS_TIMES = [3, 9, 15, 21]

conn = sqlite3.connect("data.db", check_same_thread=False)
cursor = conn.cursor()
db_lock = threading.Lock()

with db_lock:
    cursor.execute("CREATE TABLE IF NOT EXISTS attendance (date TEXT, time_slot TEXT, name TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS members (name TEXT PRIMARY KEY, total INTEGER DEFAULT 0)")
    cursor.execute("CREATE TABLE IF NOT EXISTS drops (item_name TEXT, winner TEXT, date TEXT, boss_name TEXT)")
    cursor.execute("CREATE TABLE IF NOT EXISTS boss_list (boss_name TEXT PRIMARY KEY)")
    conn.commit()

app = Flask(__name__)
@app.route("/")
def home(): return "OK"
def run(): app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
def keep_alive(): Thread(target=run).start()

# =========================
# 🔹 핵심 함수
# =========================
def get_slot():
    hour = datetime.now(KST).hour
    if 0 <= hour < 6: return "03"
    elif 6 <= hour < 12: return "09"
    elif 12 <= hour < 18: return "15"
    else: return "21"

def is_attended(name, date, slot):
    with db_lock:
        cursor.execute("SELECT 1 FROM attendance WHERE date=? AND time_slot=? AND name=?", (date, slot, name))
        return cursor.fetchone() is not None

# =========================
# 🔹 UI 컴포넌트
# =========================
class DropModal(discord.ui.Modal, title="💎 보스 득템 기록"):
    item_input = discord.ui.TextInput(label="아이템 이름", placeholder="예: 영웅 비기")
    winner_input = discord.ui.TextInput(label="획득자 이름", placeholder="예: 홍길동")
    def __init__(self, boss_name, view):
        super().__init__()
        self.boss_name, self.view = boss_name, view
    async def on_submit(self, interaction: discord.Interaction):
        item, winner = self.item_input.value, self.winner_input.value
        date = datetime.now(KST).strftime("%m-%d %H:%M")
        with db_lock:
            cursor.execute("INSERT INTO drops VALUES (?, ?, ?, ?)", (item, winner, date, self.boss_name))
            conn.commit()
        self.view.boss_status[self.boss_name] = f"✅ 컷 ({winner} - {item})"
        await interaction.response.send_message(f"✅ **[{self.boss_name}] 컷!** {winner}님 - {item} 획득!", ephemeral=False)

class BossActionSelect(discord.ui.Select):
    def __init__(self, bosses, parent_view):
        self.parent_view = parent_view
        options = []
        for b in bosses:
            options.append(discord.SelectOption(label=f"{b} 멍", emoji="💤", value=f"mung_{b}"))
            options.append(discord.SelectOption(label=f"{b} 컷", emoji="⚔️", value=f"cut_{b}"))
        super().__init__(placeholder="보스 멍/컷 선택...", options=options[:25], row=4)
    async def callback(self, interaction: discord.Interaction):
        action, boss_name = self.values[0].split("_", 1)
        if action == "mung":
            self.parent_view.boss_status[boss_name] = "💤 멍"
            await interaction.response.send_message(f"💤 **[{boss_name}]** 멍입니다.", ephemeral=False)
        else:
            await interaction.response.send_modal(DropModal(boss_name, self.parent_view))

class ToggleAttendButton(discord.ui.Button):
    def __init__(self, name, target_date, target_slot):
        self.member_name, self.target_date, self.target_slot = name, target_date, target_slot
        done = is_attended(name, target_date, target_slot)
        # 초기 상태 설정: 출석했으면 회색(secondary), 안했으면 녹색(green)
        super().__init__(label=name, style=discord.ButtonStyle.secondary if done else discord.ButtonStyle.green)
        
    async def callback(self, interaction: discord.Interaction):
        with db_lock:
            if is_attended(self.member_name, self.target_date, self.target_slot):
                cursor.execute("DELETE FROM attendance WHERE date=? AND time_slot=? AND name=?", (self.target_date, self.target_slot, self.member_name))
                cursor.execute("UPDATE members SET total = CASE WHEN total > 0 THEN total - 1 ELSE 0 END WHERE name=?", (self.member_name,))
                self.style = discord.ButtonStyle.green # 취소 시 다시 녹색
            else:
                cursor.execute("INSERT INTO attendance VALUES (?, ?, ?)", (self.target_date, self.target_slot, self.member_name))
                cursor.execute("INSERT INTO members(name, total) VALUES(?, 1) ON CONFLICT(name) DO UPDATE SET total = total + 1", (self.member_name,))
                self.style = discord.ButtonStyle.secondary # 출석 시 회색
            conn.commit()
        # 🚀 상호작용 실패 방지: defer 대신 edit_message를 사용하여 즉시 갱신
        await interaction.response.edit_message(view=self.view)

class ToggleAttendanceView(discord.ui.View):
    def __init__(self, members, target_date, target_slot, bosses, per_page=15):
        super().__init__(timeout=None)
        self.members, self.target_date, self.target_slot, self.bosses = members, target_date, target_slot, bosses
        self.current_page, self.boss_status = 0, {b: "미확인" for b in bosses}
        self.total_pages = max(1, (len(members) + per_page - 1) // per_page)
        self.build_page()

    def build_page(self):
        self.clear_items()
        start = self.current_page * 15
        for name in self.members[start:start+15]:
            self.add_item(ToggleAttendButton(name, self.target_date, self.target_slot))
        
        if self.total_pages > 1:
            prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.gray, row=3)
            async def prev_cb(i):
                self.current_page = (self.current_page - 1) % self.total_pages
                self.build_page(); await i.response.edit_message(view=self)
            prev_btn.callback = prev_cb; self.add_item(prev_btn)
            self.add_item(discord.ui.Button(label=f"{self.current_page + 1}/{self.total_pages}", style=discord.ButtonStyle.blurple, disabled=True, row=3))
            next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.gray, row=3)
            async def next_cb(i):
                self.current_page = (self.current_page + 1) % self.total_pages
                self.build_page(); await i.response.edit_message(view=self)
            next_btn.callback = next_cb; self.add_item(next_btn)

        if self.bosses:
            self.add_item(BossActionSelect(self.bosses, self))

        send_btn = discord.ui.Button(label="📊 결과 전송 (정산)", style=discord.ButtonStyle.danger, row=3)
        async def send_cb(i):
            await i.response.defer(ephemeral=True)
            log_ch = i.client.get_channel(LOG_CHANNEL_ID)
            if not log_ch: return await i.followup.send("❌ 채널 없음", ephemeral=True)
            with db_lock:
                cursor.execute("SELECT name FROM attendance WHERE date=? AND time_slot=?", (self.target_date, self.target_slot))
                attended = [r[0] for r in cursor.fetchall()]
            embed = discord.Embed(title=f"📊 {self.target_date} [{self.target_slot}:00] 정산", color=0x3498db)
            embed.add_field(name=f"👥 출석 ({len(attended)}명)", value="\n".join([f"• {n} (1점)" for n in attended]) if attended else "없음", inline=False)
            embed.add_field(name="⚔️ 보스 현황", value="\n".join([f"**{b}**: {s}" for b, s in self.boss_status.items()]) if self.bosses else "기록 없음", inline=False)
            await log_ch.send(embed=embed)
            await i.followup.send("🚀 전송 완료!", ephemeral=True)
        send_btn.callback = send_cb; self.add_item(send_btn)

# (명령어 파트는 기존과 동일하므로 하단 코드는 생략하지 말고 그대로 사용하세요)
intents = discord.Intents.default(); intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.command()
async def 출석(ctx):
    with db_lock:
        cursor.execute("SELECT name FROM members ORDER BY name ASC"); m_list = [r[0] for r in cursor.fetchall()]
        cursor.execute("SELECT boss_name FROM boss_list ORDER BY boss_name ASC"); b_list = [r[0] for r in cursor.fetchall()]
    if not m_list: return await ctx.send("❌ 등록된 인원이 없습니다.")
    now = datetime.now(KST); t_date, t_slot = now.strftime("%Y-%m-%d"), get_slot()
    await ctx.send(f"⚔️ **{t_date} [{t_slot}:00] 보스타임 패널**", view=ToggleAttendanceView(m_list, t_date, t_slot, b_list))

@bot.command()
async def 추가(ctx, *, names: str):
    for name in names.replace(" ", "").split(","):
        with db_lock: cursor.execute("INSERT OR IGNORE INTO members(name, total) VALUES(?, 0)", (name,)); conn.commit()
    await ctx.send(f"✅ {names} 추가 완료")

@bot.command()
async def 삭제(ctx, name: str):
    with db_lock: cursor.execute("DELETE FROM members WHERE name=?", (name,)); conn.commit()
    await ctx.send(f"✅ {name} 삭제 완료")

@bot.command()
async def 보스추가(ctx, name: str):
    with db_lock: cursor.execute("INSERT OR IGNORE INTO boss_list VALUES (?)", (name,)); conn.commit()
    await ctx.send(f"📌 보스 [{name}] 추가")

@bot.command()
async def 보스삭제(ctx, name: str):
    with db_lock: cursor.execute("DELETE FROM boss_list WHERE boss_name=?", (name,)); conn.commit()
    await ctx.send(f"🗑️ 보스 [{name}] 삭제")

# (나머지 조회, 주간, 명단 등도 위와 같은 패턴으로 유지)

@bot.event
async def on_ready():
    print(f"로그인 완료: {bot.user}")

keep_alive()
bot.run(os.getenv("DISCORD_TOKEN"))