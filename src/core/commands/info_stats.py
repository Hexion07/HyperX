import discord
from discord import app_commands
from discord.ext import commands

from src.core.views.modals import check_is_mod


def setup_info_stats_commands(bot: commands.Bot):
    info_stats_group = app_commands.Group(
        name="infostats",
        description="Configure the Info & Stats channel (enrollment / leave / enroller-transfer announcements)."
    )

    @info_stats_group.command(name="set", description="Set the channel used for enrollment/leave/enroller-transfer announcements.")
    @app_commands.describe(channel="Where to post Info & Stats announcements.")
    async def info_stats_set(interaction: discord.Interaction, channel: discord.TextChannel):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets
        await asyncio.to_thread(sheets.db_set_info_stats_channel, interaction.guild.id, channel.id)
        await interaction.followup.send(
            f"✅ **Info & Stats channel set** to {channel.mention}.\n"
            f"New enrollments, roster leaves, and Enroller transfers will be announced there.",
            ephemeral=True
        )

    @info_stats_group.command(name="disable", description="Turn off Info & Stats announcements.")
    async def info_stats_disable(interaction: discord.Interaction):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure this.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets
        await asyncio.to_thread(sheets.db_disable_info_stats_channel, interaction.guild.id)
        await interaction.followup.send("🔕 **Info & Stats announcements disabled.**", ephemeral=True)

    bot.tree.add_command(info_stats_group)
