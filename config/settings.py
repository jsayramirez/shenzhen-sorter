"""
Shenzhen Sorter - configuration.

Plain Python on purpose: no YAML/JSON dependency, and it's the one file
a non-technical owner should be able to open and tweak safely.
Edit values below; do not rename the keys other modules import.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Identity / accounts
# ---------------------------------------------------------------------------
WINDOWS_USERNAME = "jsayr"

# ---------------------------------------------------------------------------
# Intake (Desktop) - the human-facing side
# ---------------------------------------------------------------------------
DESKTOP_ROOT = Path(rf"C:\Users\{WINDOWS_USERNAME}\Desktop")

SHENZHEN_ROOT = DESKTOP_ROOT / "Shenzhen Sorting Facility"
SHIPPED_DIR = SHENZHEN_ROOT / "Shipped"

RECEIPT_CONSOLE_DIR = DESKTOP_ROOT / "Receipt Center Console"
GLOVE_BOX_DIR = RECEIPT_CONSOLE_DIR / "Glove Box"

# ---------------------------------------------------------------------------
# Archive (S: drive) - "Warehouse" is this project's name for the
# spec's "Master Archive". No D: drive exists on this machine; S: (Samsung
# 990 Pro) is the confirmed archive drive.
# ---------------------------------------------------------------------------
ARCHIVE_ROOT = Path(r"S:\Warehouse")
ARCHIVE_RECEIPTS_DIR = ARCHIVE_ROOT / "Archive Receipts"

# Folders that already exist on S: from prior manual sorting (CANON - Dump,
# FUJI - Dump, etc.) are NOT touched by this project. They are listed here
# only so tooling can warn if a path collision is ever attempted.
PRE_EXISTING_S_DRIVE_FOLDERS = [
    "2026", "2026 JAPAN", "CAMILLE", "CANON - Dump", "Dash Cam - Dump",
    "FUJI  - Picks", "FUJI - Dump", "KODAK - Dump", "LUMIX - Dump",
    "LUMIX - Picks", "OLYMPUS - Dump", "PENTAX - Dump", "RICOH - Dump",
    "SteamLibrary", "UNKNOWN",
]

# ---------------------------------------------------------------------------
# Dedicated cameras (Windows is camera-first per spec section 14.1)
# Matched against EXIF Make/Model. Values were read directly from real
# sample files in your existing dump folders on 2026-07-15 - confirm/adjust
# if you add or replace gear.
#
# match_make / match_model are matched case-insensitively after stripping
# whitespace (several of your cameras pad these EXIF fields with spaces).
# archive_folder is the exact top-level folder name created under
# S:\Warehouse\<archive_folder>\<Year>\<Month>\.
# ---------------------------------------------------------------------------
DEDICATED_CAMERAS = [
    {
        "archive_folder": "Fujifilm X100VI",
        "match_make": "fujifilm",
        "match_model": "x100vi",
    },
    {
        "archive_folder": "Panasonic Lumix GM1",
        "match_make": "panasonic",
        "match_model": "dmc-gm1",
    },
    {
        "archive_folder": "Canon PowerShot G9",
        "match_make": "canon",
        "match_model": "canon powershot g9",
    },
    {
        "archive_folder": "Olympus TG-6",
        "match_make": "olympus",
        "match_model": "tg-6",
    },
    {
        "archive_folder": "Pentax Q-S1",
        "match_make": "pentax",
        "match_model": "pentax q-s1",
    },
    {
        "archive_folder": "Ricoh GR Digital 4",
        "match_make": "ricoh",
        "match_model": "gr digital 4",
    },
    {
        "archive_folder": "Kodak CBB3",
        "match_make": "generalplus",
        "match_model": "cbb3",
    },
    {
        # Confirmed on a real sample (2026-07-15): this dash cam's MP4s carry
        # no Make/Model tag at all (generic "SStarMeta" chipset handler) and
        # CreateDate/MediaCreateDate are both the broken placeholder
        # "0000:00:00 00:00:00" - content-based confident matching is not
        # possible for this device, unlike every other camera above. Per an
        # explicit decision (not a guess), this is matched by dump-folder
        # name instead: any dump folder whose name contains "dash cam"
        # (case-insensitive) routes ALL its video files here. This is a
        # deliberate, user-confirmed exception to "confidently identified
        # from content" (spec 14.3) - the user is the one asserting the
        # source via how they name the folder, which is a stronger signal
        # than the transport-method hints the spec warns against.
        "archive_folder": "Dash Cam",
        "match_make": None,
        "match_model": None,
        "match_folder_name_contains": "dash cam",
    },
    {
        # Confirmed on real samples (2026-07-15): DJI photos carry confident
        # EXIF (Make="DJI", Model="OW001"), matched normally below. DJI's own
        # MP4 videos do NOT carry a Make/Model tag at all, but DJI's firmware
        # consistently names every file "DJI_<timestamp>_<seq>[_D].ext" - a
        # manufacturer filename convention, not a user-chosen folder name, so
        # this is matched by filename prefix as a fallback for files (mostly
        # video) where content-based matching comes back unconfident. This is
        # a stronger signal than the Dash Cam's folder-name hint since it's
        # baked into the file by the camera itself, not asserted by the user.
        "archive_folder": "DJI",
        "match_make": "dji",
        "match_model": "ow001",
        "match_filename_prefix": "dji_",
    },
]

# ---------------------------------------------------------------------------
# Stability / transfer-completion gates (spec section 6)
#
# These two are user-configurable at runtime (see TIMER_SETTINGS_PATH below) -
# the control panel's "TIMER SETTINGS" dialog edits them without needing to
# touch this file. The values below are only the fallback defaults used the
# first time, before any override has ever been saved.
# ---------------------------------------------------------------------------
FILE_QUIET_SECONDS = 5 * 60          # per-file: no tracked changes for this long
FOLDER_QUIET_SECONDS = 30 * 60       # whole dump folder: no new/changed content
RECONCILIATION_INTERVAL_SECONDS = 10 * 60   # periodic safety-net rescan

# Used only for a MANUAL trigger (the control panel's "Manual Onboarding"
# button, or `main.py run --manual`), never for an unattended/scheduled run.
# The person clicking the button is themselves vouching "this transfer is
# actually done" - something an automatic background check can't know - so
# the long conservative wait above doesn't apply. This is still a real,
# non-zero check, not a bypass: it exists purely to catch the edge case of
# clicking the instant a file is still actively being written (e.g. you're
# still mid drag-and-drop), not to second-guess your manual confirmation.
MANUAL_FILE_QUIET_SECONDS = 2
MANUAL_FOLDER_QUIET_SECONDS = 2

TIMER_SETTINGS_PATH = Path(r"S:\shenzhen-sorter\data\timer_settings.json")


def load_timer_settings() -> None:
    """Reads TIMER_SETTINGS_PATH if it exists and overrides the two module
    attributes above in place. Safe to call repeatedly (e.g. every time the
    control panel opens its Timer Settings dialog) to pick up a change made
    by another process/instance."""
    import json
    global FILE_QUIET_SECONDS, FOLDER_QUIET_SECONDS
    try:
        data = json.loads(TIMER_SETTINGS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if "file_quiet_seconds" in data:
        FILE_QUIET_SECONDS = int(data["file_quiet_seconds"])
    if "folder_quiet_seconds" in data:
        FOLDER_QUIET_SECONDS = int(data["folder_quiet_seconds"])


def save_timer_settings(file_quiet_seconds: int, folder_quiet_seconds: int) -> None:
    """Persists new values AND updates the live module attributes immediately,
    so an already-running process (e.g. the control panel) uses the new
    values on its very next run_session() call without needing a restart -
    stability.py reads settings.FILE_QUIET_SECONDS/FOLDER_QUIET_SECONDS fresh
    on every call rather than caching them at import time."""
    import json
    global FILE_QUIET_SECONDS, FOLDER_QUIET_SECONDS
    if file_quiet_seconds < 0 or folder_quiet_seconds < 0:
        raise ValueError("Timer values must not be negative")
    TIMER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TIMER_SETTINGS_PATH.write_text(json.dumps({
        "file_quiet_seconds": file_quiet_seconds,
        "folder_quiet_seconds": folder_quiet_seconds,
    }, indent=2))
    FILE_QUIET_SECONDS = file_quiet_seconds
    FOLDER_QUIET_SECONDS = folder_quiet_seconds


load_timer_settings()  # apply any previously-saved override immediately on import

# ---------------------------------------------------------------------------
# Free-space preflight (spec section 12.1)
# ---------------------------------------------------------------------------
SAFETY_RESERVE_BYTES = 20 * 1024**3  # 20 GB, edit to taste
# required_space = batch_size + largest_single_file_in_batch + SAFETY_RESERVE_BYTES
# (largest-file term covers .partial + eventual final coexisting briefly;
#  v1 copies one file at a time, so only one .partial ever exists at once)

# ---------------------------------------------------------------------------
# Metadata reader (ExifTool, for HEIC/RAW/video - Pillow handles plain JPEG)
# ---------------------------------------------------------------------------
EXIFTOOL_PATH = Path(r"S:\shenzhen-sorter\tools\exiftool-13.59_64\exiftool.exe")

# ---------------------------------------------------------------------------
# Hashing / copy
# ---------------------------------------------------------------------------
HASH_CHUNK_SIZE = 4 * 1024 * 1024    # 4 MiB streaming read/write chunks
PARTIAL_SUFFIX = ".partial"          # .filename.ext.partial

# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------
LEDGER_DB_PATH = Path(r"S:\shenzhen-sorter\data\ledger.sqlite3")
LOCK_FILE_PATH = Path(r"S:\shenzhen-sorter\data\writer.lock")
LOCK_STALE_SECONDS = 10 * 60         # heartbeat older than this + dead PID = stale
HARD_PAUSE_STATE_PATH = Path(r"S:\shenzhen-sorter\data\hard_pause.json")
SAFE_STOP_FLAG_PATH = Path(r"S:\shenzhen-sorter\data\safe_stop.flag")

# Compliance Check persists the missing-files list from its last run here,
# so the NEXT run's CLEAR/NOT CLEAR verdict is "anything NEW gone missing
# since last time", not "does this match the original archive record
# forever" - see integrity.py's docstring for the full design rationale.
LAST_COMPLIANCE_CHECK_PATH = Path(r"S:\shenzhen-sorter\data\last_compliance_check.json")
# Delivery Check re-evaluates Shipped's current contents fresh every run
# (nothing to diff against), but the last result is still kept for the
# control panel's status line.
LAST_DELIVERY_CHECK_PATH = Path(r"S:\shenzhen-sorter\data\last_delivery_check.json")

# Browse Month: a disposable, always-regenerated folder of hardlinks (not
# copies) collating one month's archived files across every camera/device
# into one flat, Explorer-browsable view. Lives outside ARCHIVE_ROOT so
# Compliance Check's whole-Warehouse search never has to consider it, and
# on the same volume as the Warehouse since NTFS hardlinks can't cross
# drives.
MONTH_VIEW_DIR = Path(r"S:\shenzhen-sorter\data\Month Browser")

# Browse Month leaves these Smart Device Media categories out of the view
# by default - screenshots/screen recordings/unidentified junk clutter a
# "what did I actually photograph this month" gallery. This ONLY affects
# what Browse Month links into its disposable view; every one of these
# files is still archived and fully preserved exactly as always. Dedicated
# -camera categories (a camera's own archive_folder name, e.g. "Fujifilm
# X100VI") never match anything in this set. See classify.py's
# classify_smart_device_category() for where these category names come from.
BROWSE_MONTH_EXCLUDED_CATEGORIES = {
    "Screenshots", "Screen Recordings", "Other Media", "Non-Media Files", "Unknown Files",
}

# ---------------------------------------------------------------------------
# Dry run - must default True. Only flip for a real run once you have
# reviewed dry-run output and tested against disposable files.
# ---------------------------------------------------------------------------
DRY_RUN = True
