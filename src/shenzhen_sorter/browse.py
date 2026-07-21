"""
Browse Month: a read-only, disposable view collating one month's archived
files across every camera/device into one flat folder, so File Explorer's
own thumbnail view can be used as a gallery without building one.

Uses NTFS hardlinks, not copies - a hardlink is a second directory entry
for the exact same bytes on disk, so building the view costs no extra
disk space and is effectively instant, and copying *out* of the view
(e.g. into a hand-made trip folder) behaves exactly like copying any
ordinary file, producing a real independent copy. Hardlinks can't cross
drive letters, which is why MONTH_VIEW_DIR lives on the same volume (S:)
as ARCHIVE_ROOT, but deliberately outside it (see settings.py) so
Compliance Check's whole-Warehouse search never has to consider it.

This never touches the real archive - it only reads the ledger and
creates/removes links in the disposable scratch folder.

By default, settings.BROWSE_MONTH_EXCLUDED_CATEGORIES leaves screenshots,
screen recordings, and other Smart Device Media junk categories out of
the view - see that constant's docstring for the full list and rationale.
This only affects what gets linked here; every excluded file is still
archived and fully preserved exactly as always.
"""

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from config import settings
from . import ledger


@dataclass
class MonthViewResult:
    view_dir: Path
    linked: int
    missing: list  # destination_paths the ledger recorded but weren't found on disk


def available_months() -> list[tuple[int, int, int]]:
    """(year, month, count) for every Year/Month with archived content on
    record, most recent first - the data source for the GUI's Browse
    Month picker, so it only ever offers choices that have something to
    show rather than a free-text field the user has to guess at. Counts
    already reflect BROWSE_MONTH_EXCLUDED_CATEGORIES, so what the picker
    shows matches what build_month_view will actually link."""
    ledger.init_db()
    with ledger.connection() as conn:
        months = ledger.months_with_content(conn, exclude_categories=settings.BROWSE_MONTH_EXCLUDED_CATEGORIES)
    return sorted(months, reverse=True)


def _non_colliding_name(used_names: set, name: str) -> str:
    if name not in used_names:
        return name
    stem, dot, ext = name.rpartition(".")
    stem, ext = (stem, "." + ext) if dot else (name, "")
    counter = 2
    while True:
        candidate = f"{stem} ({counter}){ext}"
        if candidate not in used_names:
            return candidate
        counter += 1


def build_month_view(year: int, month: int) -> MonthViewResult:
    view_dir = settings.MONTH_VIEW_DIR
    if view_dir.exists():
        shutil.rmtree(view_dir)
    view_dir.mkdir(parents=True, exist_ok=True)

    ledger.init_db()
    with ledger.connection() as conn:
        rows = ledger.committed_in_month(conn, year, month, exclude_categories=settings.BROWSE_MONTH_EXCLUDED_CATEGORIES)

    linked = 0
    missing = []
    used_names = set()
    for row in rows:
        source = Path(row["destination_path"])
        if not source.exists():
            missing.append(row["destination_path"])
            continue
        link_name = _non_colliding_name(used_names, source.name)
        used_names.add(link_name)
        os.link(source, view_dir / link_name)
        linked += 1

    return MonthViewResult(view_dir=view_dir, linked=linked, missing=missing)
