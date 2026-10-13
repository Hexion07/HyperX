import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

from src.core.database.sheets import init_sheets
from src.core.views.modals import PersistentRegistrationView
from src.core.commands.reaction_roles import setup_reaction_roles, warm_reaction_role_cache
from src.core.commands.webhooks import setup_webhook_commands
from src.core.commands.welcome import setup_welcome_commands
from src.core.commands.info_stats import setup_info_stats_commands
from src.core.commands.embed_builder import setup_embed_builder
from src.core.commands.help import setup_help_command
from src.core.commands.ai_assistant import setup_ai_assistant
from src.core.commands.sticky_notes import setup_sticky_notes

load_dotenv()
RAW_TOKEN = os.getenv("DISCORD_TOKEN")

# Setup clean intent bindings
intents = discord.Intents.default()
intents.message_content = True
intents.members = True 

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Register every slash-command group + their event listeners. This just wires
# up the commands/listeners in-process; nothing hits Discord until sync() runs
# inside on_ready.
setup_reaction_roles(bot)
setup_webhook_commands(bot)
setup_welcome_commands(bot)
setup_info_stats_commands(bot)
setup_embed_builder(bot)
setup_help_command(bot)
setup_ai_assistant(bot)
setup_sticky_notes(bot)

@bot.event
async def on_ready():
    print(f"✅ Bot logged into Discord gateway successfully as {bot.user}")
    
    # Registers the custom interaction callback handles for live server messages
    bot.add_view(PersistentRegistrationView()) 
    
    # Establish sheet linkages passing the thread hook along.
    # Wrapped in try/except so a Sheets/auth failure here can never block the
    # slash-command sync below — that was silently killing sync() entirely.
    try:
        init_sheets(live_bot_reference=bot)
    except Exception as e:
        print(f"⚠️ Failed to initialize Google Sheets: {e}")

    # Preload the reaction-role cache so the very first reaction click after
    # startup doesn't have to wait on a sheet read
    try:
        await warm_reaction_role_cache()
    except Exception as e:
        print(f"⚠️ Failed to warm reaction role cache: {e}")

    # Push the slash commands (/reactionrole, /webhook, /welcome, /goodbye,
    # /infostats, /sticky, /embed, /help) to Discord globally. NOTE: global
    # commands can take up to an hour to actually show up in the Discord client's picker.
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} global slash command(s). (Global commands can take up to an hour to appear — see per-guild sync below for instant visibility.)")
    except discord.HTTPException as e:
        print(f"⚠️ Failed to sync global slash commands: {e}")

    # Also mirror them straight into every guild the bot is currently in.
    # Guild-scoped syncs show up instantly (no propagation delay), which is
    # what you want while developing/testing.
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            synced_guild = await bot.tree.sync(guild=guild)
            print(f"✅ Instantly synced {len(synced_guild)} slash command(s) to guild '{guild.name}'.")
        except discord.HTTPException as e:
            print(f"⚠️ Failed to sync slash commands to guild '{guild.name}': {e}")

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        # !help no longer exists now that help_command=None is set — point
        # people at the real thing instead of just silently failing.
        if ctx.invoked_with == "help":
            await ctx.send("ℹ️ This bot now uses **/help** instead of `!help` — try that!")
        return  # swallow unknown text commands quietly, nothing to log here

    if isinstance(error, commands.MissingPermissions):
        return await ctx.send("❌ **Access Denied:** You don't have permission to use that command.")

    # Anything else is unexpected — still print it so real bugs stay visible
    import traceback
    print(f"⚠️ Unhandled command error in '{ctx.command}': {error}")
    traceback.print_exception(type(error), error, error.__traceback__)

@bot.command()
@commands.has_permissions(administrator=True)
async def spawnpanel(ctx):
    embed = discord.Embed(
        title="🎮 Team Enrollment Panel",
        description="Click the button below to enroll your team directly through our automated workspace tickets or securely via private messages.",
        color=0x2ECC71
    )
    embed.set_footer(text="HyperX Engine • Secure Enrollment Portal")
    await ctx.send(embed=embed, view=PersistentRegistrationView())
    await ctx.message.delete()

if RAW_TOKEN:
    CLEANED_TOKEN = RAW_TOKEN.strip().replace('"', '').replace("'", "")
    bot.run(CLEANED_TOKEN)
