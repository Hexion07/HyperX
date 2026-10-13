from __future__ import annotations

import random
import asyncio
import discord
import aiohttp # 👈 Added for connection tracking

# Core Database Accessors loaded inside callbacks to stay immune to cyclic initialization traps
from src.core.views.validators import (
    COUNTRY_CODES,
    validate_email,
    validate_phone,
    is_valid_uid,
    RosterSession
)

MOD_ROLE_NAME = "Tournament Moderator"
CATEGORY_NAME = "🏆 Enrollment & Update Tickets"

# ==================== ENROLLMENT LOCK / INFO & STATS HELPERS ====================
SLOT_ORDER = ["igl", "p2", "p3", "p4", "sub"]
SLOT_LABELS = {"igl": "👑 IGL", "p2": "Player 2", "p3": "Player 3", "p4": "Player 4", "sub": "🔄 Substitute"}

async def _post_info_stats(guild: discord.Guild | None, message: str):
    """Posts an announcement to this guild's configured Info & Stats channel,
    if one has been set via /infostats set. Silently no-ops otherwise so this
    never blocks the enrollment/leave/transfer flow it's called from."""
    if not guild:
        return
    import src.core.database.sheets as sheets
    try:
        config = await asyncio.to_thread(sheets.db_get_guild_config, guild.id)
    except Exception as e:
        print(f"⚠️ info & stats lookup failed: {e}")
        return
    channel_id = config.get("info_stats_channel_id") if config else None
    if not channel_id:
        return
    channel = guild.get_channel(int(channel_id))
    if not channel:
        return
    try:
        await channel.send(message)
    except discord.HTTPException as e:
        print(f"⚠️ info & stats post failed: {e}")

def _resolve_slot_id(session: RosterSession, tag_value: str):
    """Looks up the discord.Member already resolved for this tag during the
    wizard (RosterSession tracks every accepted tag -> Member) and returns
    their ID, or None (e.g. for an unused Substitute slot)."""
    if not tag_value or str(tag_value).strip().lower() in ("none", ""):
        return None
    member = session.registered_members.get(str(tag_value).strip().lower())
    return member.id if member else None

async def _check_tag_not_locked(session: RosterSession, tag_input: str, exclude_team_code: str | None = None):
    """Wraps RosterSession.check_discord_tag with a cross-team enrollment-lock
    check: even if the tag is free within THIS wizard session, reject it if
    that Discord member already occupies any of the 5 roster slots
    (IGL/P2/P3/P4/Sub) on a DIFFERENT team. They have to Leave that team first
    (Manage -> Leave Team) before they can be entered here. exclude_team_code
    lets the Update wizard allow a teammate to keep their own existing spot
    on THIS same team."""
    member_str = await session.check_discord_tag(tag_input)
    if member_str is None:
        return None
    member = session.registered_members.get(member_str.lower())
    if not member:
        return member_str
    import src.core.database.sheets as sheets
    hit = await asyncio.to_thread(sheets.db_find_locked_team, member.id, member_str)
    if hit:
        locked_team, _slot = hit
        if exclude_team_code and str(locked_team.get("team_code")) == str(exclude_team_code):
            return member_str  # it's their own existing slot on THIS team
        # Roll back the in-memory reservation check_discord_tag just made so
        # the same prompt can be retried with a different person.
        session.registered_tags.discard(member_str.lower())
        session.registered_members.pop(member_str.lower(), None)
        return None
    return member_str

def _make_lock_checked_validator(session: RosterSession, exclude_team_code: str | None = None):
    async def _validator(tag_input: str):
        return await _check_tag_not_locked(session, tag_input, exclude_team_code)
    return _validator

# ==================== MULTI-USER SAFETY: TEAM CODE RESERVATION ====================
# Two users starting registration at the same instant could previously both be
# handed the same random 6-digit code (nothing checked for collisions). This
# in-memory set + sheet check makes a code "reserved" the instant it's handed
# out, so a second concurrent registration can't pick the same one — even
# before either one has actually been saved to the sheet yet.
_PENDING_TEAM_CODES = set()
_team_code_lock = asyncio.Lock()

async def generate_unique_team_code() -> str:
    """Atomically reserves a 6-digit team code that isn't already in the sheet
    and isn't currently reserved by another in-progress registration."""
    import src.core.database.sheets as sheets
    for _ in range(20):
        candidate = str(random.randint(100000, 999999))
        async with _team_code_lock:
            if candidate in _PENDING_TEAM_CODES:
                continue
            already_used = await asyncio.to_thread(sheets.db_code_exists, candidate)
            if already_used:
                continue
            _PENDING_TEAM_CODES.add(candidate)
            return candidate
    raise RuntimeError("Could not generate a unique team code after multiple attempts.")

def release_team_code(code: str):
    """Frees up a reserved code once a registration finishes, cancels, or times
    out, so it doesn't stay permanently blocked for no reason."""
    _PENDING_TEAM_CODES.discard(code)

def check_is_mod(member: discord.Member) -> bool:
    return any(role.name == MOD_ROLE_NAME for role in member.roles) or member.guild_permissions.administrator

# ==================== MODAL COMPONENT WINDOWS ====================
class TempBanModal(discord.ui.Modal, title="🔨 Admin Team Suspension Panel"):
    target_query = discord.ui.TextInput(label="Target (Team Code, Name, or IGL Tag)", required=True)
    duration = discord.ui.TextInput(label="Duration in Days (Blank defaults to 7)", required=False, placeholder="7")
    reason = discord.ui.TextInput(label="Reason for suspension", required=False, placeholder="Slot waste / Rules violation", default="Breach of Tournament Terms.")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        import src.core.database.sheets as sheets
        
        days = 7
        if self.duration.value.strip():
            if self.duration.value.strip().isdigit():
                days = int(self.duration.value.strip())
            else:
                return await interaction.followup.send("❌ **Error:** Duration must be a valid number of days.", ephemeral=True)
                
        try:
            msg = await asyncio.to_thread(sheets.db_apply_tempban, self.target_query.value, days, self.reason.value)
            await interaction.followup.send(msg, ephemeral=True)
        except ValueError:
            await interaction.followup.send("❌ **Not Found:** No team records match that target specification query.", ephemeral=True)

class UpdateModal(discord.ui.Modal, title="✏️ Update Core Team Details"):
    team_name = discord.ui.TextInput(label="New Team Name", required=True)
    email = discord.ui.TextInput(label="New Email Address", required=True)
    phone = discord.ui.TextInput(label="New Phone Number", required=True)
    igl_ign = discord.ui.TextInput(label="New IGL IGN Name", required=True)
    igl_uid = discord.ui.TextInput(label="New IGL Game UID", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not validate_email(self.email.value):
            return await interaction.response.send_message("❌ Invalid email format.", ephemeral=True)
        if not validate_phone(self.phone.value):
            return await interaction.response.send_message("❌ Invalid phone format. Numbers only.", ephemeral=True)
        if not is_valid_uid(self.igl_uid.value):
            return await interaction.response.send_message("❌ IGL UID length must be between 8 and 12 digits.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        import src.core.database.sheets as sheets
        try:
            await asyncio.to_thread(
                sheets.db_update_team, 
                interaction.user.id, self.team_name.value.strip(), self.email.value.strip(), 
                self.phone.value.strip(), self.igl_ign.value.strip(), self.igl_uid.value.strip()
            )
            await interaction.followup.send("✅ Core details and your team row record have been updated successfully!", ephemeral=True)
        except ValueError:
            await interaction.followup.send("❌ No matching team registration records found under your account profile.", ephemeral=True)

class FetchTeamModal(discord.ui.Modal, title="🔍 Fetch Team Roster Data"):
    search_query = discord.ui.TextInput(label="Enter Code, Team Name, or IGL Tag", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        import src.core.database.sheets as sheets
        
        team = await asyncio.to_thread(sheets.db_find_team_by_query, self.search_query.value)
        if not team:
            return await interaction.followup.send("❌ **No Team Found:** No records match that Code, Team Name, or IGL info.", ephemeral=True)
            
        embed = discord.Embed(title=f"📋 Tournament Profile: {team['team_name']}", color=0x3498DB)
        embed.add_field(name="🆔 Unique Team Code", value=f"`{team['team_code']}`", inline=True)
        embed.add_field(name="🌍 Region/Country", value=team['country'], inline=True)
        embed.add_field(name="👑 Team IGL (In-Game Name)", value=f"**{team['igl_ign']}**", inline=False)
        embed.add_field(name="💬 IGL Discord Account", value=team['igl_tag'], inline=True)
        embed.add_field(name="🎮 IGL Game UID", value=f"`{team['igl_uid']}`", inline=True)
        embed.set_footer(text="HyperX Engine • Secure Public Directory Search")
        
        await interaction.followup.send(embed=embed, ephemeral=True)

# ==================== CONFIRMATION DIALOGUE COMPONENT ====================
class CancelConfirmationView(discord.ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=30.0)
        self.value = None
        self.user = user

    @discord.ui.button(label="Yes, Cancel Setup", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("❌ This confirmation panel isn't for you.", ephemeral=True)
        await interaction.response.defer()
        self.value = True
        self.stop()

    @discord.ui.button(label="No, Keep Going", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message("❌ This confirmation panel isn't for you.", ephemeral=True)
        await interaction.response.defer()
        self.value = False
        self.stop()

# ==================== ENROLLER SELECTION (WHO GETS UPDATE ACCESS) ====================
class _EnrollerSelect(discord.ui.Select):
    """The dropdown itself — one option per roster slot (IGL/P2/P3/P4), built
    from whatever that teammate's IGN + Discord tag were entered as."""
    def __init__(self, user_id: int, roster_options: list):
        options = [
            discord.SelectOption(label=f"{label} — {ign}"[:100], description=tag[:100], value=key)
            for key, label, ign, tag in roster_options
        ]
        super().__init__(placeholder="Choose the Enroller...", min_values=1, max_values=1, options=options)
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self._user_id:
            return await interaction.response.send_message("❌ This selection menu isn't for you.", ephemeral=True)
        self.view.selected_key = self.values[0]
        await interaction.response.defer()
        self.view.stop()

class _EnrollerSelectView(discord.ui.View):
    def __init__(self, user_id: int, roster_options: list):
        super().__init__(timeout=180.0)
        self.selected_key = None
        self.add_item(_EnrollerSelect(user_id, roster_options))

async def ask_enroller_choice(target_channel: discord.Messageable, prefix_mention: str, user: discord.User, session: RosterSession, roster: dict) -> int:
    """Asks 'which teammate should be the Enroller?' via a select menu built
    from the IGL/P2/P3/P4 the user just entered, and resolves the chosen
    teammate back to their actual Discord ID via the RosterSession's
    registered_members map (populated as each Discord tag was validated).
    roster is {"igl": (ign, tag), "p2": (ign, tag), "p3": (ign, tag), "p4": (ign, tag)}.
    Falls back to the invoking user's own ID if the menu times out or a
    resolved member can't be found, so the wizard can never end up with no
    enroller at all."""
    labels = {"igl": "👑 IGL", "p2": "Player 2", "p3": "Player 3", "p4": "Player 4"}
    options = [(key, labels[key], ign, tag) for key, (ign, tag) in roster.items()]

    view = _EnrollerSelectView(user.id, options)
    prompt_msg = await target_channel.send(
        f"{prefix_mention}🔐 **Which teammate do you want to be the 'Enroller'?**\n"
        f"*(The only person who will have access to update this registration later)*",
        view=view
    )
    await view.wait()

    try:
        await prompt_msg.delete()
    except discord.HTTPException:
        pass

    if view.selected_key is None:
        return user.id

    chosen = next(o for o in options if o[0] == view.selected_key)
    member = session.registered_members.get(chosen[3].lower())
    return member.id if member else user.id

# ==================== UNIVERSAL CHAT REGISTRATION LOOP ====================
async def run_chat_registration(interaction: discord.Interaction, target_channel: discord.Messageable, is_dm: bool, team_code: str):
    guild = interaction.guild
    user = interaction.user
    import src.core.database.sheets as sheets
    
    def check(m): return m.author.id == user.id and m.channel.id == target_channel.id
    data = {}
    
    session = RosterSession(guild)
    prefix_mention = "" if is_dm else f"{user.mention} "

    async def ask_question(prompt_text: str, validator_func=None, error_msg="❌ Invalid format. Please try again."):
        while True:
            formatted_prompt = f"{prompt_text}\n*💡 (Type **cancel** at any point to abort registration)*"
            
            # 1. 🛡️ Real-time connection check before showing the prompt
            if sheets.bot.latency is None or sheets.bot.latency > 5.0 or sheets.bot.is_closed():
                raise ConnectionError
                
            try:
                # Capture the exact time *just before* sending the question
                question_sent_at = discord.utils.utcnow()
                await target_channel.send(f"{prefix_mention}{formatted_prompt}")
            except (discord.HTTPException, aiohttp.ClientError):
                raise ConnectionError
            
            try:
                # 2. Add an inner loop to filter out old buffered messages
                while True:
                    msg = await sheets.bot.wait_for('message', check=check, timeout=180.0)
                    
                    # If the message was sent BEFORE our question prompt, it's backlog/lag. Ignore it!
                    if msg.created_at < question_sent_at:
                        continue
                        
                    # If we get here, it's a fresh response from the user
                    break

                val = msg.content.strip()
                if not val: continue
                
                if val.lower() == "cancel":
                    confirm_view = CancelConfirmationView(user)
                    confirm_msg = await target_channel.send(f"{prefix_mention}⚠️ **Are you sure you want to cancel?**", view=confirm_view)
                    await confirm_view.wait()
                    
                    if confirm_view.value is True:
                        try: await confirm_msg.delete()
                        except discord.HTTPException: pass
                        
                        if is_dm:
                            await target_channel.send("❌ **You have cancelled the team process.**")
                        else:
                            countdown_msg = await target_channel.send(
                                f"❌ **You have cancelled the enrollment process!**\n"
                                f"⏳ *This ticket channel will be permanently deleted in 5 seconds...*"
                            )
                            for seconds_left in range(4, 0, -1):
                                await asyncio.sleep(1)
                                try:
                                    await countdown_msg.edit(
                                        content=f"❌ **You have cancelled the enrollment process!**\n"
                                                f"⏳ *This ticket channel will be permanently deleted in {seconds_left} seconds...*"
                                    )
                                except discord.HTTPException:
                                    break
                            await asyncio.sleep(1)
                            
                            try: await target_channel.delete()
                            except discord.HTTPException: pass
                        raise InterruptedError
                    else:
                        try: await confirm_view.delete()
                        except Exception: pass
                        continue
                
                if validator_func:
                    res = await validator_func(val) if asyncio.iscoroutinefunction(validator_func) else validator_func(val)
                    if res is not None and res is not False:
                        return res
                    await target_channel.send(f"{prefix_mention}{error_msg}")
                else:
                    return val
            except asyncio.TimeoutError:
                raise TimeoutError
            except (discord.HTTPException, aiohttp.ClientError, discord.ConnectionClosed):
                raise ConnectionError

    try:
        banned, ban_msg = sheets.db_is_team_banned(str(user.id))
        if banned:
            await target_channel.send(ban_msg)
            if not is_dm: await asyncio.sleep(5); await target_channel.delete()
            return

        await target_channel.send(
            f"{prefix_mention}🎮 **Welcome to the Tournament Enrollment Wizard!**\n"
            f"*ℹ️ Any one of the 4 teammates can complete this enrollment as the team's enroller — it doesn't have to be the IGL.*"
        )

        # ------------------ SECTION 1 ------------------
        data['team_name'] = await ask_question("📋 **Section 1: Team Details**\n\nEnter your **Team Name**:")
        is_name_banned, name_ban_msg = sheets.db_is_team_banned(data['team_name'])
        if is_name_banned:
            await target_channel.send(name_ban_msg)
            if not is_dm: await asyncio.sleep(5); await target_channel.delete()
            return

        data['email'] = await ask_question("📧 Enter your **Email Address**:", validate_email, "❌ Invalid format.")
        
        def validate_country(v):
            norm = v.lower().strip()
            return (v, COUNTRY_CODES[norm]) if norm in COUNTRY_CODES else (v, "[Custom Code]")
        country_name, prefix = await ask_question("🌍 Enter your **Country Name**:", validate_country)
        data['country'] = country_name
        data['phone'] = await ask_question(f"📱 Enter **Phone Number** ({prefix}):", validate_phone, "❌ Numbers only (Minimum 7 digits).")

        # ------------------ SECTION 2: IGL ------------------
        await target_channel.send(f"{prefix_mention}\n👑 **Section 2: Profiles**")
        data['igl_ign'] = await ask_question("👤 Enter **IGL In-Game Name (IGN)**:", session.check_ign, "❌ Invalid entry or IGN has already been claimed by a teammate.")
        data['igl_uid'] = await ask_question("🆔 Enter **IGL Game UID**:", session.check_uid, "❌ UID must be in numbers(8-12 digits) and unique to this team.")
        data['igl_tag'] = await ask_question("💬 Enter **IGL Discord Member (Username/ID)**:", _make_lock_checked_validator(session), "❌ Profile not found, account is a bot, already claimed by a teammate, or already locked to another team (they must Leave that team first).")

        # ------------------ PLAYER 2 ------------------
        data['p2_ign'] = await ask_question("👤 Enter **2nd Player IGN**:", session.check_ign, "❌ Duplicate Error: This IGN matches another teammate.")
        data['p2_uid'] = await ask_question("🆔 Enter **2nd Player UID**:", session.check_uid, "❌ Invalid format or UID matches another teammate.")
        data['p2_tag'] = await ask_question("💬 Enter **2nd Player Discord**:", _make_lock_checked_validator(session), "❌ Member not found, account is a bot, matches another teammate, or already locked to another team (they must Leave that team first).")

        # ------------------ PLAYER 3 ------------------
        await target_channel.send(f"{prefix_mention}\n⚔️ **Section 3**")
        data['p3_ign'] = await ask_question("👤 Enter **3rd Player IGN**:", session.check_ign, "❌ Duplicate Error: This IGN matches another teammate.")
        data['p3_uid'] = await ask_question("🆔 Enter **3rd Player UID**:", session.check_uid, "❌ Invalid format or UID matches another teammate.")
        data['p3_tag'] = await ask_question("💬 Enter **3rd Player Discord**:", _make_lock_checked_validator(session), "❌ Member not found, account is a bot, matches another teammate, or already locked to another team (they must Leave that team first).")

        # ------------------ PLAYER 4 ------------------
        data['p4_ign'] = await ask_question("👤 Enter **4th Player IGN**:", session.check_ign, "❌ Duplicate Error: This IGN matches another teammate.")
        data['p4_uid'] = await ask_question("🆔 Enter **4th Player UID**:", session.check_uid, "❌ Invalid format or UID matches another teammate.")
        data['p4_tag'] = await ask_question("💬 Enter **4th Player Discord**:", _make_lock_checked_validator(session), "❌ Member not found, account is a bot, matches another teammate, or already locked to another team (they must Leave that team first).")

        # ------------------ SUBSTITUTE PLAYER ------------------
        def check_sub_choice(v):
            norm = v.lower().strip()
            if norm in ['yes', 'y']: return 'yes'
            if norm in ['no', 'n']: return 'no'
            return False
            
        sub_choice = await ask_question("\n🔄 Do you have a **Substitute Player**? (yes/no):", check_sub_choice, "❌ Choose yes or no.")
        
        if sub_choice == 'yes':
            data['sub_ign'] = await ask_question("👤 Enter **Substitute IGN**:", session.check_ign, "❌ Duplicate Error: This IGN matches another teammate.")
            data['sub_uid'] = await ask_question("🆔 Enter **Substitute UID**:", session.check_uid, "❌ Invalid format or UID matches another teammate.")
            data['sub_tag'] = await ask_question("💬 Enter **Substitute Discord**:", _make_lock_checked_validator(session), "❌ Member not found, account is a bot, matches another teammate, or already locked to another team (they must Leave that team first).")
        else:
            data['sub_ign'], data['sub_uid'], data['sub_tag'] = "None", "None", "None"

        # ------------------ ENROLLER SELECTION ------------------
        enroller_id = await ask_enroller_choice(
            target_channel, prefix_mention, user, session,
            {
                "igl": (data['igl_ign'], data['igl_tag']),
                "p2": (data['p2_ign'], data['p2_tag']),
                "p3": (data['p3_ign'], data['p3_tag']),
                "p4": (data['p4_ign'], data['p4_tag']),
            }
        )

        slot_ids = {slot: _resolve_slot_id(session, data[f"{slot}_tag"]) for slot in SLOT_ORDER}
        await asyncio.to_thread(sheets.db_save_team, team_code, enroller_id, data, slot_ids)
        await target_channel.send(f"{prefix_mention}🎉 **Registration Successful! Code: {team_code}**\n🔐 Enroller: <@{enroller_id}>")

        roster_mentions = []
        for slot in SLOT_ORDER:
            slot_id = slot_ids.get(slot)
            tag_val = data.get(f"{slot}_tag", "")
            if slot_id:
                roster_mentions.append(f"<@{slot_id}>")
            elif tag_val and tag_val != "None":
                roster_mentions.append(tag_val)
        await _post_info_stats(
            guild,
            f"📥 **New Enrollment:** `{data['team_name']}` (Code `{team_code}`) was enrolled by <@{enroller_id}>.\n"
            f"👥 Roster: {' '.join(roster_mentions) if roster_mentions else '—'}"
        )

        if not is_dm: await asyncio.sleep(10); await target_channel.delete()

    except TimeoutError:
        # The user didn't respond to a question within the 180s window.
        # Previously this fell through to the bare `except Exception: return`
        # below and failed completely silently — no message, and for ticket
        # channels the channel was never even deleted — which is exactly
        # the "got stuck" behavior this fixes.
        try:
            if is_dm:
                await target_channel.send(
                    "⏳ **Your registration has timed out & try again.**\n"
                    "Please collect all the data of your Team before enrollment."
                )
            else:
                countdown_msg = await target_channel.send(
                    "⏳ **Your Ticket has been timed out & try again.**\n"
                    "Please collect all the data of your Team before enrollment.\n"
                    "*This ticket channel will be deleted in 10 seconds...*"
                )
                for seconds_left in range(9, 0, -1):
                    await asyncio.sleep(1)
                    try:
                        await countdown_msg.edit(
                            content=(
                                "⏳ **Your Ticket has been timed out & try again.**\n"
                                "Please collect all the data of your Team before enrollment.\n"
                                f"*This ticket channel will be deleted in {seconds_left} seconds...*"
                            )
                        )
                    except discord.HTTPException:
                        break
                await asyncio.sleep(1)
                try:
                    await target_channel.delete()
                except discord.HTTPException:
                    pass
        except (discord.HTTPException, aiohttp.ClientError):
            pass
        return
    except ConnectionError:
        # ❌ This block runs cleanly when a VPS network drop/token error occurs mid-wizard.
        try:
            await target_channel.send(f"{prefix_mention}⚠️ **Bot API has been disconnected. Please try again later.**")
            if not is_dm: 
                await asyncio.sleep(5)
                await target_channel.delete()
        except Exception:
            pass # Failsafe if discord gateway refuses write actions entirely
        return
    except Exception:
        return
    finally:
        # Whether this registration succeeded, was cancelled, timed out, or
        # crashed, the code must stop being "reserved" so it doesn't get stuck
        # blocking that number forever. If it was actually saved to the sheet,
        # db_code_exists() will correctly report it as taken from here on.
        release_team_code(team_code)

# ==================== CHANNEL PREFERENCE GATEWAY ====================
class ChannelPreferenceView(discord.ui.View):
    def __init__(self, original_interaction: discord.Interaction):
        super().__init__(timeout=60.0)
        self.original_interaction = original_interaction

    @discord.ui.button(label="Create a Ticket Channel", style=discord.ButtonStyle.primary, emoji="🎫")
    async def ticket_choice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user
        team_code = await generate_unique_team_code()
        
        category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        if not category:
            try: category = await guild.create_category(name=CATEGORY_NAME)
            except discord.Forbidden: category = None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)
        }
        
        try:
            ticket_channel = await guild.create_text_channel(
                name=f"code-{team_code}",
                overwrites=overwrites,
                category=category,
                topic=f"Registration Room • Code {team_code}"
            )
            await interaction.edit_original_response(content=f"🎫 **Ticket Opened!** Link: {ticket_channel.mention}", view=None)
            await run_chat_registration(self.original_interaction, ticket_channel, is_dm=False, team_code=team_code)
        except Exception as e:
            release_team_code(team_code)  # ticket never opened / registration never started — free the code
            await interaction.edit_original_response(content=f"❌ Failed to build ticket: {e}", view=None)

    @discord.ui.button(label="In my DMs", style=discord.ButtonStyle.success, emoji="📥")
    async def dm_choice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user = self.original_interaction.user
        team_code = await generate_unique_team_code()
        try:
            dm_channel = await user.create_dm()
            await interaction.edit_original_response(content="📥 **Check your DMs!** Opened safely.", view=None)
            await run_chat_registration(self.original_interaction, dm_channel, is_dm=True, team_code=team_code)
        except discord.Forbidden:
            release_team_code(team_code)  # DMs closed / registration never started — free the code
            await interaction.edit_original_response(content="❌ DMs are locked.", view=None)

# ==================== NEW: VERIFICATION FALLBACK MODALS ====================

class UpdateByCodeModal(discord.ui.Modal, title="Verify Team Name / Code"):
    """Modal that opens instantly, looks the team up by Team Name or Team Code,
    then automatically fetches that team's registered enroller and only grants
    update access if the person filling this modal IS that enroller."""
    team_code_input = discord.ui.TextInput(
        label="Enter Your Team Name or Team Code",
        placeholder="e.g., 646757 or Phoenix Squad",
        min_length=2,
        max_length=50,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        # 1. Acknowledge instantly to bypass 3-second timeouts
        await interaction.response.defer(ephemeral=True)

        # 2. Keep the raw input (letters included) so team NAMES work, not just numeric codes
        raw_input = self.team_code_input.value.strip()

        if not raw_input:
            await interaction.followup.send("❌ **Invalid Input.** Please enter a team name or team code.", ephemeral=True)
            return

        import src.core.database.sheets as sheets
        import asyncio
        
        status_msg = await interaction.followup.send("🔍 Verifying Team from database... Please wait.", ephemeral=True)
        
        # 3. Search database using the flexible query function (matches Code, Team Name, or IGL Tag)
        try:
            existing_team = await asyncio.to_thread(sheets.db_find_team_by_query, raw_input)
        except Exception as e:
            print(f"[DEBUG] Search error: {e}")
            existing_team = None

        try:
            await status_msg.delete()
        except Exception:
            pass

        if not existing_team:
            await interaction.followup.send(f"❌ **Invalid Team Name/Code (`{raw_input}`).** No active matching registration found.", ephemeral=True)
            return

        # 4. 🔒 Automatically fetch the team's registered enroller and only grant
        #    update access if the requesting user IS that enroller. Knowing a
        #    team's name/code alone must never be enough to edit its roster.
        enroller_id = existing_team.get("discord_id", "")
        if not enroller_id or str(interaction.user.id) != str(enroller_id):
            await interaction.followup.send(
                f"❌ **Access Denied:** Only the registered enroller of **{existing_team.get('team_name')}** can update its details. "
                f"If you believe this is an error, please contact a tournament moderator.",
                ephemeral=True
            )
            return

        # 5. Success! Requesting user matches the team's enroller — prompt the preference selection view (Tickets vs DMs)
        from src.core.views.modals import UpdateChannelPreferenceView
        view = UpdateChannelPreferenceView(interaction, existing_team)
        await interaction.followup.send(
            f"📋 **Team Verified:** `{existing_team.get('team_name')}`\nHow would you like to complete the detail updates?", 
            view=view, 
            ephemeral=True
        )

class DeleteByCodeModal(discord.ui.Modal, title="🗑️ Verification: Enter Team Code"):
    team_code = discord.ui.TextInput(label="Enter Your Unique Team Code", required=True, min_length=6, max_length=6)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        import src.core.database.sheets as sheets
        import asyncio
        
        try:
            team = await asyncio.to_thread(sheets.db_find_team_by_query, self.team_code.value.strip())
            if not team:
                return await interaction.followup.send("❌ **Not Found:** No tournament records match that team code.", ephemeral=True)

            # 🔒 Only the team's registered enroller may delete it — knowing the
            # team code alone must never be enough, same rule as the Update flow.
            enroller_id = team.get("discord_id", "")
            if not enroller_id or str(interaction.user.id) != str(enroller_id):
                return await interaction.followup.send(
                    f"❌ **Access Denied:** Only the registered enroller of **{team.get('team_name')}** can delete it. "
                    f"If you believe this is an error, please contact a tournament moderator.",
                    ephemeral=True
                )

            await asyncio.to_thread(sheets.db_delete_team, int(team['discord_id']))
            await interaction.followup.send("🗑️ **Record Purged:** Registration entry successfully removed via Code verification.", ephemeral=True)
        except Exception:
            await interaction.followup.send("❌ **Error:** Could not process registration erasure.", ephemeral=True)

# ==================== UNIVERSAL CHAT UPDATE LOOP ====================
class UpdateConfirmationView(discord.ui.View):
    def __init__(self, timeout=120.0):
        super().__init__(timeout=timeout)
        self.value = None

    @discord.ui.button(label="Yes, Confirm Update", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_choice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.value = True
        self.stop()

    @discord.ui.button(label="No, Cancel", style=discord.ButtonStyle.danger, emoji="❌")
    async def cancel_choice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.value = False
        self.stop()


async def run_chat_update(interaction: discord.Interaction, target_channel: discord.Messageable, is_dm: bool, existing_team: dict):
    guild = interaction.guild
    user = interaction.user
    import src.core.database.sheets as sheets
    import asyncio
    import aiohttp
    import sys
    import traceback
    
    # 🛡️ INITIALIZE VALIDATORS AND ROSTER SESSION
    from src.core.views.validators import validate_email, validate_phone, RosterSession
    session = RosterSession(guild)
    
    def check(m): return m.author.id == user.id and m.channel.id == target_channel.id
    prefix_mention = "" if is_dm else f"{user.mention} "

    async def ask_update_question(prompt_text: str, current_value: str, validator_func=None, error_msg="❌ Invalid format."):
        while True:
            formatted_prompt = f"{prompt_text}\n*🔹 Current Value:* `{current_value}`\n*💡 (Type **skip** to keep current value, or **cancel** to abort)*"
            
            if sheets.bot.latency is None or sheets.bot.latency > 5.0 or sheets.bot.is_closed():
                raise ConnectionError
                
            try:
                question_sent_at = discord.utils.utcnow()
                await target_channel.send(f"{prefix_mention}{formatted_prompt}")
            except (discord.HTTPException, aiohttp.ClientError):
                raise ConnectionError
            
            try:
                while True:
                    msg = await sheets.bot.wait_for('message', check=check, timeout=180.0)
                    if msg.created_at < question_sent_at: 
                        continue
                    break

                val = msg.content.strip()
                if not val: 
                    continue
                
                # 🛡️ HANDLE 'SKIP' SAFELY
                if val.lower() == "skip":
                    # Register skipped existing values to the session memory to prevent cross-teammate duplicates
                    if validator_func and current_value and str(current_value).lower() not in ["none", "n/a"]:
                        try:
                            if asyncio.iscoroutinefunction(validator_func):
                                await validator_func(str(current_value))
                            else:
                                validator_func(str(current_value))
                        except Exception:
                            pass
                    return current_value

                # 🛡️ ALLOW EXPLICIT REMOVAL OF SUBS
                if val.lower() == "none" and "Sub" in prompt_text:
                    return "None"
                    
                if val.lower() == "cancel":
                    if is_dm:
                            await target_channel.send("❌ **You have cancelled the Updating process.**")
                    else:
                            countdown_msg = await target_channel.send(
                                f"❌ **You have cancelled the updating process!**\n"
                                f"⏳ *This ticket channel will be permanently deleted in 5 seconds...*"
                            )
                            for seconds_left in range(4, 0, -1):
                                await asyncio.sleep(1)
                                try:
                                    await countdown_msg.edit(
                                        content=f"❌ **You have cancelled the updating process!**\n"
                                                f"⏳ *This ticket channel will be permanently deleted in {seconds_left} seconds...*"
                                    )
                                except discord.HTTPException:
                                    break
                            await asyncio.sleep(1)
                            
                            try: await target_channel.delete()
                            except discord.HTTPException: pass
                    raise InterruptedError

                if validator_func:
                    res = await validator_func(val) if asyncio.iscoroutinefunction(validator_func) else validator_func(val)
                    if res is not None and res is not False:
                        return res
                    await target_channel.send(f"{prefix_mention}{error_msg}")
                else:
                    return val
            except asyncio.TimeoutError:
                raise TimeoutError
            except (discord.HTTPException, aiohttp.ClientError, discord.ConnectionClosed):
                raise ConnectionError                   

    try:
        await target_channel.send(f"{prefix_mention}✏️ **Welcome to the Team Detail Update Wizard!**")

        # 1. Prompt questions, now fully secured with Validator functions
        updated_team_name = await ask_update_question("📋 Enter your new **Team Name**:", existing_team.get('team_name', 'None'))
        updated_email = await ask_update_question("📧 Enter your new **Email Address**:", existing_team.get('email', 'None'), validate_email, "❌ Invalid email format.")
        updated_country = await ask_update_question("🌍 Enter your new **Country Name**:", existing_team.get('country', 'None'))
        updated_phone = await ask_update_question("📞 Enter your new **Phone Number**:", existing_team.get('phone', 'None'), validate_phone, "❌ Invalid format. Numbers only (Minimum 7 digits).")

        updated_igl_ign = await ask_update_question("✏️ Enter your new **IGL Ig-Name**:", existing_team.get('igl_ign', 'None'), session.check_ign, "❌ Invalid entry or IGN already registered.")
        updated_igl_uid = await ask_update_question("🔢 Enter your new **IGL UID**:", existing_team.get('igl_uid', 'None'), session.check_uid, "❌ UID must be in numbers(8-12 digits) and unique.")
        updated_igl_tag = await ask_update_question("🏷️ Enter your new **IGL Tag**:", existing_team.get('igl_tag', 'None'), _make_lock_checked_validator(session, existing_team.get('team_code')), "❌ Profile not found, account is a bot, already registered, or locked to another team.")

        updated_p2_ign = await ask_update_question("✏️ Enter your new **P2 Ig-Name**:", existing_team.get('p2_ign', 'None'), session.check_ign, "❌ Duplicate Error: This IGN matches another teammate.")
        updated_p2_uid = await ask_update_question("🔢 Enter your new **P2 UID**:", existing_team.get('p2_uid', 'None'), session.check_uid, "❌ Invalid format or UID matches another teammate.")
        updated_p2_tag = await ask_update_question("🏷️ Enter your new **P2 Tag**:", existing_team.get('p2_tag', 'None'), _make_lock_checked_validator(session, existing_team.get('team_code')), "❌ Profile not found, account is a bot, matches another teammate, or locked to another team.")

        updated_p3_ign = await ask_update_question("✏️ Enter your new **P3 Ig-Name**:", existing_team.get('p3_ign', 'None'), session.check_ign, "❌ Duplicate Error: This IGN matches another teammate.")
        updated_p3_uid = await ask_update_question("🔢 Enter your new **P3 UID**:", existing_team.get('p3_uid', 'None'), session.check_uid, "❌ Invalid format or UID matches another teammate.")
        updated_p3_tag = await ask_update_question("🏷️ Enter your new **P3 Tag**:", existing_team.get('p3_tag', 'None'), _make_lock_checked_validator(session, existing_team.get('team_code')), "❌ Profile not found, account is a bot, matches another teammate, or locked to another team.")

        updated_p4_ign = await ask_update_question("✏️ Enter your new **P4 Ig-Name**:", existing_team.get('p4_ign', 'None'), session.check_ign, "❌ Duplicate Error: This IGN matches another teammate.")
        updated_p4_uid = await ask_update_question("🔢 Enter your new **P4 UID**:", existing_team.get('p4_uid', 'None'), session.check_uid, "❌ Invalid format or UID matches another teammate.")
        updated_p4_tag = await ask_update_question("🏷️ Enter your new **P4 Tag**:", existing_team.get('p4_tag', 'None'), _make_lock_checked_validator(session, existing_team.get('team_code')), "❌ Profile not found, account is a bot, matches another teammate, or locked to another team.")

        updated_sub_ign = await ask_update_question("✏️ Enter your new **Sub Ig-Name**:", existing_team.get('sub_ign', 'None'), session.check_ign, "❌ Duplicate Error: This IGN matches another teammate.")
        updated_sub_uid = await ask_update_question("🔢 Enter your new **Sub UID**:", existing_team.get('sub_uid', 'None'), session.check_uid, "❌ Invalid format or UID matches another teammate.")
        updated_sub_tag = await ask_update_question("🏷️ Enter your new **Sub Tag**:", existing_team.get('sub_tag', 'None'), _make_lock_checked_validator(session, existing_team.get('team_code')), "❌ Profile not found, account is a bot, matches another teammate, or locked to another team.")

        # ------------------ ENROLLER SELECTION ------------------
        enroller_id = await ask_enroller_choice(
            target_channel, prefix_mention, user, session,
            {
                "igl": (updated_igl_ign, updated_igl_tag),
                "p2": (updated_p2_ign, updated_p2_tag),
                "p3": (updated_p3_ign, updated_p3_tag),
                "p4": (updated_p4_ign, updated_p4_tag),
            }
        )

        team_code_fixed = str(existing_team.get('team_code') or existing_team.get('Team ID') or '')
        if not team_code_fixed:
            raise ValueError("Could not lock target verification Team ID reference.")

        # 2. ✅ Save complete temporary backup via dictionary payload
        staging_payload = {
            "team_name": updated_team_name, "email": updated_email,
            "country": updated_country, "phone": updated_phone,
            "igl_ign": updated_igl_ign, "igl_uid": updated_igl_uid, "igl_tag": updated_igl_tag,
            "p2_ign": updated_p2_ign, "p2_uid": updated_p2_uid, "p2_tag": updated_p2_tag,
            "p3_ign": updated_p3_ign, "p3_uid": updated_p3_uid, "p3_tag": updated_p3_tag,
            "p4_ign": updated_p4_ign, "p4_uid": updated_p4_uid, "p4_tag": updated_p4_tag,
            "sub_ign": updated_sub_ign, "sub_uid": updated_sub_uid, "sub_tag": updated_sub_tag
        }

        await asyncio.to_thread(sheets.db_log_staging_update, team_code_fixed, staging_payload)
        await asyncio.sleep(1.0)

        # 3. Take explicit user confirmation consent
        confirm_view = UpdateConfirmationView()
        await target_channel.send(
            f"{prefix_mention}⚠️ **Final Step:** Confirmation\n🔐 **New Enroller:** <@{enroller_id}>\nDo you want to overwrite your active team roster data with these values now?", 
            view=confirm_view
        )
        
        await confirm_view.wait()
        
        if confirm_view.value is not True:
            await target_channel.send("❌ **Update aborted.** Ticket is going to be deleted in 5sec.")
            if not is_dm:
                await asyncio.sleep(5)
                try: await target_channel.delete()
                except discord.HTTPException: pass
            return

        status_msg = await target_channel.send("🔄 **Processing team edits... Please wait.**")

        # 5. Overwrite Row using native cell values coordinates safely
        main_worksheet = sheets.active_sheet
        cell = await asyncio.to_thread(main_worksheet.find, team_code_fixed)
        if not cell:
            raise ValueError(f"Could not locate active registration matching team code: {team_code_fixed}")
            
        target_row_index = cell.row

        # Gathers all 20 updated parameters into a clean collection array
        # (19 roster fields + the chosen Enroller's Discord ID in column U)
        update_payload = [
            str(updated_team_name), str(updated_email), str(updated_country), str(updated_phone),
            str(updated_igl_ign), str(updated_igl_uid), str(updated_igl_tag),
            str(updated_p2_ign), str(updated_p2_uid), str(updated_p2_tag),
            str(updated_p3_ign), str(updated_p3_uid), str(updated_p3_tag),
            str(updated_p4_ign), str(updated_p4_uid), str(updated_p4_tag),
            str(updated_sub_ign), str(updated_sub_uid), str(updated_sub_tag),
            str(enroller_id)
        ]

        # 🚀 Range widened to U so the newly-chosen Enroller (column U) is saved too
        range_label = f"B{target_row_index}:U{target_row_index}"
        await asyncio.to_thread(main_worksheet.update, range_label, [update_payload])

        # Keep the per-slot Discord IDs (W..AA) in sync too, so the
        # enrollment-lock check / Manage / Leave / Transfer-Enroller features
        # keep working correctly against updated teammates.
        updated_slot_ids = [
            str(_resolve_slot_id(session, updated_igl_tag) or ""),
            str(_resolve_slot_id(session, updated_p2_tag) or ""),
            str(_resolve_slot_id(session, updated_p3_tag) or ""),
            str(_resolve_slot_id(session, updated_p4_tag) or ""),
            str(_resolve_slot_id(session, updated_sub_tag) or ""),
        ]
        await asyncio.to_thread(main_worksheet.update, f"W{target_row_index}:AA{target_row_index}", [updated_slot_ids])

        # 6. Complete! Print success notification and sweep channel context
        try: 
            await status_msg.delete()
        except Exception: 
            pass
            
        await target_channel.send(f"{prefix_mention}✅ **Success! Your team data has been updated successfully.**\n*This ticket will be automatically deleted in 10 seconds...*")
        
        if not is_dm: 
            await asyncio.sleep(7)
            try: await target_channel.delete()
            except discord.HTTPException: pass

    except InterruptedError:
        return 
    except ConnectionError:
        try: await target_channel.send(f"⚠️ **__Bot API disconnected. Updates halted__**\n||{prefix_mention}||\nPLEASE TRY AGAIN LATER")
        except Exception: pass
    except Exception as e:
        print("\n" + "="*50)
        print(f"🚨 CRITICAL UPDATE LOOP ERROR: {str(e)}")
        traceback.print_exc(file=sys.stdout)
        print("="*50 + "\n")
        try: await target_channel.send(f"❌ **An unexpected system verification error occurred. Contact Admin**")
        except Exception: pass
        return

class UpdateChannelPreferenceView(discord.ui.View):
    def __init__(self, original_interaction: discord.Interaction, team_data: dict):
        super().__init__(timeout=60.0)
        self.original_interaction = original_interaction
        self.team_data = team_data

    @discord.ui.button(label="Open Update Ticket", style=discord.ButtonStyle.primary, emoji="🎫")
    async def ticket_choice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user
        
        from src.core.views.modals import CATEGORY_NAME
        category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)
        }
        
        try:
            update_channel = await guild.create_text_channel(
                name=f"update-{self.team_data.get('team_code', 'details')}",
                overwrites=overwrites,
                category=category
            )
            await interaction.edit_original_response(content=f"🎫 **Update Ticket Opened!** Go to: {update_channel.mention}", view=None)
            await run_chat_update(self.original_interaction, update_channel, is_dm=False, existing_team=self.team_data)
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Failed to build ticket: {e}", view=None)

    @discord.ui.button(label="Update in DMs", style=discord.ButtonStyle.success, emoji="📥")
    async def dm_choice(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            dm_channel = await self.original_interaction.user.create_dm()
            await interaction.edit_original_response(content="📥 **Check your DMs!** Update session started.", view=None)
            await run_chat_update(self.original_interaction, dm_channel, is_dm=True, existing_team=self.team_data)
        except discord.Forbidden:
            await interaction.edit_original_response(content="❌ Your DMs are locked.", view=None)

# ==================== COMPONENT VIEW PANEL (ALL 5 BUTTONS) ====================
# ==================== MANAGE / TRANSFER ENROLLER / LEAVE TEAM (VIEW-ONLY PANEL) ====================
def _build_team_manage_embed(team: dict) -> discord.Embed:
    embed = discord.Embed(title=f"📋 {team.get('team_name', 'Team')} — Team Overview", color=0x3498DB)
    embed.add_field(name="🆔 Team Code", value=f"`{team.get('team_code', '—')}`", inline=True)
    embed.add_field(name="🌍 Country", value=team.get('country') or "—", inline=True)
    enroller_id = team.get("discord_id")
    embed.add_field(name="🔐 Enroller", value=f"<@{enroller_id}>" if enroller_id else "—", inline=True)
    for slot in SLOT_ORDER:
        ign = (team.get(f"{slot}_ign", "") or "").strip()
        tag = (team.get(f"{slot}_tag", "") or "").strip()
        slot_id = team.get(f"{slot}_id", "")
        if not ign or ign.lower() == "none" or tag.upper() == "VACANT":
            value = "*Vacant*"
        else:
            value = f"**{ign}**\n{f'<@{slot_id}>' if slot_id else (tag or '—')}"
        embed.add_field(name=SLOT_LABELS[slot], value=value, inline=True)
    embed.set_footer(text="HyperX Engine • View-only team overview — visible to every current teammate")
    return embed


class TransferEnrollerSelect(discord.ui.Select):
    def __init__(self, user_id: int, team: dict, current_slot: str):
        options = []
        for slot in SLOT_ORDER:
            if slot == current_slot:
                continue
            ign = (team.get(f"{slot}_ign", "") or "").strip()
            tag = (team.get(f"{slot}_tag", "") or "").strip()
            if not ign or ign.lower() == "none" or tag.upper() == "VACANT":
                continue
            options.append(discord.SelectOption(
                label=f"{SLOT_LABELS[slot]} — {ign}"[:100],
                description=(tag or "—")[:100],
                value=slot
            ))
        if not options:
            options.append(discord.SelectOption(label="No eligible teammates", value="__none__"))
        super().__init__(placeholder="Choose the new Enroller...", min_values=1, max_values=1, options=options)
        self._user_id = user_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self._user_id:
            return await interaction.response.send_message("❌ This isn't your panel.", ephemeral=True)
        if self.values[0] == "__none__":
            return await interaction.response.send_message("❌ No eligible teammates to transfer to.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        import src.core.database.sheets as sheets
        try:
            team_name, old_id, new_id, new_tag = await asyncio.to_thread(
                sheets.db_transfer_enroller, interaction.user.id, self.values[0]
            )
        except PermissionError:
            return await interaction.followup.send("❌ **Access Denied:** Only the current Enroller can transfer this role.", ephemeral=True)
        except ValueError:
            return await interaction.followup.send("❌ Could not transfer — that slot is empty, invalid, or you're not on an active team.", ephemeral=True)
        await interaction.followup.send(f"✅ **Enroller role transferred to** <@{new_id}>.", ephemeral=True)
        await _post_info_stats(
            interaction.guild,
            f"🔐 **Enroller Changed:** `{team_name}` — <@{old_id}> ({interaction.user}) transferred the "
            f"Enroller role to <@{new_id}> (`{new_tag}`)."
        )


class TransferEnrollerView(discord.ui.View):
    def __init__(self, user_id: int, team: dict, current_slot: str):
        super().__init__(timeout=120.0)
        self.add_item(TransferEnrollerSelect(user_id, team, current_slot))


class LeaveTeamConfirmView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60.0)
        self.user_id = user_id
        self.value = None

    @discord.ui.button(label="Yes, Leave Team", style=discord.ButtonStyle.danger, emoji="🚪")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This isn't your panel.", ephemeral=True)
        await interaction.response.defer()
        self.value = True
        self.stop()

    @discord.ui.button(label="No, Stay", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This isn't your panel.", ephemeral=True)
        await interaction.response.defer()
        self.value = False
        self.stop()


class TeamManageView(discord.ui.View):
    """View-only team panel shown to ANY current member of the team, including
    the Substitute. 'Transfer Enroller' is visible to every teammate but only
    actually works for whoever currently holds the Enroller role. 'Leave Team'
    always removes ONLY the clicking member's own slot, never the whole roster."""
    def __init__(self, user_id: int, team: dict, slot: str):
        super().__init__(timeout=180.0)
        self.user_id = user_id
        self.team = team
        self.slot = slot

    @discord.ui.button(label="Transfer Enroller", style=discord.ButtonStyle.primary, emoji="🔐")
    async def transfer_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This isn't your panel.", ephemeral=True)
        if str(self.team.get("discord_id", "")).strip() != str(self.user_id):
            return await interaction.response.send_message(
                "❌ **Access Denied:** Only the current Enroller can transfer this role.", ephemeral=True
            )
        await interaction.response.send_message(
            "Choose who should become the new Enroller:",
            view=TransferEnrollerView(self.user_id, self.team, self.slot), ephemeral=True
        )

    @discord.ui.button(label="Leave Team", style=discord.ButtonStyle.danger, emoji="🚪")
    async def leave_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ This isn't your panel.", ephemeral=True)
        confirm_view = LeaveTeamConfirmView(self.user_id)
        await interaction.response.send_message(
            f"⚠️ **Are you sure you want to leave `{self.team.get('team_name')}`?**\n"
            f"This removes only YOUR own slot ({SLOT_LABELS.get(self.slot, self.slot)}) — the rest of the roster is untouched.",
            view=confirm_view, ephemeral=True
        )
        await confirm_view.wait()
        if confirm_view.value is not True:
            return
        import src.core.database.sheets as sheets
        try:
            result = await asyncio.to_thread(sheets.db_leave_team, self.user_id)
        except ValueError:
            return await interaction.followup.send("❌ You're no longer on a team.", ephemeral=True)
        await interaction.followup.send("🚪 **You have left the team.**", ephemeral=True)
        left_label = SLOT_LABELS.get(result["left_slot"], result["left_slot"])
        if result["disbanded"]:
            await _post_info_stats(
                interaction.guild,
                f"🚪 **Player Left:** {result['left_ign']} ({left_label}) left `{result['team_name']}`. "
                f"No members remain on the roster — the team has been disbanded."
            )
        else:
            note = ""
            if result.get("new_enroller_id"):
                note = f" The Enroller role has automatically been given to <@{result['new_enroller_id']}>."
            await _post_info_stats(
                interaction.guild,
                f"🚪 **Player Left:** {result['left_ign']} ({left_label}) left `{result['team_name']}`.{note}"
            )


class PersistentRegistrationView(discord.ui.View):
    def __init__(self):
        # Keeps your structural view permanent
        super().__init__(timeout=None)

    # 1. Your completely untouched, functional enrollment button logic
    @discord.ui.button(label="Enroll Team", style=discord.ButtonStyle.success, emoji="📝", custom_id="perm_enroll")
    async def enroll_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        import src.core.database.sheets as sheets
        import asyncio
        
        banned, ban_msg = sheets.db_is_team_banned(str(interaction.user.id))
        if banned: 
            return await interaction.followup.send(ban_msg, ephemeral=True)

        # 🔒 Strict enrollment lock: none of the 5 roster slots (IGL/P2/P3/P4/Sub)
        # can belong to more than one team at a time. This checks ALL of them,
        # not just "are you already an Enroller", so a plain teammate can't
        # start a second team either — they have to Leave (Manage -> Leave
        # Team) first.
        membership = await asyncio.to_thread(sheets.db_get_team_membership, interaction.user.id)
        if membership:
            team, slot = membership
            return await interaction.followup.send(
                f"❌ **You're already enrolled** on `{team['team_name']}` as **{SLOT_LABELS.get(slot, slot)}**.\n"
                f"Use **Manage → Leave Team** first if you want to join or create a different team.",
                ephemeral=True
            )
        
        await interaction.followup.send(content="Choose setup space preference:", view=ChannelPreferenceView(interaction), ephemeral=True)

    # 🛡️ VIEW-ONLY team panel for every current teammate (incl. Substitute):
    # see the full roster, Transfer the Enroller role, or Leave Team.
    @discord.ui.button(label="Manage", style=discord.ButtonStyle.secondary, emoji="🛡️", custom_id="perm_manage")
    async def manage_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        import src.core.database.sheets as sheets
        import asyncio

        membership = await asyncio.to_thread(sheets.db_get_team_membership, interaction.user.id)
        if not membership:
            return await interaction.followup.send(
                "❌ **Access Denied:** You're not currently on any team's active roster.", ephemeral=True
            )
        team, slot = membership
        embed = _build_team_manage_embed(team)
        await interaction.followup.send(embed=embed, view=TeamManageView(interaction.user.id, team, slot), ephemeral=True)

    # # 2. Add this safe, custom-indexed update component right below it!
    @discord.ui.button(label="Update", style=discord.ButtonStyle.primary, emoji="✏️", custom_id="perm_update_details")
    async def update_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        import src.core.database.sheets as sheets
        import asyncio
        
        # 1. Directly check if the user is recognized as this team's enroller via their Discord ID
        existing_team = await asyncio.to_thread(sheets.db_find_team_by_user_id, interaction.user.id)
        
        if existing_team:
            # 2. If recognized, bypass the code input modal and go straight to the ticket/DM preference view
            from src.core.views.modals import UpdateChannelPreferenceView
            view = UpdateChannelPreferenceView(interaction, existing_team)
            
            await interaction.response.send_message(
                content=f"📋 **Team Verified:** `{existing_team.get('team_name')}`\nHow would you like to complete the detail updates?", 
                view=view, 
                ephemeral=True
            )
        else:
            # 3. If not recognized, fallback to the manual code input modal
            from src.core.views.modals import UpdateByCodeModal
            await interaction.response.send_modal(UpdateByCodeModal())


    # @discord.ui.button(label="Update", style=discord.ButtonStyle.primary, emoji="✏️", custom_id="perm_update_details")
    # async def update_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
    #     # Open your new instant verification modal window safely within milliseconds
    #     from src.core.views.modals import UpdateByCodeModal
    #     await interaction.response.send_modal(UpdateByCodeModal())

    # @discord.ui.button(label="Update", style=discord.ButtonStyle.primary, emoji="✏️", custom_id="perm_update")
    # async def update_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
    #     import src.core.database.sheets as sheets
    #     row = sheets.db_find_row_by_discord_id(interaction.user.id)
    #     if not row:
    #         return await interaction.response.send_message("❌ Access Denied: You have not registered a team under your account.", ephemeral=True)
        
    #     await interaction.response.send_modal(UpdateModal())
    # \n\n\n\n\n\n\n
    # @discord.ui.button(label="Update", style=discord.ButtonStyle.primary, emoji="✏️", custom_id="perm_update")
    # async def update_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
    #     import src.core.database.sheets as sheets
        
    #     # 1. First step: Search by Discord User ID (No defer here so we can send a Modal directly)
    #     row = await asyncio.to_thread(sheets.db_find_row_by_discord_id, interaction.user.id)
        
    #     if row:
    #         # Found by User ID -> Send them directly to the Core Details form
    #         await interaction.response.send_modal(UpdateModal())
    #     else:
    #         # 2. Fallback step: Not found by User ID, ask for Team Code instead
    #         await interaction.response.send_modal(UpdateByCodeModal())
    # \n\n\n\n\n\n\n\n
    # @discord.ui.button(label='Update', style=discord.ButtonStyle.primary, emoji='✏️')
    # async def update_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
    #     # Open the modal instantly within milliseconds. No lookups allowed here!
    #     await interaction.response.send_modal(UpdateByCodeModal())

# BUTTON 3: DELETE (WITH SECURE YES/NO CONFIRMATION) INTERACTIVE DELETE CONFIRMATION BUTTONS
    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="perm_delete")
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        import src.core.database.sheets as sheets
        import asyncio
        
        # 🟢 Define the confirmation view locally right here to fix the NameError
        class DeleteConfirmationView(discord.ui.View):
            def __init__(self, timeout=60.0):
                super().__init__(timeout=timeout)
                self.value = None

            @discord.ui.button(label="Yes, Delete My Team", style=discord.ButtonStyle.danger, emoji="🗑️")
            async def confirm_delete(self, inter: discord.Interaction, btn: discord.ui.Button):
                await inter.response.defer()
                self.value = True
                self.stop()

            @discord.ui.button(label="No, Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
            async def cancel_delete(self, inter: discord.Interaction, btn: discord.ui.Button):
                await inter.response.defer()
                self.value = False
                self.stop()

        # 1. Verify if the user has an active registration record
        row_idx = await asyncio.to_thread(sheets.db_find_row_by_discord_id, interaction.user.id)
        
        if row_idx:
            # Generate the interactive safeguarding choice panel
            confirm_view = DeleteConfirmationView()
            
            await interaction.response.send_message(
                content=f"⚠️ {interaction.user.mention} **Are you absolutely sure you want to delete your team?**\nThis will completely remove your roster from active records!",
                view=confirm_view,
                ephemeral=True
            )
            
            await confirm_view.wait()
            
            if confirm_view.value is True:
                try:
                    # 💾 RECORD ARCHIVE: Extract active column snapshot data array
                    raw_row_data = await asyncio.to_thread(sheets.active_sheet.row_values, row_idx)
                    
                    team_payload = {
                        "team_code": raw_row_data[0] if len(raw_row_data) > 0 else "N/A",
                        "team_name": raw_row_data[1] if len(raw_row_data) > 1 else "N/A",
                        "email":     raw_row_data[2] if len(raw_row_data) > 2 else "N/A",
                        "country":   raw_row_data[3] if len(raw_row_data) > 3 else "N/A",
                        "phone":     raw_row_data[4] if len(raw_row_data) > 4 else "N/A",
                        "igl_ign":   raw_row_data[5] if len(raw_row_data) > 5 else "N/A",
                        "igl_uid":   raw_row_data[6] if len(raw_row_data) > 6 else "N/A",
                        "igl_tag":   raw_row_data[7] if len(raw_row_data) > 7 else "N/A",
                        "p2_ign":    raw_row_data[8] if len(raw_row_data) > 8 else "N/A",
                        "p2_uid":    raw_row_data[9] if len(raw_row_data) > 9 else "N/A",
                        "p2_tag":    raw_row_data[10] if len(raw_row_data) > 10 else "N/A",
                        "p3_ign":    raw_row_data[11] if len(raw_row_data) > 11 else "N/A",
                        "p3_uid":    raw_row_data[12] if len(raw_row_data) > 12 else "N/A",
                        "p3_tag":    raw_row_data[13] if len(raw_row_data) > 13 else "N/A",
                        "p4_ign":    raw_row_data[14] if len(raw_row_data) > 14 else "N/A",
                        "p4_uid":    raw_row_data[15] if len(raw_row_data) > 15 else "N/A",
                        "p4_tag":    raw_row_data[16] if len(raw_row_data) > 16 else "N/A",
                        "sub_ign":   raw_row_data[17] if len(raw_row_data) > 17 else "N/A",
                        "sub_uid":   raw_row_data[18] if len(raw_row_data) > 18 else "N/A",
                        "sub_tag":   raw_row_data[19] if len(raw_row_data) > 19 else "N/A"
                    }
                    
                    # Log the formatted snapshot payload straight to deleted_teams
                    await asyncio.to_thread(sheets.db_log_deleted_team, team_payload, interaction.user.id)
                except Exception as archive_err:
                    print(f"[WARNING] Archive backing step failed: {archive_err}")

                # 2. 🗑️ CLEAR FROM ACTIVE TRACKING WORKSPACE SHEET LAYOUT
                await asyncio.to_thread(sheets.db_delete_team, interaction.user.id)
                await interaction.edit_original_response(content="🗑️ **Record Purged:** Your Team Data has been removed", view=None)
            else:
                await interaction.edit_original_response(content="❌ **Deletion Cancelled.** Your active Team Data remains safe.", view=None)
        else:
            from src.core.views.modals import DeleteByCodeModal
            await interaction.response.send_modal(DeleteByCodeModal())

    # BUTTON 4: FETCH TEAM DATA
    @discord.ui.button(label="Fetch Team Data", style=discord.ButtonStyle.secondary, emoji="🔍", custom_id="perm_fetch")
    async def fetch_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        from src.core.views.modals import FetchTeamModal
        await interaction.response.send_modal(FetchTeamModal())

    # BUTTON 5: BAN SYSTEM
    @discord.ui.button(label="Ban", style=discord.ButtonStyle.secondary, emoji="🔴", custom_id="perm_tempban")
    async def tempban_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not check_is_mod(interaction.user):
            return await interaction.response.send_message(f"❌ **Access Denied:** Only tournament moderators can perform bans.", ephemeral=True)
            
        from src.core.views.modals import TempBanModal
        await interaction.response.send_modal(TempBanModal())