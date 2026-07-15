"""
Receipt generation (spec sections 18-20): quick-glance-first HTML, written
to both the Desktop Receipt Center Console (user-facing inbox) and the
permanent Archive Receipts\\Year\\Month folder (ledger-derived, durable).

The ledger is the only source of truth this reads from - if the ledger
can't account for a session, this refuses to claim success (spec 21.2).
"""

import time
from collections import defaultdict
from pathlib import Path

from config import settings
from . import ledger
from .common import MONTH_NAMES as _MONTH_NAMES, esc as _esc


def _render(receipt_id: str, conn) -> str:
    session = conn.execute("SELECT * FROM sessions WHERE receipt_id = ?", (receipt_id,)).fetchone()
    dump_folders = conn.execute(
        "SELECT * FROM dump_folders WHERE receipt_id = ?", (receipt_id,)
    ).fetchall()
    txns = conn.execute(
        "SELECT * FROM transactions WHERE receipt_id = ?", (receipt_id,)
    ).fetchall()

    total = len(txns)
    committed = [t for t in txns if t["status"] == "COMMITTED"]
    errored = [t for t in txns if t["status"] == "ERROR"]
    unresolved = [t for t in txns if t["status"] not in ("COMMITTED",)]

    overall_ok = total > 0 and len(committed) == total
    top_status = "VERIFIED SUCCESSFUL SORTING" if overall_ok else "NEEDS ATTENTION"

    # Sorted-to breakdown: category (dedicated camera folder OR Smart Device
    # Media/<type>) -> count, keyed off each committed transaction's own
    # sorting_category / destination_path.
    dest_breakdown = defaultdict(int)
    for t in committed:
        dest_breakdown[t["sorting_category"] or "Unknown"] += 1

    folders_html = "".join(f"<li>{_esc(f['folder_name'])}</li>" for f in dump_folders)
    dest_html = "".join(f"<li>{_esc(cat)}: {n}</li>" for cat, n in sorted(dest_breakdown.items()))

    error_rows = ""
    for t in errored:
        error_rows += f"""
        <tr>
            <td>{_esc(t['original_filename'])}</td>
            <td>{_esc(t['source_path'])}</td>
            <td>{_esc(t['errors'])}</td>
            <td>Original file untouched by the sorter.</td>
            <td>Not marked successfully archived.</td>
            <td>Safe to retry only this file after the cause is corrected.</td>
        </tr>"""

    started = time.strftime("%B %d, %Y - %I:%M %p", time.localtime(session["started_at"])) if session else "?"
    completed = (
        time.strftime("%B %d, %Y - %I:%M %p", time.localtime(session["completed_at"]))
        if session and session["completed_at"] else "In progress"
    )

    console_path = settings.RECEIPT_CONSOLE_DIR / f"{receipt_id}.html"
    archive_path = _archive_receipt_path(receipt_id, session)

    return f"""<!doctype html>
<meta charset="utf-8">
<title>{_esc(receipt_id)} - {_esc(top_status)}</title>
<style>
  body {{ font-family: Consolas, "Courier New", monospace; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.1rem; letter-spacing: 0.05em; }}
  .status-ok {{ color: #0a7a2f; }}
  .status-attn {{ color: #b3401a; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  td, th {{ border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; font-size: 0.9rem; }}
  hr {{ border: none; border-top: 1px dashed #999; }}
  .quick-glance {{ border: 2px solid #333; padding: 1rem; }}
</style>

<div class="quick-glance">
<h1 class="{'status-ok' if overall_ok else 'status-attn'}">{_esc(top_status)}</h1>
<p><b>FOLDERS RECEIVED</b></p>
<ul>{folders_html}</ul>
<p><b>SORTED TO</b></p>
<ul>{dest_html}</ul>
<hr>
<p><b>{len(committed)} / {total} files verified</b> &nbsp; {len(errored)} error(s)</p>
</div>

<h2>Original Dump Details</h2>
<table>
<tr><th>Dump folder</th><th>Files</th></tr>
{"".join(f"<tr><td>{_esc(f['folder_name'])}</td><td>{sum(1 for t in txns if t['dump_folder_id']==f['id'])}</td></tr>" for f in dump_folders)}
</table>

<h2>Sorting Results</h2>
<table>
<tr><th>Category / camera</th><th>Files committed</th></tr>
{"".join(f"<tr><td>{_esc(cat)}</td><td>{n}</td></tr>" for cat, n in sorted(dest_breakdown.items()))}
</table>

{"<h2>Error Details</h2><table><tr><th>File</th><th>Source folder</th><th>Problem</th><th>Source state</th><th>Destination state</th><th>Retry</th></tr>" + error_rows + "</table>" if errored else ""}

<h2>Verification</h2>
<table>
<tr><td>Files found</td><td>{total}</td></tr>
<tr><td>Files successfully sorted</td><td>{len(committed)} / {total}</td></tr>
<tr><td>Files verified identical</td><td>{len(committed)} / {total}</td></tr>
<tr><td>Destination files confirmed</td><td>{len(committed)} / {total}</td></tr>
<tr><td>Errors</td><td>{len(errored)}</td></tr>
<tr><td>Unresolved files</td><td>{len(unresolved)}</td></tr>
</table>

<h2>Receipt Details</h2>
<table>
<tr><td>Receipt ID</td><td>{_esc(receipt_id)}</td></tr>
<tr><td>Started</td><td>{_esc(started)}</td></tr>
<tr><td>Completed</td><td>{_esc(completed)}</td></tr>
<tr><td>Center Console copy</td><td>{_esc(console_path)}</td></tr>
<tr><td>Permanent archive copy</td><td>{_esc(archive_path)}</td></tr>
</table>
"""


def _archive_receipt_path(receipt_id: str, session) -> Path:
    ts = session["started_at"] if session else time.time()
    lt = time.localtime(ts)
    return settings.ARCHIVE_RECEIPTS_DIR / str(lt.tm_year) / _MONTH_NAMES[lt.tm_mon - 1] / f"{receipt_id}.html"


def generate_receipt(receipt_id: str) -> tuple[Path, Path]:
    with ledger.connection() as conn:
        body = _render(receipt_id, conn)
        session = conn.execute("SELECT * FROM sessions WHERE receipt_id = ?", (receipt_id,)).fetchone()

    console_path = settings.RECEIPT_CONSOLE_DIR / f"{receipt_id}.html"
    archive_path = _archive_receipt_path(receipt_id, session)

    console_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    console_path.write_text(body, encoding="utf-8")
    archive_path.write_text(body, encoding="utf-8")
    return console_path, archive_path
