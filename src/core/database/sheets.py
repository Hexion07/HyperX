from __future__ import annotations

import os
import time
import datetime
import gspread
from google.oauth2.service_account import Credentials

# Unified runtime environment handles
active_sheet = None
backup_sheet = None
staging_sheet = None  # ✅ Staging area configuration added
bot = None  # Populated dynamically by main.py upon ready state transitions

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# ==================== MULTI-USER SAFETY: READ CACHE + RETRY ====================
# A burst of concurrent users (registering/updating/looking up at the same time)
# used to trigger a fresh get_all_values() call PER lookup. That's slow and risks
# hitting Google Sheets' API rate limit (429 errors), which was previously
# swallowed by a bare `except Exception: return None` and misread as "not found".
_ROW_CACHE = {"rows": None, "ts": 0.0}
CACHE_TTL_SECONDS = 6  # short enough that writes still show up quickly to other users

def invalidate_sheet_cache():
    """Call this immediately after any write (save/update/delete) so the next
    read reflects the change instead of serving stale cached rows."""
    _ROW_CACHE["rows"] = None
    _ROW_CACHE["ts"] = 0.0

def _with_retry(func, *args, max_retries=4, base_delay=1.5, **kwargs):
    """Runs a gspread call, retrying with exponential backoff on rate-limit (429)
    errors instead of letting them bubble up and get misread as 'not found'."""
    last_err = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            last_err = e
            status = None
            try:
                status = e.response.status_code
            except Exception:
                pass
            if status == 429 and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"⚠️ Sheets API rate-limited (429). Retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})...")
                time.sleep(delay)
                continue
            raise
    raise last_err

def _get_cached_rows(force_refresh: bool = False):
    """Returns active_sheet's rows, reusing a short-lived cache so that several
    users triggering lookups within the same few seconds share one API call
    instead of each firing their own get_all_values()."""
    if not active_sheet:
        return []
    now = time.monotonic()
    stale = (now - _ROW_CACHE["ts"]) > CACHE_TTL_SECONDS
    if force_refresh or _ROW_CACHE["rows"] is None or stale:
        _ROW_CACHE["rows"] = _with_retry(active_sheet.get_all_values)
        _ROW_CACHE["ts"] = now
    return _ROW_CACHE["rows"]

# ==================== ROSTER SLOT LAYOUT (0-indexed columns on `active_teams`) ====================
# A..T (0-19) = core roster fields, U (20) = Enroller Discord ID — unchanged,
# see _row_enroller_id below. Everything from V (21) onward is APPENDED (never
# inserted) for the enrollment-lock / Manage / Leave / Transfer-Enroller
# features, so any code still reading columns 0-20 keeps working untouched.
SLOT_ORDER = ["igl", "p2", "p3", "p4", "sub"]
SLOT_LABELS = {"igl": "IGL", "p2": "Player 2", "p3": "Player 3", "p4": "Player 4", "sub": "Substitute"}
# slot -> (ign_idx, uid_idx, tag_idx, discord_id_idx)
SLOT_FIELD_INDEX = {
    "igl": (5, 6, 7, 22),    # W
    "p2":  (8, 9, 10, 23),   # X
    "p3":  (11, 12, 13, 24), # Y
    "p4":  (14, 15, 16, 25), # Z
    "sub": (17, 18, 19, 26), # AA
}
ACTIVE_TEAMS_WIDTH = 27  # A..AA
_ACTIVE_EXTRA_HEADERS = ["Exact Date & Time", "IGL Discord ID", "P2 Discord ID", "P3 Discord ID", "P4 Discord ID", "Sub Discord ID"]
VACANT = "VACANT"

def _col_letter(idx0: int) -> str:
    """Converts a 0-based column index to its spreadsheet letter (0->A, 26->AA)."""
    idx0 += 1
    letters = ""
    while idx0 > 0:
        idx0, rem = divmod(idx0 - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

def _ensure_active_headers():
    """Self-heals the header row on `active_teams` so the trailing columns
    (V..AA) always exist with readable labels, without touching whatever a
    moderator already has set up by hand in A..U."""
    if not active_sheet:
        return
    try:
        header_row = _with_retry(active_sheet.row_values, 1)
        if len(header_row) >= ACTIVE_TEAMS_WIDTH:
            return
        base = header_row[:21] + [""] * max(0, 21 - len(header_row))
        _with_retry(active_sheet.update, range_name="A1:AA1", values=[base + _ACTIVE_EXTRA_HEADERS])
    except Exception as e:
        print(f"⚠️ Could not self-heal active_teams headers: {e}")

# Pleasant, distinguishable palette. Which color a given calendar day gets is
# deterministic (based on the date's ordinal number), so enrollments made on
# the same day always share a color and this stays consistent across bot
# restarts without needing to persist a "day -> color" map anywhere.
_DAY_COLOR_PALETTE = [
    {"red": 0.80, "green": 0.90, "blue": 1.00},  # light blue
    {"red": 0.85, "green": 1.00, "blue": 0.85},  # light green
    {"red": 1.00, "green": 0.90, "blue": 0.75},  # light orange
    {"red": 1.00, "green": 0.85, "blue": 0.90},  # light pink
    {"red": 0.90, "green": 0.85, "blue": 1.00},  # light purple
    {"red": 1.00, "green": 1.00, "blue": 0.75},  # light yellow
    {"red": 0.80, "green": 1.00, "blue": 0.95},  # light teal
    {"red": 0.93, "green": 0.80, "blue": 0.80},  # light salmon
]

def _color_for_date(d: datetime.date) -> dict:
    return _DAY_COLOR_PALETTE[d.toordinal() % len(_DAY_COLOR_PALETTE)]

def _row_to_team_dict(row: list, row_index: int) -> dict:
    padded = row + [""] * (ACTIVE_TEAMS_WIDTH - len(row)) if len(row) < ACTIVE_TEAMS_WIDTH else row
    return {
        "row_index": row_index,
        "team_code": padded[0], "team_name": padded[1], "email": padded[2], "country": padded[3], "phone": padded[4],
        "igl_ign": padded[5], "igl_uid": padded[6], "igl_tag": padded[7],
        "p2_ign": padded[8], "p2_uid": padded[9], "p2_tag": padded[10],
        "p3_ign": padded[11], "p3_uid": padded[12], "p3_tag": padded[13],
        "p4_ign": padded[14], "p4_uid": padded[15], "p4_tag": padded[16],
        "sub_ign": padded[17], "sub_uid": padded[18], "sub_tag": padded[19],
        "discord_id": _row_enroller_id(row),
        "timestamp": padded[21],
        "igl_id": padded[22], "p2_id": padded[23], "p3_id": padded[24], "p4_id": padded[25], "sub_id": padded[26],
    }

def db_get_team_membership(discord_id: int):
    """Returns (team_dict, slot_key) if this Discord ID currently occupies ANY
    of the 5 roster slots (IGL/P2/P3/P4/Sub) on any active team — regardless of
    whether they're the Enroller. This is what backs the strict 'you must
    leave before joining/creating another team' rule."""
    if not active_sheet:
        return None
    search_id = str(discord_id).strip()
    try:
        all_rows = _get_cached_rows()
    except Exception as e:
        print(f"⚠️ db_get_team_membership failed: {e}")
        return None
    for index, row in enumerate(all_rows[1:], start=2):
        if len(row) < 20:
            continue
        team = _row_to_team_dict(row, index)
        for slot in SLOT_ORDER:
            if team.get(f"{slot}_id", "") == search_id and search_id:
                return team, slot
    return None

def db_find_locked_team(discord_id: int, member_tag: str | None = None):
    """Like db_get_team_membership, but also falls back to matching the raw
    Discord tag text for legacy rows saved before per-slot IDs were tracked
    (those rows only ever stored the tag string). Used for the cross-team
    enrollment-lock check while a wizard is being filled out."""
    hit = db_get_team_membership(discord_id)
    if hit:
        return hit
    if not member_tag or not active_sheet:
        return None
    cleaned_tag = member_tag.strip().lower()
    try:
        all_rows = _get_cached_rows()
    except Exception:
        return None
    for index, row in enumerate(all_rows[1:], start=2):
        if len(row) < 20:
            continue
        team = _row_to_team_dict(row, index)
        for slot in SLOT_ORDER:
            if team.get(f"{slot}_id", ""):
                continue  # already tracked by ID, no need for the text fallback
            tag_val = (team.get(f"{slot}_tag", "") or "").strip()
            if tag_val and tag_val.upper() != VACANT and tag_val.lower() == cleaned_tag:
                return team, slot
    return None

def db_leave_team(discord_id: int):
    """Removes ONLY the calling member's own slot (IGN/UID/Tag/ID cleared to
    VACANT) — never the rest of the roster. If they were the Enroller, that
    role automatically passes to the next remaining filled slot (in
    SLOT_ORDER order). If nobody is left on the roster, the team registration
    itself is archived to deleted_teams and removed. Returns a result dict
    describing what happened, for the Info & Stats announcement."""
    hit = db_get_team_membership(discord_id)
    if not hit:
        raise ValueError("NOT_ON_A_TEAM")
    team, slot = hit
    ign_i, uid_i, tag_i, id_i = SLOT_FIELD_INDEX[slot]
    left_ign = team.get(f"{slot}_ign", "")
    left_tag = team.get(f"{slot}_tag", "")
    was_enroller = str(team.get("discord_id", "")).strip() == str(discord_id).strip() and str(discord_id).strip() != ""

    remaining_slots = []
    for s in SLOT_ORDER:
        if s == slot:
            continue
        sid = team.get(f"{s}_id", "")
        stag = (team.get(f"{s}_tag", "") or "").strip()
        sign = (team.get(f"{s}_ign", "") or "").strip()
        if sign and sign.lower() != "none" and stag.upper() != VACANT:
            remaining_slots.append(s)

    # Re-locate the live row number right before writing (another user's write
    # could have shifted rows since we read the cache above).
    enroller_id_now = team.get("discord_id", "")
    fresh_row = None
    if enroller_id_now:
        try:
            fresh_row = db_find_row_by_discord_id(int(enroller_id_now), force_refresh=True)
        except (ValueError, TypeError):
            fresh_row = None
    write_row = fresh_row or team["row_index"]

    if not remaining_slots:
        # Archive to deleted_teams first, same as the normal Delete flow, so a
        # disband from the last member leaving is never a silent, unrecoverable
        # data loss.
        try:
            db_log_deleted_team({
                "team_code": team.get("team_code", "N/A"), "team_name": team.get("team_name", "N/A"),
                "email": team.get("email", "N/A"), "country": team.get("country", "N/A"), "phone": team.get("phone", "N/A"),
                "igl_ign": team.get("igl_ign", "N/A"), "igl_uid": team.get("igl_uid", "N/A"), "igl_tag": team.get("igl_tag", "N/A"),
                "p2_ign": team.get("p2_ign", "N/A"), "p2_uid": team.get("p2_uid", "N/A"), "p2_tag": team.get("p2_tag", "N/A"),
                "p3_ign": team.get("p3_ign", "N/A"), "p3_uid": team.get("p3_uid", "N/A"), "p3_tag": team.get("p3_tag", "N/A"),
                "p4_ign": team.get("p4_ign", "N/A"), "p4_uid": team.get("p4_uid", "N/A"), "p4_tag": team.get("p4_tag", "N/A"),
                "sub_ign": team.get("sub_ign", "N/A"), "sub_uid": team.get("sub_uid", "N/A"), "sub_tag": team.get("sub_tag", "N/A"),
            }, discord_id)
        except Exception as e:
            print(f"⚠️ Failed to archive disbanded team before deletion: {e}")
        try:
            _with_retry(active_sheet.delete_rows, write_row)
        except Exception as e:
            print(f"⚠️ Failed to delete disbanded team row: {e}")
        invalidate_sheet_cache()
        return {
            "team_name": team["team_name"], "left_slot": slot, "left_ign": left_ign, "left_tag": left_tag,
            "disbanded": True, "new_enroller_id": None, "new_enroller_slot": None,
        }

    ign_letter, tag_letter = _col_letter(ign_i), _col_letter(tag_i)
    _with_retry(active_sheet.update, range_name=f"{ign_letter}{write_row}:{tag_letter}{write_row}",
                values=[[VACANT, VACANT, VACANT]])
    _with_retry(active_sheet.update, range_name=f"{_col_letter(id_i)}{write_row}", values=[[""]])

    new_enroller_id = None
    new_enroller_slot = None
    if was_enroller:
        new_enroller_slot = remaining_slots[0]
        new_enroller_id = team.get(f"{new_enroller_slot}_id", "")
        _with_retry(active_sheet.update, range_name=f"U{write_row}", values=[[str(new_enroller_id)]])

    invalidate_sheet_cache()
    return {
        "team_name": team["team_name"], "left_slot": slot, "left_ign": left_ign, "left_tag": left_tag,
        "disbanded": False, "new_enroller_id": new_enroller_id, "new_enroller_slot": new_enroller_slot,
    }

def db_transfer_enroller(discord_id_requesting: int, new_slot_key: str):
    """Transfers the Enroller role to a different filled teammate slot on the
    requesting user's OWN team. Raises PermissionError if the requester isn't
    the current Enroller, ValueError if the target slot is invalid/empty.
    Returns (team_name, old_enroller_id, new_enroller_id, new_enroller_tag)."""
    hit = db_get_team_membership(discord_id_requesting)
    if not hit:
        raise ValueError("NOT_ON_A_TEAM")
    team, slot = hit
    if str(team.get("discord_id", "")).strip() != str(discord_id_requesting).strip():
        raise PermissionError("NOT_ENROLLER")
    if new_slot_key not in SLOT_ORDER or new_slot_key == slot:
        raise ValueError("BAD_SLOT")
    new_id = team.get(f"{new_slot_key}_id", "")
    new_tag = team.get(f"{new_slot_key}_tag", "")
    new_ign = (team.get(f"{new_slot_key}_ign", "") or "").strip()
    if not new_id or not new_ign or new_ign.lower() == "none" or (new_tag or "").strip().upper() == VACANT:
        raise ValueError("SLOT_EMPTY")

    fresh_row = db_find_row_by_discord_id(discord_id_requesting, force_refresh=True) or team["row_index"]
    _with_retry(active_sheet.update, range_name=f"U{fresh_row}", values=[[str(new_id)]])
    invalidate_sheet_cache()
    return team["team_name"], discord_id_requesting, new_id, new_tag

def db_code_exists(team_code: str) -> bool:
    """Checks whether a team code is already present in the sheet. Used to make
    sure two concurrent registrations can't be handed the same random code."""
    code_str = str(team_code).strip()
    try:
        rows = _get_cached_rows()
        return any(len(row) > 0 and row[0].strip() == code_str for row in rows[1:])
    except Exception as e:
        print(f"⚠️ db_code_exists could not verify uniqueness (treating as taken to be safe): {e}")
        return True  # fail safe: if we can't confirm it's free, don't hand it out

def init_sheets(live_bot_reference=None):
    """Initializes connections to Active, Backup, and Staging Google Sheets with rigorous scope caching."""
    global active_sheet, backup_sheet, staging_sheet, bot
    if live_bot_reference:
        bot = live_bot_reference
        
    active_id = os.getenv("ACTIVE_SPREADSHEET_ID")
    backup_id = os.getenv("BACKUP_SPREADSHEET_ID")
    staging_id = os.getenv("UPDATED_SPREADSHEET_ID")  # ✅ Fetch staging ID
    
    # 1. Main Client Connection
    try:
        creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
        gc = gspread.Client(auth=creds)
    except Exception as auth_error:
        print(f"❌ Critical Authentication Setup Error: {auth_error}")
        return

    # Connect to Active Spreadsheet
    try:
        if active_id:
            cleaned_active = active_id.strip().replace('"', '').replace("'", "")
            if "docs.google.com/spreadsheets/d/" in cleaned_active:
                cleaned_active = cleaned_active.split("/d/")[1].split("/")[0]
            sh_active = gc.open_by_key(cleaned_active)
            active_sheet = sh_active.worksheet("active_teams")
            print(f"✅ Connected to Active Spreadsheet: '{sh_active.title}'")
    except Exception as e:
        print(f"❌ Failed to reach Active Spreadsheet. Type: {type(e).__name__} | Error: {e}")

    # Connect to Backup Spreadsheet
    try:
        if backup_id:
            cleaned_backup = backup_id.strip().replace('"', '').replace("'", "")
            if "docs.google.com/spreadsheets/d/" in cleaned_backup:
                cleaned_backup = cleaned_backup.split("/d/")[1].split("/")[0]
            sh_backup = gc.open_by_key(cleaned_backup)
            backup_sheet = sh_backup.get_worksheet(0)
            print(f"✅ Connected to Backup Log Spreadsheet: '{sh_backup.title}'")
    except Exception as e:
        print(f"❌ Failed to reach Backup Spreadsheet. Type: {type(e).__name__} | Error Details: {e}")

    # 2. Separate Client Connection for service_account3.json Staging
    try:
        if staging_id:
            cleaned_staging = staging_id.strip().replace('"', '').replace("'", "")
            if "docs.google.com/spreadsheets/d/" in cleaned_staging:
                cleaned_staging = cleaned_staging.split("/d/")[1].split("/")[0]
            
            # ✅ Load staging credential file requested
            creds_staging = Credentials.from_service_account_file("service_account3.json", scopes=SCOPES)
            gc_staging = gspread.Client(auth=creds_staging)
            sh_staging = gc_staging.open_by_key(cleaned_staging)
            staging_sheet = sh_staging.get_worksheet(0)
            print(f"✅ Connected to Staging Spreadsheet: '{sh_staging.title}'")
    except Exception as e:
        print(f"⚠️ Warning: Staging Spreadsheet not initialized. Error Details: {e}")

def _row_enroller_id(row) -> str:
    """Extracts the enroller's Discord ID from a sheet row (Column U, with a
    legacy fallback to Column T for older rows saved with one fewer column).
    The 'enroller' is simply whoever completed the registration/update wizard —
    that can be any of the 4 teammates (or the IGL), not just the IGL specifically."""
    if len(row) >= 21:
        return row[20].strip()
    if len(row) >= 20:
        return row[19].strip()
    return ""

def db_find_row_by_discord_id(discord_id: int, force_refresh: bool = False):
    """Finds a team row index by searching the final Discord User ID column (Column T or U).
    Pass force_refresh=True before using the result to write to that row (update/delete),
    since a cached row number can go stale if another user's write shifted rows meanwhile."""
    if not active_sheet: return None
    try:
        all_rows = _get_cached_rows(force_refresh=force_refresh)
        if len(all_rows) <= 1: return None
        search_id = str(discord_id).strip()
        for index, row in enumerate(all_rows[1:], start=2):
            if _row_enroller_id(row) == search_id:
                return index
        return None
    except Exception as e:
        print(f"⚠️ db_find_row_by_discord_id failed: {e}")
        return None

def db_find_team_by_user_id(discord_id: int):
    """Fetches full data row dictionary from the active sheet via the enroller's Discord ID."""
    if not active_sheet: return None
    try:
        all_rows = _get_cached_rows()
        search_id = str(discord_id).strip()
        for row in all_rows[1:]:
            if _row_enroller_id(row) == search_id:
                return {
                    "team_code": row[0], "team_name": row[1], "email": row[2], "country": row[3], "phone": row[4],
                    "igl_ign": row[5], "igl_uid": row[6], "igl_tag": row[7],
                    "p2_ign": row[8], "p2_uid": row[9], "p2_tag": row[10],
                    "p3_ign": row[11], "p3_uid": row[12], "p3_tag": row[13],
                    "p4_ign": row[14], "p4_uid": row[15], "p4_tag": row[16],
                    "sub_ign": row[17], "sub_uid": row[18], "sub_tag": row[19],
                    "discord_id": _row_enroller_id(row)
                }
    except Exception as e:
        print(f"⚠️ db_find_team_by_user_id failed: {e}")
        return None
    return None

def db_find_team_by_query(query: str):
    """Searches active spreadsheet flexibly by Code, Team Name, or IGL Tag.
    The returned dict includes 'discord_id' — the enroller's Discord ID — so
    callers can verify whether the requesting user is actually the team's
    registered enroller before granting update/delete access."""
    if not active_sheet: return None
    cleaned_query = query.strip().lower()
    try:
        all_rows = _get_cached_rows()
        if len(all_rows) <= 1: return None
        for row in all_rows[1:]:
            if len(row) < 20: continue
            if (cleaned_query == row[0].lower() or 
                cleaned_query == row[1].lower() or 
                cleaned_query in row[7].lower()):
                return {
                    "team_code": row[0], "team_name": row[1], "email": row[2], "country": row[3], "phone": row[4],
                    "igl_ign": row[5], "igl_uid": row[6], "igl_tag": row[7],
                    "p2_ign": row[8], "p2_uid": row[9], "p2_tag": row[10],
                    "p3_ign": row[11], "p3_uid": row[12], "p3_tag": row[13],
                    "p4_ign": row[14], "p4_uid": row[15], "p4_tag": row[16],
                    "sub_ign": row[17], "sub_uid": row[18], "sub_tag": row[19],
                    "discord_id": _row_enroller_id(row)
                }
    except Exception as e:
        print(f"⚠️ db_find_team_by_query failed: {e}")
        return None
    return None

def db_save_team(team_code: str, enroller_discord_id: int, data: dict, slot_ids: dict | None = None):
    """Saves or completely overwrites an existing team roster record row in the active sheet.
    enroller_discord_id is whoever completed the wizard — any of the 4 teammates can be the
    enroller, it doesn't have to be the IGL. slot_ids optionally maps
    {"igl"/"p2"/"p3"/"p4"/"sub": discord_id}, letting the cross-team lock check and the
    Manage/Leave/Transfer-Enroller features identify each teammate reliably by ID instead of
    by their (changeable) Discord tag text. Also stamps the 'Exact Date & Time' column and
    color-highlights it so enrollments made the same calendar day are visually grouped."""
    if not active_sheet: raise RuntimeError("Active Database offline")
    _ensure_active_headers()
    sanitized = {}
    for key, value in data.items():
        v_str = str(value)
        sanitized[key] = f"'{v_str}" if v_str.startswith(('=', '+', '-', '@')) else v_str

    slot_ids = slot_ids or {}
    now = datetime.datetime.now()

    row_data = [
        str(team_code), sanitized['team_name'], sanitized['email'], sanitized['country'], sanitized['phone'],
        sanitized['igl_ign'], sanitized['igl_uid'], sanitized['igl_tag'],
        sanitized.get('p2_ign', 'None'), sanitized.get('p2_uid', 'None'), sanitized.get('p2_tag', 'None'),
        sanitized.get('p3_ign', 'None'), sanitized.get('p3_uid', 'None'), sanitized.get('p3_tag', 'None'),
        sanitized.get('p4_ign', 'None'), sanitized.get('p4_uid', 'None'), sanitized.get('p4_tag', 'None'),
        sanitized.get('sub_ign', 'None'), sanitized.get('sub_uid', 'None'), sanitized.get('sub_tag', 'None'),
        str(enroller_discord_id),
        now.strftime("%Y-%m-%d %H:%M:%S"),
        str(slot_ids.get('igl') or ''), str(slot_ids.get('p2') or ''), str(slot_ids.get('p3') or ''),
        str(slot_ids.get('p4') or ''), str(slot_ids.get('sub') or ''),
    ]
    # force_refresh=True: we're about to write by row number, so we can't risk
    # acting on a cached row index that another user's write may have shifted.
    existing_row = db_find_row_by_discord_id(enroller_discord_id, force_refresh=True)
    if existing_row:
        _with_retry(active_sheet.update, range_name=f"A{existing_row}:AA{existing_row}", values=[row_data])
        target_row = existing_row
    else:
        _with_retry(active_sheet.append_row, row_data)
        target_row = len(_with_retry(active_sheet.get_all_values))
    invalidate_sheet_cache()

    try:
        _with_retry(active_sheet.format, f"V{target_row}", {"backgroundColor": _color_for_date(now.date())})
    except Exception as e:
        print(f"⚠️ Could not color-highlight enrollment date cell: {e}")

def db_delete_team(discord_id: int):
    """Removes a team row cleanly from the active registration sheet layout."""
    if not active_sheet: raise RuntimeError("Active Database offline")
    row = db_find_row_by_discord_id(discord_id, force_refresh=True)
    if not row: raise ValueError("NOT_FOUND")
    _with_retry(active_sheet.delete_rows, row)
    invalidate_sheet_cache()

def db_log_deleted_team(team_data: dict, deleted_by_id: int):
    """Appends a wiped registration entry straight to the archive backup sheet tab."""
    global active_sheet
    import gspread
    
    try:
        # Access the active sheet's master workbook file directly
        master_workbook = active_sheet.spreadsheet
        
        # Open your tracking log sheet tab by its exact name string
        try:
            target_log_sheet = master_workbook.worksheet("deleted_teams")
        except gspread.exceptions.WorksheetNotFound:
            # If the tab doesn't exist yet, build it automatically on the fly inside the file
            target_log_sheet = master_workbook.add_worksheet(title="deleted_teams", rows="500", cols="25")
            # Seed header columns layout
            target_log_sheet.append_row([
                "Team Code", "Team Name", "Email", "Country", "Phone", "IGL IGN", "IGL UID", "IGL Discord Tag",
                "P2 IGN", "P2 UID", "P2 Tag", "P3 IGN", "P3 UID", "P3 Tag", "P4 IGN", "P4 UID", "P4 Tag",
                "Sub IGN", "Sub UID", "Sub Tag", "Deleted By ID", "Timestamp"
            ])

        row_payload = [
            team_data.get('team_code', 'N/A'), team_data.get('team_name', 'N/A'), team_data.get('email', 'N/A'),
            team_data.get('country', 'N/A'), team_data.get('phone', 'N/A'), team_data.get('igl_ign', 'N/A'),
            team_data.get('igl_uid', 'N/A'), team_data.get('igl_tag', 'N/A'), team_data.get('p2_ign', 'N/A'),
            team_data.get('p2_uid', 'N/A'), team_data.get('p2_tag', 'N/A'), team_data.get('p3_ign', 'N/A'),
            team_data.get('p3_uid', 'N/A'), team_data.get('p3_tag', 'N/A'), team_data.get('p4_ign', 'N/A'),
            team_data.get('p4_uid', 'N/A'), team_data.get('p4_tag', 'N/A'), team_data.get('sub_ign', 'N/A'),
            team_data.get('sub_uid', 'N/A'), team_data.get('sub_tag', 'N/A'), str(deleted_by_id),
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ]
        
        target_log_sheet.append_row(row_payload)
        return True
    except Exception as append_err: 
        print(f"❌ Error writing row to backup sheet: {append_err}")
        return False

# Inside src/core.database.sheets.py
def db_log_staging_update(team_code, data_dict):
    """Logs the complete updated team roster array to the staging worksheet."""
    # Example setup: extracts everything cleanly from a dictionary payload
    row_values = [
        team_code,
        data_dict.get('team_name'),
        data_dict.get('email'),
        data_dict.get('country'),
        data_dict.get('phone'),
        data_dict.get('igl_ign'),
        data_dict.get('igl_uid'),
        data_dict.get('igl_tag'),
        data_dict.get('p2_ign'),
        data_dict.get('p2_uid'),
        data_dict.get('p2_tag'),
        data_dict.get('p3_ign'),
        data_dict.get('p3_uid'),
        data_dict.get('p3_tag'),
        data_dict.get('p4_ign'),
        data_dict.get('p4_uid'),
        data_dict.get('p4_tag'),
        data_dict.get('sub_ign'),
        data_dict.get('sub_uid'),
        data_dict.get('sub_tag')
    ]
    # your existing code here to append row_values to your staging sheet...
    global active_sheet
    import gspread
    
    try:
        # Access the master workbook file directly
        master_workbook = active_sheet.spreadsheet
        
        # Open your tracking log sheet tab by its exact name string
        try:
            target_staging_sheet = master_workbook.worksheet("updated_teams")
        except gspread.exceptions.WorksheetNotFound:
            # If the tab doesn't exist yet, build it automatically on the fly
            target_staging_sheet = master_workbook.add_worksheet(title="updated_teams", rows="500", cols="5")
            # Seed header columns layout
            target_staging_sheet.append_row(["Team Code", "Team Name", "Email", "Country", "Timestamp"])

        # Format and append row values payload
        row_payload = [
            str(team_code), 
            str(team_name), 
            str(email), 
            str(country),
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ]
        target_staging_sheet.append_row(row_payload)
        return True
    except Exception as err:
        print(f"❌ Error writing row to updated_teams tab: {err}")
        return False

# ==================== TEMPBAN DATA WORKSPACE ====================
ACTIVE_TEMPBANS = {} 

def db_is_team_banned(query: str) -> tuple[bool, str]:
    cleaned = query.strip().lower()
    now = datetime.datetime.now()
    for target, ban_info in list(ACTIVE_TEMPBANS.items()):
        if ban_info["expires_at"] and now > ban_info["expires_at"]:
            del ACTIVE_TEMPBANS[target]
            continue
        if cleaned in target or cleaned == ban_info["team_code"].lower() or cleaned == ban_info["team_name"].lower():
            expiry_str = ban_info["expires_at"].strftime("%Y-%m-%d %H:%M") if ban_info["expires_at"] else "Permanent"
            return True, f"🚨 **Registration Suspended:** Target is currently restricted from participating until `{expiry_str}`. Reason: *{ban_info['reason']}*"
    return False, ""

def db_apply_tempban(search_query: str, duration_days: int, reason: str) -> str:
    team = db_find_team_by_query(search_query)
    if not team: raise ValueError("NOT_FOUND")
    target_key = team["igl_tag"].lower()
    expires_at = datetime.datetime.now() + datetime.timedelta(days=duration_days) if duration_days > 0 else None
    ACTIVE_TEMPBANS[target_key] = {
        "team_code": team["team_code"],
        "team_name": team["team_name"],
        "expires_at": expires_at,
        "reason": reason
    }
    return f"✅ **Roster Banned:** '{team['team_name']}' has been restricted successfully."

# ==================== REACTION ROLES ====================
def _get_or_create_ws(title: str, headers: list):
    """Fetches a worksheet tab by name from the same workbook as active_sheet,
    creating it (with a header row) the first time it's needed."""
    if not active_sheet: raise RuntimeError("Active Database offline")
    master_workbook = active_sheet.spreadsheet
    try:
        return master_workbook.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = master_workbook.add_worksheet(title=title, rows="1000", cols=str(len(headers)))
        ws.append_row(headers)
        return ws

RR_HEADERS = ["Guild ID", "Message ID", "Channel ID", "Emoji", "Role ID", "Role Name"]

def db_add_reaction_role(guild_id, message_id, channel_id, emoji: str, role_id, role_name: str):
    """Creates or overwrites the role mapping for a given message+emoji combo."""
    ws = _get_or_create_ws("reaction_roles", RR_HEADERS)
    rows = _with_retry(ws.get_all_values)
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) >= 4 and row[0] == str(guild_id) and row[1] == str(message_id) and row[3] == emoji:
            _with_retry(ws.update, range_name=f"A{idx}:F{idx}",
                        values=[[str(guild_id), str(message_id), str(channel_id), emoji, str(role_id), role_name]])
            return
    _with_retry(ws.append_row, [str(guild_id), str(message_id), str(channel_id), emoji, str(role_id), role_name])

def db_remove_reaction_role(guild_id, message_id, emoji: str) -> bool:
    """Deletes a single role mapping. Returns True if something was actually removed."""
    ws = _get_or_create_ws("reaction_roles", RR_HEADERS)
    rows = _with_retry(ws.get_all_values)
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) >= 4 and row[0] == str(guild_id) and row[1] == str(message_id) and row[3] == emoji:
            _with_retry(ws.delete_rows, idx)
            return True
    return False

def db_list_reaction_roles(guild_id=None):
    """Returns every reaction-role mapping, optionally filtered to one guild.
    Used both by the listing command and to warm the bot's in-memory cache on startup."""
    ws = _get_or_create_ws("reaction_roles", RR_HEADERS)
    rows = _with_retry(ws.get_all_values)
    results = []
    for row in rows[1:]:
        if len(row) < 5: continue
        if guild_id is not None and row[0] != str(guild_id): continue
        results.append({
            "guild_id": row[0], "message_id": row[1], "channel_id": row[2],
            "emoji": row[3], "role_id": row[4], "role_name": row[5] if len(row) > 5 else ""
        })
    return results

# ==================== WEBHOOK MANAGEMENT ====================
WEBHOOK_HEADERS = ["Guild ID", "Name", "Webhook ID", "Webhook URL", "Channel ID", "Owner Discord ID", "Created At"]

def db_save_webhook(guild_id, name: str, webhook_id, webhook_url: str, channel_id, owner_discord_id):
    ws = _get_or_create_ws("webhooks", WEBHOOK_HEADERS)
    _with_retry(ws.append_row, [
        str(guild_id), name, str(webhook_id), webhook_url, str(channel_id), str(owner_discord_id),
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ])

def db_find_webhook(guild_id, name: str):
    """Case-insensitive lookup by name, scoped to one guild."""
    ws = _get_or_create_ws("webhooks", WEBHOOK_HEADERS)
    rows = _with_retry(ws.get_all_values)
    cleaned = name.strip().lower()
    for row in rows[1:]:
        if len(row) >= 6 and row[0] == str(guild_id) and row[1].strip().lower() == cleaned:
            return {
                "guild_id": row[0], "name": row[1], "webhook_id": row[2], "webhook_url": row[3],
                "channel_id": row[4], "owner_discord_id": row[5]
            }
    return None

def db_delete_webhook_record(guild_id, name: str):
    """Removes the DB row for a webhook (does NOT delete the Discord webhook itself —
    callers should do that via the REST/discord.py webhook object first)."""
    ws = _get_or_create_ws("webhooks", WEBHOOK_HEADERS)
    rows = _with_retry(ws.get_all_values)
    cleaned = name.strip().lower()
    for idx, row in enumerate(rows[1:], start=2):
        if len(row) >= 6 and row[0] == str(guild_id) and row[1].strip().lower() == cleaned:
            record = {
                "guild_id": row[0], "name": row[1], "webhook_id": row[2], "webhook_url": row[3],
                "channel_id": row[4], "owner_discord_id": row[5]
            }
            _with_retry(ws.delete_rows, idx)
            return record
    return None

def db_list_webhooks(guild_id, owner_discord_id=None):
    ws = _get_or_create_ws("webhooks", WEBHOOK_HEADERS)
    rows = _with_retry(ws.get_all_values)
    results = []
    for row in rows[1:]:
        if len(row) < 6 or row[0] != str(guild_id): continue
        if owner_discord_id is not None and row[5] != str(owner_discord_id): continue
        results.append({
            "guild_id": row[0], "name": row[1], "webhook_id": row[2], "webhook_url": row[3],
            "channel_id": row[4], "owner_discord_id": row[5]
        })
    return results

# ==================== WELCOME / GOODBYE (JOIN-LEAVE TRIGGERS) ====================
# "Info Stats Channel ID" was appended (never inserted) as a 6th column so any
# guild_config sheet that already existed keeps working unchanged for rows
# saved before this feature existed (they simply pad to "").
GUILD_CONFIG_HEADERS = ["Guild ID", "Welcome Channel ID", "Welcome Message", "Goodbye Channel ID", "Goodbye Message", "Info Stats Channel ID"]
_GUILD_CONFIG_WIDTH = len(GUILD_CONFIG_HEADERS)

def _upsert_guild_config(guild_id, **updates):
    ws = _get_or_create_ws("guild_config", GUILD_CONFIG_HEADERS)
    rows = _with_retry(ws.get_all_values)
    for idx, row in enumerate(rows[1:], start=2):
        if row and row[0] == str(guild_id):
            padded = row + [""] * (_GUILD_CONFIG_WIDTH - len(row))
            merged = [
                str(guild_id),
                str(updates.get("welcome_channel_id", padded[1])),
                updates.get("welcome_message", padded[2]),
                str(updates.get("goodbye_channel_id", padded[3])),
                updates.get("goodbye_message", padded[4]),
                str(updates.get("info_stats_channel_id", padded[5])),
            ]
            _with_retry(ws.update, range_name=f"A{idx}:F{idx}", values=[merged])
            return
    new_row = [
        str(guild_id),
        str(updates.get("welcome_channel_id", "")), updates.get("welcome_message", ""),
        str(updates.get("goodbye_channel_id", "")), updates.get("goodbye_message", ""),
        str(updates.get("info_stats_channel_id", "")),
    ]
    _with_retry(ws.append_row, new_row)

def db_set_welcome(guild_id, channel_id, message: str):
    _upsert_guild_config(guild_id, welcome_channel_id=channel_id, welcome_message=message)

def db_disable_welcome(guild_id):
    _upsert_guild_config(guild_id, welcome_channel_id="", welcome_message="")

def db_set_goodbye(guild_id, channel_id, message: str):
    _upsert_guild_config(guild_id, goodbye_channel_id=channel_id, goodbye_message=message)

def db_disable_goodbye(guild_id):
    _upsert_guild_config(guild_id, goodbye_channel_id="", goodbye_message="")

def db_set_info_stats_channel(guild_id, channel_id):
    _upsert_guild_config(guild_id, info_stats_channel_id=channel_id)

def db_disable_info_stats_channel(guild_id):
    _upsert_guild_config(guild_id, info_stats_channel_id="")

def db_get_guild_config(guild_id):
    ws = _get_or_create_ws("guild_config", GUILD_CONFIG_HEADERS)
    rows = _with_retry(ws.get_all_values)
    for row in rows[1:]:
        if row and row[0] == str(guild_id):
            padded = row + [""] * (_GUILD_CONFIG_WIDTH - len(row))
            return {
                "welcome_channel_id": padded[1], "welcome_message": padded[2],
                "goodbye_channel_id": padded[3], "goodbye_message": padded[4],
                "info_stats_channel_id": padded[5],
            }
    return None


# ==================== AI ASSISTANT CONFIG ====================
# "Strict Mode" was added after this sheet may already exist in some
# deployments, so it's appended as a 5th column rather than inserted — any
# existing rows without it just read back as "" -> strict_enabled=False.
# ==================== AI ASSISTANT CONFIG ====================
# Columns were added over time — always appended, never inserted, so any
# rows saved by an older version of the bot still read back fine (missing
# cells just come back as "" -> falsy defaults).
AI_CONFIG_HEADERS = [
    "Scope", "FAQ Enabled", "General Enabled", "Updated At", "Strict Mode",
    "Custom Instructions", "FAQ Channel ID", "General Channel ID",
]
_AI_CONFIG_WIDTH = len(AI_CONFIG_HEADERS)

def db_get_ai_config(scope: str):
    """scope is a guild ID (as a string) for a server, or the literal
    string 'DM' for the global Direct-Message scope. Returns None if this
    scope has never been configured (callers should treat that as
    everything-off)."""
    ws = _get_or_create_ws("ai_config", AI_CONFIG_HEADERS)
    rows = _with_retry(ws.get_all_values)
    for row in rows[1:]:
        if row and row[0] == scope:
            padded = row + [""] * (_AI_CONFIG_WIDTH - len(row))
            return {
                "faq_enabled": padded[1].strip().upper() == "TRUE",
                "general_enabled": padded[2].strip().upper() == "TRUE",
                "strict_enabled": padded[4].strip().upper() == "TRUE",
                "custom_instructions": padded[5],
                "faq_channel_id": padded[6].strip() or None,
                "general_channel_id": padded[7].strip() or None,
            }
    return None

def db_set_ai_config(scope: str, faq_enabled=None, general_enabled=None, strict_enabled=None,
                      custom_instructions=None, faq_channel_id=None, general_channel_id=None):
    """Upserts a scope's config. Passing None for any flag/field leaves it
    unchanged (so a single dashboard control only has to send the one value
    it's actually changing). To explicitly CLEAR custom_instructions or a
    channel id, pass an empty string "" rather than None."""
    ws = _get_or_create_ws("ai_config", AI_CONFIG_HEADERS)
    rows = _with_retry(ws.get_all_values)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _merge(padded):
        current_faq = padded[1].strip().upper() == "TRUE"
        current_general = padded[2].strip().upper() == "TRUE"
        current_strict = padded[4].strip().upper() == "TRUE"
        return [
            scope,
            "TRUE" if (current_faq if faq_enabled is None else faq_enabled) else "FALSE",
            "TRUE" if (current_general if general_enabled is None else general_enabled) else "FALSE",
            timestamp,
            "TRUE" if (current_strict if strict_enabled is None else strict_enabled) else "FALSE",
            padded[5] if custom_instructions is None else custom_instructions,
            padded[6] if faq_channel_id is None else faq_channel_id,
            padded[7] if general_channel_id is None else general_channel_id,
        ]

    for idx, row in enumerate(rows[1:], start=2):
        if row and row[0] == scope:
            padded = row + [""] * (_AI_CONFIG_WIDTH - len(row))
            _with_retry(ws.update, range_name=f"A{idx}:H{idx}", values=[_merge(padded)])
            return
    _with_retry(ws.append_row, _merge([""] * _AI_CONFIG_WIDTH))


# ==================== AI PER-CHANNEL CONFIG (stack any number of channels) ====================
# Supersedes the old single "FAQ Channel ID"/"General Channel ID" columns
# above — those are left in place (unused) for backward compatibility with
# any existing sheet, but the bot now reads from this table instead, which
# supports any number of channels, each independently configured.
AI_CHANNEL_HEADERS = ["Guild ID", "Channel ID", "Mode", "Strict Mode", "Custom Instructions", "Updated At"]
_AI_CHANNEL_WIDTH = len(AI_CHANNEL_HEADERS)

def db_list_ai_channels(guild_id: str):
    """Returns every configured channel for this guild as a list of dicts:
    {"channel_id", "mode" ("faq"/"general"), "strict_enabled", "custom_instructions"}."""
    ws = _get_or_create_ws("ai_channels", AI_CHANNEL_HEADERS)
    rows = _with_retry(ws.get_all_values)
    result = []
    for row in rows[1:]:
        if row and row[0] == guild_id:
            padded = row + [""] * (_AI_CHANNEL_WIDTH - len(row))
            result.append({
                "channel_id": padded[1],
                "mode": padded[2].strip().lower() or "faq",
                "strict_enabled": padded[3].strip().upper() == "TRUE",
                "custom_instructions": padded[4],
            })
    return result

def db_get_ai_channel(guild_id: str, channel_id: str):
    for ch in db_list_ai_channels(guild_id):
        if ch["channel_id"] == channel_id:
            return ch
    return None

def db_set_ai_channel(guild_id: str, channel_id: str, mode=None, strict_enabled=None, custom_instructions=None):
    """Upserts one channel's config. None for any field leaves it unchanged
    on an existing row (or defaults to faq/off/"" on a brand new one)."""
    ws = _get_or_create_ws("ai_channels", AI_CHANNEL_HEADERS)
    rows = _with_retry(ws.get_all_values)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for idx, row in enumerate(rows[1:], start=2):
        if row and row[0] == guild_id and len(row) > 1 and row[1] == channel_id:
            padded = row + [""] * (_AI_CHANNEL_WIDTH - len(row))
            current_strict = padded[3].strip().upper() == "TRUE"
            new_mode = padded[2] if mode is None else mode
            new_strict = current_strict if strict_enabled is None else strict_enabled
            new_instructions = padded[4] if custom_instructions is None else custom_instructions
            _with_retry(ws.update, range_name=f"A{idx}:F{idx}",
                        values=[[guild_id, channel_id, new_mode, "TRUE" if new_strict else "FALSE",
                                 new_instructions, timestamp]])
            return
    _with_retry(ws.append_row, [
        guild_id, channel_id, mode or "faq", "TRUE" if strict_enabled else "FALSE",
        custom_instructions or "", timestamp
    ])

def db_delete_ai_channel(guild_id: str, channel_id: str):
    ws = _get_or_create_ws("ai_channels", AI_CHANNEL_HEADERS)
    rows = _with_retry(ws.get_all_values)
    for idx, row in enumerate(rows[1:], start=2):
        if row and row[0] == guild_id and len(row) > 1 and row[1] == channel_id:
            _with_retry(ws.delete_rows, idx)
            return


# ==================== STICKY NOTES (per-channel auto-reposting requirements) ====================
# One row per (guild, channel). "Repost After" is how many non-bot messages
# are allowed to pile up in that channel before the note gets deleted and
# resent so it lands back at the bottom. Message counters/cooldowns/the
# currently-posted message id are NOT stored here — those live in memory in
# the command module, since writing to Sheets on every single message would
# be far too slow/rate-limit-prone for something this chatty.
STICKY_HEADERS = ["Guild ID", "Channel ID", "Title", "Content", "Color", "Repost After", "Enabled", "Updated At"]
_STICKY_WIDTH = len(STICKY_HEADERS)

def db_list_sticky_notes(guild_id: str):
    """Returns every sticky note configured for this guild as a list of dicts:
    {"channel_id", "title", "content", "color", "repost_after", "enabled"}."""
    ws = _get_or_create_ws("sticky_notes", STICKY_HEADERS)
    rows = _with_retry(ws.get_all_values)
    result = []
    for row in rows[1:]:
        if row and row[0] == guild_id:
            padded = row + [""] * (_STICKY_WIDTH - len(row))
            repost_raw = padded[5].strip()
            result.append({
                "channel_id": padded[1],
                "title": padded[2],
                "content": padded[3],
                "color": padded[4].strip() or "2ECC71",
                "repost_after": int(repost_raw) if repost_raw.isdigit() and int(repost_raw) > 0 else 5,
                "enabled": padded[6].strip().upper() == "TRUE",
            })
    return result

def db_get_sticky_note(guild_id: str, channel_id: str):
    for note in db_list_sticky_notes(guild_id):
        if note["channel_id"] == channel_id:
            return note
    return None

def db_set_sticky_note(guild_id: str, channel_id: str, title=None, content=None, color=None,
                        repost_after=None, enabled=None):
    """Upserts one channel's sticky note. None for any field leaves it unchanged
    on an existing row (or falls back to a sane default on a brand new one)."""
    ws = _get_or_create_ws("sticky_notes", STICKY_HEADERS)
    rows = _with_retry(ws.get_all_values)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for idx, row in enumerate(rows[1:], start=2):
        if row and row[0] == guild_id and len(row) > 1 and row[1] == channel_id:
            padded = row + [""] * (_STICKY_WIDTH - len(row))
            current_enabled = padded[6].strip().upper() == "TRUE"
            merged = [
                guild_id, channel_id,
                padded[2] if title is None else title,
                padded[3] if content is None else content,
                padded[4] if color is None else color,
                padded[5] if repost_after is None else str(repost_after),
                "TRUE" if (current_enabled if enabled is None else enabled) else "FALSE",
                timestamp,
            ]
            _with_retry(ws.update, range_name=f"A{idx}:H{idx}", values=[merged])
            return
    _with_retry(ws.append_row, [
        guild_id, channel_id, title or "", content or "", color or "2ECC71",
        str(repost_after or 5), "TRUE" if (enabled is None or enabled) else "FALSE", timestamp
    ])

def db_delete_sticky_note(guild_id: str, channel_id: str):
    ws = _get_or_create_ws("sticky_notes", STICKY_HEADERS)
    rows = _with_retry(ws.get_all_values)
    for idx, row in enumerate(rows[1:], start=2):
        if row and row[0] == guild_id and len(row) > 1 and row[1] == channel_id:
            _with_retry(ws.delete_rows, idx)
            return