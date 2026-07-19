"""
Human controls (spec section 12.6): Start/Resume, Safe Stop, Status, plus
the two "open folder" conveniences. Exposed as a CLI here; wire to desktop
shortcuts/.bat files (spec allows this - "a giant dashboard is not
required" per section 12.7) rather than a full GUI for v1.

Design note on Safe Stop, since this system runs as a periodically
scheduled task rather than a continuously-running daemon (see main.py):
each invocation processes one session to completion and exits, so there
is no "long batch to interrupt mid-flight" in the way a daemon would have.
Safe Stop instead means "don't start the *next* scheduled session" - it
sets a flag that run_session() checks before doing anything else. Because
crash recovery is already required to handle a hard kill safely (spec
section 7), this achieves the same guarantee (no false success, resumable
state) without needing signal-handling in a long-lived process.
"""

import argparse
import os
import time

from config import settings
from . import folder_repair, ledger, preflight


def is_safe_stopped() -> bool:
    return settings.SAFE_STOP_FLAG_PATH.exists()


def cmd_start():
    settings.SAFE_STOP_FLAG_PATH.unlink(missing_ok=True)
    paused = preflight.is_hard_paused()
    if paused:
        print(f"Cannot fully resume: still HARD PAUSED ({paused.get('reason')}).")
        print("Fix the underlying condition, then run start again - the next")
        print("session will re-run safety preflight automatically.")
    else:
        print("RUNNING. New scheduled sessions may proceed.")


def cmd_safe_stop():
    settings.SAFE_STOP_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings.SAFE_STOP_FLAG_PATH.write_text(str(time.time()))
    print("SAFE STOP set. No new sorting session will start until 'start' is run again.")
    print("Any session already in progress finishes its current file safely; nothing is killed mid-write.")


def cmd_status():
    if is_safe_stopped():
        print("Status: STOPPED (Safe Stop is active)")
    else:
        paused = preflight.is_hard_paused()
        if paused:
            print(f"Status: PAUSED - SAFETY CONDITION ({paused.get('reason')})")
            print(f"Details: {paused.get('details')}")
        else:
            print("Status: RUNNING (waiting for eligible folders / scheduled sessions)")

    try:
        ledger.init_db()
        with ledger.connection() as conn:
            last = conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        if last:
            txns = None
            with ledger.connection() as conn:
                txns = conn.execute(
                    "SELECT status, COUNT(*) as n FROM transactions WHERE receipt_id = ? GROUP BY status",
                    (last["receipt_id"],),
                ).fetchall()
            print(f"Last session: {last['receipt_id']} ({last['status']})")
            for row in txns:
                print(f"  {row['status']}: {row['n']}")
        else:
            print("Last session: none yet")
    except Exception as e:
        print(f"(could not read ledger: {e})")


def cmd_open_sorting_facility():
    settings.SHENZHEN_ROOT.mkdir(parents=True, exist_ok=True)
    os.startfile(str(settings.SHENZHEN_ROOT))


def cmd_open_receipt_console():
    settings.RECEIPT_CONSOLE_DIR.mkdir(parents=True, exist_ok=True)
    os.startfile(str(settings.RECEIPT_CONSOLE_DIR))


def cmd_repair_folders():
    result = folder_repair.repair_folders()
    if result.created:
        print(f"Created: {', '.join(result.created)}")
    if result.already_present:
        print(f"Already present: {', '.join(result.already_present)}")
    for msg in result.refused:
        print(f"REFUSED: {msg}")


def main():
    parser = argparse.ArgumentParser(prog="shenzhen-control")
    parser.add_argument("action", choices=[
        "start", "safe-stop", "status", "open-sorting-facility", "open-receipt-console",
        "repair-folders",
    ])
    args = parser.parse_args()
    {
        "start": cmd_start,
        "safe-stop": cmd_safe_stop,
        "status": cmd_status,
        "open-sorting-facility": cmd_open_sorting_facility,
        "open-receipt-console": cmd_open_receipt_console,
        "repair-folders": cmd_repair_folders,
    }[args.action]()


if __name__ == "__main__":
    main()
