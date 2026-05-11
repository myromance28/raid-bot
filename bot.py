import discord
from discord.ext import commands
import os

# ===== 기본 설정 =====
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

TOKEN = os.getenv("DISCORD_TOKEN")

# ===== 점수 시스템 =====
scores = {
    "법소리": 0,
    "원턴": 0,
    "으르렁": 0
}

# 중복 방지 (유저 + 캐릭터 기준)
attended = set()

# ===== 버튼 UI =====
class RaidView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def give_point(self, interaction, name):

        user_id = interaction.user.id
        key = f"{user_id}_{name}"

        # 중복 체크
        if key in attended:
            await interaction.response.send_message("이미 이 캐릭터로 출석했어요", ephemeral=True)
            return

        attended.add(key)

        # 점수 증가
        scores[name] += 1

        await interaction.response.send_message(
            f"✅ {name} +1점 완료 (총 {scores[name]}점)",
            ephemeral=True
        )

    @discord.ui.button(label="법소리 출석", style=discord.ButtonStyle.green)
    async def bosori(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.give_point(interaction, "법소리")

    @discord.ui.button(label="원턴 출석", style=discord.ButtonStyle.green)
    async def oneturn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.give_point(interaction, "원턴")

    @discord.ui.button(label="으르렁 출석", style=discord.ButtonStyle.green)
    async def growl(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.give_point(interaction, "으르렁")

# ===== 봇 시작 =====
@bot.event
async def on_ready():
    print("봇 실행 완료")

    channel_id = 1170263342926004334  # 👈 여기에 채널 ID 넣기
    channel = bot.get_channel(channel_id)

    if channel:
        await channel.send("📢 레이드 출석 체크", view=RaidView())

# ===== 랭킹 명령어 =====
@bot.command()
async def 랭킹(ctx):

    sorted_data = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    msg = "\n".join([
        f"{i+1}. {name} {score}점"
        for i, (name, score) in enumerate(sorted_data)
    ])

    await ctx.send(msg if msg else "아직 점수 없음")

# ===== 실행 =====
bot.run(TOKEN)