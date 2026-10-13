import discord
from discord import app_commands
from discord.ext import commands

from src.core.views.modals import check_is_mod

# Placeholders supported in templates: {user} mention, {username} plain name, {server} guild name
def _render_template(template: str, member: discord.Member) -> str:
    return (template
            .replace("{user}", member.mention)
            .replace("{username}", member.name)
            .replace("{server}", member.guild.name))


def setup_welcome_commands(bot: commands.Bot):
    welcome_group = app_commands.Group(name="welcome", description="Configure the message sent when someone joins.")
    goodbye_group = app_commands.Group(name="goodbye", description="Configure the message sent when someone leaves.")

    @welcome_group.command(name="set", description="Set the welcome message and channel.")
    @app_commands.describe(channel="Where to post the welcome message.", message="Supports {user}, {username}, {server} placeholders.")
    async def welcome_set(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets
        await asyncio.to_thread(sheets.db_set_welcome, interaction.guild.id, channel.id, message)
        await interaction.followup.send(f"✅ **Welcome messages enabled** in {channel.mention}.\nPreview: {_render_template(message, interaction.user)}", ephemeral=True)

    @welcome_group.command(name="disable", description="Turn off welcome messages.")
    async def welcome_disable(interaction: discord.Interaction):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets
        await asyncio.to_thread(sheets.db_disable_welcome, interaction.guild.id)
        await interaction.followup.send("🔕 **Welcome messages disabled.**", ephemeral=True)

    @goodbye_group.command(name="set", description="Set the goodbye message and channel.")
    @app_commands.describe(channel="Where to post the goodbye message.", message="Supports {user}, {username}, {server} placeholders.")
    async def goodbye_set(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets
        await asyncio.to_thread(sheets.db_set_goodbye, interaction.guild.id, channel.id, message)
        await interaction.followup.send(f"✅ **Goodbye messages enabled** in {channel.mention}.\nPreview: {_render_template(message, interaction.user)}", ephemeral=True)

    @goodbye_group.command(name="disable", description="Turn off goodbye messages.")
    async def goodbye_disable(interaction: discord.Interaction):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets
        await asyncio.to_thread(sheets.db_disable_goodbye, interaction.guild.id)
        await interaction.followup.send("🔕 **Goodbye messages disabled.**", ephemeral=True)

    bot.tree.add_command(welcome_group)
    bot.tree.add_command(goodbye_group)

    async def on_member_join(member: discord.Member):
        if member.bot:
            return
        import asyncio
        import src.core.database.sheets as sheets
        config = await asyncio.to_thread(sheets.db_get_guild_config, member.guild.id)
        if not config or not config.get("welcome_channel_id"):
            return
        channel = member.guild.get_channel(int(config["welcome_channel_id"]))
        if not channel:
            return
        try:
            await channel.send(_render_template(config["welcome_message"], member))
        except discord.HTTPException as e:
            print(f"⚠️ Welcome message failed to send: {e}")

    async def on_member_remove(member: discord.Member):
        if member.bot:
            return
        import asyncio
        import src.core.database.sheets as sheets
        config = await asyncio.to_thread(sheets.db_get_guild_config, member.guild.id)
        if not config or not config.get("goodbye_channel_id"):
            return
        channel = member.guild.get_channel(int(config["goodbye_channel_id"]))
        if not channel:
            return
        try:
            await channel.send(_render_template(config["goodbye_message"], member))
        except discord.HTTPException as e:
            print(f"⚠️ Goodbye message failed to send: {e}")

    bot.add_listener(on_member_join, name="on_member_join")
    bot.add_listener(on_member_remove, name="on_member_remove")
