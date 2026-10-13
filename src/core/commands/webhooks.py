import base64
import json
from urllib.parse import quote

import discord
from discord import app_commands
from discord.ext import commands

from src.core.views.modals import check_is_mod


def _build_discohook_link(webhook_url: str) -> str:
    """Builds a link that opens discohook.app with this webhook already
    selected as the send target, using Discohook's own documented `data`
    query parameter (see https://docs.discohook.app/doc-608486) — so the
    user never has to copy/paste the webhook URL into Discohook by hand."""
    query_data = {
        "version": "d2",
        "messages": [{"data": {"content": ""}}],
        "targets": [{"type": 1, "url": webhook_url}],
    }
    encoded = base64.b64encode(json.dumps(query_data).encode("utf-8")).decode("ascii")
    return f"https://discohook.app/?data={quote(encoded, safe='')}"


def _discohook_button_view(webhook_url: str) -> discord.ui.View:
    view = discord.ui.View()
    view.add_item(discord.ui.Button(
        label="🪝 Open in Discohook", style=discord.ButtonStyle.link, url=_build_discohook_link(webhook_url)
    ))
    return view


def setup_webhook_commands(bot: commands.Bot):
    group = app_commands.Group(name="webhook", description="Create and manage channel webhooks.")

    @group.command(name="create", description="Create a new webhook in a channel.")
    @app_commands.describe(name="A friendly name to refer to this webhook by later.", channel="Where the webhook should post. Defaults to this channel.")
    async def create_cmd(interaction: discord.Interaction, name: str, channel: discord.TextChannel = None):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can manage webhooks.", ephemeral=True)

        target_channel = channel or interaction.channel
        await interaction.response.defer(ephemeral=True)

        import asyncio
        import src.core.database.sheets as sheets

        existing = await asyncio.to_thread(sheets.db_find_webhook, interaction.guild.id, name)
        if existing:
            return await interaction.followup.send(f"❌ A webhook named **{name}** already exists in this server. Pick a different name.", ephemeral=True)

        try:
            webhook = await target_channel.create_webhook(name=name, reason=f"Created by {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send("❌ I don't have permission to manage webhooks in that channel.", ephemeral=True)
        except discord.HTTPException as e:
            return await interaction.followup.send(f"❌ Failed to create webhook: {e}", ephemeral=True)

        await asyncio.to_thread(sheets.db_save_webhook, interaction.guild.id, name, webhook.id, webhook.url, target_channel.id, interaction.user.id)

        embed = discord.Embed(title="✅ Webhook Created", color=0x2ECC71)
        embed.add_field(name="Name", value=name, inline=True)
        embed.add_field(name="Channel", value=target_channel.mention, inline=True)
        embed.set_footer(text="Use /webhook info to retrieve the URL later.")
        await interaction.followup.send(embed=embed, view=_discohook_button_view(webhook.url), ephemeral=True)

    @group.command(name="delete", description="Delete one of this server's webhooks.")
    @app_commands.describe(name="The name of the webhook to delete.")
    async def delete_cmd(interaction: discord.Interaction, name: str):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can manage webhooks.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets

        record = await asyncio.to_thread(sheets.db_find_webhook, interaction.guild.id, name)
        if not record:
            return await interaction.followup.send(f"❌ No webhook named **{name}** was found.", ephemeral=True)

        try:
            discord_webhook = await bot.fetch_webhook(int(record["webhook_id"]))
            await discord_webhook.delete(reason=f"Deleted by {interaction.user}")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass  # already gone on Discord's side, or we lack perms — still clean up our record

        await asyncio.to_thread(sheets.db_delete_webhook_record, interaction.guild.id, name)
        await interaction.followup.send(f"🗑️ **Deleted:** Webhook **{name}** has been removed.", ephemeral=True)

    @group.command(name="list", description="List every webhook registered in this server.")
    async def list_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets

        rows = await asyncio.to_thread(sheets.db_list_webhooks, interaction.guild.id)
        if not rows:
            return await interaction.followup.send("📭 No webhooks are registered in this server yet.", ephemeral=True)

        embed = discord.Embed(title="🪝 Registered Webhooks", color=0x3498DB)
        for row in rows[:25]:
            embed.add_field(name=row["name"], value=f"Channel: <#{row['channel_id']}> • Owner: <@{row['owner_discord_id']}>", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(name="info", description="Get the URL and details for a registered webhook.")
    @app_commands.describe(name="The name of the webhook.")
    async def info_cmd(interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets

        record = await asyncio.to_thread(sheets.db_find_webhook, interaction.guild.id, name)
        if not record:
            return await interaction.followup.send(f"❌ No webhook named **{name}** was found.", ephemeral=True)

        is_owner = str(interaction.user.id) == record["owner_discord_id"]
        is_mod = check_is_mod(interaction.user)

        embed = discord.Embed(title=f"🪝 Webhook: {record['name']}", color=0x3498DB)
        embed.add_field(name="Channel", value=f"<#{record['channel_id']}>", inline=True)
        embed.add_field(name="Owner", value=f"<@{record['owner_discord_id']}>", inline=True)
        view = None
        if is_owner or is_mod:
            embed.add_field(name="URL", value=f"||{record['webhook_url']}||", inline=False)
            view = _discohook_button_view(record["webhook_url"])
        else:
            embed.add_field(name="URL", value="🔒 Only the owner or a moderator can view this.", inline=False)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    bot.tree.add_command(group)
