"""
Control panel (spec section 12.7). Tkinter only - no extra dependency,
matches the spec's own preference for a small, obvious control surface
over a full dashboard.

Long-running actions (a real sorting session) run on a background thread
so the window never freezes; results come back to the UI thread through a
plain queue, polled on a timer - the standard safe pattern for Tkinter,
since Tkinter widgets must only be touched from the main thread.

This module is the VISUAL layer only. Every button here calls straight
into the same control/pipeline/integrity/folder_repair/ledger functions
the old layout called - nothing about session handling, safety gating,
checks, or the ledger changed to build this layout.
"""

import os
import queue
import shutil
import threading
import time
import tkinter as tk
from tkinter import font as tkfont, messagebox, scrolledtext

from config import settings
from . import browse, control, folder_repair, integrity, ledger, pipeline, preflight
from .common import MONTH_NAMES

REFRESH_MS = 3000     # how often we re-check real status/countdown against disk+ledger
TICK_MS = 1000        # how often the countdown label re-renders between real refreshes
MANUAL_HOLD_SECONDS = 10  # cancel window after pressing the primary button, before it actually runs

# ---------------------------------------------------------------------------
# Palette - light gray/off-white utility look, green for healthy/running,
# red reserved for Safe Stop and genuinely critical conditions.
# ---------------------------------------------------------------------------
BG = "#f2f2f3"
CARD_BG = "#ffffff"
BORDER = "#dcdcdc"
TEXT_PRIMARY = "#1a1a1a"
TEXT_SECONDARY = "#5f6368"
GREEN = "#0a7a2f"
GREEN_DARK = "#08611f"
BLUE = "#1a5fb3"
GRAY = "#5f6368"
RED = "#b3401a"
AMBER = "#a06a00"


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
        self.configure(bg=BG)
        self.geometry("640x860")
        self.minsize(560, 700)
        self.resizable(True, True)

        self._work_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._busy = False
        self._countdown_has_pending = None  # None = not checked yet; False = nothing waiting; True = counting down
        self._countdown_remaining = None    # seconds, as of _countdown_synced_at
        self._countdown_synced_at = 0.0
        self._countdown_summary_text = ""
        self._hold_seconds_left = None      # None = no hold in progress
        self._hold_preview_only = False     # captured checkbox state at the moment of the original press
        self._advanced_expanded = True
        self._wrap_labels = []              # labels whose wraplength tracks window width

        self._build_widgets()
        self.protocol("WM_DELETE_WINDOW", self._on_close_attempt)
        self.bind("<Configure>", self._on_resize)
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

    def _on_resize(self, event):
        if event.widget is not self:
            return
        wrap = max(280, event.width - 64)
        for label in self._wrap_labels:
            label.config(wraplength=wrap)

    def _start_log_resize(self, event):
        self._log_resize_start_y = event.y_root
        self._log_resize_start_lines = int(self.log.cget("height"))

    def _do_log_resize(self, event):
        """Drag the grip below Activity Log to make it taller/shorter.
        Converts pixel drag distance to a text-line delta using the log's
        own font metrics, so dragging feels proportionate regardless of
        font size. The window (and, if content overflows it, the outer
        scrollbar) accommodates whatever height results - this only ever
        changes the log's own size, nothing else's."""
        line_px = tkfont.Font(font=self.log.cget("font")).metrics("linespace")
        delta_lines = int(round((event.y_root - self._log_resize_start_y) / line_px))
        self.log.config(height=max(4, self._log_resize_start_lines + delta_lines))

    # ------------------------------------------------------------------
    # Small layout helpers
    # ------------------------------------------------------------------
    def _card(self, parent, **pack_kwargs):
        card = tk.Frame(parent, bg=CARD_BG, highlightbackground=BORDER, highlightthickness=1, bd=0)
        card.pack(fill="x", padx=0, pady=(0, 10), **pack_kwargs)
        return card

    def _flat_button(self, parent, text, command, bg, fg, width=None, font_size=10, bold=True):
        btn = tk.Button(
            parent, text=text, command=command, bg=bg, fg=fg,
            activebackground=bg, activeforeground=fg,
            relief="flat", bd=0, highlightthickness=0, cursor="hand2",
            font=("Segoe UI", font_size, "bold" if bold else "normal"),
            padx=10, pady=8,
        )
        if width:
            btn.config(width=width)
        return btn

    def _outline_button(self, parent, text, command, color, width=None, font_size=10):
        btn = tk.Button(
            parent, text=text, command=command, bg=CARD_BG, fg=color,
            activebackground="#f5f5f5", activeforeground=color,
            relief="solid", bd=1, highlightbackground=color, highlightcolor=color,
            highlightthickness=1, cursor="hand2",
            font=("Segoe UI", font_size, "bold"),
            padx=8, pady=7,
        )
        if width:
            btn.config(width=width)
        return btn

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_widgets(self):
        # The whole panel scrolls: Advanced Settings (and now a
        # user-resizable Activity Log, see the grip below) can make the
        # real content taller than the window, so everything lives inside
        # a Canvas+Scrollbar instead of a bare Frame. Mouse wheel scrolls
        # it from anywhere except directly over the log (which keeps its
        # own independent scrolling via its built-in scrollbar).
        container = tk.Frame(self, bg=BG)
        container.pack(fill="both", expand=True, padx=18, pady=16)

        self._canvas = tk.Canvas(container, bg=BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y", padx=(8, 0))

        outer = tk.Frame(self._canvas, bg=BG)
        canvas_window = self._canvas.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(canvas_window, width=e.width))
        self._canvas.bind_all("<MouseWheel>", lambda e: self._canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        # --- Header -------------------------------------------------------
        # Font size matches RUNNING/START PARCEL DISTRIBUTION below (12pt)
        # for a consistent type scale across header/status/primary button.
        tk.Label(outer, text="CONTROL PANEL", bg=BG, fg=TEXT_SECONDARY,
                 font=("Segoe UI", 12, "bold")).pack(pady=(0, 2))

        # --- Top status -------------------------------------------------
        self.status_var = tk.StringVar(value="CHECKING...")
        self.status_label = tk.Label(outer, textvariable=self.status_var, bg=BG,
                                      font=("Segoe UI", 12, "bold"))
        self.status_label.pack(pady=(0, 4))

        self.detail_var = tk.StringVar(value="")
        detail_label = tk.Label(outer, textvariable=self.detail_var, bg=BG, fg=TEXT_SECONDARY,
                                 font=("Segoe UI", 9), justify="center")
        detail_label.pack(pady=(0, 8))
        self._wrap_labels.append(detail_label)

        # --- Alert banners (hidden unless something needs attention) ----
        self.alerts_frame = tk.Frame(outer, bg=BG)
        self.alerts_frame.pack(fill="x", pady=(0, 4))

        # --- Info card: last session / free space / intake condition ----
        info_card = self._card(outer)
        info_inner = tk.Frame(info_card, bg=CARD_BG)
        info_inner.pack(fill="x", padx=14, pady=12)

        row1 = tk.Frame(info_inner, bg=CARD_BG)
        row1.pack(fill="x")
        self.last_session_var = tk.StringVar(value="Checking last session...")
        last_session_label = tk.Label(row1, textvariable=self.last_session_var, bg=CARD_BG, fg=TEXT_PRIMARY,
                                       font=("Segoe UI", 11, "bold"), anchor="w", justify="left")
        last_session_label.pack(fill="x")
        self._wrap_labels.append(last_session_label)

        ttk_sep = tk.Frame(info_inner, bg=BORDER, height=1)
        ttk_sep.pack(fill="x", pady=8)

        row2 = tk.Frame(info_inner, bg=CARD_BG)
        row2.pack(fill="x")
        self.disk_space_var = tk.StringVar(value="Checking free space...")
        self.disk_space_label = tk.Label(row2, textvariable=self.disk_space_var, bg=CARD_BG,
                                          font=("Segoe UI", 10, "bold"), anchor="w", justify="left")
        self.disk_space_label.pack(fill="x")
        self._wrap_labels.append(self.disk_space_label)

        row3 = tk.Frame(info_inner, bg=CARD_BG)
        row3.pack(fill="x", pady=(8, 0))
        self.countdown_var = tk.StringVar(value="Checking intake...")
        self.countdown_label = tk.Label(row3, textvariable=self.countdown_var, bg=CARD_BG, fg=TEXT_SECONDARY,
                                         font=("Segoe UI", 10), anchor="w", justify="left")
        self.countdown_label.pack(fill="x")
        self._wrap_labels.append(self.countdown_label)

        # --- Primary action ----------------------------------------------
        self.manual_button = self._flat_button(
            outer, "\U0001F4E6  START PARCEL DISTRIBUTION", self._on_manual_button_press,
            bg=GREEN, fg="white", font_size=12,
        )
        self.manual_button.pack(fill="x", pady=(4, 10), ipady=6)
        self._manual_button_default_bg = GREEN
        # Preview only checkbox lives at the bottom of the window (see end
        # of _build_widgets) but the BooleanVar itself is created here so
        # _on_manual_button_press can read it regardless of build order.
        self.preview_only_var = tk.BooleanVar(value=False)

        primary_row = tk.Frame(outer, bg=BG)
        primary_row.pack(fill="x", pady=(0, 10))
        primary_row.columnconfigure(0, weight=1)
        primary_row.columnconfigure(1, weight=1)
        self.start_button = self._outline_button(primary_row, "▶  START / RESUME", self._on_start, GREEN)
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 6), ipady=4)
        self.safe_stop_button = self._outline_button(primary_row, "⏹  SAFE STOP", self._on_safe_stop, RED)
        self.safe_stop_button.grid(row=0, column=1, sticky="ew", padx=(6, 0), ipady=4)

        # --- Folder shortcuts: 2x2 grid - Sorting Facility/Center Console
        # on top, Warehouse under Sorting Facility and Browse Month under
        # Center Console directly below it. -------------------------------
        shortcuts_row = tk.Frame(outer, bg=BG)
        shortcuts_row.pack(fill="x", pady=(0, 14))
        shortcuts_row.columnconfigure(0, weight=1)
        shortcuts_row.columnconfigure(1, weight=1)
        pad = dict(sticky="ew", ipady=6)

        def _shortcut_button(text, command):
            return tk.Button(shortcuts_row, text=text, command=command,
                              bg=CARD_BG, fg=TEXT_PRIMARY, relief="solid", bd=1, highlightbackground=BORDER,
                              font=("Segoe UI", 9, "bold"), cursor="hand2")

        _shortcut_button("\U0001F4C1 SORTING FACILITY", self._on_open_facility).grid(
            row=0, column=0, padx=(0, 4), pady=(0, 4), **pad)
        _shortcut_button("\U0001F4C4 CENTER CONSOLE", self._on_open_console).grid(
            row=0, column=1, padx=(4, 0), pady=(0, 4), **pad)
        _shortcut_button("\U0001F3EC WAREHOUSE", self._on_open_warehouse).grid(
            row=1, column=0, padx=(0, 4), **pad)
        self.browse_month_button = _shortcut_button("\U0001F4C5 BROWSE MONTH", self._on_browse_month)
        self.browse_month_button.grid(row=1, column=1, padx=(4, 0), **pad)

        # --- Activity Log -----------------------------------------------
        # Height is user-adjustable (drag the grip below it), independent
        # of the outer window's own scrollbar - see _start_log_resize/
        # _do_log_resize. expand=False so the log's own configured
        # character-height (not leftover pack space) is what governs it.
        tk.Label(outer, text="Activity Log", bg=BG, fg=TEXT_PRIMARY,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 4))
        log_card = tk.Frame(outer, bg=CARD_BG, highlightbackground=BORDER, highlightthickness=1)
        log_card.pack(fill="x", expand=False)
        self.log = scrolledtext.ScrolledText(log_card, height=10, state="disabled", font=("Consolas", 9),
                                              bg=CARD_BG, fg=TEXT_PRIMARY, relief="flat", bd=0)
        self.log.pack(fill="both", expand=True, padx=1, pady=1)

        log_grip = tk.Frame(outer, bg=BORDER, height=7, cursor="sb_v_double_arrow")
        log_grip.pack(fill="x", pady=(2, 12))
        log_grip.bind("<Button-1>", self._start_log_resize)
        log_grip.bind("<B1-Motion>", self._do_log_resize)

        # --- Advanced Settings -----------------------------------------------
        self._build_advanced_settings(outer)

    def _build_advanced_settings(self, parent):
        header = tk.Frame(parent, bg=BG)
        header.pack(fill="x", pady=(0, 4))
        self._advanced_toggle_var = tk.StringVar(value="⚙ Advanced Settings   ⌄")
        toggle = tk.Label(header, textvariable=self._advanced_toggle_var, bg=BG, fg=TEXT_PRIMARY,
                           font=("Segoe UI", 10, "bold"), cursor="hand2")
        toggle.pack(anchor="w")
        toggle.bind("<Button-1>", lambda e: self._toggle_advanced())

        self.advanced_body = tk.Frame(parent, bg=BG)
        self.advanced_body.pack(fill="x", pady=(4, 0))

        # StringVars for the two check rows' result/timestamp text - created
        # up front so _refresh_compliance_status/_refresh_delivery_status
        # can set them regardless of build order.
        self.delivery_result_var = tk.StringVar(value="Never run")
        self.delivery_when_var = tk.StringVar(value="")
        self.compliance_result_var = tk.StringVar(value="Never run")
        self.compliance_when_var = tk.StringVar(value="")

        # 1. DELIVERY CHECK
        self.delivery_label, self.delivery_check_button = self._add_check_row(
            self.advanced_body, "DELIVERY CHECK", "Confirm Shipped originals are archived",
            self.delivery_result_var, self.delivery_when_var, self._on_delivery_check,
        )

        # 2. COMPLIANCE CHECK
        self.compliance_label, self.compliance_check_button = self._add_check_row(
            self.advanced_body, "COMPLIANCE CHECK", "Reconcile the Warehouse archive",
            self.compliance_result_var, self.compliance_when_var, self._on_compliance_check,
        )

        # 3. DEEP VERIFICATION - a modifier on Compliance Check, not its own
        # independent check (see _on_deep_verify_start's docstring).
        deep_card = self._card(self.advanced_body)
        deep_inner = tk.Frame(deep_card, bg=CARD_BG)
        deep_inner.pack(fill="x", padx=12, pady=10)
        deep_inner.columnconfigure(0, weight=1)
        left = tk.Frame(deep_inner, bg=CARD_BG)
        left.grid(row=0, column=0, sticky="w")
        tk.Label(left, text="DEEP VERIFICATION", bg=CARD_BG, fg=TEXT_PRIMARY,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(anchor="w")
        tk.Label(left, text="Re-hash content, catches corruption (slower)", bg=CARD_BG, fg=TEXT_SECONDARY,
                 font=("Segoe UI", 9), anchor="w").pack(anchor="w")
        self.deep_verify_var = tk.BooleanVar(value=False)
        tk.Checkbutton(left, text="Apply to the next Compliance Check", variable=self.deep_verify_var,
                        bg=CARD_BG, fg=TEXT_SECONDARY, font=("Segoe UI", 9),
                        activebackground=CARD_BG, selectcolor=CARD_BG).pack(anchor="w", pady=(2, 0))
        self.deep_verify_start_button = self._outline_button(
            deep_inner, "START", self._on_deep_verify_start, BLUE, font_size=9)
        self.deep_verify_start_button.grid(row=0, column=1, sticky="e", padx=(10, 0))

        # 4/5. TIMER SETTINGS + REPAIR FOLDERS, side by side
        bottom_row = tk.Frame(self.advanced_body, bg=BG)
        bottom_row.pack(fill="x", pady=(0, 0))
        bottom_row.columnconfigure(0, weight=1)
        bottom_row.columnconfigure(1, weight=1)

        timer_card = tk.Frame(bottom_row, bg=CARD_BG, highlightbackground=BORDER, highlightthickness=1)
        timer_card.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        timer_inner = tk.Frame(timer_card, bg=CARD_BG)
        timer_inner.pack(fill="both", expand=True, padx=12, pady=10)
        tk.Label(timer_inner, text="TIMER SETTINGS", bg=CARD_BG, fg=TEXT_PRIMARY,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(anchor="w")
        tk.Label(timer_inner, text="Adjust stability wait times", bg=CARD_BG, fg=TEXT_SECONDARY,
                 font=("Segoe UI", 9), anchor="w").pack(anchor="w", pady=(0, 8))
        self._outline_button(timer_inner, "OPEN", self._on_timer_settings, BLUE, font_size=9).pack(anchor="w")

        repair_card = tk.Frame(bottom_row, bg=CARD_BG, highlightbackground=BORDER, highlightthickness=1)
        repair_card.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        repair_inner = tk.Frame(repair_card, bg=CARD_BG)
        repair_inner.pack(fill="both", expand=True, padx=12, pady=10)
        tk.Label(repair_inner, text="REPAIR FOLDERS", bg=CARD_BG, fg=TEXT_PRIMARY,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(anchor="w")
        tk.Label(repair_inner, text="Rebuild watched folders if needed", bg=CARD_BG, fg=TEXT_SECONDARY,
                 font=("Segoe UI", 9), anchor="w").pack(anchor="w", pady=(0, 8))
        self.repair_folders_button = self._outline_button(
            repair_inner, "REPAIR", self._on_repair_folders, BLUE, font_size=9)
        self.repair_folders_button.pack(anchor="w")

        # Preview only - lives inside Advanced Settings, below the other
        # controls. Same BooleanVar created in _build_widgets; Manual
        # Onboarding reads it at press time regardless of where it's shown.
        tk.Checkbutton(self.advanced_body, text="Preview only (no changes)", variable=self.preview_only_var,
                        bg=BG, fg=TEXT_SECONDARY, font=("Segoe UI", 9),
                        activebackground=BG, selectcolor=CARD_BG).pack(anchor="w", pady=(10, 0))

    def _add_check_row(self, parent, title, subtitle, result_var, when_var, command):
        """Builds one DELIVERY CHECK / COMPLIANCE CHECK row: title+subtitle
        on the left, last result+timestamp in the middle, a RUN CHECK
        button on the right. Returns (result_label, run_button) so the
        caller can keep updating result_label's color and wire the
        existing busy/disable logic to run_button by its normal attribute
        name (self.delivery_check_button / self.compliance_check_button)."""
        card = self._card(parent)
        inner = tk.Frame(card, bg=CARD_BG)
        inner.pack(fill="x", padx=12, pady=10)
        inner.columnconfigure(1, weight=1)

        left = tk.Frame(inner, bg=CARD_BG)
        left.grid(row=0, column=0, sticky="w")
        tk.Label(left, text=title, bg=CARD_BG, fg=TEXT_PRIMARY,
                 font=("Segoe UI", 10, "bold"), anchor="w").pack(anchor="w")
        tk.Label(left, text=subtitle, bg=CARD_BG, fg=TEXT_SECONDARY,
                 font=("Segoe UI", 9), anchor="w").pack(anchor="w")

        middle = tk.Frame(inner, bg=CARD_BG)
        middle.grid(row=0, column=1, sticky="e", padx=(10, 10))
        result_label = tk.Label(middle, textvariable=result_var, bg=CARD_BG,
                                 font=("Segoe UI", 9, "bold"), anchor="e", justify="right")
        result_label.pack(anchor="e")
        tk.Label(middle, textvariable=when_var, bg=CARD_BG, fg=TEXT_SECONDARY,
                 font=("Segoe UI", 8), anchor="e", justify="right").pack(anchor="e")

        run_button = self._outline_button(inner, "RUN CHECK", command, BLUE, font_size=9)
        run_button.grid(row=0, column=2, sticky="e")

        return result_label, run_button

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
            self.status_var.set("TRANSFERRING")
            self.status_label.config(fg=BLUE)
            self.detail_var.set(
                "A file transfer is in progress. Do not close this window, disconnect "
                "S:\\ or the source device (camera/card/phone), or let the computer sleep "
                "until it finishes."
            )
        elif control.is_safe_stopped():
            self.status_var.set("STOPPED")
            self.status_label.config(fg=GRAY)
            self.detail_var.set("Safe Stop is active. Press START / RESUME to allow new sessions again.")
        else:
            paused = preflight.is_hard_paused()
            if paused:
                self.status_var.set("PAUSED — SAFETY CONDITION")
                self.status_label.config(fg=RED)
                self.detail_var.set(f"Reason: {paused.get('reason')}  |  {paused.get('details')}")
            else:
                self.status_var.set("RUNNING")
                self.status_label.config(fg=GREEN)
                self.detail_var.set("")

        self.last_session_var.set(self._last_session_summary())
        self._refresh_disk_space()
        self._refresh_compliance_status()
        self._refresh_delivery_status()
        self._refresh_alerts()
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
        self._compliance_state = state
        if state is None:
            self.compliance_result_var.set("Never run")
            self.compliance_when_var.set("")
            self.compliance_label.config(fg=TEXT_SECONDARY)
            return
        when = time.strftime("%b %d, %I:%M %p", time.localtime(state["completed_at"]))
        if state["problem_count"] == 0:
            self.compliance_result_var.set(f"✅ CLEAR ({state['total_committed']} files tracked)")
            self.compliance_label.config(fg=TEXT_SECONDARY)
        else:
            self.compliance_result_var.set(f"⚠ {state['problem_count']} new problem(s)")
            self.compliance_label.config(fg=RED)
        self.compliance_when_var.set(when)

    def _refresh_delivery_status(self):
        state = integrity.load_last_delivery_state()
        self._delivery_state = state
        if state is None:
            self.delivery_result_var.set("Never run")
            self.delivery_when_var.set("")
            self.delivery_label.config(fg=TEXT_SECONDARY)
            return
        when = time.strftime("%b %d, %I:%M %p", time.localtime(state["completed_at"]))
        if state["total_in_shipped"] == 0:
            self.delivery_result_var.set("✅ CLEAR — Shipped is empty")
            self.delivery_label.config(fg=TEXT_SECONDARY)
        elif state["unconfirmed_count"] == 0:
            self.delivery_result_var.set(f"✅ CLEAR — {state['total_in_shipped']} item(s) confirmed")
            self.delivery_label.config(fg=TEXT_SECONDARY)
        else:
            self.delivery_result_var.set(f"⚠ {state['unconfirmed_count']} item(s) NOT confirmed")
            self.delivery_label.config(fg=RED)
        self.delivery_when_var.set(when)

    def _refresh_alerts(self):
        """Anything that needs a human's attention - hard pause, or a check
        that came back not-clear - gets surfaced here too, near the top,
        in addition to its normal home in Advanced Settings. Healthy
        results are NOT duplicated here; only problems are."""
        for child in self.alerts_frame.winfo_children():
            child.destroy()

        # Note: hard-pause is already shown prominently by the main
        # status/detail labels above, so it isn't duplicated as a banner
        # here - only the two checks need a separate banner since their
        # "healthy" home (Advanced Settings) is collapsed/out of view.
        banners = []
        comp = getattr(self, "_compliance_state", None)
        if comp is not None and comp["problem_count"] > 0:
            banners.append((
                "⚠ COMPLIANCE CHECK NEEDS ATTENTION",
                f"{comp['problem_count']} item(s) could not be reconciled against the Warehouse - see the receipt for details.",
            ))

        deliv = getattr(self, "_delivery_state", None)
        if deliv is not None and deliv["total_in_shipped"] > 0 and deliv["unconfirmed_count"] > 0:
            banners.append((
                "⚠ DELIVERY CHECK NEEDS ATTENTION",
                f"{deliv['unconfirmed_count']} item(s) in Shipped could not be matched to the Warehouse. Do not clear Shipped yet.",
            ))

        for title, detail in banners:
            banner = tk.Frame(self.alerts_frame, bg="#fbeae5", highlightbackground=RED, highlightthickness=1)
            banner.pack(fill="x", pady=(0, 6))
            tk.Label(banner, text=title, bg="#fbeae5", fg=RED, font=("Segoe UI", 10, "bold"),
                     anchor="w", justify="left").pack(fill="x", padx=10, pady=(8, 0))
            detail_label = tk.Label(banner, text=detail, bg="#fbeae5", fg=RED, font=("Segoe UI", 9),
                                     anchor="w", justify="left", wraplength=560)
            detail_label.pack(fill="x", padx=10, pady=(2, 8))
            self._wrap_labels.append(detail_label)

    def _refresh_disk_space(self):
        """shutil.disk_usage is a cheap syscall - safe to call directly on
        the UI thread on every refresh, unlike the countdown's filesystem
        walk which needs a background thread."""
        try:
            drive = settings.ARCHIVE_ROOT.drive + "\\"
            usage = shutil.disk_usage(drive)
        except OSError as e:
            self.disk_space_var.set(f"Warehouse: could not check free space ({e})")
            self.disk_space_label.config(fg=RED)
            return

        free_gb = usage.free / 1024**3
        total_gb = usage.total / 1024**3
        reserve_gb = settings.SAFETY_RESERVE_BYTES / 1024**3

        if usage.free < settings.SAFETY_RESERVE_BYTES:
            self.disk_space_var.set(
                f"⚠ Warehouse: {free_gb:,.1f} GB free — below the {reserve_gb:,.0f} GB safety reserve"
            )
            self.disk_space_label.config(fg=RED)
        elif usage.free < settings.SAFETY_RESERVE_BYTES * 2:
            self.disk_space_var.set(f"Warehouse: {free_gb:,.1f} GB free (getting close to reserve)")
            self.disk_space_label.config(fg=AMBER)
        else:
            # Normal healthy state - free space only, no total/percent per spec.
            self.disk_space_var.set(f"Warehouse: {free_gb:,.1f} GB free")
            self.disk_space_label.config(fg=TEXT_PRIMARY)
        # total_gb kept available above for the warning branches if ever needed;
        # intentionally unused in the healthy-state text.
        _ = total_gb

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
            self.countdown_label.config(fg=TEXT_SECONDARY)
        elif self._countdown_has_pending is None:
            self.countdown_var.set("Checking intake...")
            self.countdown_label.config(fg=TEXT_SECONDARY)
        elif self._countdown_has_pending is False:
            self.countdown_var.set("Nothing waiting in Shenzhen Sorting Facility.")
            self.countdown_label.config(fg=TEXT_SECONDARY)
        else:
            remaining = max(0.0, self._countdown_remaining - (time.time() - self._countdown_synced_at))
            summary = self._countdown_summary_text
            second_line = "" if self._busy else "\nor press START PARCEL DISTRIBUTION to start sorting now"
            if remaining <= 0:
                self.countdown_var.set(f"{summary} ready for sorting now{second_line}")
                self.countdown_label.config(fg=GREEN)
            else:
                self.countdown_var.set(f"{summary} ready for sorting in {_format_mmss(remaining)}{second_line}")
                self.countdown_label.config(fg=TEXT_SECONDARY)
        self.after(TICK_MS, self._tick_countdown)

    def _last_session_summary(self) -> str:
        try:
            ledger.init_db()
            with ledger.connection() as conn:
                last = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
                if not last:
                    return "No sessions yet"
                total = conn.execute(
                    "SELECT COUNT(*) as n FROM transactions WHERE receipt_id = ?", (last["receipt_id"],)
                ).fetchone()["n"]
                committed = conn.execute(
                    "SELECT COUNT(*) as n FROM transactions WHERE receipt_id = ? AND status = 'COMMITTED'",
                    (last["receipt_id"],),
                ).fetchone()["n"]
            icon = "✅" if committed == total else "⚠"
            summary = f"{icon} {last['receipt_id']} — {committed}/{total} files"
            if committed < total:
                summary += f"  ({total - committed} error(s))"
            return summary
        except Exception as e:  # noqa: BLE001 - status display must never crash the panel
            return f"(could not read ledger: {e})"

    # ------------------------------------------------------------------
    # Advanced Settings: check rows, deep verify, timer/repair row
    # ------------------------------------------------------------------
    def _toggle_advanced(self):
        self._advanced_expanded = not self._advanced_expanded
        if self._advanced_expanded:
            self.advanced_body.pack(fill="x", pady=(4, 0))
            self._advanced_toggle_var.set("⚙ Advanced Settings   ⌄")
        else:
            self.advanced_body.pack_forget()
            self._advanced_toggle_var.set("⚙ Advanced Settings   ›")

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

    def _on_browse_month(self):
        """Read-only: lets the user PICK a Year/Month (not type one),
        restricted to combinations that actually have archived content on
        record - browse.available_months() is the ledger-backed source of
        truth for that, so there's never a choice that comes back empty.
        Then builds the disposable hardlinked view (browse.build_month_view)
        and opens it in Explorer. Nothing about the real archive changes -
        see browse.py's docstring. Blocked while busy for the same reason
        every other action here is: one thing happening at a time keeps
        the mental model simple, even though this particular action never
        touches the writer lock."""
        if self._busy:
            self._log("A transfer or check is already running - please wait for it to finish.")
            return

        months = browse.available_months()  # [(year, month, count), ...]
        if not months:
            messagebox.showinfo("Browse Month", "Nothing has been archived yet - there's no content to browse.")
            return

        by_year = {}
        for year, month, count in months:
            by_year.setdefault(year, []).append((month, count))
        for year in by_year:
            by_year[year].sort()
        years_desc = sorted(by_year, reverse=True)

        dialog = tk.Toplevel(self)
        dialog.title("Browse Month")
        dialog.geometry("400x320")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        tk.Label(dialog, text="Browse Month", font=("Segoe UI", 11, "bold")).pack(pady=(14, 6))
        tk.Label(
            dialog,
            text="Collates every archived file captured in the chosen\n"
                 "Year/Month, across every camera and Smart Device Media,\n"
                 "into one disposable folder and opens it in Explorer.\n"
                 "Read-only - nothing in the Warehouse is moved or changed.\n"
                 "Screenshots, screen recordings, and unidentified files\n"
                 "are left out of the view (still fully archived either way).",
            font=("Segoe UI", 9), justify="left",
        ).pack(padx=16, pady=(0, 12))

        form = tk.Frame(dialog)
        form.pack()

        def month_label(month, count):
            return f"{MONTH_NAMES[month - 1]} ({count})"

        tk.Label(form, text="Year:").grid(row=0, column=0, sticky="w", pady=4)
        year_var = tk.StringVar(value=str(years_desc[0]))
        year_menu = tk.OptionMenu(form, year_var, *[str(y) for y in years_desc])
        year_menu.config(width=18)
        year_menu.grid(row=0, column=1, padx=8, sticky="w")

        tk.Label(form, text="Month:").grid(row=1, column=0, sticky="w", pady=4)
        month_var = tk.StringVar()
        month_menu = tk.OptionMenu(form, month_var, "")
        month_menu.config(width=18)
        month_menu.grid(row=1, column=1, padx=8, sticky="w")

        def refresh_months(*_args):
            entries = by_year[int(year_var.get())]
            labels = [month_label(m, c) for m, c in entries]
            menu = month_menu["menu"]
            menu.delete(0, "end")
            for label in labels:
                menu.add_command(label=label, command=lambda v=label: month_var.set(v))
            month_var.set(labels[-1])  # most recent month within the selected year

        year_var.trace_add("write", refresh_months)
        refresh_months()

        def on_browse():
            year = int(year_var.get())
            month = int(month_var.get().split("-", 1)[0])
            dialog.destroy()
            self._busy = True
            self._set_busy_ui(True)
            self._log(f"Browse Month {year:04d}-{month:02d} starting...")
            thread = threading.Thread(target=self._browse_month_worker, args=(year, month), daemon=True)
            thread.start()

        button_row = tk.Frame(dialog)
        button_row.pack(pady=16)
        tk.Button(button_row, text="Browse", width=12, command=on_browse).grid(row=0, column=0, padx=6)
        tk.Button(button_row, text="Cancel", width=12, command=dialog.destroy).grid(row=0, column=1, padx=6)

    def _browse_month_worker(self, year: int, month: int):
        try:
            result = browse.build_month_view(year, month)
            lines = [f"Browse Month {year:04d}-{month:02d}: {result.linked} file(s) linked into {result.view_dir}"]
            if result.missing:
                lines.append(f"  {len(result.missing)} ledger-recorded file(s) could not be found on disk.")
            os.startfile(str(result.view_dir))
            self._work_queue.put(("result", "\n".join(lines)))
        except Exception as e:  # noqa: BLE001 - surface any crash to the log instead of losing it silently
            self._work_queue.put(("result", f"Browse Month crashed: {e}"))

    def _on_safe_stop(self):
        control.cmd_safe_stop()
        self._log("Safe Stop pressed - no new session will start until Start/Resume.")
        self._do_status_refresh()

    def _on_open_facility(self):
        control.cmd_open_sorting_facility()

    def _on_open_console(self):
        control.cmd_open_receipt_console()

    def _on_open_warehouse(self):
        control.cmd_open_warehouse()

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
        uninterrupted. This is the same Manual Onboarding behavior as
        before, just relabeled START PARCEL DISTRIBUTION."""
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
        self.manual_button.config(text=f"⏹  STOP  ({self._hold_seconds_left}s until it starts)",
                                   bg=RED, fg="white")
        self._hold_seconds_left -= 1
        self.after(1000, self._tick_manual_hold)

    def _cancel_manual_hold(self):
        self._hold_seconds_left = None
        self.manual_button.config(text="\U0001F4E6  START PARCEL DISTRIBUTION",
                                   bg=self._manual_button_default_bg, fg="white")
        self._log("Manual Onboarding cancelled during the hold - nothing happened.")

    def _start_manual_run(self):
        self._hold_seconds_left = None
        self.manual_button.config(text="\U0001F4E6  START PARCEL DISTRIBUTION",
                                   bg=self._manual_button_default_bg, fg="white")
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
        self.deep_verify_start_button.config(state=state)
        self.repair_folders_button.config(state=state)
        self.browse_month_button.config(state=state)

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

    def _on_deep_verify_start(self):
        """Deep Verification is not its own standalone check - it's a
        modifier on Compliance Check (existing backend behavior). This
        button turns the flag on and immediately runs Compliance Check
        with it, exactly like ticking the checkbox and pressing RUN CHECK
        would."""
        self.deep_verify_var.set(True)
        self._on_compliance_check()

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
