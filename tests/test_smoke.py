"""
Disposable-copy smoke test (spec section 23, partial coverage).

Runs the real pipeline against throwaway files in a scratch sandbox -
never against the real Desktop Shenzhen Sorting Facility or the real
S:\\Warehouse. All config paths are monkeypatched before any pipeline
module is imported.

This is NOT the full spec section 23 test matrix (that needs a much
larger corpus: HEIC, RAW+JPEG, Live Photo pairs, crash-mid-copy
simulation, disk-full simulation, etc. - tracked as a follow-up). This
covers the golden path plus the two things most likely to be silently
wrong: duplicate/collision handling and stale-.partial recovery.

Usage: python tests/test_smoke.py
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

SAMPLE_SOURCE = Path(r"S:\CANON - Dump\100CANON\IMG_0419.JPG")

_passed = []
_failed = []


def check(name, condition):
    if condition:
        _passed.append(name)
        print(f"  PASS: {name}")
    else:
        _failed.append(name)
        print(f"  FAIL: {name}")


def main():
    if not SAMPLE_SOURCE.exists():
        print(f"Sample source missing: {SAMPLE_SOURCE} - cannot run smoke test.")
        sys.exit(1)

    sandbox = Path(tempfile.mkdtemp(prefix="shenzhen_smoke_"))
    print(f"Sandbox: {sandbox}")

    desktop = sandbox / "Desktop"
    shenzhen = desktop / "Shenzhen Sorting Facility"
    shipped = shenzhen / "Shipped"
    console = desktop / "Receipt Center Console"
    archive = sandbox / "Warehouse"
    dump_folder = shenzhen / "Test Dump"
    dump_folder.mkdir(parents=True)
    shipped.mkdir(parents=True)
    console.mkdir(parents=True)
    archive.mkdir(parents=True)

    from config import settings
    settings.DESKTOP_ROOT = desktop
    settings.SHENZHEN_ROOT = shenzhen
    settings.SHIPPED_DIR = shipped
    settings.RECEIPT_CONSOLE_DIR = console
    settings.GLOVE_BOX_DIR = console / "Glove Box"
    settings.ARCHIVE_ROOT = archive
    settings.ARCHIVE_RECEIPTS_DIR = archive / "Archive Receipts"
    settings.LEDGER_DB_PATH = sandbox / "ledger.sqlite3"
    settings.LOCK_FILE_PATH = sandbox / "writer.lock"
    settings.HARD_PAUSE_STATE_PATH = sandbox / "hard_pause.json"
    settings.SAFE_STOP_FLAG_PATH = sandbox / "safe_stop.flag"
    settings.LAST_COMPLIANCE_CHECK_PATH = sandbox / "last_compliance_check.json"
    settings.LAST_DELIVERY_CHECK_PATH = sandbox / "last_delivery_check.json"
    settings.FILE_QUIET_SECONDS = 0
    settings.FOLDER_QUIET_SECONDS = 0

    from shenzhen_sorter import pipeline, preflight, control, ledger, lock

    # --- Test 0: countdown reflects real remaining time (GUI's live display) -
    # Uses real, short-but-nonzero gates rather than the 0/0 used everywhere
    # else in this file, since a countdown of an already-zero gate wouldn't
    # actually test the math. Restores 0/0 afterward for the rest of the file.
    settings.FILE_QUIET_SECONDS = 1
    settings.FOLDER_QUIET_SECONDS = 2
    countdown_folder = shenzhen / "Countdown Test"
    countdown_folder.mkdir()
    shutil.copy2(SAMPLE_SOURCE, countdown_folder / "COUNTDOWN.JPG")

    status = pipeline.get_countdown_status()
    check("countdown detects the new batch immediately", status["has_pending"] is True)
    check("countdown counts it as 1 folder, 0 loose files", status["folder_count"] == 1 and status["file_count"] == 0)
    check("countdown shows time remaining, not already-ready", status["seconds_remaining"] > 0)

    time.sleep(2.2)
    status2 = pipeline.get_countdown_status()
    check("countdown reaches zero once both gates clear", status2["seconds_remaining"] == 0)

    # A folder AND a loose file present at once - the exact case that made
    # naming a single "soonest" batch confusing/incomplete.
    shutil.copy2(SAMPLE_SOURCE, shenzhen / "LOOSE_ALONGSIDE_FOLDER.JPG")
    status3 = pipeline.get_countdown_status()
    check("countdown counts both a folder AND a loose file at once",
          status3["folder_count"] == 1 and status3["file_count"] == 1)
    (shenzhen / "LOOSE_ALONGSIDE_FOLDER.JPG").unlink()

    settings.FILE_QUIET_SECONDS = 0
    settings.FOLDER_QUIET_SECONDS = 0
    shutil.rmtree(countdown_folder)  # this test's only job was the countdown math, not real sorting

    # --- Test 0b: manual=True uses the short guard, manual=False still waits -
    settings.FILE_QUIET_SECONDS = 100
    settings.FOLDER_QUIET_SECONDS = 100
    manual_folder = shenzhen / "Manual Test"
    manual_folder.mkdir()
    shutil.copy2(SAMPLE_SOURCE, manual_folder / "MANUAL_TEST.JPG")

    result_auto = pipeline.run_session(dry_run=True, manual=False)
    check("automatic (manual=False) run finds nothing eligible under the long gate",
          result_auto.receipt_id == "" or result_auto.total == 0)

    time.sleep(settings.MANUAL_FILE_QUIET_SECONDS + 0.5)
    result_manual = pipeline.run_session(dry_run=True, manual=True)
    check("manual=True run finds the file eligible under the short guard", result_manual.total == 1)

    settings.FILE_QUIET_SECONDS = 0
    settings.FOLDER_QUIET_SECONDS = 0
    shutil.rmtree(manual_folder)  # this test's only job was proving the manual/automatic gate split

    # --- Test 1: golden path -------------------------------------------------
    shutil.copy2(SAMPLE_SOURCE, dump_folder / "IMG_0419.JPG")

    result = pipeline.run_session(dry_run=True)
    check("dry run finds 1 file and plans it", result.total == 1 and result.committed == 0)
    check("dry run does not create archive files", not any(archive.rglob("*.JPG")))

    result = pipeline.run_session(dry_run=False)
    check("live run commits the file", result.committed == 1 and not result.errors)
    committed_files = list(archive.rglob("IMG_0419.JPG"))
    check("file landed under Canon PowerShot G9 folder", any("Canon PowerShot G9" in str(p) for p in committed_files))
    check("dump folder moved to Shipped", (shipped / "Test Dump" / "IMG_0419.JPG").exists())
    check("original dump folder gone from Shenzhen root", not dump_folder.exists())
    check("receipt written to Center Console", any(console.glob("*.html")))
    check("receipt written to Archive Receipts", any(archive.rglob("*.html")))

    # --- Test 1b: Compliance Check -------------------------------------------
    from shenzhen_sorter import integrity

    committed_path = committed_files[0]
    original_bytes = committed_path.read_bytes()

    clean = integrity.run_compliance_check(deep_verify=False)
    check("compliance check on a healthy archive is clear", clean.clear and clean.total_committed >= 1)

    # Reorganizing WITHIN the Warehouse (e.g. a user-made "Girlfriend" folder)
    # must never count as a problem - use a dedicated throwaway file for this
    # so relocating it doesn't disturb committed_path for later tests.
    reorg_source_folder = shenzhen / "Reorg Test"
    reorg_source_folder.mkdir()
    (reorg_source_folder / "REORG_TEST.JPG").write_bytes(SAMPLE_SOURCE.read_bytes() + b"\x00unique-marker-for-reorg-test")
    reorg_result = pipeline.run_session(dry_run=False)
    check("reorg test file committed", reorg_result.committed == 1 and not reorg_result.errors)
    reorg_committed_path = next(archive.rglob("REORG_TEST.JPG"))

    user_made_folder = archive / "Girlfriend"
    user_made_folder.mkdir()
    relocated_path = user_made_folder / "REORG_TEST.JPG"
    reorg_committed_path.rename(relocated_path)

    after_reorg = integrity.run_compliance_check(deep_verify=False)
    check("relocating a file within the Warehouse is NOT reported as missing", after_reorg.clear)
    check("relocation is recorded as a relocation, not a loss",
          any(r.new_path == str(relocated_path) for r in after_reorg.relocations))
    with ledger.connection() as conn:
        updated_row = conn.execute(
            "SELECT destination_path FROM transactions WHERE original_filename = 'REORG_TEST.JPG' "
            "AND status = 'COMMITTED' ORDER BY commit_time DESC LIMIT 1"
        ).fetchone()
    check("ledger bookkeeping updated to the new location", updated_row["destination_path"] == str(relocated_path))

    # Genuinely missing case, plus the "compare to last check" baseline behavior.
    committed_path.unlink()
    missing_report = integrity.run_compliance_check(deep_verify=False)
    check("compliance check catches a genuinely deleted file",
          not missing_report.clear and any(p.original_filename == "IMG_0419.JPG" for p in missing_report.new_missing))

    console_path, archive_receipt_path = integrity.generate_compliance_receipt(missing_report)
    check("compliance receipt written to its own Integrity Checks/Compliance Check folder",
          "Integrity Checks" in str(console_path) and "Compliance Check" in str(console_path))
    check("compliance receipt names the missing file", "IMG_0419.JPG" in console_path.read_text())

    still_missing_report = integrity.run_compliance_check(deep_verify=False)
    check("running the check again with nothing new changed is CLEAR (matches last check's baseline)",
          still_missing_report.clear)

    committed_path.write_bytes(original_bytes)  # restore so downstream dedup tests see valid content again
    restored_report = integrity.run_compliance_check(deep_verify=False)
    check("restoring the file is picked up as resolved", "IMG_0419.JPG" in restored_report.resolved)

    # Corruption (same size, different bytes): must NOT be caught without
    # deep_verify - that's the documented limitation - only WITH it.
    corrupted = bytes((b + 1) % 256 for b in original_bytes)
    assert len(corrupted) == len(original_bytes)
    committed_path.write_bytes(corrupted)

    shallow_after_corruption = integrity.run_compliance_check(deep_verify=False)
    check("compliance check without deep verify does NOT catch same-size corruption",
          shallow_after_corruption.clear)

    deep_after_corruption = integrity.run_compliance_check(deep_verify=True)
    check("compliance check WITH deep verify catches same-size corruption",
          not deep_after_corruption.clear and any(p.original_filename == "IMG_0419.JPG" for p in deep_after_corruption.corrupted))

    corrupt_console_path, _ = integrity.generate_compliance_receipt(deep_after_corruption)
    corrupt_receipt_text = corrupt_console_path.read_text()
    expected_intact = deep_after_corruption.total_committed - deep_after_corruption.total_drift_from_original - len(deep_after_corruption.corrupted)
    check("receipt's headline 'confirmed intact' count subtracts corrupted files too, not just missing ones",
          f"{expected_intact} / {deep_after_corruption.total_committed}" in corrupt_receipt_text)

    committed_path.write_bytes(original_bytes)  # restore for downstream tests

    # --- Test 1c: Delivery Check ----------------------------------------------
    delivery_ok = integrity.run_delivery_check()
    check("delivery check on a properly-shipped folder is clear", delivery_ok.clear and delivery_ok.total_in_shipped >= 1)

    # Delete the archived copy backing a Shipped item - exactly the real
    # incident this whole feature was built from (the DJI photos).
    shipped_sample = next((shipped / "Test Dump").glob("*.JPG"))
    with ledger.connection() as conn:
        row = conn.execute(
            "SELECT destination_path FROM transactions WHERE source_path LIKE ? AND status = 'COMMITTED' LIMIT 1",
            (f"%{shipped_sample.name}%",),
        ).fetchone()
    Path(row["destination_path"]).unlink()

    delivery_broken = integrity.run_delivery_check()
    check("delivery check catches a Shipped item whose archived copy is gone",
          not delivery_broken.clear and any(p.filename == shipped_sample.name for p in delivery_broken.unconfirmed))

    d_console_path, d_archive_path = integrity.generate_delivery_receipt(delivery_broken)
    check("delivery receipt written to its own Integrity Checks/Delivery Check folder",
          "Integrity Checks" in str(d_console_path) and "Delivery Check" in str(d_console_path))

    # Restore for anything downstream that might rglob this file.
    shutil.copy2(SAMPLE_SOURCE, row["destination_path"])

    # --- Test 2: duplicate detection (same name + same bytes) ---------------
    dump_folder2 = shenzhen / "Test Dump 2"
    dump_folder2.mkdir()
    shutil.copy2(SAMPLE_SOURCE, dump_folder2 / "IMG_0419.JPG")
    # Force a name collision against the already-archived file by copying
    # into the exact same category/year/month the first run used.
    existing = committed_files[0]
    shutil.copy2(SAMPLE_SOURCE, dump_folder2 / existing.name)

    result2 = pipeline.run_session(dry_run=False)
    check("duplicate run has no errors", not result2.errors)
    dup_marker_files = [p for p in archive.rglob("IMG_0419*") if "__dup" in p.name]
    check("byte-identical duplicate did NOT create a second physical copy", len(dup_marker_files) == 0)

    # --- Test 2b: duplicate detection across DIFFERENT filenames, same bytes
    # (spec section 8's "different filenames + same SHA-256" case) ----------
    dump_folder2b = shenzhen / "Test Dump 2b"
    dump_folder2b.mkdir()
    shutil.copy2(SAMPLE_SOURCE, dump_folder2b / "TOTALLY_DIFFERENT_NAME.JPG")

    before_count = len(list(archive.rglob("*.JPG"))) + len(list(archive.rglob("*.RAF")))
    result2b = pipeline.run_session(dry_run=False)
    after_count = len(list(archive.rglob("*.JPG"))) + len(list(archive.rglob("*.RAF")))
    check("cross-filename duplicate run has no errors", not result2b.errors)
    check("cross-filename duplicate did NOT create a new physical file", after_count == before_count)
    check("cross-filename duplicate's own name never appears in the archive",
          not any(p.name == "TOTALLY_DIFFERENT_NAME.JPG" for p in archive.rglob("*.JPG")))

    # --- Test 2c: dry-run collision detection across batches in ONE session --
    # Two different-content files that would both land on the same filename -
    # a live run's second one gets a __dup suffix (checked via disk state as
    # each commits); a dry run never writes anything, so without tracking
    # what's already been PLANNED this session, both would wrongly get told
    # "you're the only one, no collision", silently disagreeing with what a
    # real run would actually do.
    collide_folder_a = shenzhen / "Collide A"
    collide_folder_a.mkdir()
    (collide_folder_a / "COLLIDE.JPG").write_bytes(SAMPLE_SOURCE.read_bytes() + b"\x00collide-a-marker")
    collide_folder_b = shenzhen / "Collide B"
    collide_folder_b.mkdir()
    (collide_folder_b / "COLLIDE.JPG").write_bytes(SAMPLE_SOURCE.read_bytes() + b"\x00collide-b-different-marker")

    dry_collide = pipeline.run_session(dry_run=True)
    check("dry run with a same-session collision has no errors", not dry_collide.errors)
    with ledger.connection() as conn:
        planned = conn.execute(
            "SELECT destination_path FROM transactions WHERE original_filename = 'COLLIDE.JPG' "
            "AND receipt_id = ? ORDER BY discovery_time", (dry_collide.receipt_id,),
        ).fetchall()
    check("dry run plans 2 distinct destinations for the colliding pair", len(planned) == 2)
    dest_paths = [r["destination_path"] for r in planned]
    check("dry run gives the second same-session collision a __dup suffix instead of silently agreeing",
          dest_paths[0] != dest_paths[1] and any("__dup" in p for p in dest_paths))

    shutil.rmtree(collide_folder_a)
    shutil.rmtree(collide_folder_b)

    # --- Test 3: stale .partial is rebuilt, never trusted --------------------
    # Placed directly at the real planned destination (found via a real dry
    # run first) rather than guessing the Year/Month folder, since that
    # depends on the sample photo's own EXIF capture date, not today's date.
    # Content must be unique (not a plain copy of SAMPLE_SOURCE) so the
    # cross-filename dedup check from Test 2b doesn't recognize this as a
    # duplicate of an already-archived file and skip creating it entirely -
    # that would be correct dedup behavior, just not what this test needs.
    dump_folder3 = shenzhen / "Test Dump 3"
    dump_folder3.mkdir()
    stale_test_path = dump_folder3 / "STALE_TEST.JPG"
    stale_test_content = SAMPLE_SOURCE.read_bytes() + b"\x00unique-marker-for-test-3"
    stale_test_path.write_bytes(stale_test_content)

    planned = pipeline.run_session(dry_run=True)
    with ledger.connection() as conn:
        planned_row = conn.execute(
            "SELECT destination_path FROM transactions WHERE original_filename = 'STALE_TEST.JPG' "
            "ORDER BY discovery_time DESC LIMIT 1"
        ).fetchone()
    real_dest = Path(planned_row["destination_path"])
    real_dest.parent.mkdir(parents=True, exist_ok=True)

    from shenzhen_sorter.copy_verify import partial_path_for
    stale_partial = partial_path_for(real_dest)
    stale_partial.write_bytes(b"garbage-not-a-real-copy")
    check("stale .partial exists before run", stale_partial.exists())

    result3 = pipeline.run_session(dry_run=False)
    check("stale-.partial run has no errors", not result3.errors)
    check("stale .partial was not trusted/left behind", not stale_partial.exists())
    check("real copy replaced the stale partial and verifies",
          real_dest.exists() and real_dest.stat().st_size == len(stale_test_content))

    # --- Test 4: single-writer lock blocks a second concurrent writer -------
    with lock.WriterLock() as held:
        try:
            second = lock.WriterLock()
            second.acquire()
            check("second concurrent lock is refused", False)
        except lock.LockHeld:
            check("second concurrent lock is refused", True)

    # --- Test 4b: ExifTool path (RAW file) classifies correctly -------------
    raw_sample = Path(r"S:\FUJI - Dump\127_FUJI\DSCF5867.RAF")
    if raw_sample.exists():
        from shenzhen_sorter import classify
        meta = classify.read_capture_metadata(raw_sample)
        check("ExifTool reads RAW Make/Model confidently", meta.confidence == "confident")
        check("ExifTool RAW matches Fujifilm X100VI", classify.match_dedicated_camera(meta) == "Fujifilm X100VI")
    else:
        print("  SKIP: no RAW sample available for ExifTool check")

    # --- Test 4b2: Dash Cam routes by dump-folder-name hint, not content ----
    dashcam_sample = Path(r"S:\Dash Cam - Dump\New folder\REC20241109-214238-69.mp4")
    if dashcam_sample.exists():
        from shenzhen_sorter import classify
        dc_meta = classify.read_capture_metadata(dashcam_sample)
        check("dash cam file has no confident Make/Model (as expected)", dc_meta.confidence != "confident")
        check("dash cam NOT matched without a folder-name hint",
              classify.match_dedicated_camera(dc_meta, dump_folder_label="Random Dump") is None)
        check("dash cam matched via folder-name hint",
              classify.match_dedicated_camera(dc_meta, dump_folder_label="Dash Cam Footage") == "Dash Cam")

        dashcam_folder = shenzhen / "Dash Cam Footage"
        dashcam_folder.mkdir()
        shutil.copy2(dashcam_sample, dashcam_folder / dashcam_sample.name)
        dc_result = pipeline.run_session(dry_run=False)
        check("dash cam run has no errors", not dc_result.errors)
        check("dash cam file archived under its own Dash Cam folder",
              any("Dash Cam" in str(p) and "Smart Device Media" not in str(p) for p in archive.rglob(dashcam_sample.name)))
    else:
        print("  SKIP: no dash cam sample available")

    # --- Test 4b3: DJI - photos match by content, videos (no Make/Model)
    # match by DJI's own consistent "DJI_" filename prefix instead --------
    dji_photo = Path(r"S:\Warehouse\DJI\2026\02-February\DJI_20260203095545_0145_D.JPG")
    dji_video = Path(r"S:\Warehouse\DJI\2026\02-February\DJI_20260203114327_0158_D.MP4")
    if dji_photo.exists() and dji_video.exists():
        from shenzhen_sorter import classify
        photo_meta = classify.read_capture_metadata(dji_photo)
        check("DJI photo has confident Make/Model", photo_meta.confidence == "confident")
        check("DJI photo matches via content", classify.match_dedicated_camera(photo_meta, source_path=dji_photo) == "DJI")

        video_meta = classify.read_capture_metadata(dji_video)
        check("DJI video has no confident Make/Model (as expected)", video_meta.confidence != "confident")
        check("DJI video NOT matched without its filename",
              classify.match_dedicated_camera(video_meta, source_path=None) is None)
        check("DJI video matched via its own DJI_ filename prefix",
              classify.match_dedicated_camera(video_meta, source_path=dji_video) == "DJI")

        dji_folder = shenzhen / "DJI Dump"
        dji_folder.mkdir()
        shutil.copy2(dji_photo, dji_folder / dji_photo.name)
        shutil.copy2(dji_video, dji_folder / dji_video.name)
        dji_result = pipeline.run_session(dry_run=False)
        check("DJI run has no errors", not dji_result.errors)
        check("DJI photo landed under its own top-level DJI folder",
              any(p.parts[-4] == "DJI" for p in archive.rglob(dji_photo.name)))
        check("DJI video landed under its own top-level DJI folder (matched by filename, not content)",
              any(p.parts[-4] == "DJI" for p in archive.rglob(dji_video.name)))
    else:
        print("  SKIP: no DJI sample available")

    # --- Test 4c: loose files in Shenzhen root are sorted like a dump folder,
    # and land in Shipped individually (no wrapper folder to move) ----------
    shutil.copy2(SAMPLE_SOURCE, shenzhen / "LOOSE_FILE.JPG")

    result_loose_dry = pipeline.run_session(dry_run=True)
    check("dry run sees the loose file", result_loose_dry.total == 1)
    check("loose batch labeled correctly", pipeline.LOOSE_FILES_LABEL in result_loose_dry.dump_folders)

    result_loose = pipeline.run_session(dry_run=False)
    check("loose file run has no errors", result_loose.committed == 1 and not result_loose.errors)
    check("loose file itself moved into Shipped (not a subfolder)", (shipped / "LOOSE_FILE.JPG").exists())
    check("loose file no longer sitting in Shenzhen root", not (shenzhen / "LOOSE_FILE.JPG").exists())

    # --- Test 5: free-space math -------------------------------------------
    required = preflight.required_free_space(batch_size=100, largest_file=40, reserve=10)
    check("required_free_space formula = batch + largest + reserve", required == 150)

    # --- Test 5a: timer settings save/load roundtrip ------------------------
    # save_timer_settings() takes SECONDS (the GUI dialog does the
    # minutes->seconds conversion itself before calling it).
    settings.TIMER_SETTINGS_PATH = sandbox / "timer_settings.json"
    settings.save_timer_settings(60, 120)
    check("save_timer_settings updates live FILE_QUIET_SECONDS immediately", settings.FILE_QUIET_SECONDS == 60)
    check("save_timer_settings updates live FOLDER_QUIET_SECONDS immediately", settings.FOLDER_QUIET_SECONDS == 120)
    check("save_timer_settings persisted a file", settings.TIMER_SETTINGS_PATH.exists())

    settings.FILE_QUIET_SECONDS = 999
    settings.FOLDER_QUIET_SECONDS = 999
    settings.load_timer_settings()
    check("load_timer_settings restores persisted values", settings.FILE_QUIET_SECONDS == 60 and settings.FOLDER_QUIET_SECONDS == 120)

    try:
        settings.save_timer_settings(-1, 5)
        check("save_timer_settings rejects negative values", False)
    except ValueError:
        check("save_timer_settings rejects negative values", True)

    # Restore the 0/0 test-speed values used by every other test in this file.
    settings.FILE_QUIET_SECONDS = 0
    settings.FOLDER_QUIET_SECONDS = 0

    # --- Test 5b: companion follows parent's category, not its own extension
    # (spec section 9's mandatory grouping rule) - synthetic HEIC+AAE pair,
    # since no real HEIC/AAE samples exist on this machine yet. The HEIC
    # content is deliberately fake (unreadable by ExifTool), which forces it
    # into "Other Media" rather than "Camera Imports" - the point of this
    # test is only to confirm the AAE lands in the SAME bucket as its
    # parent, not that the bucket itself is "correct" for a real iPhone photo.
    companion_folder = shenzhen / "Companion Test"
    companion_folder.mkdir()
    (companion_folder / "COMPANION_TEST.HEIC").write_bytes(b"not a real heic file, just for grouping test")
    (companion_folder / "COMPANION_TEST.AAE").write_bytes(b"fake aae sidecar content")

    result5b = pipeline.run_session(dry_run=False)
    check("companion test run has no errors", not result5b.errors)
    heic_dest = list(archive.rglob("COMPANION_TEST.HEIC"))
    aae_dest = list(archive.rglob("COMPANION_TEST.AAE"))
    check("both companion files landed somewhere", len(heic_dest) == 1 and len(aae_dest) == 1)
    if heic_dest and aae_dest:
        check("AAE companion shares its HEIC parent's category folder, not 'Unknown Files'",
              heic_dest[0].parent == aae_dest[0].parent)

    # --- Test 6: retry only touches unresolved work (spec section 7, locked) -
    from shenzhen_sorter import copy_verify as copy_verify_mod, cloud_guard as cloud_guard_mod

    retry_folder = shenzhen / "Retry Test"
    retry_folder.mkdir()
    shutil.copy2(SAMPLE_SOURCE, retry_folder / "RETRY_GOOD.JPG")
    shutil.copy2(SAMPLE_SOURCE, retry_folder / "RETRY_BAD.JPG")

    original_require_safe = cloud_guard_mod.require_safe_to_read

    def flaky_require_safe(path):
        if path.name == "RETRY_BAD.JPG":
            raise RuntimeError("simulated failure")
        return original_require_safe(path)

    cloud_guard_mod.require_safe_to_read = flaky_require_safe
    result6a = pipeline.run_session(dry_run=False)
    cloud_guard_mod.require_safe_to_read = original_require_safe  # fix the "cause" before retry

    check("retry test: 1 committed + 1 error on first pass", result6a.committed == 1 and len(result6a.errors) == 1)
    check("retry test: folder NOT shipped yet (still has unresolved file)", retry_folder.exists())

    original_copy = copy_verify_mod.copy_to_partial_with_hash
    copied_names = []

    def spy_copy(source_path, final_path):
        copied_names.append(source_path.name)
        return original_copy(source_path, final_path)

    copy_verify_mod.copy_to_partial_with_hash = spy_copy
    result6b = pipeline.run_session(dry_run=False)
    copy_verify_mod.copy_to_partial_with_hash = original_copy

    check("retry test: second pass has no errors", not result6b.errors)
    check("retry test: only the previously-failed file was physically recopied", copied_names == ["RETRY_BAD.JPG"])
    check("retry test: folder shipped once fully reconciled",
          not retry_folder.exists() and (shipped / "Retry Test" / "RETRY_GOOD.JPG").exists())

    # --- Test 7: disk-full mid-copy triggers a real hard pause (spec 12.2/12.3),
    # not just an ordinary per-file error - this test must run last, since a
    # hard pause blocks every subsequent run_session() call until cleared. --
    import errno as errno_mod

    diskfull_folder = shenzhen / "Disk Full Test"
    diskfull_folder.mkdir()
    shutil.copy2(SAMPLE_SOURCE, diskfull_folder / "WONT_FIT.JPG")
    shutil.copy2(SAMPLE_SOURCE, diskfull_folder / "ALSO_WONT_FIT.JPG")  # different name, would be deduped anyway, just proving the batch stops rather than trying it

    def out_of_space_copy(source_path, final_path):
        raise OSError(errno_mod.ENOSPC, "No space left on device")

    original_copy2 = copy_verify_mod.copy_to_partial_with_hash
    copy_verify_mod.copy_to_partial_with_hash = out_of_space_copy
    result7 = pipeline.run_session(dry_run=False)
    copy_verify_mod.copy_to_partial_with_hash = original_copy2

    check("disk-full run reports the hard pause", result7.paused_reason == "disk_full_during_copy")
    check("disk-full run still recorded the per-file error too", len(result7.errors) == 1)
    paused_state = preflight.is_hard_paused()
    check("hard-pause state persisted to disk", paused_state is not None and paused_state.get("reason") == "disk_full_during_copy")
    check("folder with the disk-full failure was NOT shipped", diskfull_folder.exists())

    result7b = pipeline.run_session(dry_run=True)
    check("hard pause blocks further sessions until manually cleared", result7b.paused_reason == "disk_full_during_copy")

    preflight.clear_hard_pause()
    result7c = pipeline.run_session(dry_run=True)
    check("clearing the hard pause allows the next session to proceed normally", result7c.paused_reason is None)

    print(f"\n{len(_passed)} passed, {len(_failed)} failed")
    shutil.rmtree(sandbox, ignore_errors=True)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
