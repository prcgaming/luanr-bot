# bot.py
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import sqlite3
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID_RAW = os.getenv("GUILD_ID")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not configured.")

if not GUILD_ID_RAW or not GUILD_ID_RAW.isdigit():
    raise RuntimeError("GUILD_ID must be a numeric Discord server ID.")

GUILD_ID = int(GUILD_ID_RAW)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

SQLITE_PATH = DATA_DIR / "bot.sqlite3"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("server-management-bot")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Database:
    """
    SQLite database layer.

    The SQL is deliberately isolated here so the storage backend can later be
    replaced with PostgreSQL or Supabase without rewriting command handlers.
    """

    def __init__(self, path: Path):
        self.path = path
        self.connection = sqlite3.connect(
            self.path,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self.lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    log_channel_id INTEGER,
                    modlog_channel_id INTEGER,
                    staff_role_id INTEGER,
                    language TEXT NOT NULL DEFAULT 'en',
                    timezone TEXT NOT NULL DEFAULT 'UTC',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS welcome_settings (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    welcome_message TEXT NOT NULL DEFAULT
                        'Welcome {user} to {server}! You are member #{member_count}.',
                    goodbye_message TEXT NOT NULL DEFAULT
                        '{username} has left {server}.',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    embed_enabled INTEGER NOT NULL DEFAULT 0,
                    role_id INTEGER,
                    image_url TEXT,
                    FOREIGN KEY (guild_id)
                        REFERENCES guild_settings(guild_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS boost_settings (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    boost_message TEXT NOT NULL DEFAULT
                        'Thank you {user} for boosting {server}!',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    embed_enabled INTEGER NOT NULL DEFAULT 0,
                    role_id INTEGER,
                    FOREIGN KEY (guild_id)
                        REFERENCES guild_settings(guild_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS moderation_cases (
                    case_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    target_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS permission_backups (
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    permission_name TEXT NOT NULL,
                    previous_value INTEGER,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (
                        guild_id,
                        channel_id,
                        permission_name
                    )
                );

                CREATE TABLE IF NOT EXISTS autoresponders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    trigger TEXT NOT NULL,
                    response TEXT NOT NULL,
                    exact_match INTEGER NOT NULL DEFAULT 0,
                    channel_id INTEGER,
                    cooldown_seconds INTEGER NOT NULL DEFAULT 10,
                    enabled INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS leveling_users (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    xp INTEGER NOT NULL DEFAULT 0,
                    level INTEGER NOT NULL DEFAULT 0,
                    last_message_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS polls (
                    poll_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    question TEXT NOT NULL,
                    options TEXT NOT NULL,
                    closed INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS poll_votes (
                    poll_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    option_index INTEGER NOT NULL,
                    PRIMARY KEY (poll_id, user_id),
                    FOREIGN KEY (poll_id)
                        REFERENCES polls(poll_id)
                        ON DELETE CASCADE
                );
                """
            )
            self.connection.commit()

    async def execute(
        self,
        query: str,
        parameters: tuple = (),
    ) -> sqlite3.Cursor:
        async with self.lock:
            cursor = self.connection.execute(query, parameters)
            self.connection.commit()
            return cursor

    async def fetchone(
        self,
        query: str,
        parameters: tuple = (),
    ) -> Optional[sqlite3.Row]:
        async with self.lock:
            cursor = self.connection.execute(query, parameters)
            return cursor.fetchone()

    async def fetchall(
        self,
        query: str,
        parameters: tuple = (),
    ) -> list[sqlite3.Row]:
        async with self.lock:
            cursor = self.connection.execute(query, parameters)
            return cursor.fetchall()

    async def close(self) -> None:
        async with self.lock:
            self.connection.close()


db = Database(SQLITE_PATH)


# ---------------------------------------------------------------------------
# Discord helpers
# ---------------------------------------------------------------------------

def now() -> int:
    return int(time.time())


def safe_text(value: str, maximum: int = 1000) -> str:
    value = value.strip()
    return value[:maximum]


def format_message(
    template: str,
    *,
    member: discord.Member,
    guild: discord.Guild,
) -> str:
    replacements = {
        "{user}": member.mention,
        "{username}": discord.utils.escape_mentions(member.display_name),
        "{server}": discord.utils.escape_mentions(guild.name),
        "{member_count}": str(guild.member_count or len(guild.members)),
    }

    result = template
    for key, replacement in replacements.items():
        result = result.replace(key, replacement)

    return result[:2000]


def member_can_be_moderated(
    moderator: discord.Member,
    target: discord.Member,
) -> tuple[bool, str]:
    if moderator.id == target.id:
        return False, "You cannot moderate yourself."

    if target.id == moderator.guild.owner_id:
        return False, "The server owner cannot be moderated."

    if target.top_role >= moderator.top_role and moderator.id != moderator.guild.owner_id:
        return False, "That member has an equal or higher role than you."

    return True, ""


def bot_can_manage_member(
    guild: discord.Guild,
    target: discord.Member,
) -> tuple[bool, str]:
    me = guild.me

    if me is None:
        return False, "I could not determine my server member record."

    if target.id == guild.owner_id:
        return False, "The server owner cannot be managed."

    if target.top_role >= me.top_role:
        return False, "That member's highest role is above my highest role."

    return True, ""


def bot_can_manage_role(
    guild: discord.Guild,
    role: discord.Role,
) -> tuple[bool, str]:
    me = guild.me

    if me is None:
        return False, "I could not determine my server member record."

    if role.is_default():
        return False, "The @everyone role cannot be managed."

    if role.managed:
        return False, "Managed integration roles cannot be managed."

    if role >= me.top_role:
        return False, "That role is above or equal to my highest role."

    return True, ""


async def send_log(
    guild: discord.Guild,
    message: str,
    *,
    embed: Optional[discord.Embed] = None,
) -> None:
    row = await db.fetchone(
        """
        SELECT log_channel_id, modlog_channel_id
        FROM guild_settings
        WHERE guild_id = ?
        """,
        (guild.id,),
    )

    if not row:
        return

    channel_id = row["modlog_channel_id"] or row["log_channel_id"]
    if not channel_id:
        return

    channel = guild.get_channel(channel_id)

    if not isinstance(channel, discord.TextChannel):
        return

    try:
        await channel.send(
            content=message[:2000] if message else None,
            embed=embed,
        )
    except discord.HTTPException:
        logger.exception("Could not send a log message in guild %s", guild.id)


async def create_case(
    guild: discord.Guild,
    action: str,
    target_id: int,
    moderator_id: int,
    reason: str,
) -> int:
    cursor = await db.execute(
        """
        INSERT INTO moderation_cases
        (guild_id, action, target_id, moderator_id, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            guild.id,
            action,
            target_id,
            moderator_id,
            safe_text(reason, 500),
            now(),
        ),
    )
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    allowed_mentions=discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=True,
    ),
)


# ---------------------------------------------------------------------------
# Startup and synchronization
# ---------------------------------------------------------------------------

@bot.event
async def on_ready() -> None:
    await db.initialize()

    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)

    try:
        synced = await bot.tree.sync(guild=guild)
        logger.info(
            "Logged in as %s; synchronized %d slash commands.",
            bot.user,
            len(synced),
        )
    except discord.HTTPException:
        logger.exception("Slash-command synchronization failed.")


@bot.event
async def on_disconnect() -> None:
    logger.warning("Disconnected from Discord.")


@bot.event
async def on_resumed() -> None:
    logger.info("Discord connection resumed.")


# ---------------------------------------------------------------------------
# Global slash-command error handler
# ---------------------------------------------------------------------------

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You do not have permission to use this command."
    elif isinstance(error, app_commands.BotMissingPermissions):
        message = "I am missing one or more permissions required for that action."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"Try again in {error.retry_after:.1f} seconds."
    elif isinstance(error, app_commands.TransformerError):
        message = "One or more command arguments were invalid."
    elif isinstance(error, app_commands.CheckFailure):
        message = "You are not allowed to use this command here."
    else:
        message = "Something went wrong while processing that command."
        logger.exception(
            "Unhandled application-command error",
            exc_info=error,
        )

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.exception("Could not send command error response.")


# ---------------------------------------------------------------------------
# Help command
# ---------------------------------------------------------------------------

HELP_SECTIONS = {
    "Getting Started": [
        ("/help", "Show this help menu."),
        ("/ping", "Check bot latency and connectivity."),
        ("/setup_view", "View the current server configuration."),
    ],
    "Channel Management": [
        ("/channel create", "Create a text channel."),
        ("/channel delete", "Delete a channel."),
        ("/channel rename", "Rename a channel."),
        ("/channel topic", "Change a text channel topic."),
        ("/channel slowmode", "Set channel slowmode."),
        ("/hide", "Hide a channel from @everyone."),
        ("/unhide", "Restore channel visibility."),
        ("/lock", "Keep a channel visible but prevent messages."),
        ("/unlock", "Restore the previous send-message permission."),
    ],
    "Moderation": [
        ("/kick", "Kick a member."),
        ("/ban", "Ban a member."),
        ("/timeout", "Timeout a member."),
        ("/untimeout", "Remove a member timeout."),
        ("/warn", "Warn a member and save the case."),
        ("/warnings", "View a member's recent warnings."),
    ],
}


def build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛠️ Server Management Bot — Help",
        description=(
            "Use the slash commands below to manage your server. "
            "Commands are permission-protected where necessary.\n\n"
            "**Tip:** Type `/` in Discord and start typing a command to see "
            "its arguments and options."
        ),
        color=discord.Color.blurple(),
    )

    for section, commands_list in HELP_SECTIONS.items():
        value = "\n".join(
            f"`{command}` — {description}"
            for command, description in commands_list
        )
        embed.add_field(name=section, value=value, inline=False)

    embed.set_footer(text="Use /help anytime to see the available commands.")
    return embed


@bot.tree.command(
    name="help",
    description="Show the bot's commands and what they do.",
)
@app_commands.guild_only()
async def help_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        embed=build_help_embed(),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="ping",
    description="Check whether the bot is responding.",
)
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 5.0)
async def ping(interaction: discord.Interaction) -> None:
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(
        f"Pong: {latency} ms.",
        ephemeral=True,
    )


@bot.tree.command(
    name="setup_view",
    description="View the current server configuration.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_view(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    assert guild is not None

    settings = await db.fetchone(
        """
        SELECT *
        FROM guild_settings
        WHERE guild_id = ?
        """,
        (guild.id,),
    )

    welcome = await db.fetchone(
        """
        SELECT *
        FROM welcome_settings
        WHERE guild_id = ?
        """,
        (guild.id,),
    )

    boost = await db.fetchone(
        """
        SELECT *
        FROM boost_settings
        WHERE guild_id = ?
        """,
        (guild.id,),
    )

    embed = discord.Embed(
        title="Server Configuration",
        color=discord.Color.blurple(),
    )

    if settings:
        log_channel = (
            guild.get_channel(settings["log_channel_id"])
            if settings["log_channel_id"]
            else None
        )
        modlog_channel = (
            guild.get_channel(settings["modlog_channel_id"])
            if settings["modlog_channel_id"]
            else None
        )

        embed.add_field(
            name="Logging",
            value=(
                f"General: {log_channel.mention if log_channel else 'Not configured'}\n"
                f"Moderation: "
                f"{modlog_channel.mention if modlog_channel else 'Not configured'}"
            ),
            inline=False,
        )

    if welcome:
        welcome_channel = (
            guild.get_channel(welcome["channel_id"])
            if welcome["channel_id"]
            else None
        )

        embed.add_field(
            name="Welcome",
            value=(
                f"Enabled: {'Yes' if welcome['enabled'] else 'No'}\n"
                f"Channel: "
                f"{welcome_channel.mention if welcome_channel else 'Not configured'}\n"
                f"Embed: {'Yes' if welcome['embed_enabled'] else 'No'}"
            ),
            inline=False,
        )

    if boost:
        boost_channel = (
            guild.get_channel(boost["channel_id"])
            if boost["channel_id"]
            else None
        )

        embed.add_field(
            name="Boosts",
            value=(
                f"Enabled: {'Yes' if boost['enabled'] else 'No'}\n"
                f"Channel: "
                f"{boost_channel.mention if boost_channel else 'Not configured'}\n"
                f"Embed: {'Yes' if boost['embed_enabled'] else 'No'}"
            ),
            inline=False,
        )

    if not settings and not welcome and not boost:
        embed.description = "No server settings have been configured yet."

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Channel management
# ---------------------------------------------------------------------------

channel_group = app_commands.Group(
    name="channel",
    description="Manage server channels.",
)


@channel_group.command(name="create", description="Create a text channel.")
@app_commands.describe(
    name="The channel name.",
    category="Optional category.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def channel_create(
    interaction: discord.Interaction,
    name: str,
    category: Optional[discord.CategoryChannel] = None,
) -> None:
    guild = interaction.guild
    assert guild is not None

    name = re.sub(r"[^a-zA-Z0-9_-]", "-", name.strip().lower())[:90]

    if len(name) < 1:
        await interaction.response.send_message(
            "Channel names must contain at least one valid character.",
            ephemeral=True,
        )
        return

    try:
        channel = await guild.create_text_channel(
            name=name,
            category=category,
            reason=f"Created by {interaction.user} ({interaction.user.id})",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to create channels.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Channel creation failed in guild %s", guild.id)
        await interaction.response.send_message(
            "Discord rejected the channel creation request.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Created {channel.mention}.",
        ephemeral=True,
    )

    await send_log(
        guild,
        f"{interaction.user.mention} created channel {channel.mention}.",
    )


@channel_group.command(name="delete", description="Delete a channel.")
@app_commands.describe(channel="The channel to delete.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def channel_delete(
    interaction: discord.Interaction,
    channel: discord.abc.GuildChannel,
) -> None:
    guild = interaction.guild
    assert guild is not None

    if channel.id == interaction.channel_id:
        await interaction.response.send_message(
            "For safety, use this command from another channel.",
            ephemeral=True,
        )
        return

    try:
        await channel.delete(
            reason=f"Deleted by {interaction.user} ({interaction.user.id})",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to delete that channel.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Channel deletion failed in guild %s", guild.id)
        await interaction.response.send_message(
            "Discord rejected the channel deletion request.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "The channel was deleted.",
        ephemeral=True,
    )

    await send_log(
        guild,
        f"{interaction.user.mention} deleted channel `{channel.name}`.",
    )


@channel_group.command(name="rename", description="Rename a channel.")
@app_commands.describe(
    channel="The channel to rename.",
    name="The new channel name.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def channel_rename(
    interaction: discord.Interaction,
    channel: discord.abc.GuildChannel,
    name: str,
) -> None:
    name = re.sub(r"[^a-zA-Z0-9_-]", "-", name.strip().lower())[:90]

    if not name:
        await interaction.response.send_message(
            "That is not a valid channel name.",
            ephemeral=True,
        )
        return

    try:
        await channel.edit(
            name=name,
            reason=f"Renamed by {interaction.user} ({interaction.user.id})",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to rename that channel.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Channel rename failed.")
        await interaction.response.send_message(
            "Discord rejected the channel rename request.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Renamed the channel to `{name}`.",
        ephemeral=True,
    )


@channel_group.command(name="topic", description="Change a text channel topic.")
@app_commands.describe(
    channel="The text channel.",
    topic="The new topic.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def channel_topic(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    topic: str,
) -> None:
    try:
        await channel.edit(
            topic=safe_text(topic, 1024),
            reason=f"Topic changed by {interaction.user} ({interaction.user.id})",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to edit that channel.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Channel topic update failed.")
        await interaction.response.send_message(
            "Discord rejected the topic update.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "The channel topic was updated.",
        ephemeral=True,
    )


@channel_group.command(name="slowmode", description="Set channel slowmode.")
@app_commands.describe(
    channel="The text channel.",
    seconds="Slowmode duration from 0 to 21600 seconds.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def channel_slowmode(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    seconds: app_commands.Range[int, 0, 21600],
) -> None:
    try:
        await channel.edit(
            slowmode_delay=seconds,
            reason=f"Slowmode changed by {interaction.user} ({interaction.user.id})",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to change slowmode.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Slowmode update failed.")
        await interaction.response.send_message(
            "Discord rejected the slowmode update.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Slowmode set to {seconds} seconds.",
        ephemeral=True,
    )


async def save_permission_backup(
    guild_id: int,
    channel_id: int,
    permission_name: str,
    previous_value: Optional[bool],
) -> None:
    encoded = None if previous_value is None else int(previous_value)

    await db.execute(
        """
        INSERT INTO permission_backups
        (guild_id, channel_id, permission_name, previous_value, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, channel_id, permission_name)
        DO NOTHING
        """,
        (
            guild_id,
            channel_id,
            permission_name,
            encoded,
            now(),
        ),
    )


async def restore_permission_backup(
    guild_id: int,
    channel_id: int,
    permission_name: str,
) -> Optional[bool]:
    row = await db.fetchone(
        """
        SELECT previous_value
        FROM permission_backups
        WHERE guild_id = ?
          AND channel_id = ?
          AND permission_name = ?
        """,
        (guild_id, channel_id, permission_name),
    )

    if not row:
        return None

    previous_value = row["previous_value"]
    await db.execute(
        """
        DELETE FROM permission_backups
        WHERE guild_id = ?
          AND channel_id = ?
          AND permission_name = ?
        """,
        (guild_id, channel_id, permission_name),
    )

    if previous_value is None:
        return None

    return bool(previous_value)


@bot.tree.command(name="hide", description="Hide a channel from @everyone.")
@app_commands.describe(channel="The channel to hide.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def hide_channel(
    interaction: discord.Interaction,
    channel: discord.abc.GuildChannel,
) -> None:
    guild = interaction.guild
    assert guild is not None

    everyone = guild.default_role
    overwrite = channel.overwrites_for(everyone)

    await save_permission_backup(
        guild.id,
        channel.id,
        "view_channel",
        overwrite.view_channel,
    )

    try:
        overwrite.view_channel = False
        await channel.set_permissions(
            everyone,
            overwrite=overwrite,
            reason=f"Channel hidden by {interaction.user} ({interaction.user.id})",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to change that channel's permissions.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Channel hide failed.")
        await interaction.response.send_message(
            "Discord rejected the permission update.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"{channel.mention} is now hidden from @everyone.",
        ephemeral=True,
    )

    await send_log(
        guild,
        f"{interaction.user.mention} hid {channel.mention} from @everyone.",
    )


@bot.tree.command(name="unhide", description="Restore a channel's visibility.")
@app_commands.describe(channel="The channel to unhide.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_channels=True)
async def unhide_channel(
    interaction: discord.Interaction,
    channel: discord.abc.GuildChannel,
) -> None:
    guild = interaction.guild
    assert guild is not None

    previous_value = await restore_permission_backup(
        guild.id,
        channel.id,
        "view_channel",
    )

    everyone = guild.default_role
    overwrite = channel.overwrites_for(everyone)
    overwrite.view_channel = previous_value

    try:
        await channel.set_permissions(
            everyone,
            overwrite=overwrite,
            reason=f"Channel unhidden by {interaction.user} ({interaction.user.id})",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to change that channel's permissions.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Channel unhide failed.")
        await interaction.response.send_message(
            "Discord rejected the permission update.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"{channel.mention} visibility was restored.",
        ephemeral=True,
    )

    await send_log(
        guild,
        f"{interaction.user.mention} restored visibility for {channel.mention}.",
    )


@bot.tree.command(name="lock", description="Prevent @everyone from sending messages.")
@app_commands.describe(channel="The channel to lock.")
@app_commands.guild_only()
@app_commands.checks.has_any_role()
async def lock_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
) -> None:
    member = interaction.user

    if not isinstance(member, discord.Member) or not (
        member.guild_permissions.manage_channels
        or member.guild_permissions.manage_messages
    ):
        await interaction.response.send_message(
            "You need Manage Channels or Manage Messages.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    assert guild is not None

    everyone = guild.default_role
    overwrite = channel.overwrites_for(everyone)

    await save_permission_backup(
        guild.id,
        channel.id,
        "send_messages",
        overwrite.send_messages,
    )

    try:
        overwrite.send_messages = False
        await channel.set_permissions(
            everyone,
            overwrite=overwrite,
            reason=f"Channel locked by {interaction.user} ({interaction.user.id})",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to lock that channel.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Channel lock failed.")
        await interaction.response.send_message(
            "Discord rejected the permission update.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"{channel.mention} is locked. Members can still view it.",
        ephemeral=True,
    )

    await send_log(
        guild,
        f"{interaction.user.mention} locked {channel.mention}.",
    )


@bot.tree.command(name="unlock", description="Restore @everyone's send permission.")
@app_commands.describe(channel="The channel to unlock.")
@app_commands.guild_only()
@app_commands.checks.has_any_role()
async def unlock_channel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
) -> None:
    member = interaction.user

    if not isinstance(member, discord.Member) or not (
        member.guild_permissions.manage_channels
        or member.guild_permissions.manage_messages
    ):
        await interaction.response.send_message(
            "You need Manage Channels or Manage Messages.",
            ephemeral=True,
        )
        return

    guild = interaction.guild
    assert guild is not None

    previous_value = await restore_permission_backup(
        guild.id,
        channel.id,
        "send_messages",
    )

    everyone = guild.default_role
    overwrite = channel.overwrites_for(everyone)
    overwrite.send_messages = previous_value

    try:
        await channel.set_permissions(
            everyone,
            overwrite=overwrite,
            reason=f"Channel unlocked by {interaction.user} ({interaction.user.id})",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to unlock that channel.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Channel unlock failed.")
        await interaction.response.send_message(
            "Discord rejected the permission update.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"{channel.mention} send permission was restored.",
        ephemeral=True,
    )

    await send_log(
        guild,
        f"{interaction.user.mention} unlocked {channel.mention}.",
    )


bot.tree.add_command(channel_group)


# ---------------------------------------------------------------------------
# Moderation commands
# ---------------------------------------------------------------------------

@bot.tree.command(name="kick", description="Kick a member.")
@app_commands.describe(
    member="The member to kick.",
    reason="The reason for the kick.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.checks.cooldown(1, 5.0)
async def kick_member(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided.",
) -> None:
    guild = interaction.guild
    moderator = interaction.user
    assert guild is not None
    assert isinstance(moderator, discord.Member)

    allowed, error = member_can_be_moderated(moderator, member)
    if not allowed:
        await interaction.response.send_message(error, ephemeral=True)
        return

    allowed, error = bot_can_manage_member(guild, member)
    if not allowed:
        await interaction.response.send_message(error, ephemeral=True)
        return

    reason = safe_text(reason, 500)

    try:
        await member.kick(reason=reason)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to kick that member.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Kick failed in guild %s", guild.id)
        await interaction.response.send_message(
            "Discord rejected the kick request.",
            ephemeral=True,
        )
        return

    case_id = await create_case(
        guild,
        "kick",
        member.id,
        moderator.id,
        reason,
    )

    await interaction.response.send_message(
        f"{member} was kicked. Case #{case_id}.",
        ephemeral=True,
    )

    await send_log(
        guild,
        f"Case #{case_id}: {moderator.mention} kicked `{member}`. Reason: {reason}",
    )


@bot.tree.command(name="ban", description="Ban a member.")
@app_commands.describe(
    member="The member to ban.",
    reason="The reason for the ban.",
    delete_message_days="Days of recent messages to delete, from 0 to 7.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.checks.cooldown(1, 5.0)
async def ban_member(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided.",
    delete_message_days: app_commands.Range[int, 0, 7] = 0,
) -> None:
    guild = interaction.guild
    moderator = interaction.user
    assert guild is not None
    assert isinstance(moderator, discord.Member)

    allowed, error = member_can_be_moderated(moderator, member)
    if not allowed:
        await interaction.response.send_message(error, ephemeral=True)
        return

    allowed, error = bot_can_manage_member(guild, member)
    if not allowed:
        await interaction.response.send_message(error, ephemeral=True)
        return

    reason = safe_text(reason, 500)

    try:
        await member.ban(
            reason=reason,
            delete_message_days=delete_message_days,
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to ban that member.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Ban failed in guild %s", guild.id)
        await interaction.response.send_message(
            "Discord rejected the ban request.",
            ephemeral=True,
        )
        return

    case_id = await create_case(
        guild,
        "ban",
        member.id,
        moderator.id,
        reason,
    )

    await interaction.response.send_message(
        f"{member} was banned. Case #{case_id}.",
        ephemeral=True,
    )

    await send_log(
        guild,
        f"Case #{case_id}: {moderator.mention} banned `{member}`. Reason: {reason}",
    )


@bot.tree.command(name="timeout", description="Timeout a member.")
@app_commands.describe(
    member="The member to timeout.",
    minutes="Timeout length in minutes.",
    reason="The reason for the timeout.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.checks.cooldown(1, 5.0)
async def timeout_member(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: str = "No reason provided.",
) -> None:
    guild = interaction.guild
    moderator = interaction.user
    assert guild is not None
    assert isinstance(moderator, discord.Member)

    allowed, error = member_can_be_moderated(moderator, member)
    if not allowed:
        await interaction.response.send_message(error, ephemeral=True)
        return

    allowed, error = bot_can_manage_member(guild, member)
    if not allowed:
        await interaction.response.send_message(error, ephemeral=True)
        return

    reason = safe_text(reason, 500)

    try:
        await member.timeout(
            timedelta(minutes=minutes),
            reason=reason,
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to timeout that member.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Timeout failed in guild %s", guild.id)
        await interaction.response.send_message(
            "Discord rejected the timeout request.",
            ephemeral=True,
        )
        return

    case_id = await create_case(
        guild,
        "timeout",
        member.id,
        moderator.id,
        reason,
    )

    await interaction.response.send_message(
        f"{member} was timed out for {minutes} minutes. Case #{case_id}.",
        ephemeral=True,
    )

    await send_log(
        guild,
        f"Case #{case_id}: {moderator.mention} timed out `{member}` "
        f"for {minutes} minutes. Reason: {reason}",
    )


@bot.tree.command(name="untimeout", description="Remove a member's timeout.")
@app_commands.describe(member="The member to untimeout.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout_member(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    guild = interaction.guild
    moderator = interaction.user
    assert guild is not None
    assert isinstance(moderator, discord.Member)

    allowed, error = member_can_be_moderated(moderator, member)
    if not allowed:
        await interaction.response.send_message(error, ephemeral=True)
        return

    allowed, error = bot_can_manage_member(guild, member)
    if not allowed:
        await interaction.response.send_message(error, ephemeral=True)
        return

    try:
        await member.timeout(None, reason=f"Removed by {moderator}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "I do not have permission to remove that timeout.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        logger.exception("Removing timeout failed.")
        await interaction.response.send_message(
            "Discord rejected the timeout removal request.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"Timeout removed from {member.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="warn", description="Warn a member.")
@app_commands.describe(
    member="The member to warn.",
    reason="The warning reason.",
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.checks.cooldown(1, 3.0)
async def warn_member(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "No reason provided.",
) -> None:
    guild = interaction.guild
    moderator = interaction.user
    assert guild is not None
    assert isinstance(moderator, discord.Member)

    allowed, error = member_can_be_moderated(moderator, member)
    if not allowed:
        await interaction.response.send_message(error, ephemeral=True)
        return

    reason = safe_text(reason, 500)

    await db.execute(
        """
        INSERT INTO warnings
        (guild_id, user_id, moderator_id, reason, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (guild.id, member.id, moderator.id, reason, now()),
    )

    count_row = await db.fetchone(
        """
        SELECT COUNT(*) AS count
        FROM warnings
        WHERE guild_id = ?
          AND user_id = ?
        """,
        (guild.id, member.id),
    )

    count = count_row["count"] if count_row else 1

    await interaction.response.send_message(
        f"{member.mention} was warned. They now have {count} warning(s).",
        ephemeral=True,
    )

    await send_log(
        guild,
        f"{moderator.mention} warned {member.mention}. "
        f"Total warnings: {count}. Reason: {reason}",
    )


@bot.tree.command(name="warnings", description="View a member's warnings.")
@app_commands.describe(member="The member whose warnings to view.")
@app_commands.guild_only()
@app_commands.checks.has_permissions(manage_messages=True)
async def view_warnings(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    guild = interaction.guild
    assert guild is not None

    rows = await db.fetchall(
        """
        SELECT moderator_id, reason, created_at
        FROM warnings
        WHERE guild_id = ?
          AND user_id = ?
        ORDER BY id DESC
        LIMIT 20
        """,
        (guild.id, member.id),
    )

    if not rows:
        await interaction.response.send_message(
            f"{member.mention} has no warnings.",
            ephemeral=True,
        )
        return

    lines = []

    for index, row in enumerate(rows, start=1):
        moderator = guild.get_member(row["moderator_id"])
        moderator_name = moderator.display_name if moderator else "Unknown moderator"
        lines.append(
            f"**{index}.** {row['reason']} — {moderator_name}"
        )

    embed = discord.Embed(
        title=f"Warnings for {member}",
        description="\n".join(lines)[:4096],
        color=discord.Color.orange(),
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Bot process
# ---------------------------------------------------------------------------

async def shutdown() -> None:
    await db.close()


def run() -> None:
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
    finally:
        try:
            asyncio.run(shutdown())
        except RuntimeError:
            pass


if __name__ == "__main__":
    run()
