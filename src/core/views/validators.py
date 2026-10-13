import re
import discord

COUNTRY_CODES = {
    "india": "+91", "united states": "+1", "usa": "+1", "united kingdom": "+44", "uk": "+44",
    "canada": "+1", "australia": "+61", "germany": "+49", "france": "+33", "singapore": "+65",
    "malaysia": "+60", "indonesia": "+62", "philippines": "+63", "pakistan": "+92", "bangladesh": "+880",
    "nepal": "+977", "uae": "+971", "saudi arabia": "+966", "brazil": "+55", "south africa": "+27"
}

def validate_email(email_input: str) -> str | None:
    cleaned = email_input.strip()
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    if re.match(pattern, cleaned):
        return cleaned
    return None

def validate_phone(phone_input: str) -> str | None:
    cleaned = phone_input.strip()
    if cleaned.isdigit() and len(cleaned) >= 7:
        return cleaned
    return None

def is_valid_uid(uid: str) -> bool:
    cleaned = uid.strip()
    return cleaned.isdigit() and 8 <= len(cleaned) <= 12

async def parse_and_validate_member(guild: discord.Guild, user_input: str):
    cleaned = user_input.strip()
    if cleaned.startswith("<@") and cleaned.endswith(">"):
        cleaned = cleaned.replace("<@", "").replace(">", "").replace("!", "")
    
    member = None
    if cleaned.isdigit():
        if guild:
            try: 
                member = await guild.fetch_member(int(cleaned))
            except discord.NotFound: 
                return None
    else:
        if guild:
            member = guild.get_member_named(cleaned)
        
    if member and member.bot:
        return None
        
    return member

class RosterSession:
    """Isolates active session state memory to cross-validate team constraints."""
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.registered_igns = set()
        self.registered_uids = set()
        self.registered_tags = set()
        # Keyed by the lowercased tag string (e.g. "username#1234"), so a later
        # "who should be the Enroller?" step can resolve a chosen tag back to
        # the actual discord.Member object (and therefore their Discord ID).
        self.registered_members = {}

    def check_ign(self, ign_input: str) -> str | None:
        cleaned = ign_input.strip()
        if not cleaned or cleaned.lower() in self.registered_igns:
            return None
        self.registered_igns.add(cleaned.lower())
        return cleaned

    def check_uid(self, uid_input: str) -> str | None:
        cleaned = uid_input.strip()
        if not is_valid_uid(cleaned) or cleaned in self.registered_uids:
            return None
        self.registered_uids.add(cleaned)
        return cleaned

    async def check_discord_tag(self, tag_input: str) -> str | None:
        member = await parse_and_validate_member(self.guild, tag_input)
        if not member:
            return None
        
        member_str = str(member)
        if member_str.lower() in self.registered_tags:
            return None
            
        self.registered_tags.add(member_str.lower())
        self.registered_members[member_str.lower()] = member
        return member_str