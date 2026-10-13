import re
import discord
from discord import app_commands
from discord.ext import commands

HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


class EmbedBuilderModal(discord.ui.Modal, title="🎨 Embed Builder"):
    embed_title = discord.ui.TextInput(label="Title", required=False, max_length=256)
    description = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, required=False, max_length=4000)
    color_hex = discord.ui.TextInput(label="Color (hex, e.g. 2ECC71)", required=False, max_length=7, placeholder="2ECC71")
    image_url = discord.ui.TextInput(label="Image URL", required=False)
    footer = discord.ui.TextInput(label="Footer Text", required=False, max_length=2048)

    def __init__(self, target_channel: discord.TextChannel):
        super().__init__()
        self.target_channel = target_channel

    async def on_submit(self, interaction: discord.Interaction):
        if not self.embed_title.value and not self.description.value:
            return await interaction.response.send_message("❌ An embed needs at least a title or a description.", ephemeral=True)

        color = 0x2ECC71
        if self.color_hex.value.strip():
            if not HEX_RE.match(self.color_hex.value.strip()):
                return await interaction.response.send_message("❌ Invalid hex color. Example: `2ECC71`.", ephemeral=True)
            color = int(self.color_hex.value.strip().lstrip("#"), 16)

        embed = discord.Embed(
            title=self.embed_title.value or None,
            description=self.description.value or None,
            color=color
        )
        if self.image_url.value.strip():
            embed.set_image(url=self.image_url.value.strip())
        if self.footer.value.strip():
            embed.set_footer(text=self.footer.value.strip())

        try:
            await self.target_channel.send(embed=embed)
        except discord.Forbidden:
            return await interaction.response.send_message(f"❌ I don't have permission to send messages in {self.target_channel.mention}.", ephemeral=True)

        await interaction.response.send_message(f"✅ Embed sent to {self.target_channel.mention}!", ephemeral=True)


def setup_embed_builder(bot: commands.Bot):
    @bot.tree.command(name="embed", description="Design and send a custom embed.")
    @app_commands.describe(channel="Where to send it. Defaults to this channel.")
    async def embed_cmd(interaction: discord.Interaction, channel: discord.TextChannel = None):
        target = channel or interaction.channel
        await interaction.response.send_modal(EmbedBuilderModal(target))
