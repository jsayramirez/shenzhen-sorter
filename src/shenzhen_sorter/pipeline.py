"""
Pipeline orchestrator - one full sorting session, Windows path
(spec sections 4, 13, 14, 15; state machine in Appendix B).

DISCOVERED -> WAITING_FOR_STABILITY -> STABLE -> METADATA_READ ->
ASSET_GROUPED -> DESTINATION_PLANNED -> COPYING -> COPIED -> VERIFYING ->
VERIFIED -> COMMITTED, with QUARANTINED/ERROR branches at any point.

This module assumes DRY_RUN is the default (config/settings.py). In dry
run, every step through DESTINATION_PLANNED still runs for real (discovery,
stability, metadata, grouping, collision math) - only COPYING onward is
skipped, matching spec section 21.4 exactly (dry run may plan destinations
and detect collisions; it must not copy/commit/create files).
"""

import errno
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from config import settings
from . import ledger, stability, classify, dates, copy_verify, cloud_guard, preflight, receipt, control
from .common import month_folder_name
from .lock import WriterLock, LockHeld


def _is_disk_full_error(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    if exc.errno == errno.ENOSPC:
        return True
    return getattr(exc, "winerror", None) == 112  # ERROR_DISK_FULL


@dataclass
class FilePlan:
    source_path: Path
    txn_id: str
    destination_path: Path = None
    category: str = ""
    year: int = 0
    month: int = 0
    duplicate_of: Path = None
    warnings: list = field(default_factory=list)


LOOSE_FILES_LABEL = "(Loose Files)"


@dataclass
class Batch:
    """One accounted-for unit of work: either a real dump folder, or the
    implicit shared batch of files sitting loose in the Shenzhen root."""
    label: str
    stable_files: list
    is_loose: bool


@dataclass
class SessionResult:
    receipt_id: str
    dump_folders: list
    committed: int = 0
    total: int = 0
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    paused_reason: str = None




_IGNORABLE_ROOT_NOISE = {"desktop.ini", "thumbs.db"}


def _discover_dump_folders() -> list[Path]:
    root = settings.SHENZHEN_ROOT
    if not root.exists():
        return []
    return [p for p in root.iterdir() if p.is_dir() and p.name != "Shipped"]


def _loose_files_in_root() -> list[Path]:
    """Files dropped directly into Shenzhen Sorting Facility's root, not
    inside a dump folder. The spec's glossary models a batch as always
    living in a named Dump Folder; this project extends that with an
    additional implicit shared batch (LOOSE_FILES_LABEL) so a file dropped
    straight into the root still gets sorted rather than silently
    forgotten (spec 21.2's no-silent-failures rule, applied to a workflow
    gap rather than a safety one). See run_session() for how this batch is
    processed identically to a real dump folder, and _ship_loose_files()
    for why its Shipped semantics differ (no wrapper folder to relocate)."""
    root = settings.SHENZHEN_ROOT
    if not root.exists():
        return []
    return [
        p for p in root.iterdir()
        if p.is_file() and p.name.lower() not in _IGNORABLE_ROOT_NOISE
        and not classify.is_apple_double_junk(p)
    ]


def get_countdown_status() -> dict:
    """Read-only status check for the control panel's countdown display:
    an aggregate view of everything currently sitting in the intake (loose
    files counted individually, dump folders counted as folders - naming
    every batch got unreadable once a folder AND loose files were both
    present at once) plus how long until the SOONEST of them becomes
    eligible. Calling this runs the same observation-recording scan as a
    real session (safe/idempotent - it's exactly what
    scan_dump_folder/scan_files already do on every call) but never copies,
    verifies, commits, or ships anything.

    Note the countdown is informational only, not authoritative: a file
    with size_ever_shrank set will never actually become eligible even
    once its displayed countdown reaches zero (see stability.py) - a rare
    edge case not worth complicating this display for.
    """
    dump_folders = _discover_dump_folders()
    loose_files = _loose_files_in_root()
    if not dump_folders and not loose_files:
        return {"has_pending": False}

    ledger.init_db()
    soonest_remaining = None
    folder_count = 0
    with ledger.connection() as conn:
        for folder in dump_folders:
            files = []
            for dirpath, _dirnames, filenames in os.walk(folder):
                for fn in filenames:
                    files.append(Path(dirpath) / fn)
            if not files:
                continue
            folder_count += 1
            report = stability.scan_files(conn, files)
            if soonest_remaining is None or report.seconds_until_ready < soonest_remaining:
                soonest_remaining = report.seconds_until_ready

        file_count = len(loose_files)
        if loose_files:
            report = stability.scan_files(conn, loose_files)
            if soonest_remaining is None or report.seconds_until_ready < soonest_remaining:
                soonest_remaining = report.seconds_until_ready

    if folder_count == 0 and file_count == 0:
        return {"has_pending": False}

    return {
        "has_pending": True,
        "file_count": file_count,
        "folder_count": folder_count,
        "seconds_remaining": max(0.0, soonest_remaining if soonest_remaining is not None else 0.0),
    }


def _plan_destination(source_path: Path, category_root: Path, resolved_date, subfolder: str | None) -> Path:
    parts = [category_root, str(resolved_date.year), month_folder_name(resolved_date.month)]
    if subfolder:
        parts.append(subfolder)
    dest_dir = Path(*[str(p) for p in parts])
    return dest_dir / source_path.name


def _plan_non_colliding_path(dest_path: Path, planned_this_session: set) -> tuple[Path, Path | None]:
    """Returns (candidate_path, original_colliding_path). If dest_path is
    free, candidate == dest_path and original is None. If dest_path is
    already taken, picks a counter-suffixed alternate candidate but also
    returns the original path so the caller can, once the source hash is
    known post-copy, check whether this is actually a byte-identical
    duplicate of what's already there (spec section 8: filename is never
    identity - same name + same hash = duplicate, same name + different
    hash = preserve both under distinct names).

    planned_this_session tracks every destination already claimed earlier
    in THIS run (dry or live) - a live run naturally sees earlier commits
    on disk via dest_path.exists(), but a dry run never writes anything,
    so two different source files across two batches that would both land
    on the same filename need this to still show the second one getting a
    __dup suffix in the preview, matching what a live run would actually do."""
    taken = dest_path.exists() or dest_path in planned_this_session
    if not taken:
        planned_this_session.add(dest_path)
        return dest_path, None
    candidate = dest_path.with_name(f"{dest_path.stem}__dup1{dest_path.suffix}")
    n = 2
    while candidate.exists() or candidate in planned_this_session:
        candidate = dest_path.with_name(f"{dest_path.stem}__dup{n}{dest_path.suffix}")
        n += 1
    planned_this_session.add(candidate)
    return candidate, dest_path


def run_session(dry_run: bool = None, manual: bool = False) -> SessionResult:
    """manual=True (the control panel's "Manual Onboarding" button, or
    `main.py run --manual`) uses a short, fixed anti-mid-write guard instead
    of the full configured quiet-time gates - see settings.py's
    MANUAL_FILE_QUIET_SECONDS/MANUAL_FOLDER_QUIET_SECONDS docstring for why
    that's a deliberate choice, not a safety bypass. An unattended/scheduled
    run must never pass manual=True - it has no human vouching that a
    transfer actually finished."""
    dry_run = settings.DRY_RUN if dry_run is None else dry_run
    file_quiet = settings.MANUAL_FILE_QUIET_SECONDS if manual else None
    folder_quiet = settings.MANUAL_FOLDER_QUIET_SECONDS if manual else None

    if control.is_safe_stopped():
        return SessionResult(receipt_id="", dump_folders=[], paused_reason="safe_stop_active")

    paused = preflight.is_hard_paused()
    if paused:
        return SessionResult(receipt_id="", dump_folders=[], paused_reason=paused.get("reason"))

    if cloud_guard.desktop_is_cloud_redirected(settings.DESKTOP_ROOT):
        preflight.enter_hard_pause("desktop_cloud_redirected", {"desktop": str(settings.DESKTOP_ROOT)})
        return SessionResult(receipt_id="", dump_folders=[], paused_reason="desktop_cloud_redirected")

    try:
        lock = WriterLock()
        lock.acquire()
    except LockHeld as e:
        return SessionResult(receipt_id="", dump_folders=[], paused_reason=f"writer_lock_held: {e}")

    try:
        ledger.init_db()
        dump_folders = _discover_dump_folders()
        loose_files = _loose_files_in_root()

        if not dump_folders and not loose_files:
            return SessionResult(receipt_id="", dump_folders=[])

        with ledger.connection() as conn:
            batches = []
            for folder in dump_folders:
                report = stability.scan_dump_folder(conn, folder, file_quiet, folder_quiet)
                batches.append(Batch(label=folder.name, stable_files=report.stable_files, is_loose=False))
            if loose_files:
                loose_report = stability.scan_files(conn, loose_files, file_quiet, folder_quiet)
                batches.append(Batch(label=LOOSE_FILES_LABEL, stable_files=loose_report.stable_files, is_loose=True))

            all_stable_files = [p for b in batches for p in b.stable_files]
            batch_labels = [b.label for b in batches]

            if not all_stable_files:
                return SessionResult(receipt_id="", dump_folders=batch_labels)

            pf = preflight.run_preflight(settings.ARCHIVE_ROOT, all_stable_files)
            if not pf.ok:
                return SessionResult(receipt_id="", dump_folders=batch_labels, paused_reason=pf.reason)

            receipt_id = ledger.new_receipt_id("SZ")
            ledger.start_session(conn, receipt_id)

            result = SessionResult(receipt_id=receipt_id, dump_folders=batch_labels)
            hard_pause_info = None  # set by the member loop below; checked after each loop level to unwind cleanly
            planned_this_session = set()  # destinations already claimed this run - see _plan_non_colliding_path

            for batch in batches:
                if hard_pause_info is not None:
                    break
                stable_files = batch.stable_files
                if not stable_files:
                    continue
                # Loose files share one ledger "dump folder" row keyed on the
                # Shenzhen root itself - source_path is informational only.
                source_marker = str(settings.SHENZHEN_ROOT) if batch.is_loose else str(settings.SHENZHEN_ROOT / batch.label)
                dump_folder_id = ledger.add_dump_folder(conn, receipt_id, batch.label, source_marker)

                groups, junk = classify.group_companions(stable_files)
                if junk:
                    result.notes.append(
                        f"{batch.label}: skipped {len(junk)} macOS AppleDouble sidecar file(s) "
                        "(._*, not real content)"
                    )

                for group in groups:
                    if hard_pause_info is not None:
                        break
                    # Metadata, resolved date, and destination category are computed
                    # ONCE per group, from the parent asset only, and then applied to
                    # every member (parent + companions). Companions must follow their
                    # parent's category rather than being reclassified by their own
                    # extension (spec section 9's mandatory grouping rule) - an AAE or
                    # XMP sidecar has no photo/video extension of its own and would
                    # otherwise fall into Unknown Files even though it's part of a
                    # confidently-classified group. This also avoids re-reading the
                    # same parent file's metadata once per companion.
                    group_error = None
                    try:
                        cloud_guard.require_safe_to_read(group.parent)
                        meta = classify.read_capture_metadata(group.parent)
                        resolved = dates.resolve_date(meta.capture_datetime, group.parent)
                        camera_folder = classify.match_dedicated_camera(meta, source_path=group.parent, dump_folder_label=batch.label)
                        if camera_folder:
                            category = camera_folder
                            category_root = settings.ARCHIVE_ROOT / camera_folder
                            subfolder = None
                        else:
                            category = classify.classify_smart_device_category(group.parent, meta)
                            category_root = settings.ARCHIVE_ROOT / "Smart Device Media"
                            subfolder = category
                    except Exception as e:  # noqa: BLE001 - a parent-level failure must fail the whole group visibly
                        group_error = str(e)

                    for member in [group.parent] + group.companions:
                        if hard_pause_info is not None:
                            break
                        result.total += 1
                        txn_id = ledger.create_transaction(conn, receipt_id, dump_folder_id, str(member), member.name)

                        prior = ledger.find_committed_by_source_path(conn, str(member), exclude_txn_id=txn_id)
                        if prior is not None:
                            # Already verified and committed in an earlier session (this
                            # dump folder just wasn't fully reconciled yet, e.g. a sibling
                            # file failed). Retry must only touch unresolved work (spec
                            # section 7, locked) - carry the prior result forward instead
                            # of re-reading/re-hashing/re-copying a file that already
                            # succeeded. In dry run, only the preview count reflects this -
                            # the ledger itself is left untouched (spec 21.4: dry run must
                            # not commit success states, even ones that are already true).
                            if not dry_run:
                                ledger.set_status(
                                    conn, txn_id, "COMMITTED",
                                    destination_path=prior["destination_path"],
                                    destination_size=prior["destination_size"],
                                    destination_sha256=prior["destination_sha256"],
                                    source_size=prior["source_size"],
                                    source_sha256=prior["source_sha256"],
                                    sorting_category=prior["sorting_category"],
                                    camera_make=prior["camera_make"],
                                    camera_model=prior["camera_model"],
                                    capture_date=prior["capture_date"],
                                    warnings="already_committed_in_prior_session",
                                )
                            result.committed += 1
                            continue

                        if group_error is not None:
                            ledger.record_error(conn, txn_id, f"group metadata/classification failed: {group_error}")
                            result.errors.append(f"{member}: {group_error}")
                            continue

                        try:
                            cloud_guard.require_safe_to_read(member)
                            ledger.set_status(conn, txn_id, "STABLE")

                            ledger.set_status(conn, txn_id, "METADATA_READ",
                                               camera_make=meta.make, camera_model=meta.model)

                            ledger.set_status(conn, txn_id, "ASSET_GROUPED",
                                               asset_group_id=str(group.parent),
                                               warnings="stem-only grouping evidence, not certain" if group.low_confidence else None)

                            dest = _plan_destination(member, category_root, resolved, subfolder)

                            candidate_dest, colliding_path = _plan_non_colliding_path(dest, planned_this_session)
                            ledger.set_status(conn, txn_id, "DESTINATION_PLANNED",
                                               destination_path=str(candidate_dest), sorting_category=category,
                                               capture_date=f"{resolved.year:04d}-{resolved.month:02d}" if resolved.confident or resolved.year else None,
                                               warnings=resolved.warning)

                            if dry_run:
                                continue  # spec 21.4: dry run must not copy/commit

                            partial = copy_verify.copy_to_partial_with_hash(member, candidate_dest)
                            ledger.set_status(conn, txn_id, "COPYING",
                                               source_size=partial.source_size, source_sha256=partial.source_sha256)
                            ledger.set_status(conn, txn_id, "COPIED")

                            ledger.set_status(conn, txn_id, "VERIFYING")
                            cr = copy_verify.verify(partial.source_size, partial.source_sha256, partial.partial_path)
                            ledger.set_status(conn, txn_id, "VERIFIED",
                                               destination_size=cr.destination_size, destination_sha256=cr.destination_sha256)

                            final_dest = candidate_dest

                            # Filename is never identity (spec section 8) - check for a
                            # byte-identical duplicate two ways: (1) against ANY prior
                            # committed file with the same content hash regardless of its
                            # filename (the "different filenames, same SHA-256" case), via
                            # the ledger; (2) against whatever is already sitting at the
                            # originally-planned path, for content the ledger doesn't know
                            # about (e.g. pre-existing files from before this system).
                            duplicate_of = None
                            existing_committed = ledger.find_committed_by_destination_sha256(
                                conn, partial.source_sha256, exclude_txn_id=txn_id)
                            if existing_committed is not None:
                                existing_path = Path(existing_committed["destination_path"])
                                if existing_path.exists():
                                    e_size, e_sha256 = copy_verify.hash_file_independent(existing_path)
                                    if e_size == partial.source_size and e_sha256 == partial.source_sha256:
                                        duplicate_of = existing_path

                            if duplicate_of is None and colliding_path is not None:
                                existing_size, existing_sha256 = copy_verify.hash_file_independent(colliding_path)
                                if existing_size == partial.source_size and existing_sha256 == partial.source_sha256:
                                    duplicate_of = colliding_path

                            if duplicate_of is not None:
                                # Discard this .partial (source untouched either way) and
                                # record the existing file as the destination of record.
                                # destination_size/sha256 are already known (that's exactly
                                # what qualified this as a duplicate) - recording them means
                                # Compliance Check can later find and relocate this row too if
                                # the shared file ever gets reorganized, same as any other.
                                partial.partial_path.unlink(missing_ok=True)
                                ledger.set_status(conn, txn_id, "COMMITTED",
                                                   destination_path=str(duplicate_of),
                                                   destination_size=partial.source_size,
                                                   destination_sha256=partial.source_sha256,
                                                   warnings="duplicate_of_existing_archived_file")
                                result.committed += 1
                                continue

                            copy_verify.commit(partial.partial_path, final_dest)
                            copy_verify.final_recheck(final_dest, partial.source_size, partial.source_sha256)
                            ledger.set_status(conn, txn_id, "COMMITTED")
                            result.committed += 1

                        except OSError as e:
                            ledger.record_error(conn, txn_id, str(e))
                            result.errors.append(f"{member}: {e}")
                            if _is_disk_full_error(e):
                                # Spec 12.2/12.3: disk fills during an active copy is a hard-pause
                                # trigger, not an ordinary per-file failure to log and move past.
                                # Setting the flag (checked at the top of each loop level above)
                                # unwinds out of the remaining groups/batches this session rather
                                # than continuing to attempt more copies against a full disk.
                                hard_pause_info = {"file": str(member), "destination": str(settings.ARCHIVE_ROOT)}
                        except Exception as e:  # noqa: BLE001 - intentionally broad: any failure -> visible, not silent
                            ledger.record_error(conn, txn_id, str(e))
                            result.errors.append(f"{member}: {e}")

                # hard_pause_info check matters here even though it looks redundant with
                # dump_folder_fully_reconciled: a hard pause can abort mid-group, before
                # every remaining file in this batch was even given a transaction row, and
                # "fully reconciled" only counts rows that exist - it would not otherwise
                # notice files that were never attempted at all.
                if not dry_run and hard_pause_info is None and ledger.dump_folder_fully_reconciled(conn, dump_folder_id):
                    if batch.is_loose:
                        _ship_loose_files(conn, dump_folder_id)
                    else:
                        _ship_folder(settings.SHENZHEN_ROOT / batch.label)
                    ledger.mark_dump_folder_shipped(conn, dump_folder_id)

            if hard_pause_info is not None:
                preflight.enter_hard_pause("disk_full_during_copy", hard_pause_info)
                result.paused_reason = "disk_full_during_copy"

            ledger.complete_session(conn, receipt_id, "VERIFIED_SUCCESSFUL" if not result.errors else "NEEDS_ATTENTION")

        if dry_run:
            print(f"[DRY RUN] receipt {receipt_id}: {result.total} files planned, "
                  f"{len(result.errors)} would-be error(s). No files copied, no receipt written.")
        else:
            console_path, archive_path = receipt.generate_receipt(receipt_id)
            print(f"Receipt written: {console_path} and {archive_path}")

        return result
    finally:
        lock.release()


def _ship_folder(folder: Path) -> None:
    """Same-volume guardrail (spec section 13): refuse if this would
    silently become a cross-volume copy-and-delete."""
    dest = settings.SHIPPED_DIR / folder.name
    if folder.drive.lower() != settings.SHIPPED_DIR.drive.lower():
        raise RuntimeError(f"Refusing cross-volume Shipped move for {folder}")
    if dest.exists():
        raise RuntimeError(f"Shipped destination already exists: {dest}")
    shutil.move(str(folder), str(dest))


def _ship_loose_files(conn, dump_folder_id: int) -> None:
    """Loose-files equivalent of _ship_folder: since there's no wrapper
    folder to relocate, each individually-committed source file moves into
    Shipped on its own (same-volume guardrail still applies per file)."""
    rows = conn.execute(
        "SELECT DISTINCT source_path FROM transactions WHERE dump_folder_id = ? AND status = 'COMMITTED'",
        (dump_folder_id,),
    ).fetchall()
    for row in rows:
        src = Path(row["source_path"])
        if not src.exists():
            continue  # already shipped by an earlier partial run; nothing to do
        dest = settings.SHIPPED_DIR / src.name
        if src.drive.lower() != settings.SHIPPED_DIR.drive.lower():
            raise RuntimeError(f"Refusing cross-volume Shipped move for {src}")
        if dest.exists():
            raise RuntimeError(f"Shipped destination already exists: {dest}")
        shutil.move(str(src), str(dest))
