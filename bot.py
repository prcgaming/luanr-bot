import logging
import os

import discord
from discord.ext import commands


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")

if not GUILD_ID:
    raise RuntimeError("GUILD_ID is missing")

try:
    GUILD_ID = int(GUILD_ID)
except ValueError:
    raise RuntimeError("GUILD_ID must contain numbers only")


class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(
            command_prefix="!",
            intents=intents,
        )

    async def setup_hook(self):
        guild = discord.Object(id=GUILD_ID)

        # Copy the commands to this specific server.
        self.tree.copy_global_to(guild=guild)

        # Register them in this server.
        synced_commands = await self.tree.sync(guild=guild)

        logging.info(
            "Synced %d command(s) to server %s",
            len(synced_commands),
            GUILD_ID,
        )


bot = MyBot()


@bot.event
async def on_ready():
    logging.info("Bot is online as %s", bot.user)


@bot.tree.command(
    name="ping",
    description="Checks whether the bot is online",
)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")


@bot.tree.command(
    name="hello",
    description="The bot says hello",
)
async def hello(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"Hello, {interaction.user.mention}!"
    )


bot.run(TOKEN, log_handler=None)
