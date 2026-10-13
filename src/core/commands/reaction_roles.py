import discord
from discord import app_commands
from discord.ext import commands

from src.core.views.modals import check_is_mod

# In-memory cache mirroring the "reaction_roles" sheet tab, keyed by
# (guild_id, message_id, emoji) -> role_id. Warmed at startup and kept in
# sync on every add/remove so a reaction click doesn't need a sheet read.
_RR_CACHE: dict[tuple[str, str, str], str] = {}


def _cache_key(guild_id, message_id, emoji) -> tuple[str, str, str]:
    return (str(guild_id), str(message_id), str(emoji))


async def warm_reaction_role_cache():
    """Called once from on_ready to preload every configured mapping."""
    import asyncio
    import src.core.database.sheets as sheets
    try:
        rows = await asyncio.to_thread(sheets.db_list_reaction_roles)
        for row in rows:
            _RR_CACHE[_cache_key(row["guild_id"], row["message_id"], row["emoji"])] = row["role_id"]
        print(f"✅ Reaction Roles: warmed cache with {len(rows)} mapping(s).")
    except Exception as e:
        print(f"⚠️ Reaction Roles: failed to warm cache: {e}")


def setup_reaction_roles(bot: commands.Bot):
    group = app_commands.Group(name="reactionrole", description="Configure emoji-triggered role assignment.")

    @group.command(name="add", description="Attach a role to an emoji reaction on a message.")
    @app_commands.describe(
        message_id="The ID of the message to react to (right-click it -> Copy Message ID).",
        emoji="The emoji people will react with.",
        role="The role to grant/revoke on reaction."
    )
    async def add_cmd(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure reaction roles.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        target_message = None
        target_channel = None
        for channel in interaction.guild.text_channels:
            try:
                target_message = await channel.fetch_message(int(message_id))
                target_channel = channel
                break
            except (discord.NotFound, discord.Forbidden, ValueError):
                continue

        if not target_message:
            return await interaction.followup.send("❌ **Not Found:** Couldn't locate that message ID in any channel I can see.", ephemeral=True)

        try:
            await target_message.add_reaction(emoji)
        except discord.HTTPException:
            return await interaction.followup.send("❌ **Invalid Emoji:** I couldn't react with that emoji (custom emoji must be from a server I'm in).", ephemeral=True)

        import asyncio
        import src.core.database.sheets as sheets
        await asyncio.to_thread(sheets.db_add_reaction_role, interaction.guild.id, target_message.id, target_channel.id, emoji, role.id, role.name)
        _RR_CACHE[_cache_key(interaction.guild.id, target_message.id, emoji)] = str(role.id)

        await interaction.followup.send(f"✅ **Configured!** Reacting with {emoji} on [that message]({target_message.jump_url}) now grants **{role.name}**.", ephemeral=True)

    @group.command(name="remove", description="Remove a reaction-role mapping from a message.")
    @app_commands.describe(message_id="The ID of the message the mapping is on.", emoji="The emoji to unbind.")
    async def remove_cmd(interaction: discord.Interaction, message_id: str, emoji: str):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure reaction roles.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets
        removed = await asyncio.to_thread(sheets.db_remove_reaction_role, interaction.guild.id, message_id, emoji)
        _RR_CACHE.pop(_cache_key(interaction.guild.id, message_id, emoji), None)

        if removed:
            await interaction.followup.send("🗑️ **Removed.** That reaction no longer grants a role.", ephemeral=True)
        else:
            await interaction.followup.send("❌ **Not Found:** No mapping exists for that message/emoji combo.", ephemeral=True)

    @group.command(name="list", description="List every reaction-role mapping configured in this server.")
    async def list_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets
        rows = await asyncio.to_thread(sheets.db_list_reaction_roles, interaction.guild.id)

        if not rows:
            return await interaction.followup.send("📭 No reaction roles are configured in this server yet.", ephemeral=True)

        embed = discord.Embed(title="🎭 Reaction Roles", color=0x9B59B6)
        for row in rows[:25]:
            embed.add_field(
                name=f"{row['emoji']} → {row['role_name'] or row['role_id']}",
                value=f"Message: `{row['message_id']}` • Channel: <#{row['channel_id']}>",
                inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    bot.tree.add_command(group)

    async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
        if payload.member is None or payload.member.bot or payload.guild_id is None:
            return
        emoji = str(payload.emoji)
        role_id = _RR_CACHE.get(_cache_key(payload.guild_id, payload.message_id, emoji))
        if not role_id:
            return
        guild = bot.get_guild(payload.guild_id)
        if not guild:
            return
        role = guild.get_role(int(role_id))
        if not role:
            return
        try:
            await payload.member.add_roles(role, reason="Reaction role")
        except discord.HTTPException as e:
            print(f"⚠️ Reaction Roles: failed to add role: {e}")

    async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        emoji = str(payload.emoji)
        role_id = _RR_CACHE.get(_cache_key(payload.guild_id, payload.message_id, emoji))
        if not role_id:
            return
        guild = bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        role = guild.get_role(int(role_id))
        if not member or member.bot or not role:
            return
        try:
            await member.remove_roles(role, reason="Reaction role removed")
        except discord.HTTPException as e:
            print(f"⚠️ Reaction Roles: failed to remove role: {e}")

    bot.add_listener(on_raw_reaction_add, name="on_raw_reaction_add")
    bot.add_listener(on_raw_reaction_remove, name="on_raw_reaction_remove")
