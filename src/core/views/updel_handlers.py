import asyncio
import discord
from src.database.sheets import db_save_team, db_delete_team, db_log_deleted_team

CATEGORY_NAME = "🏆 Enrollment Tickets"

# ... rest of your run_chat_modification, initiate_update_ticket, and execute_team_deletion code stays exactly the same ...

async def run_chat_modification(interaction: discord.Interaction, target_channel: discord.Messageable, existing_team: dict):
    user = interaction.user
    def check(m): return m.author.id == user.id and m.channel.id == target_channel.id
    data = {}
    
    await target_channel.send(
        f"🔄 **Team Roster Modification Wizard Online!**\n"
        f"Modifying Registration Code: `{existing_team['team_code']}` (Current Team: **{existing_team['team_name']}**)\n"
        f"-------------------------------------------------------"
    )

    async def ask_question(prompt_text: str, default_val: str):
        while True:
            formatted = f"📝 {prompt_text}\n*Current Value:* `{default_val}`\n*(Type **skip** to keep current, or **cancel** to abort)*"
            await target_channel.send(formatted)
            try:
                msg = await interaction.client.wait_for('message', check=check, timeout=120.0)
                val = msg.content.strip()
                if not val: 
                    continue
                if val.lower() == "cancel":
                    await target_channel.send("❌ **Modification process cancelled.** Closing ticket...")
                    await asyncio.sleep(5)
                    await target_channel.delete()
                    raise InterruptedError
                if val.lower() == "skip":
                    return default_val
                return val
            except asyncio.TimeoutError:
                await target_channel.delete()
                raise TimeoutError

    try:
        data['team_name'] = await ask_question("Enter updated **Team Name**:", existing_team['team_name'])
        data['email'] = await ask_question("Enter updated **Email Address**:", existing_team['email'])
        data['country'] = await ask_question("Enter updated **Country Name**:", existing_team['country'])
        data['phone'] = await ask_question("Enter updated **Phone Number**:", existing_team['phone'])
        
        data['igl_ign'] = await ask_question("Enter updated **IGL In-Game Name (IGN)**:", existing_team['igl_ign'])
        data['igl_uid'] = await ask_question("Enter updated **IGL Game UID**:", existing_team['igl_uid'])
        data['igl_tag'] = await ask_question("Enter updated **IGL Discord Handle**:", existing_team['igl_tag'])

        for p in ['p2', 'p3', 'p4', 'sub']:
            data[f'{p}_ign'] = await ask_question(f"Enter updated **{p.upper()} Player IGN**:", existing_team[f'{p}_ign'])
            data[f'{p}_uid'] = await ask_question(f"Enter updated **{p.upper()} Player UID**:", existing_team[f'{p}_uid'])
            data[f'{p}_tag'] = await ask_question(f"Enter updated **{p.upper()} Player Discord**:", existing_team[f'{p}_tag'])

        await asyncio.to_thread(db_save_team, existing_team['team_code'], user.id, data)
        await target_channel.send("✅ **Roster updates synced to Google Sheet successfully!** This room will close in 5s.")
        await asyncio.sleep(5)
        await target_channel.delete()
    except Exception:
        return

async def initiate_update_ticket(interaction: discord.Interaction, existing_team: dict):
    guild = interaction.guild
    user = interaction.user
    category = discord.utils.get(guild.categories, name=CATEGORY_NAME) or await guild.create_category(name=CATEGORY_NAME)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        user: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, read_message_history=True)
    }
    
    chan = await guild.create_text_channel(name=f"modify-{existing_team['team_code']}", overwrites=overwrites, category=category)
    await interaction.followup.send(f"🎫 **Modification Room Ready!** Please click here: {chan.mention}", ephemeral=True)
    await run_chat_modification(interaction, chan, existing_team)

async def execute_team_deletion(interaction: discord.Interaction, existing_team: dict):
    # Pass metrics to sheets_backup module
    backup_logged = await asyncio.to_thread(db_log_deleted_team, existing_team, interaction.user.id)
    if not backup_logged:
        return await interaction.followup.send("⚠️ **Database Backup Error:** Failed to log entry to backup sheet. Action aborted.", ephemeral=True)
        
    # Purge from sheets_active module
    await asyncio.to_thread(db_delete_team, interaction.user.id)
    await interaction.followup.send("🗑️ **Roster Purged Successfully!** Removed from active sheet and archived securely.", ephemeral=True)