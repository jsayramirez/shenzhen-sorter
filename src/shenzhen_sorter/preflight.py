"""
Free-space preflight and persistent hard-pause (spec section 12).

Hard-pause state is a plain JSON file on disk, deliberately outside the
SQLite ledger, so that "are we paused" can be checked even if the ledger
itself is ever suspect (spec 12.3 lists ledger inconsistency as its own
hard-pause trigger - the pause flag can't depend on the thing it might
need to flag as broken). It survives process restart and reboot by
construction (it's just a file); nothing in this module ever clears it
except an explicit resume() call after preflight passes again.
"""

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from config import settings


class HardPaused(RuntimeError):
    pass


@dataclass
class PreflightResult:
    ok: bool
    required_bytes: int
    available_bytes: int
    reason: str = ""


def estimate_batch_size(file_paths: list[Path]) -> int:
    total = 0
    for p in file_paths:
        try:
            total += p.stat().st_size
        except FileNotFoundError:
            continue
    return total


def largest_file_size(file_paths: list[Path]) -> int:
    sizes = []
    for p in file_paths:
        try:
            sizes.append(p.stat().st_size)
        except FileNotFoundError:
            continue
    return max(sizes) if sizes else 0


def required_free_space(batch_size: int, largest_file: int, reserve: int = None) -> int:
    reserve = reserve if reserve is not None else settings.SAFETY_RESERVE_BYTES
    # batch_size (final destinations) + largest_file (the one .partial that
    # can exist alongside its not-yet-deleted predecessor's final copy,
    # since v1 copies one file at a time) + configurable safety reserve.
    return batch_size + largest_file + reserve


def check_destination_free_space(destination_drive: Path, required_bytes: int) -> PreflightResult:
    usage = shutil.disk_usage(str(destination_drive))
    ok = usage.free >= required_bytes
    reason = "" if ok else (
        f"Insufficient free space on {destination_drive}: "
        f"required {required_bytes / 1024**3:.1f} GB, available {usage.free / 1024**3:.1f} GB"
    )
    return PreflightResult(ok=ok, required_bytes=required_bytes, available_bytes=usage.free, reason=reason)


def destination_is_writable(destination_root: Path) -> bool:
    """Probes inside the actual archive root (creating it if needed), never
    the bare drive root - writing to a drive's root directory can fail on
    permissions grounds unrelated to whether OUR destination is writable,
    and would litter a probe file somewhere we don't otherwise touch."""
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
        probe = destination_root / f".write_probe_{os.getpid()}.tmp"
        probe.write_bytes(b"0")
        probe.unlink()
        return True
    except OSError:
        return False


def enter_hard_pause(reason: str, details: dict) -> None:
    path = settings.HARD_PAUSE_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "paused_at": time.time(),
        "reason": reason,
        "details": details,
    }, indent=2))


def is_hard_paused() -> dict | None:
    path = settings.HARD_PAUSE_STATE_PATH
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        # A corrupt pause-state file is itself a critical-ledger-adjacent
        # inconsistency (spec 12.3) - stay paused rather than guess.
        return {"reason": "hard_pause_state_file_corrupt", "details": {}}


def clear_hard_pause() -> None:
    settings.HARD_PAUSE_STATE_PATH.unlink(missing_ok=True)


def run_preflight(destination_root: Path, file_paths: list[Path]) -> PreflightResult:
    """Call before any new copying begins, and again on manual Start/Resume
    after a hard pause (spec 12.6: resume must re-run all safety preflight).

    destination_root should be the actual archive root (e.g. settings.ARCHIVE_ROOT),
    not a bare drive letter - free space is checked against its drive, but the
    writability probe happens inside it so it never touches a drive's root dir.
    """
    destination_drive = Path(destination_root.drive + "\\")

    if not destination_drive.exists():
        result = PreflightResult(ok=False, required_bytes=0, available_bytes=0,
                                  reason=f"Destination drive {destination_drive} is unavailable")
        enter_hard_pause("destination_unavailable", {"drive": str(destination_drive)})
        return result

    if not destination_is_writable(destination_root):
        result = PreflightResult(ok=False, required_bytes=0, available_bytes=0,
                                  reason=f"Destination {destination_root} is read-only")
        enter_hard_pause("destination_read_only", {"destination": str(destination_root)})
        return result

    batch_size = estimate_batch_size(file_paths)
    largest = largest_file_size(file_paths)
    required = required_free_space(batch_size, largest)
    result = check_destination_free_space(destination_drive, required)
    if not result.ok:
        enter_hard_pause("insufficient_free_space", {
            "drive": str(destination_drive),
            "required_bytes": result.required_bytes,
            "available_bytes": result.available_bytes,
        })
        return result

    clear_hard_pause()
    return result
