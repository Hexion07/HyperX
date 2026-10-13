import discord
from discord import app_commands
from discord.ext import commands

CATEGORIES = {
    "tournament": {
        "label": "🏆 Tournament Enrollment",
        "emoji": "🏆",
        "description": "Registering, updating, and managing tournament teams.",
        "fields": [
            ("!spawnpanel", "**Admin only.** Posts the persistent Team Enrollment Panel with all 6 buttons below."),
            ("📝 Enroll Team", "Opens a wizard (in a private ticket or your DMs) to register a new 4-player team + optional sub. Any one of the 4 teammates can be the one who enrolls — it doesn't have to be the IGL. **Strictly locked:** none of the 5 roster slots (IGL/P2/P3/P4/Sub) can be on more than one team at a time — you must Leave your current team (via Manage) before joining or creating another."),
            ("🛡️ Manage", "View-only team overview, visible to every current teammate (including the Substitute). From here: **Transfer Enroller** (current Enroller only) hands the Enroller role to another filled slot, and **Leave Team** removes only your own slot — if you were the Enroller, that role auto-passes to the next remaining teammate."),
            ("✏️ Update", "If you're the enroller, jumps straight to the update wizard. Otherwise, asks for your Team Name/Code, looks up who enrolled that team, and only lets *that* person continue."),
            ("🗑️ Delete", "Deletes your team's registration after a Yes/No confirmation. Archives the roster to the backup sheet first."),
            ("🔍 Fetch Team Data", "Public lookup — search any team by Code, Team Name, or IGL Discord tag."),
            ("🔴 Ban", "**Moderator only.** Temporarily suspends a team from enrolling/participating."),
        ]
    },
    "reactionrole": {
        "label": "🎭 Reaction Roles",
        "emoji": "🎭",
        "description": "Let members react to a message to get (or remove) a role.",
        "fields": [
            ("/reactionrole add", "**Moderator only.** `message_id`, `emoji`, `role` — reacts to the target message and binds that emoji to the role. Reacting toggles the role on/off."),
            ("/reactionrole remove", "**Moderator only.** `message_id`, `emoji` — unbinds a mapping."),
            ("/reactionrole list", "Lists every reaction-role mapping configured in this server."),
        ]
    },
    "webhook": {
        "label": "🪝 Webhooks",
        "emoji": "🪝",
        "description": "Create and manage channel webhooks for posting messages from outside Discord.",
        "fields": [
            ("/webhook create", "**Moderator only.** `name`, `channel` (optional) — creates a Discord webhook and saves it under a friendly name."),
            ("/webhook delete", "**Moderator only.** `name` — deletes both the Discord webhook and its saved record."),
            ("/webhook list", "Lists every webhook registered in this server (name, channel, owner)."),
            ("/webhook info", "`name` — shows a webhook's details. The URL is only revealed to its owner or a moderator."),
        ]
    },
    "welcome": {
        "label": "👋 Welcome & Goodbye",
        "emoji": "👋",
        "description": "Automatic messages when members join or leave.",
        "fields": [
            ("/welcome set", "**Moderator only.** `channel`, `message` — enables welcome messages. Supports `{user}`, `{username}`, `{server}` placeholders."),
            ("/welcome disable", "**Moderator only.** Turns off welcome messages."),
            ("/goodbye set", "**Moderator only.** `channel`, `message` — enables goodbye messages. Same placeholders as above."),
            ("/goodbye disable", "**Moderator only.** Turns off goodbye messages."),
        ]
    },
    "infostats": {
        "label": "📊 Info & Stats",
        "emoji": "📊",
        "description": "Announcements for new enrollments, roster leaves, and Enroller transfers.",
        "fields": [
            ("/infostats set", "**Moderator only.** `channel` — every new team enrollment (pinging the roster + naming the Enroller), every Leave Team, and every Enroller transfer gets posted here."),
            ("/infostats disable", "**Moderator only.** Turns off Info & Stats announcements."),
        ]
    },
    "embed": {
        "label": "🎨 Embed Builder",
        "emoji": "🎨",
        "description": "Design and post a custom embed without any coding.",
        "fields": [
            ("/embed", "`channel` (optional) — opens a form for Title, Description, Color, Image URL, and Footer, then posts the result."),
        ]
    },
    "ai": {
        "label": "🤖 AI Assistant",
        "emoji": "🤖",
        "description": "Talk to a Gemini-powered assistant — @mention the bot, DM it, use /ask, or type in a configured channel.",
        "fields": [
            ("@mention, DM, or /ask", "Chat with the assistant directly. Replies use whichever modes are currently enabled for this scope via /ai dashboard."),
            ("Configured channels", "Any number of channels can be independently set up via /ai channel — every message typed there gets an automatic reply, no mention needed, each with its own mode, strict filter, and instructions."),
            ("/ai dashboard", "**Moderator only.** `scope` (This Server / Direct Messages) — controls @mention/DM/`/ask` behavior: toggle **Tournament FAQ** and **General Chat** independently, toggle **Strict Filter** (keyword-blocks off-topic questions before they reach the AI, only while in pure Tournament FAQ mode), and set global **Custom Instructions** used as a fallback anywhere more specific instructions aren't set."),
            ("/ai channel set", "**Moderator only.** `channel`, `mode` (Tournament FAQ Only / General Chat), `strict` (optional) — pins that channel to the chosen mode. Stack as many channels as you want; each is fully independent."),
            ("/ai channel instructions", "**Moderator only.** `channel` — opens a popup to give that specific channel its own custom info (prize pool, rules, schedule, etc.), overriding the global instructions just for that channel."),
            ("/ai channel list", "**Moderator only.** Shows every channel currently configured in this server and its settings."),
            ("/ai channel remove", "**Moderator only.** `channel` — stops the AI from auto-responding there."),
        ]
    },
    "sticky": {
        "label": "📌 Sticky Notes",
        "emoji": "📌",
        "description": "Pin a requirements message to a channel that auto-reposts to stay at the bottom.",
        "fields": [
            ("/sticky dashboard", "**Moderator only.** Opens an interactive control panel — pick a channel from the dropdown, then create/edit, enable/disable, or delete its sticky note."),
            ("/sticky set", "**Moderator only.** `channel` — opens a popup to set the note's title, requirements text, color, and how many messages before it reposts."),
            ("/sticky remove", "**Moderator only.** `channel` — deletes the sticky note (and its currently-posted message) from a channel."),
            ("/sticky list", "Shows every channel in this server with a sticky note configured."),
            ("/sticky post", "**Moderator only.** `channel` — immediately bumps that channel's sticky note back to the bottom."),
        ]
    },
}


def _build_embed(key: str) -> discord.Embed:
    cat = CATEGORIES[key]
    embed = discord.Embed(title=f"{cat['label']}", description=cat["description"], color=0x2ECC71)
    for name, value in cat["fields"]:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="HyperX Engine • Use the dropdown below to browse other categories")
    return embed


def _build_overview_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📖 HyperX Bot — Help Guide",
        description="Everything this bot can do, grouped by category. Pick one from the dropdown below to see the full command list.",
        color=0x3498DB
    )
    for key, cat in CATEGORIES.items():
        embed.add_field(name=cat["label"], value=cat["description"], inline=False)
    embed.set_footer(text="HyperX Engine • Secure Enrollment Portal")
    return embed


class HelpCategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Overview", value="__overview__", emoji="📖")
        ] + [
            discord.SelectOption(label=cat["label"].split(" ", 1)[1], value=key, emoji=cat["emoji"])
            for key, cat in CATEGORIES.items()
        ]
        super().__init__(placeholder="Browse a category...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__overview__":
            await interaction.response.edit_message(embed=_build_overview_embed(), view=self.view)
        else:
            await interaction.response.edit_message(embed=_build_embed(self.values[0]), view=self.view)


class HelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180.0)
        self.add_item(HelpCategorySelect())


def setup_help_command(bot: commands.Bot):
    @bot.tree.command(name="help", description="Show every command and feature this bot has.")
    async def help_cmd(interaction: discord.Interaction):
        await interaction.response.send_message(embed=_build_overview_embed(), view=HelpView(), ephemeral=True)
