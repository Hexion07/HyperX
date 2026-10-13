import logging
import discord
import aiohttp
from discord.ext import commands

# Suppress raw internal gateway connection logs from displaying in the terminal console
discord_logger = logging.getLogger('discord.client')
discord_logger.setLevel(logging.CRITICAL)

async def safe_interaction_respond(interaction: discord.Interaction, fallback_text: str = "❌ Something went wrong, please try again later."):
    try:
        if interaction.client.is_closed():
            return
            
        if not interaction.response.is_done():
            await interaction.response.send_message(fallback_text, ephemeral=True)
        else:
            await interaction.followup.send(fallback_text, ephemeral=True)
            
    except (discord.HTTPException, aiohttp.ClientError):
        pass

def setup_global_error_handler(bot: commands.Bot):
    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        if isinstance(error.__cause__, (discord.HTTPException, aiohttp.ClientError, ConnectionError)):
            await safe_interaction_respond(interaction, "⚠️ Bot API has been disconnected. Please try again later.")
        else:
            await safe_interaction_respond(interaction, "❌ Something went wrong, please try again later.")