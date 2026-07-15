"""
Stability / transfer-completion gates (spec section 6).

A filesystem watcher event is a wake-up hint only - it can fire early, fire
repeatedly, or be triggered by antivirus. This module is the actual gate:
a file must be observed unchanged for FILE_QUIET_SECONDS, AND its whole
dump folder must have had no new/changed content for FOLDER_QUIET_SECONDS,
before anything downstream treats it as eligible.

Deviation worth flagging: the spec's default gate is time-only ("5 minutes
without tracked changes"). For a very large file copied slowly off a
memory card, transfer can stall mid-write for several minutes at a
buffer/cluster boundary on some readers - a pure time gate can call an
unfinished file "stable" mid-copy. This module adds one more requirement
on top of the spec's time gate: size must never have been observed to
shrink across scans. A shrink is physically impossible for a file that is
only ever being appended to during a normal copy-in, so an observed shrink
means something unusual is happening (the source app truncated and is
rewriting it) and the file is treated as never-stable until a full quiet
window passes with no further shrink.
"""

import os
import time
from dataclasses import dataclass
from pathlib import Path

from config import settings
from . import ledger


@dataclass
class StabilityReport:
    stable_files: list          # list[Path] ready for downstream processing
    unstable_files: list        # list[Path] still within quiet gates
    folder_quiet: bool          # whether the WHOLE dump folder gate passed
    seconds_until_ready: float = 0.0  # 0 if already ready now; else time until BOTH gates pass


def scan_dump_folder(conn, dump_folder_path: Path, file_quiet_seconds: float = None,
                      folder_quiet_seconds: float = None) -> StabilityReport:
    """Recursive scan of a real dump folder (may contain nested subfolders,
    e.g. a copied SD card's DCIM structure). file_quiet_seconds/
    folder_quiet_seconds default to the configured settings values; pass
    settings.MANUAL_FILE_QUIET_SECONDS/MANUAL_FOLDER_QUIET_SECONDS for a
    manually-triggered run instead of an unattended/scheduled one."""
    now_paths = []
    for dirpath, _dirnames, filenames in os.walk(dump_folder_path):
        for fn in filenames:
            p = Path(dirpath) / fn
            now_paths.append(p)
    return scan_files(conn, now_paths, file_quiet_seconds, folder_quiet_seconds)


def scan_files(conn, now_paths: list[Path], file_quiet_seconds: float = None,
               folder_quiet_seconds: float = None) -> StabilityReport:
    """Same gate logic as scan_dump_folder, but over an explicit file list -
    used directly for the loose-files-in-root batch, which must NOT recurse
    into sibling dump folders or Shipped."""
    file_quiet_seconds = settings.FILE_QUIET_SECONDS if file_quiet_seconds is None else file_quiet_seconds
    folder_quiet_seconds = settings.FOLDER_QUIET_SECONDS if folder_quiet_seconds is None else folder_quiet_seconds

    stable, unstable = [], []
    most_recent_unchanged_since = None
    max_file_remaining = 0.0

    for p in now_paths:
        try:
            st = p.stat()
        except FileNotFoundError:
            # Disappeared between listing and stat - treat conservatively
            # as unstable this round; next scan will reconcile.
            continue
        row = ledger.observe_file(conn, str(p), st.st_size, st.st_mtime)
        quiet_for = row["last_seen_at"] - row["unchanged_since"]
        is_file_stable = (quiet_for >= file_quiet_seconds) and not row["size_ever_shrank"]
        file_remaining = max(0.0, file_quiet_seconds - quiet_for)
        max_file_remaining = max(max_file_remaining, file_remaining)
        if most_recent_unchanged_since is None or row["unchanged_since"] > most_recent_unchanged_since:
            most_recent_unchanged_since = row["unchanged_since"]
        (stable if is_file_stable else unstable).append(p)

    if most_recent_unchanged_since is None:
        folder_quiet = False  # empty folder: nothing to do, not "quiet-and-ready"
        seconds_until_ready = 0.0
    else:
        folder_quiet_for = time.time() - most_recent_unchanged_since
        folder_remaining = max(0.0, folder_quiet_seconds - folder_quiet_for)
        folder_quiet = folder_remaining <= 0
        # Both gates must clear - a countdown only reflects reality if it
        # shows whichever one still has time left, not just the folder gate.
        seconds_until_ready = max(folder_remaining, max_file_remaining)

    # Only promote per-file stability to "usable" once the whole-folder gate
    # also passes - matches spec's combination of both gates, not either alone.
    usable = stable if folder_quiet else []
    still_waiting = [p for p in now_paths if p not in usable]
    return StabilityReport(stable_files=usable, unstable_files=still_waiting, folder_quiet=folder_quiet,
                            seconds_until_ready=seconds_until_ready)
