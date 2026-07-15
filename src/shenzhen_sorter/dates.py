"""
Date/chronology resolution (spec section 10).

Priority: confident embedded capture date first, filesystem mtime only as
a controlled fallback, and never fabricate certainty for impossible/absurd
dates. This module only handles the "plausibility" gate; the actual
per-format metadata reading lives in classify.py (and is currently
JPEG-only - see that module's docstring for why).
"""

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MIN_PLAUSIBLE_YEAR = 1995   # earliest year any camera in this project's config could have shot
FUTURE_SLACK_DAYS = 2       # allow small clock drift, not a genuinely "future" date


@dataclass
class ResolvedDate:
    year: int
    month: int
    source: str            # "embedded" | "filesystem_fallback"
    confident: bool
    warning: Optional[str] = None


def _parse_exif_datetime(value: str) -> Optional[datetime.datetime]:
    # EXIF DateTimeOriginal format: "YYYY:MM:DD HH:MM:SS"
    try:
        return datetime.datetime.strptime(value.strip(), "%Y:%m:%d %H:%M:%S")
    except (ValueError, AttributeError):
        return None


def _is_plausible(dt: datetime.datetime) -> bool:
    now = datetime.datetime.now()
    if dt.year < MIN_PLAUSIBLE_YEAR:
        return False
    if dt > now + datetime.timedelta(days=FUTURE_SLACK_DAYS):
        return False
    return True


def resolve_date(capture_datetime: Optional[str], source_path: Path) -> ResolvedDate:
    if capture_datetime:
        dt = _parse_exif_datetime(capture_datetime)
        if dt is not None and _is_plausible(dt):
            return ResolvedDate(year=dt.year, month=dt.month, source="embedded", confident=True)

    # Controlled fallback: filesystem mtime, but still gated for plausibility.
    # An implausible fallback is NOT silently used - the spec is explicit
    # that 1970-epoch-error and impossible-future folders must never be
    # created without warning.
    try:
        mtime = source_path.stat().st_mtime
        dt = datetime.datetime.fromtimestamp(mtime)
    except (FileNotFoundError, OSError, OverflowError):
        return ResolvedDate(year=0, month=0, source="filesystem_fallback", confident=False,
                             warning="No embedded date and filesystem timestamp unavailable")

    if _is_plausible(dt):
        return ResolvedDate(year=dt.year, month=dt.month, source="filesystem_fallback", confident=False,
                             warning="No confident embedded capture date; used filesystem timestamp")

    return ResolvedDate(year=0, month=0, source="filesystem_fallback", confident=False,
                         warning=f"Both embedded and filesystem dates are implausible ({dt.isoformat()})")
