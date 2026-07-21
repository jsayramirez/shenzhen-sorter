"""
Entry point. Run from anywhere with:
    python S:\\shenzhen-sorter\\main.py run             - one sorting session (dry run by default)
    python S:\\shenzhen-sorter\\main.py run --live       - one REAL sorting session
    python S:\\shenzhen-sorter\\main.py run --live --manual
        - REAL session using the short manual anti-mid-write guard instead of
          the full conservative wait. Only use this when you're personally
          confirming the files have actually finished arriving - a scheduled/
          unattended run must never use --manual (see pipeline.run_session's
          docstring). The control panel's "Manual Onboarding" button always
          runs this way.
    python S:\\shenzhen-sorter\\main.py status
    python S:\\shenzhen-sorter\\main.py start
    python S:\\shenzhen-sorter\\main.py safe-stop
    python S:\\shenzhen-sorter\\main.py open-sorting-facility
    python S:\\shenzhen-sorter\\main.py open-receipt-console
    python S:\\shenzhen-sorter\\main.py open-warehouse
    python S:\\shenzhen-sorter\\main.py repair-folders
        - Recreates the Shenzhen Sorting Facility / Receipt Center Console /
          Warehouse folder structure if any of it was deleted, renamed, or
          moved. The Warehouse itself is only ever recreated automatically
          if the ledger has no prior archived content on record - otherwise
          this refuses and tells you why, rather than risk masking real
          data loss or a disconnected/renamed drive.
    python S:\\shenzhen-sorter\\main.py browse-month <year> <month>
        - Read-only: collates every archived file captured that Year/Month,
          across every camera and Smart Device Media, into one disposable
          hardlinked folder and opens it in Explorer. Nothing in the real
          archive is moved or copied by this - it's just a temporary,
          always-regenerated view for browsing. Copying a file out of the
          view into your own hand-made trip folder produces a real,
          independent copy, same as copying it from its original location.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from shenzhen_sorter import control, pipeline  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    action = sys.argv[1]
    if action == "run":
        live = "--live" in sys.argv[2:]
        manual = "--manual" in sys.argv[2:]
        result = pipeline.run_session(dry_run=not live, manual=manual)
        if result.paused_reason and not result.receipt_id:
            # Paused before any session started (safe-stop, prior hard-pause,
            # preflight failure, etc.) - nothing was processed at all.
            print(f"Not running: {result.paused_reason}")
            for note in result.notes:
                print(f"  note: {note}")
            return
        if not result.receipt_id:
            print("Nothing to do (no eligible stable files).")
            for note in result.notes:
                print(f"  note: {note}")
            return
        print(f"Session {result.receipt_id}: Verified and Sorted {result.committed}/{result.total}, "
              f"{len(result.errors)} error(s).")
        if result.paused_reason:
            # Pause triggered mid-session (e.g. disk filled during copy) - the
            # session above is real and already has a receipt; this is a
            # separate, additional stop condition for anything after it.
            print(f"HARD PAUSED: {result.paused_reason}. Manual 'start' + fresh preflight required before the next run.")
        for note in result.notes:
            print(f"  note: {note}")
        for err in result.errors:
            print(f"  error: {err}")
    elif action == "status":
        control.cmd_status()
    elif action == "start":
        control.cmd_start()
    elif action == "safe-stop":
        control.cmd_safe_stop()
    elif action == "open-sorting-facility":
        control.cmd_open_sorting_facility()
    elif action == "open-receipt-console":
        control.cmd_open_receipt_console()
    elif action == "open-warehouse":
        control.cmd_open_warehouse()
    elif action == "repair-folders":
        control.cmd_repair_folders()
    elif action == "browse-month":
        if len(sys.argv) < 4:
            print("Usage: browse-month <year> <month>")
            return
        control.cmd_browse_month(int(sys.argv[2]), int(sys.argv[3]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
