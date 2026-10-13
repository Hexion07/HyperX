import re
import time
import discord
from discord import app_commands
from discord.ext import commands

from src.core.views.modals import check_is_mod

HEX_RE = re.compile(r"^[0-9a-fA-F]{6}$")

# ==================== IN-MEMORY RUNTIME STATE ====================
# None of this is persisted to Sheets — writing on every single message would
# be slow and rate-limit-prone. It's rebuilt naturally as messages come in;
# worst case after a restart is one extra "grace period" before a channel's
# note bumps itself back to the bottom again.
_MSG_COUNTS: dict[int, int] = {}          # channel_id -> messages seen since last repost
_LAST_STICKY: dict[int, int] = {}         # channel_id -> message_id of the currently-posted note
_LAST_REPOST: dict[int, float] = {}       # channel_id -> monotonic() time of the last repost
_MIN_REPOST_INTERVAL_SECONDS = 3.0        # burst guard so a flurry of messages can't spam-repost

# guild_id -> (timestamp, {channel_id: note}) — short-lived so the on_message
# listener isn't hitting Sheets on every single message in a busy server.
_CONFIG_CACHE: dict[str, tuple] = {}
_CACHE_TTL_SECONDS = 10.0


async def _get_guild_sticky_map(guild_id: str, force_refresh: bool = False) -> dict:
    now = time.monotonic()
    cached = _CONFIG_CACHE.get(guild_id)
    if not force_refresh and cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    import asyncio
    import src.core.database.sheets as sheets

    notes = await asyncio.to_thread(sheets.db_list_sticky_notes, guild_id)
    mapping = {n["channel_id"]: n for n in notes}
    _CONFIG_CACHE[guild_id] = (now, mapping)
    return mapping


def _invalidate_cache(guild_id: str):
    _CONFIG_CACHE.pop(guild_id, None)


def _parse_color(hex_str: str) -> int:
    cleaned = (hex_str or "").strip().lstrip("#")
    if HEX_RE.match(cleaned):
        return int(cleaned, 16)
    return 0x2ECC71


def _build_sticky_embed(note: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"📌 {note['title']}" if note.get("title") else "📌 Channel Requirements",
        description=note["content"],
        color=_parse_color(note.get("color")),
    )
    embed.set_footer(text="Pinned requirements • auto-bumped to stay at the bottom of this channel")
    return embed


async def _post_or_bump_sticky(channel: discord.abc.Messageable, note: dict) -> discord.Message:
    """Deletes the previously-tracked sticky message in this channel (if any)
    and sends a fresh one so it lands back at the bottom. Resets this
    channel's message counter and repost cooldown either way."""
    old_id = _LAST_STICKY.get(channel.id)
    if old_id:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass  # already gone, or we can't touch it — just post the new one anyway

    new_msg = await channel.send(embed=_build_sticky_embed(note))
    _LAST_STICKY[channel.id] = new_msg.id
    _MSG_COUNTS[channel.id] = 0
    _LAST_REPOST[channel.id] = time.monotonic()
    return new_msg


async def _remove_sticky_message(channel: discord.abc.Messageable, channel_id: int):
    old_id = _LAST_STICKY.pop(channel_id, None)
    if old_id and channel is not None:
        try:
            old_msg = await channel.fetch_message(old_id)
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    _MSG_COUNTS.pop(channel_id, None)
    _LAST_REPOST.pop(channel_id, None)


# ==================== CREATE / EDIT MODAL ====================
class _StickyModal(discord.ui.Modal, title="📌 Sticky Note — Channel Requirements"):
    note_title = discord.ui.TextInput(
        label="Title", required=False, max_length=256, placeholder="e.g. Registration Requirements",
    )
    content = discord.ui.TextInput(
        label="Requirements / content", style=discord.TextStyle.paragraph, required=True, max_length=4000,
        placeholder="e.g. Every entry must include your IGN, UID, and team code...",
    )
    color_hex = discord.ui.TextInput(
        label="Color (hex, e.g. 2ECC71)", required=False, max_length=7, placeholder="2ECC71",
    )
    repost_after = discord.ui.TextInput(
        label="Repost after how many messages?", required=False, max_length=3, placeholder="5",
    )

    def __init__(self, channel: discord.TextChannel, existing: dict | None, on_saved):
        super().__init__()
        self.channel = channel
        self._on_saved = on_saved
        if existing:
            self.note_title.default = existing.get("title") or None
            self.content.default = existing.get("content") or None
            self.color_hex.default = existing.get("color") or None
            self.repost_after.default = str(existing.get("repost_after", 5))
        else:
            self.repost_after.default = "5"

    async def on_submit(self, interaction: discord.Interaction):
        color_value = self.color_hex.value.strip().lstrip("#")
        if color_value and not HEX_RE.match(color_value):
            return await interaction.response.send_message("❌ Invalid hex color. Example: `2ECC71`.", ephemeral=True)

        repost_value = self.repost_after.value.strip() or "5"
        if not repost_value.isdigit() or int(repost_value) < 1:
            return await interaction.response.send_message(
                "❌ Repost threshold must be a whole number of 1 or more.", ephemeral=True
            )

        # Defer FIRST — the Sheets write plus the repost send/delete below can
        # exceed Discord's 3s interaction window, which would otherwise
        # invalidate the token before we get a chance to respond at all.
        await interaction.response.defer(ephemeral=True)

        note = {
            "channel_id": str(self.channel.id),
            "title": self.note_title.value.strip(),
            "content": str(self.content.value),
            "color": color_value or "2ECC71",
            "repost_after": int(repost_value),
            "enabled": True,
        }
        await self._on_saved(interaction, note)


# ==================== EMBEDS ====================
def _build_dashboard_embed(guild_name: str, notes_map: dict, selected_channel_id: str | None) -> discord.Embed:
    embed = discord.Embed(
        title="📌 Sticky Notes Dashboard", color=0x9B59B6,
        description=(
            f"Server: **{guild_name}**\n"
            f"Pin a requirements message to any channel — it automatically deletes and reposts itself "
            f"so it stays at the bottom, once a configurable number of new messages have come in.\n\n"
            f"Pick a channel below to create, edit, toggle, or delete its sticky note."
        ),
    )

    configured = list(notes_map.values())
    if configured:
        lines = []
        for note in configured[:10]:
            status = "✅" if note["enabled"] else "⏸️"
            title = note["title"] or "Untitled"
            lines.append(f"{status} <#{note['channel_id']}> — **{title}** (every {note['repost_after']} msgs)")
        if len(configured) > 10:
            lines.append(f"_…and {len(configured) - 10} more_")
        embed.add_field(name=f"Configured Channels ({len(configured)})", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Configured Channels (0)", value="_None yet — select a channel below to add one._", inline=False)

    if selected_channel_id:
        note = notes_map.get(selected_channel_id)
        if note:
            preview = note["content"]
            if len(preview) > 500:
                preview = preview[:500] + "…"
            embed.add_field(
                name=f"Selected: <#{selected_channel_id}>",
                value=(
                    f"**{note['title'] or 'Untitled'}**\n"
                    f"Status: {'✅ Enabled' if note['enabled'] else '⏸️ Disabled'} • "
                    f"Reposts every {note['repost_after']} messages\n\n{preview}"
                ),
                inline=False,
            )
        else:
            embed.add_field(
                name=f"Selected: <#{selected_channel_id}>",
                value="_No sticky note here yet — click **Create Note** below._",
                inline=False,
            )

    embed.set_footer(text="Command-line equivalents: /sticky set, /sticky list, /sticky remove, /sticky post")
    return embed


def _channel_list_embed(guild_name: str, notes: list) -> discord.Embed:
    embed = discord.Embed(
        title="📌 Sticky-Configured Channels", color=0x9B59B6,
        description=f"Server: **{guild_name}**",
    )
    if not notes:
        embed.add_field(name="No channels configured yet", value="Use `/sticky set` or `/sticky dashboard` to add one.", inline=False)
        return embed

    for note in notes[:25]:
        status = "✅ Enabled" if note["enabled"] else "⏸️ Disabled"
        preview = (note["content"] or "").strip()
        if len(preview) > 150:
            preview = preview[:150] + "…"
        embed.add_field(
            name=f"<#{note['channel_id']}> — {note['title'] or 'Untitled'}",
            value=f"{status} • every {note['repost_after']} messages\n{preview}",
            inline=False,
        )
    return embed


# ==================== DASHBOARD VIEW ====================
class _StickyDashboardView(discord.ui.View):
    def __init__(self, guild_id: str, guild_name: str, user_id: int, notes_map: dict,
                 selected_channel_id: str | None = None):
        super().__init__(timeout=300.0)
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.user_id = user_id
        self.notes_map = notes_map
        self.selected_channel_id = selected_channel_id
        self._render_items()

    def _status_embed(self) -> discord.Embed:
        return _build_dashboard_embed(self.guild_name, self.notes_map, self.selected_channel_id)

    def _not_yours(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id != self.user_id

    def _render_items(self):
        self.clear_items()

        select = discord.ui.ChannelSelect(
            placeholder="Select a channel to configure...",
            channel_types=[discord.ChannelType.text],
            row=0,
        )

        async def select_callback(interaction: discord.Interaction):
            if self._not_yours(interaction):
                return await interaction.response.send_message("❌ This dashboard isn't for you.", ephemeral=True)
            self.selected_channel_id = str(select.values[0].id)
            self._render_items()
            await interaction.response.edit_message(embed=self._status_embed(), view=self)

        select.callback = select_callback
        self.add_item(select)

        note = self.notes_map.get(self.selected_channel_id) if self.selected_channel_id else None
        no_selection = self.selected_channel_id is None

        edit_button = discord.ui.Button(
            label="✏️ Edit Note" if note else "➕ Create Note",
            style=discord.ButtonStyle.primary, row=1, disabled=no_selection,
        )
        edit_button.callback = self._edit_callback
        self.add_item(edit_button)

        toggle_button = discord.ui.Button(
            label="⏸️ Disable" if (note and note["enabled"]) else "▶️ Enable",
            style=discord.ButtonStyle.success if (note and note["enabled"]) else discord.ButtonStyle.secondary,
            row=1, disabled=note is None,
        )
        toggle_button.callback = self._toggle_callback
        self.add_item(toggle_button)

        delete_button = discord.ui.Button(label="🗑️ Delete", style=discord.ButtonStyle.danger, row=1, disabled=note is None)
        delete_button.callback = self._delete_callback
        self.add_item(delete_button)

    async def _edit_callback(self, interaction: discord.Interaction):
        if self._not_yours(interaction):
            return await interaction.response.send_message("❌ This dashboard isn't for you.", ephemeral=True)

        channel = interaction.guild.get_channel(int(self.selected_channel_id))
        if channel is None:
            return await interaction.response.send_message("❌ That channel no longer exists.", ephemeral=True)

        existing = self.notes_map.get(self.selected_channel_id)

        async def on_saved(modal_interaction: discord.Interaction, note: dict):
            import asyncio
            import src.core.database.sheets as sheets
            await asyncio.to_thread(
                sheets.db_set_sticky_note, self.guild_id, note["channel_id"],
                title=note["title"], content=note["content"], color=note["color"],
                repost_after=note["repost_after"], enabled=True,
            )
            _invalidate_cache(self.guild_id)
            self.notes_map[note["channel_id"]] = note

            try:
                await _post_or_bump_sticky(channel, note)
            except discord.Forbidden:
                await modal_interaction.followup.send(
                    f"✅ Saved, but I don't have permission to post in {channel.mention}.", ephemeral=True
                )

            self._render_items()
            await modal_interaction.edit_original_response(embed=self._status_embed(), view=self)

        await interaction.response.send_modal(_StickyModal(channel, existing, on_saved))

    async def _toggle_callback(self, interaction: discord.Interaction):
        if self._not_yours(interaction):
            return await interaction.response.send_message("❌ This dashboard isn't for you.", ephemeral=True)

        note = self.notes_map.get(self.selected_channel_id)
        if not note:
            return await interaction.response.send_message("❌ Nothing configured for that channel.", ephemeral=True)

        await interaction.response.defer()

        import asyncio
        import src.core.database.sheets as sheets
        new_enabled = not note["enabled"]
        await asyncio.to_thread(sheets.db_set_sticky_note, self.guild_id, self.selected_channel_id, enabled=new_enabled)
        _invalidate_cache(self.guild_id)
        note["enabled"] = new_enabled

        channel = interaction.guild.get_channel(int(self.selected_channel_id))
        if new_enabled and channel:
            try:
                await _post_or_bump_sticky(channel, note)
            except discord.HTTPException:
                pass
        elif not new_enabled:
            await _remove_sticky_message(channel, int(self.selected_channel_id))

        self._render_items()
        await interaction.edit_original_response(embed=self._status_embed(), view=self)

    async def _delete_callback(self, interaction: discord.Interaction):
        if self._not_yours(interaction):
            return await interaction.response.send_message("❌ This dashboard isn't for you.", ephemeral=True)

        await interaction.response.defer()
        channel_id = self.selected_channel_id

        import asyncio
        import src.core.database.sheets as sheets
        await asyncio.to_thread(sheets.db_delete_sticky_note, self.guild_id, channel_id)
        _invalidate_cache(self.guild_id)
        self.notes_map.pop(channel_id, None)

        channel = interaction.guild.get_channel(int(channel_id))
        await _remove_sticky_message(channel, int(channel_id))

        self._render_items()
        await interaction.edit_original_response(embed=self._status_embed(), view=self)


# ==================== SETUP ====================
def setup_sticky_notes(bot: commands.Bot):
    group = app_commands.Group(name="sticky", description="Configure per-channel sticky requirement notes.")

    # ---------- /sticky dashboard ----------
    @group.command(name="dashboard", description="Open the sticky notes control panel for this server.")
    @app_commands.guild_only()
    async def dashboard_cmd(interaction: discord.Interaction):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure sticky notes.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild.id)
        notes_map = await _get_guild_sticky_map(guild_id, force_refresh=True)
        view = _StickyDashboardView(guild_id, interaction.guild.name, interaction.user.id, notes_map)
        await interaction.followup.send(embed=view._status_embed(), view=view, ephemeral=True)

    # ---------- /sticky set ----------
    @group.command(name="set", description="Create or edit the sticky requirements note for a channel.")
    @app_commands.guild_only()
    @app_commands.describe(channel="The channel to pin a sticky note to.")
    async def set_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure sticky notes.", ephemeral=True)

        import asyncio
        import src.core.database.sheets as sheets
        guild_id = str(interaction.guild.id)
        existing = await asyncio.to_thread(sheets.db_get_sticky_note, guild_id, str(channel.id))

        async def on_saved(modal_interaction: discord.Interaction, note: dict):
            await asyncio.to_thread(
                sheets.db_set_sticky_note, guild_id, note["channel_id"],
                title=note["title"], content=note["content"], color=note["color"],
                repost_after=note["repost_after"], enabled=True,
            )
            _invalidate_cache(guild_id)
            try:
                await _post_or_bump_sticky(channel, note)
                await modal_interaction.followup.send(f"✅ Sticky note saved and posted in {channel.mention}.", ephemeral=True)
            except discord.Forbidden:
                await modal_interaction.followup.send(
                    f"✅ Saved, but I don't have permission to post in {channel.mention}.", ephemeral=True
                )

        await interaction.response.send_modal(_StickyModal(channel, existing, on_saved))

    # ---------- /sticky remove ----------
    @group.command(name="remove", description="Delete a channel's sticky note.")
    @app_commands.guild_only()
    @app_commands.describe(channel="The channel to remove the sticky note from.")
    async def remove_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure sticky notes.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets
        guild_id = str(interaction.guild.id)
        existing = await asyncio.to_thread(sheets.db_get_sticky_note, guild_id, str(channel.id))
        if not existing:
            return await interaction.followup.send(f"❌ {channel.mention} doesn't have a sticky note.", ephemeral=True)

        await asyncio.to_thread(sheets.db_delete_sticky_note, guild_id, str(channel.id))
        _invalidate_cache(guild_id)
        await _remove_sticky_message(channel, channel.id)

        await interaction.followup.send(f"🗑️ Removed the sticky note from {channel.mention}.", ephemeral=True)

    # ---------- /sticky list ----------
    @group.command(name="list", description="List every channel with a sticky note in this server.")
    @app_commands.guild_only()
    async def list_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets
        notes = await asyncio.to_thread(sheets.db_list_sticky_notes, str(interaction.guild.id))
        await interaction.followup.send(embed=_channel_list_embed(interaction.guild.name, notes), ephemeral=True)

    # ---------- /sticky post ----------
    @group.command(name="post", description="Immediately bump a channel's sticky note back to the bottom.")
    @app_commands.guild_only()
    @app_commands.describe(channel="The channel whose sticky note should be bumped now.")
    async def post_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure sticky notes.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets
        note = await asyncio.to_thread(sheets.db_get_sticky_note, str(interaction.guild.id), str(channel.id))
        if not note:
            return await interaction.followup.send(f"❌ {channel.mention} doesn't have a sticky note configured.", ephemeral=True)
        if not note["enabled"]:
            return await interaction.followup.send(
                f"❌ {channel.mention}'s sticky note is currently disabled. Enable it from `/sticky dashboard` first.", ephemeral=True
            )

        try:
            await _post_or_bump_sticky(channel, note)
        except discord.Forbidden:
            return await interaction.followup.send(f"❌ I don't have permission to post in {channel.mention}.", ephemeral=True)
        await interaction.followup.send(f"📌 Bumped the sticky note in {channel.mention}.", ephemeral=True)

    bot.tree.add_command(group)

    # ==================== AUTO-REPOST LISTENER ====================
    async def on_message(message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = str(message.guild.id)
        mapping = await _get_guild_sticky_map(guild_id)
        note = mapping.get(str(message.channel.id))
        if not note or not note["enabled"]:
            return

        channel_id = message.channel.id
        count = _MSG_COUNTS.get(channel_id, 0) + 1
        if count < note["repost_after"]:
            _MSG_COUNTS[channel_id] = count
            return

        # Burst guard: if a flurry of messages all cross the threshold back
        # to back, only actually bump the sticky once per cooldown window —
        # still counts the message, so it bumps again as soon as the
        # cooldown clears rather than getting stuck.
        now = time.monotonic()
        if now - _LAST_REPOST.get(channel_id, 0.0) < _MIN_REPOST_INTERVAL_SECONDS:
            _MSG_COUNTS[channel_id] = count
            return

        try:
            await _post_or_bump_sticky(message.channel, note)
        except discord.HTTPException as e:
            print(f"⚠️ Failed to repost sticky note in #{message.channel}: {e}")

    bot.add_listener(on_message, name="on_message")
