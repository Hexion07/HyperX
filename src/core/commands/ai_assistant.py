import time
import discord
from discord import app_commands
from discord.ext import commands

from src.core.views.modals import check_is_mod
from src.core.commands.help import CATEGORIES

# ==================== PER-USER COOLDOWN ====================
# A cheap guard against someone spamming @mentions/DMs/`/ask`/channel
# triggers and burning through your Gemini quota/bill. Not persisted —
# resets on restart, which is fine. Shared across every trigger since it's
# keyed purely by user id.
_COOLDOWN_SECONDS = 5
_LAST_USED: dict[int, float] = {}


def _on_cooldown(user_id: int) -> bool:
    now = time.monotonic()
    last = _LAST_USED.get(user_id, 0.0)
    if now - last < _COOLDOWN_SECONDS:
        return True
    _LAST_USED[user_id] = now
    return False


# ==================== CONVERSATION MEMORY (SHORT, IN-MEMORY) ====================
# Keyed by (scope, user_id). Not persisted to the sheet — it's just enough
# context for a natural back-and-forth, not a permanent transcript.
_MAX_TURNS = 6
_CONVO_HISTORY: dict[tuple, list] = {}


# ==================== HARD (KEYWORD) FILTER ====================
# Used only when the active mode for this message is pure FAQ (general off)
# AND Strict Mode is on. A question must contain at least one of these
# (case-insensitive) or it's refused before any Gemini call is made — saves
# cost and guarantees off-topic questions never reach the model at all,
# unlike FAQ mode's own instruction-only steering.
# Keep this list updated as your tournament vocabulary changes.
TOURNAMENT_KEYWORDS = [
    "tournament", "match", "matches", "schedule", "scrim", "scrims",
    "registration", "register", "team", "roster", "slot", "slots",
    "score", "scoring", "point", "points", "standings", "bracket",
    "rule", "rules", "prize", "prize pool", "igl", "bgmi", "pubg",
    "pm", "group stage", "qualifier", "finals", "leaderboard",
    "eliminations", "kill points", "webhook", "reactionrole", "welcome",
    "embed", "command", "bot", "help",
]


def _matches_tournament_keywords(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in TOURNAMENT_KEYWORDS)


def _build_faq_context() -> str:
    """Flattens the same category data /help uses into plain text, so the
    AI's answers about the bot stay consistent with what /help actually says."""
    lines = []
    for cat in CATEGORIES.values():
        lines.append(f"### {cat['label']}")
        for name, value in cat["fields"]:
            lines.append(f"- {name}: {value}")
    lines.append("### AI Assistant")
    lines.append("- @mention the bot, DM it, or use /ask: Talk to the AI assistant directly.")
    lines.append("- In any channel a moderator has configured with /ai channel set, just type — no mention needed.")
    lines.append("- /ai dashboard: **Moderator only.** Toggle the global (mention/DM) modes and instructions.")
    lines.append("- /ai channel set/list/remove: **Moderator only.** Stack any number of channels, each with its own mode, strict filter, and instructions.")
    return "\n".join(lines)


def _chunk_text(text: str, limit: int = 1900):
    """Splits a long reply into Discord-safe (<2000 char) chunks, preferring
    to break on a newline so we don't cut a sentence/code block in half."""
    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:]
    if text:
        chunks.append(text)
    return chunks or [""]


def _build_instruction(faq_enabled: bool, general_enabled: bool, custom_instructions: str = "") -> str:
    identity = (
        "You are the AI assistant built into '𝙎𝘼𝙉𝙏𝙕 HyperX', a Discord bot that runs esports tournament "
        "enrollment. Keep replies concise and conversational, formatted for Discord chat — short paragraphs, "
        "markdown where it genuinely helps, sparing use of emoji. Never invent team codes, stats, or roster "
        "data you weren't actually given."
    )

    if faq_enabled and general_enabled:
        base = (
            f"{identity}\n\nYou may discuss any topic the user brings up. When a question is about this "
            f"tournament bot or the tournament itself, answer using the reference info below rather than "
            f"guessing.\n\n{_build_faq_context()}"
        )
    elif faq_enabled:
        base = (
            f"{identity}\n\nONLY answer questions about this tournament bot and the tournament, using the "
            f"reference info below. If asked about anything unrelated, politely say that's outside what you "
            f"can help with here and suggest asking a moderator.\n\n{_build_faq_context()}"
        )
    else:
        base = f"{identity}\n\nYou can chat about any topic the user brings up."

    custom_instructions = (custom_instructions or "").strip()
    if custom_instructions:
        base += (
            "\n\n### Staff-provided tournament information (treat as ground truth over anything you'd "
            f"otherwise guess):\n{custom_instructions}"
        )
    return base


async def _generate_ai_reply(scope: str, author_id: int, user_text: str,
                              faq_enabled: bool, general_enabled: bool, strict_enabled: bool,
                              custom_instructions: str = "") -> str:
    from src.core.ai.gemini_client import generate_reply

    # Hard filter: only ever applies in pure FAQ mode (general off) with
    # strict on. Runs BEFORE touching the Gemini API — an off-topic question
    # never costs you a request.
    if faq_enabled and not general_enabled and strict_enabled and not _matches_tournament_keywords(user_text):
        return (
            "🔒 This is set to **tournament questions only** and your question doesn't look "
            "tournament-related. Ask a moderator if you need help with something else."
        )

    instruction = _build_instruction(faq_enabled, general_enabled, custom_instructions)

    key = (scope, author_id)
    history = _CONVO_HISTORY.get(key, [])

    reply_text, updated_history = await generate_reply(instruction, user_text, history)
    _CONVO_HISTORY[key] = updated_history[-(_MAX_TURNS * 2):]
    return reply_text


# ==================== INSTRUCTIONS MODAL (shared by global dashboard + per-channel setup) ====================
class _InstructionsModal(discord.ui.Modal, title="AI Instructions"):
    instructions = discord.ui.TextInput(
        label="Tell the AI about your tournament",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. Prize pool, reg deadline, match format, scoring, finals date...",
        max_length=4000,
        required=False,
    )

    def __init__(self, current_text: str, on_saved):
        super().__init__()
        self.instructions.default = current_text or None
        self._on_saved = on_saved

    async def on_submit(self, interaction: discord.Interaction):
        # Defer FIRST — the Sheets write in on_saved can exceed Discord's 3s
        # interaction window (especially with retry/backoff), which would
        # otherwise invalidate the interaction token before we can respond.
        await interaction.response.defer()
        await self._on_saved(interaction, str(self.instructions.value or ""))


# ==================== GLOBAL DASHBOARD (mention / DM fallback behavior) ====================
def _build_status_embed(scope_label: str, faq_enabled: bool, general_enabled: bool, strict_enabled: bool,
                         custom_instructions: str) -> discord.Embed:
    embed = discord.Embed(
        title="🤖 AI Assistant Dashboard", color=0x9B59B6,
        description=(
            f"Scope: **{scope_label}**\n"
            f"_This controls @mentions, DMs, and `/ask` — for channels where the AI should respond to "
            f"every message with no mention needed, use `/ai channel set` instead (any number of channels, "
            f"each independently configured)._"
        ),
    )
    embed.add_field(name="🧭 Tournament FAQ", value="✅ Enabled" if faq_enabled else "⬜ Disabled", inline=True)
    embed.add_field(name="💬 General Chat", value="✅ Enabled" if general_enabled else "⬜ Disabled", inline=True)
    strict_value = "✅ Enabled" if strict_enabled else "⬜ Disabled"
    if general_enabled:
        strict_value += " _(ignored while General Chat is on)_"
    elif not faq_enabled:
        strict_value += " _(ignored while Tournament FAQ is off)_"
    embed.add_field(name="🔒 Strict Filter", value=strict_value, inline=True)

    instructions_preview = custom_instructions.strip() if custom_instructions else ""
    if instructions_preview:
        if len(instructions_preview) > 300:
            instructions_preview = instructions_preview[:300] + "…"
        embed.add_field(name="📝 Custom Tournament Instructions", value=instructions_preview, inline=False)
    else:
        embed.add_field(name="📝 Custom Tournament Instructions", value="_None set yet_", inline=False)

    overall = "🟢 Active" if (faq_enabled or general_enabled) else "🔴 Silent for mentions/DMs/`/ask` (channel-specific setups still work)"
    embed.add_field(name="Overall Status", value=overall, inline=False)
    embed.set_footer(text="Use /ai channel set/list/remove to configure individual channels.")
    return embed


class _AIDashboardView(discord.ui.View):
    def __init__(self, scope: str, faq_enabled: bool, general_enabled: bool, strict_enabled: bool,
                 user_id: int, scope_label: str, custom_instructions: str):
        super().__init__(timeout=300.0)
        self.scope = scope
        self.faq_enabled = faq_enabled
        self.general_enabled = general_enabled
        self.strict_enabled = strict_enabled
        self.user_id = user_id
        self.scope_label = scope_label
        self.custom_instructions = custom_instructions
        self._render_items()

    def _status_embed(self) -> discord.Embed:
        return _build_status_embed(
            self.scope_label, self.faq_enabled, self.general_enabled, self.strict_enabled, self.custom_instructions,
        )

    def _render_items(self):
        self.clear_items()
        self.add_item(self._make_toggle("faq", "🧭 Tournament FAQ", self.faq_enabled, row=0))
        self.add_item(self._make_toggle("general", "💬 General Chat", self.general_enabled, row=0))
        self.add_item(self._make_toggle("strict", "🔒 Strict Filter", self.strict_enabled, row=0))
        self.add_item(self._make_instructions_button(row=0))

    def _make_toggle(self, which: str, label_prefix: str, enabled: bool, row: int) -> discord.ui.Button:
        button = discord.ui.Button(
            label=f"{label_prefix}: {'ON' if enabled else 'OFF'}",
            style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
            row=row,
        )

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("❌ This dashboard isn't for you.", ephemeral=True)

            # Defer FIRST: the Sheets write below can exceed Discord's 3s
            # interaction window (especially with retry/backoff), which
            # would otherwise invalidate the interaction token before we
            # get a chance to respond at all.
            await interaction.response.defer()

            import asyncio
            import src.core.database.sheets as sheets

            if which == "faq":
                self.faq_enabled = not self.faq_enabled
                await asyncio.to_thread(sheets.db_set_ai_config, self.scope, faq_enabled=self.faq_enabled)
            elif which == "general":
                self.general_enabled = not self.general_enabled
                await asyncio.to_thread(sheets.db_set_ai_config, self.scope, general_enabled=self.general_enabled)
            else:
                self.strict_enabled = not self.strict_enabled
                await asyncio.to_thread(sheets.db_set_ai_config, self.scope, strict_enabled=self.strict_enabled)

            self._render_items()
            await interaction.edit_original_response(embed=self._status_embed(), view=self)

        button.callback = callback
        return button

    def _make_instructions_button(self, row: int) -> discord.ui.Button:
        button = discord.ui.Button(label="📝 Edit Instructions", style=discord.ButtonStyle.primary, row=row)

        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("❌ This dashboard isn't for you.", ephemeral=True)

            async def on_saved(modal_interaction: discord.Interaction, new_text: str):
                import asyncio
                import src.core.database.sheets as sheets
                await asyncio.to_thread(sheets.db_set_ai_config, self.scope, custom_instructions=new_text)
                self.custom_instructions = new_text
                await modal_interaction.edit_original_response(embed=self._status_embed(), view=self)

            await interaction.response.send_modal(_InstructionsModal(self.custom_instructions, on_saved))

        button.callback = callback
        return button


# ==================== PER-CHANNEL CONFIG (stack any number of channels) ====================
_MODE_LABELS = {"faq": "🧭 Tournament FAQ", "general": "💬 General Chat"}


def _channel_list_embed(guild_name: str, channels: list) -> discord.Embed:
    embed = discord.Embed(
        title="📌 AI-Configured Channels", color=0x9B59B6,
        description=f"Server: **{guild_name}**\nEvery message typed in one of these channels gets an automatic reply — no mention needed.",
    )
    if not channels:
        embed.add_field(name="No channels configured yet", value="Use `/ai channel set` to add one.", inline=False)
        return embed

    for ch in channels[:25]:
        mode_label = _MODE_LABELS.get(ch["mode"], ch["mode"])
        strict_label = " + 🔒 Strict" if (ch["strict_enabled"] and ch["mode"] == "faq") else ""
        instructions_preview = (ch["custom_instructions"] or "").strip()
        if instructions_preview:
            if len(instructions_preview) > 150:
                instructions_preview = instructions_preview[:150] + "…"
        else:
            instructions_preview = "_No custom instructions_"
        embed.add_field(
            name=f"<#{ch['channel_id']}> — {mode_label}{strict_label}",
            value=instructions_preview,
            inline=False,
        )
    return embed


def setup_ai_assistant(bot: commands.Bot):
    group = app_commands.Group(name="ai", description="Configure the AI assistant.")
    channel_group = app_commands.Group(name="channel", description="Configure per-channel AI behavior.", parent=group)

    @group.command(name="dashboard", description="Open the global AI assistant control panel (mentions, DMs, /ask).")
    @app_commands.guild_only()
    @app_commands.describe(scope="Configure behavior for this server, or for Direct Messages.")
    @app_commands.choices(scope=[
        app_commands.Choice(name="This Server", value="server"),
        app_commands.Choice(name="Direct Messages", value="dm"),
    ])
    async def dashboard_cmd(interaction: discord.Interaction, scope: app_commands.Choice[str] = None):
        # Always check mod status via the guild this was run in — even when
        # configuring the DM scope, "are you a mod" has to be answered
        # somewhere with actual roles to check, and DMs have none.
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure the AI assistant.", ephemeral=True)

        scope_value = scope.value if scope else "server"
        if scope_value == "dm":
            scope_key, scope_label = "DM", "Direct Messages (Global)"
        else:
            scope_key, scope_label = str(interaction.guild.id), f"This Server ({interaction.guild.name})"

        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets

        config = await asyncio.to_thread(sheets.db_get_ai_config, scope_key)
        faq_enabled = config["faq_enabled"] if config else False
        general_enabled = config["general_enabled"] if config else False
        strict_enabled = config["strict_enabled"] if config else False
        custom_instructions = config["custom_instructions"] if config else ""

        view = _AIDashboardView(scope_key, faq_enabled, general_enabled, strict_enabled, interaction.user.id, scope_label, custom_instructions)
        await interaction.followup.send(embed=view._status_embed(), view=view, ephemeral=True)

    # ---------- /ai channel set ----------
    @channel_group.command(name="set", description="Configure this server's AI behavior for a specific channel.")
    @app_commands.guild_only()
    @app_commands.describe(
        channel="The channel to configure.",
        mode="What this channel should be limited to.",
        strict="Only applies to Tournament FAQ mode: keyword-filter off-topic questions before they reach the AI.",
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="Tournament FAQ Only", value="faq"),
        app_commands.Choice(name="General Chat", value="general"),
    ])
    async def channel_set_cmd(interaction: discord.Interaction, channel: discord.TextChannel,
                               mode: app_commands.Choice[str], strict: bool = False):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure the AI assistant.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets

        guild_id = str(interaction.guild.id)
        existing = await asyncio.to_thread(sheets.db_get_ai_channel, guild_id, str(channel.id))
        await asyncio.to_thread(
            sheets.db_set_ai_channel, guild_id, str(channel.id),
            mode=mode.value, strict_enabled=strict,
            custom_instructions=existing["custom_instructions"] if existing else None,
        )
        await interaction.followup.send(
            f"✅ {channel.mention} is now set to **{_MODE_LABELS[mode.value]}**"
            f"{' with 🔒 Strict Filter on' if (strict and mode.value == 'faq') else ''}.\n"
            f"Use `/ai channel instructions` to give this channel its own custom info, or it'll fall back to "
            f"the global instructions from `/ai dashboard` if none are set here.",
            ephemeral=True,
        )

    # ---------- /ai channel instructions ----------
    @channel_group.command(name="instructions", description="Set custom AI instructions for one specific channel.")
    @app_commands.guild_only()
    @app_commands.describe(channel="The channel to write instructions for.")
    async def channel_instructions_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure the AI assistant.", ephemeral=True)

        import asyncio
        import src.core.database.sheets as sheets

        guild_id = str(interaction.guild.id)
        existing = await asyncio.to_thread(sheets.db_get_ai_channel, guild_id, str(channel.id))
        if not existing:
            return await interaction.response.send_message(
                f"❌ {channel.mention} isn't configured yet. Run `/ai channel set` for it first.", ephemeral=True
            )

        async def on_saved(modal_interaction: discord.Interaction, new_text: str):
            await asyncio.to_thread(sheets.db_set_ai_channel, guild_id, str(channel.id), custom_instructions=new_text)
            await modal_interaction.followup.send(f"✅ Instructions saved for {channel.mention}.", ephemeral=True)

        await interaction.response.send_modal(_InstructionsModal(existing["custom_instructions"], on_saved))

    # ---------- /ai channel remove ----------
    @channel_group.command(name="remove", description="Stop the AI from auto-responding in a channel.")
    @app_commands.guild_only()
    @app_commands.describe(channel="The channel to remove.")
    async def channel_remove_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message("❌ **Access Denied:** Only tournament moderators can configure the AI assistant.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets

        guild_id = str(interaction.guild.id)
        existing = await asyncio.to_thread(sheets.db_get_ai_channel, guild_id, str(channel.id))
        if not existing:
            return await interaction.followup.send(f"❌ {channel.mention} wasn't configured.", ephemeral=True)

        await asyncio.to_thread(sheets.db_delete_ai_channel, guild_id, str(channel.id))
        await interaction.followup.send(f"🗑️ Removed AI configuration for {channel.mention}.", ephemeral=True)

    # ---------- /ai channel list ----------
    @channel_group.command(name="list", description="List every channel the AI is configured for in this server.")
    @app_commands.guild_only()
    async def channel_list_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        import asyncio
        import src.core.database.sheets as sheets

        channels = await asyncio.to_thread(sheets.db_list_ai_channels, str(interaction.guild.id))
        await interaction.followup.send(embed=_channel_list_embed(interaction.guild.name, channels), ephemeral=True)

    bot.tree.add_command(group)

    # ==================== /ask (explicit slash-command trigger) ====================
    @bot.tree.command(name="ask", description="Ask the AI assistant a question")
    @app_commands.describe(question="What do you want to ask?")
    async def ask(interaction: discord.Interaction, question: str):
        if _on_cooldown(interaction.user.id):
            await interaction.response.send_message(
                "⏳ Slow down a bit — try again in a few seconds.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        import asyncio
        import src.core.database.sheets as sheets

        scope_key = str(interaction.guild.id) if interaction.guild else "DM"
        config = await asyncio.to_thread(sheets.db_get_ai_config, scope_key)
        faq_enabled = config["faq_enabled"] if config else False
        general_enabled = config["general_enabled"] if config else False
        strict_enabled = config["strict_enabled"] if config else False
        custom_instructions = config["custom_instructions"] if config else ""

        if not faq_enabled and not general_enabled:
            await interaction.followup.send(
                "🔴 The AI assistant is turned off here. Ask a moderator to enable it via `/ai dashboard`."
            )
            return

        try:
            reply_text = await _generate_ai_reply(
                scope_key, interaction.user.id, question, faq_enabled, general_enabled, strict_enabled,
                custom_instructions,
            )
        except Exception as e:
            print(f"⚠️ Gemini request failed: {e}")
            reply_text = "❌ Something went wrong talking to the AI assistant. Try again in a moment."

        embed = discord.Embed(title="🤖 AI Assistant", description=reply_text[:4000], color=0x5865F2)
        embed.set_footer(text=f"Asked by {interaction.user.display_name}")
        await interaction.followup.send(embed=embed)

    # ==================== AUTO-REPLY LISTENER (mention / DM / any configured channel) ====================
    async def on_message(message: discord.Message):
        if message.author.bot or bot.user is None:
            return
        if message.content.startswith("!"):
            return  # leave legacy prefix commands alone, never treat them as AI input

        is_dm = isinstance(message.channel, discord.DMChannel)
        is_mention = bot.user in message.mentions
        scope_key = "DM" if is_dm else str(message.guild.id)

        import asyncio
        import src.core.database.sheets as sheets

        # A configured channel is checked FIRST and, if matched, completely
        # overrides the scope-level toggles for that message — each channel
        # is independently configured (its own mode/strict/instructions),
        # and any number of channels can be stacked this way.
        channel_config = None
        if not is_dm:
            channel_config = await asyncio.to_thread(sheets.db_get_ai_channel, scope_key, str(message.channel.id))

        if channel_config:
            effective_faq = channel_config["mode"] == "faq"
            effective_general = channel_config["mode"] == "general"
            strict_enabled = channel_config["strict_enabled"]
            custom_instructions = channel_config["custom_instructions"]
            if not custom_instructions:
                # Fall back to the scope-wide instructions if this channel
                # doesn't have its own — still fully independent on mode/strict.
                fallback_config = await asyncio.to_thread(sheets.db_get_ai_config, scope_key)
                custom_instructions = fallback_config["custom_instructions"] if fallback_config else ""
        elif is_dm or is_mention:
            config = await asyncio.to_thread(sheets.db_get_ai_config, scope_key)
            faq_enabled = config["faq_enabled"] if config else False
            general_enabled = config["general_enabled"] if config else False
            if not faq_enabled and not general_enabled:
                return  # AI is off for this scope — stay silent rather than nagging every mention
            effective_faq, effective_general = faq_enabled, general_enabled
            strict_enabled = config["strict_enabled"] if config else False
            custom_instructions = config["custom_instructions"] if config else ""
        else:
            return  # not a configured channel, not a mention, not a DM — ignore

        if _on_cooldown(message.author.id):
            return

        content = message.content
        for m in message.mentions:
            content = content.replace(f"<@{m.id}>", "").replace(f"<@!{m.id}>", "")
        content = content.strip() or "Hello!"

        async with message.channel.typing():
            try:
                reply_text = await _generate_ai_reply(
                    scope_key, message.author.id, content, effective_faq, effective_general, strict_enabled,
                    custom_instructions,
                )
            except Exception as e:
                print(f"⚠️ Gemini request failed: {e}")
                try:
                    await message.reply("⚠️ Sorry, I couldn't process that right now. Please try again shortly.", mention_author=False)
                except discord.HTTPException:
                    pass
                return

        for chunk in _chunk_text(reply_text):
            try:
                await message.reply(chunk, mention_author=False)
            except discord.HTTPException:
                break

    bot.add_listener(on_message, name="on_message")
