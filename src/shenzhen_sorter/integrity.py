"""
Archive integrity verification - two purpose-built checks, manual only
(no automatic background schedule - see the design discussion this came
from: an automatic periodic scan either floods Receipt Center Console
with "nothing wrong" receipts most cycles, or needs real delta-tracking
complexity to avoid it, neither of which a single-user tool needs. A
manual check sidesteps both - every run is deliberate, so every run
earning a receipt is correct, not spam).

COMPLIANCE CHECK - general archive health. Cross-references the ledger's
COMMITTED transactions against what's actually on disk under the archive
root, folder by folder. A file not at its recorded path is NOT immediately
a problem - reorganizing your own archive (making a new folder, moving
things into it) is explicitly supported, not penalized. Before calling
anything missing, the rest of the Warehouse is searched (by size, then a
hash re-check) for the same content elsewhere; if found, the ledger's
bookkeeping is quietly updated to the new location and it's logged as
"relocated," not "missing" - the ORIGINAL FILE bytes are never touched
during this. Only content that can't be found anywhere counts as missing.

The CLEAR/NOT CLEAR verdict compares against the PREVIOUS Compliance
Check's snapshot, not the ledger's original expectations forever - if you
deliberately delete something, the first check after that says NOT CLEAR
and names it; running the check again with nothing further changed says
CLEAR, because nothing NEW happened since last time. This is what avoids
needing a "review and confirm every missing file" workflow - simply
running the check again after your own intentional change accepts it as
the new baseline. A secondary, non-blocking line still reports total
drift from the original ledger record, so that number is never hidden,
even though it doesn't block anything.

DELIVERY CHECK - a narrower, different question: is everything currently
sitting in Shipped confirmed safely archived, so it's safe to delete or
clean out? Matched by content hash (not path) against the ledger, then
the same anywhere-in-the-Warehouse search as Compliance Check. Always a
fresh evaluation of Shipped's current contents - there's nothing to diff
against from a prior run, unlike Compliance Check.
"""

import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from config import settings
from . import copy_verify, ledger
from .common import MONTH_NAMES as _MONTH_NAMES, esc as _esc

ProgressCallback = Optional[Callable[[int, int], None]]


# ---------------------------------------------------------------------------
# Shared: locate content anywhere under the Warehouse, not just its recorded path
# ---------------------------------------------------------------------------

def _build_size_index(archive_root: Path) -> dict:
    """One walk of the whole Warehouse, size -> [paths]. Built once per
    check and reused for every lookup, rather than re-walking per file."""
    index = defaultdict(list)
    for dirpath, _dirnames, filenames in os.walk(archive_root):
        for fn in filenames:
            p = Path(dirpath) / fn
            try:
                size = p.stat().st_size
            except OSError:
                continue
            index[size].append(p)
    return index


def _find_elsewhere(size_index: dict, expected_size: int, expected_sha256: str,
                     exclude: Optional[Path] = None) -> Optional[Path]:
    """Searches the whole-Warehouse size index for a file matching the
    expected size, then confirms by hash. Cheap in the common case (most
    sizes have few candidates); only hashes files that are plausible
    matches, not the entire archive."""
    for candidate in size_index.get(expected_size, []):
        if exclude is not None and candidate == exclude:
            continue
        try:
            _, candidate_hash = copy_verify.hash_file_independent(candidate)
        except OSError:
            continue
        if candidate_hash == expected_sha256:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Compliance Check
# ---------------------------------------------------------------------------

@dataclass
class ComplianceProblem:
    original_filename: str
    expected_folder: str
    receipt_id: str


@dataclass
class ComplianceRelocation:
    original_filename: str
    old_path: str
    new_path: str


@dataclass
class ComplianceReport:
    started_at: float
    completed_at: float
    deep_verify: bool
    total_committed: int
    new_missing: list = field(default_factory=list)       # list[ComplianceProblem] - the actual verdict driver
    still_missing_known: int = 0                            # already-accepted from a prior check, not re-alarmed
    resolved: list = field(default_factory=list)             # list[str filenames] - missing last time, found now
    relocations: list = field(default_factory=list)          # list[ComplianceRelocation]
    corrupted: list = field(default_factory=list)            # list[ComplianceProblem] - only if deep_verify
    total_drift_from_original: int = 0                       # informational only, never blocks

    @property
    def clear(self) -> bool:
        return len(self.new_missing) == 0 and len(self.corrupted) == 0


def _load_compliance_snapshot() -> dict:
    try:
        return json.loads(settings.LAST_COMPLIANCE_CHECK_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"missing_paths": []}


def _save_compliance_snapshot(missing_paths: list, summary: dict) -> None:
    settings.LAST_COMPLIANCE_CHECK_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings.LAST_COMPLIANCE_CHECK_PATH.write_text(json.dumps({
        **summary,
        "missing_paths": missing_paths,
    }))


def load_last_compliance_state() -> Optional[dict]:
    try:
        return json.loads(settings.LAST_COMPLIANCE_CHECK_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def run_compliance_check(deep_verify: bool = False, progress_callback: ProgressCallback = None) -> ComplianceReport:
    ledger.init_db()
    started_at = time.time()

    with ledger.connection() as conn:
        rows = conn.execute("SELECT * FROM transactions WHERE status = 'COMMITTED'").fetchall()

    total = len(rows)
    size_index = _build_size_index(settings.ARCHIVE_ROOT)
    prior_missing = set(_load_compliance_snapshot().get("missing_paths", []))

    currently_missing_paths = []
    new_missing = []
    relocations = []
    corrupted = []
    updates_needed = []  # (txn_id, new_path) to write back to the ledger

    for i, row in enumerate(rows):
        dest = row["destination_path"]
        if not dest:
            continue
        dest_path = Path(dest)
        folder = str(dest_path.parent.relative_to(settings.ARCHIVE_ROOT)) if dest_path.is_relative_to(settings.ARCHIVE_ROOT) else str(dest_path.parent)

        if dest_path.exists():
            if deep_verify and row["destination_sha256"]:
                _, actual_hash = copy_verify.hash_file_independent(dest_path)
                if actual_hash != row["destination_sha256"]:
                    corrupted.append(ComplianceProblem(row["original_filename"], folder, row["receipt_id"]))
        else:
            # Not at its recorded spot - search the rest of the Warehouse by
            # content before ever calling this "missing". Reorganizing your
            # own archive must never be penalized.
            found = None
            if row["destination_size"] is not None and row["destination_sha256"]:
                found = _find_elsewhere(size_index, row["destination_size"], row["destination_sha256"], exclude=dest_path)
            if found is not None:
                relocations.append(ComplianceRelocation(row["original_filename"], dest, str(found)))
                updates_needed.append((row["txn_id"], str(found)))
            else:
                currently_missing_paths.append(dest)
                if dest not in prior_missing:
                    new_missing.append(ComplianceProblem(row["original_filename"], folder, row["receipt_id"]))

        if progress_callback and (i % 20 == 0 or i == total - 1):
            progress_callback(i + 1, total)

    if updates_needed:
        with ledger.connection() as conn:
            for txn_id, new_path in updates_needed:
                ledger.set_status(conn, txn_id, "COMMITTED", destination_path=new_path,
                                   warnings="relocated_within_warehouse_by_compliance_check")

    resolved = [p for p in prior_missing if p not in currently_missing_paths]
    still_missing_known = len(currently_missing_paths) - len(new_missing)

    report = ComplianceReport(
        started_at=started_at, completed_at=time.time(), deep_verify=deep_verify,
        total_committed=total, new_missing=new_missing, still_missing_known=still_missing_known,
        resolved=[Path(p).name for p in resolved], relocations=relocations, corrupted=corrupted,
        total_drift_from_original=len(currently_missing_paths),
    )

    _save_compliance_snapshot(currently_missing_paths, {
        "completed_at": report.completed_at,
        "total_committed": total,
        "problem_count": len(new_missing) + len(corrupted),
        "total_missing": len(currently_missing_paths),
        "deep_verify": deep_verify,
    })
    return report


# ---------------------------------------------------------------------------
# Delivery Check
# ---------------------------------------------------------------------------

@dataclass
class DeliveryProblem:
    filename: str
    shipped_path: str


@dataclass
class DeliveryReport:
    started_at: float
    completed_at: float
    total_in_shipped: int
    confirmed: list = field(default_factory=list)   # list[str filenames]
    unconfirmed: list = field(default_factory=list)  # list[DeliveryProblem]

    @property
    def clear(self) -> bool:
        return len(self.unconfirmed) == 0


def _save_last_delivery_state(report: DeliveryReport) -> None:
    settings.LAST_DELIVERY_CHECK_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings.LAST_DELIVERY_CHECK_PATH.write_text(json.dumps({
        "completed_at": report.completed_at,
        "total_in_shipped": report.total_in_shipped,
        "unconfirmed_count": len(report.unconfirmed),
    }))


def load_last_delivery_state() -> Optional[dict]:
    try:
        return json.loads(settings.LAST_DELIVERY_CHECK_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def run_delivery_check(progress_callback: ProgressCallback = None) -> DeliveryReport:
    ledger.init_db()
    started_at = time.time()

    shipped_files = []
    if settings.SHIPPED_DIR.exists():
        for dirpath, _dirnames, filenames in os.walk(settings.SHIPPED_DIR):
            for fn in filenames:
                shipped_files.append(Path(dirpath) / fn)

    total = len(shipped_files)
    confirmed, unconfirmed = [], []

    if total:
        size_index = _build_size_index(settings.ARCHIVE_ROOT)
        with ledger.connection() as conn:
            for i, shipped_path in enumerate(shipped_files):
                try:
                    size = shipped_path.stat().st_size
                    _, file_hash = copy_verify.hash_file_independent(shipped_path)
                except OSError:
                    unconfirmed.append(DeliveryProblem(shipped_path.name, str(shipped_path)))
                    continue

                txn = conn.execute(
                    "SELECT * FROM transactions WHERE source_sha256 = ? AND status = 'COMMITTED' "
                    "ORDER BY commit_time DESC LIMIT 1",
                    (file_hash,),
                ).fetchone()

                archived_ok = False
                if txn is not None:
                    dest_path = Path(txn["destination_path"]) if txn["destination_path"] else None
                    if dest_path is not None and dest_path.exists():
                        archived_ok = True
                    elif txn["destination_size"] is not None and txn["destination_sha256"]:
                        archived_ok = _find_elsewhere(size_index, txn["destination_size"], txn["destination_sha256"]) is not None

                if archived_ok:
                    confirmed.append(shipped_path.name)
                else:
                    unconfirmed.append(DeliveryProblem(shipped_path.name, str(shipped_path)))

                if progress_callback and (i % 20 == 0 or i == total - 1):
                    progress_callback(i + 1, total)

    report = DeliveryReport(
        started_at=started_at, completed_at=time.time(), total_in_shipped=total,
        confirmed=confirmed, unconfirmed=unconfirmed,
    )
    _save_last_delivery_state(report)
    return report


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def _receipt_paths(receipt_id: str, subfolder: str, when: float) -> tuple[Path, Path]:
    console_path = settings.RECEIPT_CONSOLE_DIR / "Integrity Checks" / subfolder / f"{receipt_id}.html"
    lt = time.localtime(when)
    archive_path = (settings.ARCHIVE_RECEIPTS_DIR / "Integrity Checks" / subfolder
                    / str(lt.tm_year) / _MONTH_NAMES[lt.tm_mon - 1] / f"{receipt_id}.html")
    return console_path, archive_path


def _write_receipt(console_path: Path, archive_path: Path, body: str) -> None:
    console_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    console_path.write_text(body, encoding="utf-8")
    archive_path.write_text(body, encoding="utf-8")


_RECEIPT_STYLE = """
  body { font-family: Consolas, "Courier New", monospace; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
  h1 { font-size: 1.1rem; letter-spacing: 0.05em; }
  .status-ok { color: #0a7a2f; }
  .status-attn { color: #b3401a; }
  table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
  td, th { border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; font-size: 0.9rem; }
  hr { border: none; border-top: 1px dashed #999; }
  .quick-glance { border: 2px solid #333; padding: 1rem; }
"""


def generate_compliance_receipt(report: ComplianceReport) -> tuple[Path, Path]:
    receipt_id = ledger.new_receipt_id("CHK")
    top_status = "CLEAR" if report.clear else "NOT CLEAR"
    console_path, archive_path = _receipt_paths(receipt_id, "Compliance Check", report.completed_at)

    new_missing_rows = "".join(
        f"<tr><td>{_esc(p.original_filename)}</td><td>{_esc(p.expected_folder)}</td><td>{_esc(p.receipt_id)}</td></tr>"
        for p in report.new_missing
    )
    corrupted_rows = "".join(
        f"<tr><td>{_esc(p.original_filename)}</td><td>{_esc(p.expected_folder)}</td><td>{_esc(p.receipt_id)}</td></tr>"
        for p in report.corrupted
    )
    relocation_rows = "".join(
        f"<tr><td>{_esc(r.original_filename)}</td><td>{_esc(r.old_path)}</td><td>{_esc(r.new_path)}</td></tr>"
        for r in report.relocations
    )

    confirmed_intact = report.total_committed - report.total_drift_from_original - len(report.corrupted)
    glance_line = (
        f"<b>{confirmed_intact} / {report.total_committed} "
        f"confirmed intact</b> &nbsp; {len(report.new_missing)} new problem(s)"
        + (f" &nbsp; {len(report.corrupted)} corrupted" if report.deep_verify else "")
    )

    started = time.strftime("%B %d, %Y - %I:%M %p", time.localtime(report.started_at))
    completed = time.strftime("%B %d, %Y - %I:%M %p", time.localtime(report.completed_at))

    body = f"""<!doctype html>
<meta charset="utf-8">
<title>{_esc(receipt_id)} - {_esc(top_status)}</title>
<style>{_RECEIPT_STYLE}</style>

<div class="quick-glance">
<h1 class="{'status-ok' if report.clear else 'status-attn'}">{_esc(top_status)}</h1>
<p><b>Compliance Check</b> {"(deep content verify)" if report.deep_verify else "(location + size only)"}</p>
<hr>
<p>{glance_line}</p>
{"<p>New since last check - not yet accounted for:</p>" if report.new_missing else ""}
</div>

{"<h2>New Missing Since Last Check</h2><table><tr><th>File</th><th>Expected folder</th><th>Originally archived by</th></tr>" + new_missing_rows + "</table>" if report.new_missing else ""}

{"<h2>Corrupted (content changed)</h2><table><tr><th>File</th><th>Folder</th><th>Originally archived by</th></tr>" + corrupted_rows + "</table>" if report.corrupted else ""}

{"<h2>Relocated Within the Warehouse</h2><p>Found elsewhere - not a problem, bookkeeping updated to match.</p><table><tr><th>File</th><th>Old recorded location</th><th>Found at</th></tr>" + relocation_rows + "</table>" if report.relocations else ""}

{"<h2>Resolved Since Last Check</h2><p>Previously missing, now found again: " + _esc(", ".join(report.resolved)) + "</p>" if report.resolved else ""}

<h2>Context</h2>
<table>
<tr><td>Already-known missing (from a prior check, not newly alarmed)</td><td>{report.still_missing_known}</td></tr>
<tr><td>Total drift from original archive record (informational only)</td><td>{report.total_drift_from_original}</td></tr>
</table>

<h2>Receipt Details</h2>
<table>
<tr><td>Receipt ID</td><td>{_esc(receipt_id)}</td></tr>
<tr><td>Check type</td><td>Compliance Check{" - deep verify" if report.deep_verify else ""}</td></tr>
<tr><td>Started</td><td>{_esc(started)}</td></tr>
<tr><td>Completed</td><td>{_esc(completed)}</td></tr>
<tr><td>Files tracked</td><td>{report.total_committed}</td></tr>
<tr><td>Center Console copy</td><td>{_esc(console_path)}</td></tr>
<tr><td>Permanent archive copy</td><td>{_esc(archive_path)}</td></tr>
</table>
"""
    _write_receipt(console_path, archive_path, body)
    return console_path, archive_path


def generate_delivery_receipt(report: DeliveryReport) -> tuple[Path, Path]:
    receipt_id = ledger.new_receipt_id("CHK")
    top_status = "CLEAR" if report.clear else "NOT CLEAR"
    console_path, archive_path = _receipt_paths(receipt_id, "Delivery Check", report.completed_at)

    unconfirmed_rows = "".join(
        f"<tr><td>{_esc(p.filename)}</td><td>{_esc(p.shipped_path)}</td></tr>"
        for p in report.unconfirmed
    )

    if report.total_in_shipped == 0:
        glance = "Shipped is empty. Nothing to check, nothing pending cleanup."
    elif report.clear:
        glance = f"{len(report.confirmed)} item(s) in Shipped are all confirmed safely archived. Safe to delete or move them."
    else:
        glance = f"{len(report.unconfirmed)} item(s) in Shipped are NOT confirmed archived. Do not delete these yet."

    started = time.strftime("%B %d, %Y - %I:%M %p", time.localtime(report.started_at))
    completed = time.strftime("%B %d, %Y - %I:%M %p", time.localtime(report.completed_at))

    body = f"""<!doctype html>
<meta charset="utf-8">
<title>{_esc(receipt_id)} - {_esc(top_status)}</title>
<style>{_RECEIPT_STYLE}</style>

<div class="quick-glance">
<h1 class="{'status-ok' if report.clear else 'status-attn'}">{_esc(top_status)}</h1>
<p><b>Delivery Check</b> - is everything in Shipped confirmed archived?</p>
<hr>
<p>{_esc(glance)}</p>
</div>

{"<h2>Not Confirmed</h2><table><tr><th>File</th><th>Location in Shipped</th></tr>" + unconfirmed_rows + "</table>" if report.unconfirmed else ""}

<h2>Receipt Details</h2>
<table>
<tr><td>Receipt ID</td><td>{_esc(receipt_id)}</td></tr>
<tr><td>Check type</td><td>Delivery Check</td></tr>
<tr><td>Started</td><td>{_esc(started)}</td></tr>
<tr><td>Completed</td><td>{_esc(completed)}</td></tr>
<tr><td>Items in Shipped</td><td>{report.total_in_shipped}</td></tr>
<tr><td>Center Console copy</td><td>{_esc(console_path)}</td></tr>
<tr><td>Permanent archive copy</td><td>{_esc(archive_path)}</td></tr>
</table>
"""
    _write_receipt(console_path, archive_path, body)
    return console_path, archive_path
