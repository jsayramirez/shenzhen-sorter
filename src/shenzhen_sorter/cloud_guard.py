"""
Cloud-sync / placeholder detection (spec sections 11 and 12.3).

Two distinct risks, both checked live on every run (not just once at
setup), because OneDrive's "Backup" feature can redirect Desktop later
without the user necessarily noticing:

1. Desktop *redirection*: OneDrive can quietly take over the Desktop
   folder. If the intake root is actually inside OneDrive, "the file
   exists on Desktop" no longer means "the file's bytes are fully local."
2. Per-file *placeholders*: even without full redirection, an individual
   file can be a OneDrive Files-On-Demand / iCloud placeholder - it has a
   real name and reported size, but the bytes are not resident on disk
   until something forces a download. Hashing a placeholder without
   checking this first risks hashing a partially-downloaded stub (or
   blocking indefinitely) and calling it verified.

This is treated as a hard precondition, not a warning banner: if the
Desktop is cloud-redirected, or a source file is a placeholder, the file
is left alone and the run pauses for that item rather than guessing.
"""

import ctypes
import os
from pathlib import Path

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000

CLOUD_PLACEHOLDER_MASK = (
    FILE_ATTRIBUTE_REPARSE_POINT
    | FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
)


class CloudRedirectionDetected(RuntimeError):
    pass


class CloudPlaceholderFile(RuntimeError):
    pass


def desktop_is_cloud_redirected(desktop_root: Path) -> bool:
    """True if the Windows 'Desktop' shell folder points somewhere under
    a OneDrive path rather than a plain local folder."""
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer") \
        or os.environ.get("OneDriveCommercial")
    if not onedrive:
        return False
    try:
        onedrive_resolved = str(Path(onedrive).resolve()).lower()
        desktop_resolved = str(Path(desktop_root).resolve()).lower()
    except OSError:
        return False
    return desktop_resolved.startswith(onedrive_resolved)


def file_is_cloud_placeholder(path: Path) -> bool:
    """True if the file's Windows attributes indicate it is a cloud
    placeholder (OneDrive Files-On-Demand or similar) rather than fully
    resident local content."""
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    except Exception:
        return False
    if attrs == 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES
        return False
    return bool(attrs & CLOUD_PLACEHOLDER_MASK)


def require_safe_to_read(path: Path) -> None:
    """Raise before any hashing/copy touches `path` if it is a cloud
    placeholder. Callers should catch this, quarantine the item with a
    clear reason, and move on rather than crashing the whole session."""
    if file_is_cloud_placeholder(path):
        raise CloudPlaceholderFile(
            f"{path} is a cloud placeholder (OneDrive/iCloud) - not fully "
            "resident locally. Make it 'Always keep on this device' and retry."
        )
