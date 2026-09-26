import os
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("MTUyNDY4NzA0OTcwNDA4MzU5Nw.GktjzE.BYMvTzUUEljLeUkL530kBgnk4g2-tzAa4N1NP0")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set")


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(
            command_prefix="!",
            intents=intents
        )

    async def setup_hook(self):
        await self.tree.sync()


bot = MyBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.tree.command(name="hello", description="Says hello")
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Hello, {interaction.user.mention}!"
    )


@bot.tree.command(name="ping", description="Checks whether the bot is online")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")


bot.run(TOKEN)
