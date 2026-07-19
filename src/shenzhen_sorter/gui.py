"""
Minimal control panel (spec section 12.7). Tkinter only - no extra
dependency, matches the spec's own preference for a small, obvious
control surface over a full dashboard.

Long-running actions (a real sorting session) run on a background thread
so the window never freezes; results come back to the UI thread through a
plain queue, polled on a timer - the standard safe pattern for Tkinter,
since Tkinter widgets must only be touched from the main thread.
"""

import queue
import shutil
import threading
import time
import tkinter as tk
from tkinter import messagebox, scrolledtext

from config import settings
from . import control, folder_repair, integrity, ledger, pipeline, preflight

REFRESH_MS = 3000     # how often we re-check real status/countdown against disk+ledger
TICK_MS = 1000        # how often the countdown label re-renders between real refreshes
MANUAL_HOLD_SECONDS = 10  # cancel window after pressing Manual Onboarding, before it actually runs


def _format_mmss(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def _format_pending_summary(file_count: int, folder_count: int) -> str:
    """Naming every individual batch stopped making sense once a real dump
    folder AND loose files could both be waiting at the same time - this
    gives a plain count of each instead, e.g. "5 files, 2 folders"."""
    parts = []
    if file_count:
        parts.append(f"{file_count} file{'s' if file_count != 1 else ''}")
    if folder_count:
        parts.append(f"{folder_count} folder{'s' if folder_count != 1 else ''}")
    return ", ".join(parts) if parts else "Nothing"


class ControlPanel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Shenzhen Sorting Facility - Control")
        self.geometry("580x740")
        self.resizable(False, False)

        self._work_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._busy = False
        self._countdown_has_pending = None  # None = not checked yet; False = nothing waiting; True = counting down
        self._countdown_remaining = None    # seconds, as of _countdown_synced_at
        self._countdown_synced_at = 0.0
        self._countdown_summary_text = ""
        self._hold_seconds_left = None      # None = no hold in progress
        self._hold_preview_only = False     # captured checkbox state at the moment of the original press

        self._build_widgets()
        self.protocol("WM_DELETE_WINDOW", self._on_close_attempt)
        self._refresh_status()
        self._tick_countdown()
        self._poll_queue()

    def _on_close_attempt(self):
        if self._busy:
            if not messagebox.askyesno(
                "Transfer in progress",
                "A sorting transfer is currently running. Closing this window now "
                "ends the program and will interrupt it mid-file.\n\n"
                "It's safe to just leave this window open in the background until "
                "it finishes. Close anyway?",
                parent=self,
            ):
                return
        self.destroy()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_widgets(self):
        header = tk.Label(self, text="CONTROL PANEL", font=("Segoe UI", 14, "bold"))
        header.pack(pady=(12, 4))

        self.status_var = tk.StringVar(value="Checking...")
        self.status_label = tk.Label(self, textvariable=self.status_var, font=("Segoe UI", 12, "bold"))
        self.status_label.pack(pady=(0, 2))

        self.detail_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self.detail_var, font=("Segoe UI", 9), wraplength=520, justify="left").pack(pady=(0, 4))

        self.last_session_var = tk.StringVar(value="Last session: (checking)")
        tk.Label(self, textvariable=self.last_session_var, font=("Segoe UI", 9), justify="left").pack(pady=(0, 2))

        self.disk_space_var = tk.StringVar(value="Checking free space...")
        self.disk_space_label = tk.Label(self, textvariable=self.disk_space_var, font=("Segoe UI", 9), justify="left")
        self.disk_space_label.pack(pady=(0, 2))

        self.compliance_var = tk.StringVar(value="Compliance check: never run")
        self.compliance_label = tk.Label(self, textvariable=self.compliance_var, font=("Segoe UI", 9), justify="left")
        self.compliance_label.pack(pady=(0, 2))

        self.delivery_var = tk.StringVar(value="Delivery check: never run")
        self.delivery_label = tk.Label(self, textvariable=self.delivery_var, font=("Segoe UI", 9), justify="left")
        self.delivery_label.pack(pady=(0, 6))

        self.countdown_var = tk.StringVar(value="Checking intake...")
        self.countdown_label = tk.Label(self, textvariable=self.countdown_var, font=("Segoe UI", 10, "bold"),
                                         wraplength=520, justify="center")
        self.countdown_label.pack(pady=(0, 10))

        self.preview_only_var = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="Preview only (no changes)", variable=self.preview_only_var).pack(pady=(0, 2))

        button_frame = tk.Frame(self)
        button_frame.pack(pady=4)

        self.manual_button = tk.Button(button_frame, text="MANUAL ONBOARDING", width=46,
                                        command=self._on_manual_button_press, font=("Segoe UI", 10, "bold"))
        self.manual_button.grid(row=0, column=0, columnspan=2, padx=4, pady=3)
        self._manual_button_default_bg = self.manual_button.cget("bg")
        self.start_button = tk.Button(button_frame, text="START / RESUME", width=22, command=self._on_start)
        self.start_button.grid(row=1, column=0, padx=4, pady=3)
        tk.Button(button_frame, text="SAFE STOP", width=22, command=self._on_safe_stop).grid(row=1, column=1, padx=4, pady=3)
        tk.Button(button_frame, text="OPEN SORTING FACILITY", width=22, command=self._on_open_facility).grid(row=2, column=0, padx=4, pady=3)
        tk.Button(button_frame, text="OPEN RECEIPT CONSOLE", width=22, command=self._on_open_console).grid(row=2, column=1, padx=4, pady=3)
        tk.Button(button_frame, text="TIMER SETTINGS", width=46, command=self._on_timer_settings).grid(row=3, column=0, columnspan=2, padx=4, pady=3)

        self.repair_folders_button = tk.Button(button_frame, text="REPAIR FOLDERS", width=46,
                                                command=self._on_repair_folders)
        self.repair_folders_button.grid(row=4, column=0, columnspan=2, padx=4, pady=3)

        self.deep_verify_var = tk.BooleanVar(value=False)
        tk.Checkbutton(button_frame, text="Deep verify (re-hash content, catches corruption - slower)",
                       variable=self.deep_verify_var).grid(row=5, column=0, columnspan=2, pady=(6, 0))

        self.compliance_check_button = tk.Button(button_frame, text="COMPLIANCE CHECK", width=22,
                                                  command=self._on_compliance_check)
        self.compliance_check_button.grid(row=6, column=0, padx=4, pady=3)
        self.delivery_check_button = tk.Button(button_frame, text="DELIVERY CHECK", width=22,
                                                command=self._on_delivery_check)
        self.delivery_check_button.grid(row=6, column=1, padx=4, pady=3)

        tk.Label(self, text="Activity log", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=12, pady=(10, 0))
        self.log = scrolledtext.ScrolledText(self, height=14, width=68, state="disabled", font=("Consolas", 9))
        self.log.pack(padx=12, pady=(2, 10))

    # ------------------------------------------------------------------
    # Status refresh (read-only, cheap - safe to run on a timer)
    # ------------------------------------------------------------------
    def _refresh_status(self):
        """The recurring entry point (scheduled once, reschedules itself).
        Button handlers that want an immediate refresh call
        _do_status_refresh() directly instead, so they don't accidentally
        spawn a second permanent self.after() chain on top of this one."""
        self._do_status_refresh()
        self.after(REFRESH_MS, self._refresh_status)

    def _do_status_refresh(self):
        if self._busy:
            # A real transfer is actively running right now - this always wins
            # over Safe Stop/paused display, since it's the thing actually
            # happening at this moment regardless of what's queued for after.
            self.status_var.set("Status: TRANSFERRING")
            self.status_label.config(fg="#1a5fb3")
            self.detail_var.set(
                "A file transfer is in progress. Do not close this window, disconnect "
                "S:\\ or the source device (camera/card/phone), or let the computer sleep "
                "until it finishes."
            )
        elif control.is_safe_stopped():
            self.status_var.set("Status: STOPPED")
            self.status_label.config(fg="#555555")
            self.detail_var.set("Safe Stop is active. Press START / RESUME to allow new sessions again.")
        else:
            paused = preflight.is_hard_paused()
            if paused:
                self.status_var.set("Status: PAUSED - SAFETY CONDITION")
                self.status_label.config(fg="#b3401a")
                self.detail_var.set(f"Reason: {paused.get('reason')}  |  {paused.get('details')}")
            else:
                self.status_var.set("Status: RUNNING")
                self.status_label.config(fg="#0a7a2f")
                self.detail_var.set("")

        self.last_session_var.set(f"Last session: {self._last_session_summary()}")
        self._refresh_disk_space()
        self._refresh_compliance_status()
        self._refresh_delivery_status()
        if not self._busy:
            # Skipped while a real transfer or check is running: both that
            # work and this countdown scan call stability.scan_files, which
            # reads-then-writes file_observations rows without the writer
            # lock coordinating them - running both at once could race and
            # perturb the actual stability-gate timing the safety design
            # depends on. The countdown is also moot while something's
            # already actively happening.
            threading.Thread(target=self._countdown_worker, daemon=True).start()

    def _refresh_compliance_status(self):
        """Reflects the LAST manual Compliance Check's result, not a live/
        automatic state - there is no automatic scanning (see integrity.py's
        docstring for why). Reading the small persisted state file is
        cheap, safe to do directly on the UI thread."""
        state = integrity.load_last_compliance_state()
        if state is None:
            self.compliance_var.set("Compliance check: never run")
            self.compliance_label.config(fg="#555555")
            return
        when = time.strftime("%b %d, %I:%M %p", time.localtime(state["completed_at"]))
        if state["problem_count"] == 0:
            self.compliance_var.set(f"Compliance check: CLEAR ({state['total_committed']} files tracked, {when})")
            self.compliance_label.config(fg="#555555")
        else:
            self.compliance_var.set(
                f"Compliance check: {state['problem_count']} new problem(s) ({when}) - see receipt for details"
            )
            self.compliance_label.config(fg="#b3401a")

    def _refresh_delivery_status(self):
        state = integrity.load_last_delivery_state()
        if state is None:
            self.delivery_var.set("Delivery check: never run")
            self.delivery_label.config(fg="#555555")
            return
        when = time.strftime("%b %d, %I:%M %p", time.localtime(state["completed_at"]))
        if state["total_in_shipped"] == 0:
            self.delivery_var.set(f"Delivery check: CLEAR - Shipped is empty ({when})")
            self.delivery_label.config(fg="#555555")
        elif state["unconfirmed_count"] == 0:
            self.delivery_var.set(f"Delivery check: CLEAR - {state['total_in_shipped']} item(s) confirmed archived ({when})")
            self.delivery_label.config(fg="#555555")
        else:
            self.delivery_var.set(
                f"Delivery check: {state['unconfirmed_count']} item(s) NOT confirmed ({when}) - do not delete yet"
            )
            self.delivery_label.config(fg="#b3401a")

    def _refresh_disk_space(self):
        """shutil.disk_usage is a cheap syscall - safe to call directly on
        the UI thread on every refresh, unlike the countdown's filesystem
        walk which needs a background thread."""
        try:
            drive = settings.ARCHIVE_ROOT.drive + "\\"
            usage = shutil.disk_usage(drive)
        except OSError as e:
            self.disk_space_var.set(f"Archive drive {settings.ARCHIVE_ROOT.drive}: could not check free space ({e})")
            self.disk_space_label.config(fg="#b3401a")
            return

        free_gb = usage.free / 1024**3
        total_gb = usage.total / 1024**3
        pct_free = (usage.free / usage.total * 100) if usage.total else 0
        reserve_gb = settings.SAFETY_RESERVE_BYTES / 1024**3
        self.disk_space_var.set(
            f"{drive} free space: {free_gb:,.1f} GB free of {total_gb:,.1f} GB ({pct_free:.0f}%)"
        )
        if usage.free < settings.SAFETY_RESERVE_BYTES:
            self.disk_space_label.config(fg="#b3401a")  # below the configured safety reserve itself
        elif usage.free < settings.SAFETY_RESERVE_BYTES * 2:
            self.disk_space_label.config(fg="#a06a00")  # getting close - amber
        else:
            self.disk_space_label.config(fg="#555555")

    def _countdown_worker(self):
        # Filesystem walk + ledger observation update - done off the UI
        # thread so a large intake batch never makes the window feel frozen.
        try:
            status = pipeline.get_countdown_status()
        except Exception as e:  # noqa: BLE001 - the countdown must never crash the panel
            status = {"has_pending": False, "error": str(e)}
        self._work_queue.put(("countdown", status))

    def _tick_countdown(self):
        if self._busy:
            self.countdown_var.set("(countdown paused while a transfer/check is running)")
            self.countdown_label.config(fg="#555555")
        elif self._countdown_has_pending is None:
            self.countdown_var.set("Checking intake...")
            self.countdown_label.config(fg="#555555")
        elif self._countdown_has_pending is False:
            self.countdown_var.set("Nothing waiting in Shenzhen Sorting Facility.")
            self.countdown_label.config(fg="#555555")
        else:
            remaining = max(0.0, self._countdown_remaining - (time.time() - self._countdown_synced_at))
            summary = self._countdown_summary_text
            second_line = "" if self._busy else '\nor press MANUAL ONBOARDING to start sorting now'
            if remaining <= 0:
                self.countdown_var.set(f'{summary} ready for sorting now{second_line}')
                self.countdown_label.config(fg="#0a7a2f")
            else:
                self.countdown_var.set(f'{summary} ready for sorting in {_format_mmss(remaining)}{second_line}')
                self.countdown_label.config(fg="#555555")
        self.after(TICK_MS, self._tick_countdown)

    def _last_session_summary(self) -> str:
        try:
            ledger.init_db()
            with ledger.connection() as conn:
                last = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
                if not last:
                    return "none yet"
                total = conn.execute(
                    "SELECT COUNT(*) as n FROM transactions WHERE receipt_id = ?", (last["receipt_id"],)
                ).fetchone()["n"]
                committed = conn.execute(
                    "SELECT COUNT(*) as n FROM transactions WHERE receipt_id = ? AND status = 'COMMITTED'",
                    (last["receipt_id"],),
                ).fetchone()["n"]
            summary = f"{last['receipt_id']} — Verified and Sorted: {committed}/{total}"
            if committed < total:
                summary += f" ({total - committed} error(s))"
            return summary
        except Exception as e:  # noqa: BLE001 - status display must never crash the panel
            return f"(could not read ledger: {e})"

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------
    def _log(self, text: str):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _on_start(self):
        control.cmd_start()
        self._log("Start/Resume pressed.")
        self._do_status_refresh()

    def _on_timer_settings(self):
        settings.load_timer_settings()  # pick up any change saved by another instance first
        dialog = tk.Toplevel(self)
        dialog.title("Timer Settings")
        dialog.geometry("380x260")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        tk.Label(dialog, text="Stability / quiet-time gates", font=("Segoe UI", 11, "bold")).pack(pady=(14, 6))
        tk.Label(
            dialog,
            text="A file must sit unchanged for the FILE time, and the\n"
                 "whole batch must have no new/changed content for the\n"
                 "FOLDER time, before it's treated as safely finished\n"
                 "arriving. Lower values check sooner but are less\n"
                 "cautious about slow transfers.",
            font=("Segoe UI", 9), justify="left",
        ).pack(padx=16, pady=(0, 12))

        form = tk.Frame(dialog)
        form.pack()

        tk.Label(form, text="Per-file quiet time (minutes):").grid(row=0, column=0, sticky="w", pady=4)
        file_var = tk.StringVar(value=str(settings.FILE_QUIET_SECONDS / 60))
        tk.Entry(form, textvariable=file_var, width=10).grid(row=0, column=1, padx=8)

        tk.Label(form, text="Whole-folder quiet time (minutes):").grid(row=1, column=0, sticky="w", pady=4)
        folder_var = tk.StringVar(value=str(settings.FOLDER_QUIET_SECONDS / 60))
        tk.Entry(form, textvariable=folder_var, width=10).grid(row=1, column=1, padx=8)

        def on_save():
            try:
                file_minutes = float(file_var.get())
                folder_minutes = float(folder_var.get())
                if file_minutes < 0 or folder_minutes < 0:
                    raise ValueError("Values must not be negative")
            except ValueError as e:
                messagebox.showerror("Invalid value", f"Please enter a valid non-negative number of minutes.\n({e})", parent=dialog)
                return
            settings.save_timer_settings(int(round(file_minutes * 60)), int(round(folder_minutes * 60)))
            self._log(f"Timer settings updated: file={file_minutes}min, folder={folder_minutes}min.")
            dialog.destroy()

        button_row = tk.Frame(dialog)
        button_row.pack(pady=16)
        tk.Button(button_row, text="Save", width=12, command=on_save).grid(row=0, column=0, padx=6)
        tk.Button(button_row, text="Cancel", width=12, command=dialog.destroy).grid(row=0, column=1, padx=6)

    def _on_safe_stop(self):
        control.cmd_safe_stop()
        self._log("Safe Stop pressed - no new session will start until Start/Resume.")
        self._do_status_refresh()

    def _on_open_facility(self):
        control.cmd_open_sorting_facility()

    def _on_open_console(self):
        control.cmd_open_receipt_console()

    def _on_repair_folders(self):
        """Recreates the Shenzhen Sorting Facility / Receipt Center Console /
        Warehouse folder structure if any of it was deleted, renamed, or
        moved, so the system doesn't just silently stop working. The
        Warehouse specifically is only ever recreated if the ledger has no
        prior archived content on record - see folder_repair.repair_folders
        and preflight.archive_root_needs_manual_repair for why."""
        if self._busy:
            self._log("A transfer or check is already running - please wait for it to finish.")
            return
        result = folder_repair.repair_folders()
        lines = []
        if result.created:
            lines.append(f"Created: {', '.join(result.created)}")
        if result.already_present:
            lines.append(f"Already present: {', '.join(result.already_present)}")
        for msg in result.refused:
            lines.append(f"NOT recreated: {msg}")
        summary = "\n".join(lines) if lines else "Nothing to do."
        self._log("Repair Folders:\n  " + summary.replace("\n", "\n  "))
        if result.refused:
            messagebox.showwarning("Repair Folders", summary)
        else:
            messagebox.showinfo("Repair Folders", summary)
        self._do_status_refresh()

    def _on_manual_button_press(self):
        """The button does double duty: first press starts a cancelable
        hold (an "oops" window - the button itself becomes STOP for the
        duration); a second press during the hold cancels it instead of
        starting anything. Nothing actually runs until the hold completes
        uninterrupted."""
        if self._hold_seconds_left is not None:
            self._cancel_manual_hold()
            return
        if self._busy:
            self._log("A session is already running - please wait for it to finish.")
            return
        self._hold_preview_only = self.preview_only_var.get()
        self._hold_seconds_left = MANUAL_HOLD_SECONDS
        self._log(f"Manual Onboarding starting in {MANUAL_HOLD_SECONDS}s - press STOP to cancel.")
        self._tick_manual_hold()

    def _tick_manual_hold(self):
        if self._hold_seconds_left is None:
            return  # cancelled by _cancel_manual_hold already
        if self._hold_seconds_left <= 0:
            self._start_manual_run()
            return
        self.manual_button.config(text=f"STOP  ({self._hold_seconds_left}s until Manual Onboarding starts)",
                                   bg="#b3401a", fg="white")
        self._hold_seconds_left -= 1
        self.after(1000, self._tick_manual_hold)

    def _cancel_manual_hold(self):
        self._hold_seconds_left = None
        self.manual_button.config(text="MANUAL ONBOARDING", bg=self._manual_button_default_bg, fg="black")
        self._log("Manual Onboarding cancelled during the hold - nothing happened.")

    def _start_manual_run(self):
        self._hold_seconds_left = None
        self.manual_button.config(text="MANUAL ONBOARDING", bg=self._manual_button_default_bg, fg="black")
        preview_only = self._hold_preview_only
        self._busy = True
        self._set_busy_ui(True)
        self._log(f"Manual Onboarding started ({'preview only, no changes' if preview_only else 'live'})...")
        thread = threading.Thread(target=self._run_session_worker, args=(not preview_only,), daemon=True)
        thread.start()

    def _set_busy_ui(self, busy: bool):
        """Manual Onboarding, Start/Resume, and the two check buttons are
        disabled and grayed out while a real transfer OR a check is
        running, so nothing tampers with state mid-copy and a check never
        runs concurrently with an active sort (verifying a file mid-copy
        would be meaningless). Safe Stop is deliberately left enabled -
        it's the one control meant to remain usable no matter what's
        happening (it only affects whether the NEXT session is allowed to
        start, not this one)."""
        state = "disabled" if busy else "normal"
        self.manual_button.config(state=state)
        self.start_button.config(state=state)
        self.compliance_check_button.config(state=state)
        self.delivery_check_button.config(state=state)
        self.repair_folders_button.config(state=state)

    def _run_session_worker(self, live: bool):
        try:
            # manual=True: a human just pressed the button, so the short
            # anti-mid-write guard applies instead of the full conservative
            # wait (see pipeline.run_session's docstring for why that's safe).
            result = pipeline.run_session(dry_run=not live, manual=True)
            self._work_queue.put(("result", self._format_result(result)))
        except Exception as e:  # noqa: BLE001 - surface any crash to the log instead of losing it silently
            self._work_queue.put(("result", f"Session crashed: {e}"))

    def _on_compliance_check(self):
        if self._busy:
            self._log("A transfer or check is already running - please wait for it to finish.")
            return
        deep_verify = self.deep_verify_var.get()
        self._busy = True
        self._set_busy_ui(True)
        self._log(f"Compliance Check starting{' (deep verify)' if deep_verify else ''}...")
        thread = threading.Thread(target=self._compliance_worker, args=(deep_verify,), daemon=True)
        thread.start()

    def _compliance_worker(self, deep_verify: bool):
        def on_progress(checked, total):
            self._work_queue.put(("verify_progress", f"  checked {checked}/{total}..."))

        try:
            report = integrity.run_compliance_check(deep_verify=deep_verify, progress_callback=on_progress)
            console_path, _archive_path = integrity.generate_compliance_receipt(report)
            lines = [f"Compliance Check complete: {'CLEAR' if report.clear else 'NOT CLEAR'}."]
            if report.new_missing:
                lines.append(f"  {len(report.new_missing)} new missing file(s) since last check.")
            if report.corrupted:
                lines.append(f"  {len(report.corrupted)} corrupted file(s) found.")
            if report.relocations:
                lines.append(f"  {len(report.relocations)} file(s) found reorganized elsewhere - records updated.")
            if report.resolved:
                lines.append(f"  {len(report.resolved)} previously-missing file(s) found again.")
            lines.append(f"  Receipt: {console_path}")
            self._work_queue.put(("result", "\n".join(lines)))
        except Exception as e:  # noqa: BLE001 - surface any crash to the log instead of losing it silently
            self._work_queue.put(("result", f"Compliance Check crashed: {e}"))

    def _on_delivery_check(self):
        if self._busy:
            self._log("A transfer or check is already running - please wait for it to finish.")
            return
        self._busy = True
        self._set_busy_ui(True)
        self._log("Delivery Check starting...")
        thread = threading.Thread(target=self._delivery_worker, daemon=True)
        thread.start()

    def _delivery_worker(self):
        def on_progress(checked, total):
            self._work_queue.put(("verify_progress", f"  checked {checked}/{total}..."))

        try:
            report = integrity.run_delivery_check(progress_callback=on_progress)
            console_path, _archive_path = integrity.generate_delivery_receipt(report)
            if report.total_in_shipped == 0:
                summary = "Delivery Check complete: CLEAR - Shipped is empty."
            elif report.clear:
                summary = f"Delivery Check complete: CLEAR - all {len(report.confirmed)} item(s) in Shipped confirmed archived."
            else:
                summary = f"Delivery Check complete: NOT CLEAR - {len(report.unconfirmed)} item(s) not confirmed."
            summary += f"\n  Receipt: {console_path}"
            self._work_queue.put(("result", summary))
        except Exception as e:  # noqa: BLE001 - surface any crash to the log instead of losing it silently
            self._work_queue.put(("result", f"Delivery Check crashed: {e}"))

    @staticmethod
    def _format_result(result) -> str:
        if result.paused_reason and not result.receipt_id:
            return f"Not running: {result.paused_reason}"
        if not result.receipt_id:
            lines = ["Nothing to do (no eligible stable files)."]
        else:
            lines = [f"Session {result.receipt_id}: Verified and Sorted {result.committed}/{result.total}, "
                     f"{len(result.errors)} error(s)."]
            if result.paused_reason:
                lines.append(f"HARD PAUSED: {result.paused_reason} - manual Start/Resume required.")
        for note in result.notes:
            lines.append(f"  note: {note}")
        for err in result.errors:
            lines.append(f"  error: {err}")
        return "\n".join(lines)

    def _poll_queue(self):
        try:
            while True:
                tag, payload = self._work_queue.get_nowait()
                if tag == "result":
                    self._log(payload)
                    self._busy = False
                    self._set_busy_ui(False)
                    self._do_status_refresh()
                elif tag == "countdown":
                    if payload.get("has_pending"):
                        self._countdown_has_pending = True
                        self._countdown_remaining = payload["seconds_remaining"]
                        self._countdown_synced_at = time.time()
                        self._countdown_summary_text = _format_pending_summary(
                            payload["file_count"], payload["folder_count"])
                    else:
                        self._countdown_has_pending = False
                elif tag == "verify_progress":
                    self._log(payload)
        except queue.Empty:
            pass
        self.after(300, self._poll_queue)


def main():
    settings.SHENZHEN_ROOT.mkdir(parents=True, exist_ok=True)
    settings.RECEIPT_CONSOLE_DIR.mkdir(parents=True, exist_ok=True)
    app = ControlPanel()
    app.mainloop()


if __name__ == "__main__":
    main()
