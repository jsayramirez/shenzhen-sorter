"""
Folder repair: recreates the Shenzhen Sorting Facility / Receipt Center
Console / Warehouse folder structure if any of it gets deleted, renamed,
or moved out from under the system, so a human doesn't have to notice a
missing folder before the system can operate again.

The intake/output folders (Shenzhen root, Shipped, Receipt Center Console,
Glove Box) are low-stakes to recreate on demand - they're containers the
system already creates lazily elsewhere (gui.main(), control.py's "open"
commands), so doing it explicitly here is just making that convenience a
named, user-triggered action.

The Warehouse (archive root) is NOT treated the same way: it holds the
entire archive, so a missing Warehouse could mean a genuine catastrophic
deletion, a disconnected/remapped drive, or a rename - not "nothing has
ever been archived yet". See preflight.archive_root_needs_manual_repair
for the ledger-history check this defers to before ever creating it.
"""

from dataclasses import dataclass, field

from config import settings
from . import preflight


@dataclass
class RepairResult:
    created: list = field(default_factory=list)
    already_present: list = field(default_factory=list)
    refused: list = field(default_factory=list)


def repair_folders() -> RepairResult:
    result = RepairResult()

    low_stakes = (
        ("Shenzhen Sorting Facility", settings.SHENZHEN_ROOT),
        ("Shipped", settings.SHIPPED_DIR),
        ("Receipt Center Console", settings.RECEIPT_CONSOLE_DIR),
        ("Glove Box", settings.GLOVE_BOX_DIR),
    )
    for label, path in low_stakes:
        if path.exists():
            result.already_present.append(label)
        else:
            path.mkdir(parents=True, exist_ok=True)
            result.created.append(label)

    if settings.ARCHIVE_ROOT.exists():
        result.already_present.append("Warehouse")
        settings.ARCHIVE_RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
        return result

    history = preflight.archive_root_needs_manual_repair()
    if history is None:
        settings.ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
        settings.ARCHIVE_RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
        result.created.append("Warehouse")
    else:
        detail = (f"{history} previously archived file(s) on record"
                   if history >= 0 else "the ledger itself could not be read")
        result.refused.append(
            f"Warehouse ({settings.ARCHIVE_ROOT}) is missing, but {detail}. "
            "Not recreating automatically - confirm the drive is connected and "
            "the folder wasn't just renamed or moved before creating a fresh "
            "empty one."
        )

    return result
