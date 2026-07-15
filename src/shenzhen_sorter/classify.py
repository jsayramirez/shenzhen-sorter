"""
Metadata reading, camera-origin classification, and companion grouping
(spec sections 9, 10, 14).

Plain JPEG is read via Pillow. HEIC, RAW (RAF/CR2/etc.), and video
(MOV/MP4) go through ExifTool (installed at config.settings.EXIFTOOL_PATH).
Anything ExifTool itself can't read returns confidence="unknown" rather
than fabricating certainty, which routes those files to conservative
fallbacks (Unknown Files, or filesystem-timestamp date with a warning).

Known real limitation for video: QuickTime files can carry two disagreeing
dates - DateTimeOriginal/CreateDate (often local capture time as the
camera understood it) and MediaCreateDate/TrackCreateDate (often true UTC).
Confirmed on a real sample in this project: DateTimeOriginal read
"2026:01:12 10:16:59" while MediaCreateDate read "2026:01:12 18:17:35" for
the same file - an ~8 hour gap consistent with a Pacific-time/UTC offset.
This module prefers DateTimeOriginal/CreateDate (closer to what a human
would call "the day I shot this"), which is the right choice for deciding
a Year/Month archive folder, but does not claim the ambiguity is resolved.

Companion grouping matches by filename stem, which is the spec's own
documented pattern for Live Photo / RAW+JPEG / RAW+XMP / AAE (section 9).
Per section 9's own caution, this is conservative-but-not-certain evidence;
files are still preserved and grouped even when a stem match is the only
signal, but the group is marked low_confidence so quarantine/logging can
reflect that later if you want stricter handling.

AppleDouble sidecar files (macOS writes "._realname.ext" resource-fork
carriers when copying to non-HFS volumes - several of your existing S:\\
dump folders already have these) are recognized and excluded from normal
classification; they are not real photo/video content.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import settings

NON_MEDIA_EXTENSIONS = {
    ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".rtf", ".csv",
    ".zip", ".7z", ".rar", ".epub", ".html", ".json",
}

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".heic", ".png", ".dng", ".raf", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".pef"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi"}
RAW_EXTENSIONS = {".raf", ".cr2", ".cr3", ".nef", ".arw", ".orf", ".rw2", ".pef", ".dng"}
SIDECAR_EXTENSIONS = {".xmp", ".aae"}

SCREENSHOT_FILENAME_HINTS = re.compile(r"screen ?shot", re.IGNORECASE)
SCREEN_RECORDING_FILENAME_HINTS = re.compile(r"screen ?record|rpreplay", re.IGNORECASE)


@dataclass
class CaptureMetadata:
    make: Optional[str] = None
    model: Optional[str] = None
    capture_datetime: Optional[str] = None  # ISO string once resolved; None = unresolved
    confidence: str = "unknown"  # "confident" | "unknown"
    warnings: list = field(default_factory=list)


def is_apple_double_junk(path: Path) -> bool:
    return path.name.startswith("._")


def _normalize(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def read_capture_metadata(path: Path) -> CaptureMetadata:
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        return _read_jpeg_exif(path)
    if ext in (RAW_EXTENSIONS | VIDEO_EXTENSIONS | {".heic"}):
        return _read_via_exiftool(path)
    return CaptureMetadata(
        confidence="unknown",
        warnings=[f"No metadata reader for {ext}."],
    )


def _read_via_exiftool(path: Path) -> CaptureMetadata:
    if not settings.EXIFTOOL_PATH.exists():
        return CaptureMetadata(confidence="unknown", warnings=["ExifTool not installed"])
    try:
        proc = subprocess.run(
            [str(settings.EXIFTOOL_PATH), "-j", "-Make", "-Model",
             "-DateTimeOriginal", "-CreateDate", "-MediaCreateDate", "-TrackCreateDate",
             str(path)],
            capture_output=True, timeout=30, check=False,
        )
        data = json.loads(proc.stdout)[0]
    except (subprocess.TimeoutExpired, json.JSONDecodeError, IndexError, OSError) as e:
        return CaptureMetadata(confidence="unknown", warnings=[f"ExifTool failed: {e}"])

    make = data.get("Make")
    model = data.get("Model")
    # Prefer DateTimeOriginal/CreateDate (closer to local capture time) over
    # MediaCreateDate/TrackCreateDate (often true UTC) - see module docstring.
    capture_dt = data.get("DateTimeOriginal") or data.get("CreateDate")
    warnings = []
    if not capture_dt and (data.get("MediaCreateDate") or data.get("TrackCreateDate")):
        capture_dt = data.get("MediaCreateDate") or data.get("TrackCreateDate")
        warnings.append("Only a UTC-style QuickTime date was available; local capture day may be off by one.")
    confidence = "confident" if (make and model) else "unknown"
    return CaptureMetadata(make=make, model=model, capture_datetime=capture_dt,
                            confidence=confidence, warnings=warnings)


def _read_jpeg_exif(path: Path) -> CaptureMetadata:
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
    except ImportError:
        return CaptureMetadata(confidence="unknown", warnings=["Pillow not installed"])

    try:
        img = Image.open(path)
        exif = img._getexif() or {}
    except Exception as e:
        return CaptureMetadata(confidence="unknown", warnings=[f"Could not read EXIF: {e}"])

    tags = {TAGS.get(k, k): v for k, v in exif.items()}
    make = tags.get("Make")
    model = tags.get("Model")
    capture_dt = tags.get("DateTimeOriginal") or tags.get("DateTime")
    confidence = "confident" if (make and model) else "unknown"
    return CaptureMetadata(make=make, model=model, capture_datetime=capture_dt, confidence=confidence)


def match_dedicated_camera(meta: CaptureMetadata, source_path: Optional[Path] = None,
                            dump_folder_label: Optional[str] = None) -> Optional[str]:
    """Returns the configured archive_folder name if this is a confident
    match against a known dedicated camera, else None. Three signals are
    tried in order, most-reliable first:

    1. Content (Make/Model EXIF) - the normal case for every camera.
    2. Filename prefix (match_filename_prefix) - for a device whose own
       firmware stamps a consistent filename convention but doesn't always
       embed Make/Model (e.g. DJI's video files carry no Make/Model tag but
       are always named "DJI_<timestamp>_<seq>.ext"). This is baked in by
       the camera itself, not chosen by the user, so it's trusted even when
       content-matching alone can't confirm the file.
    3. Dump-folder name (match_folder_name_contains) - the narrowest and
       least certain signal, only for a device with literally no other
       identifying trace (see the Dash Cam entry in settings.py). This is
       an explicit, user-confirmed exception, not a generic fallback.
    """
    if meta.confidence == "confident":
        make = _normalize(meta.make)
        model = _normalize(meta.model)
        for cam in settings.DEDICATED_CAMERAS:
            if not cam.get("match_make") or not cam.get("match_model"):
                continue
            if cam["match_make"] in make and cam["match_model"] in model:
                return cam["archive_folder"]

    if source_path is not None:
        name = _normalize(source_path.name)
        for cam in settings.DEDICATED_CAMERAS:
            prefix = cam.get("match_filename_prefix")
            if prefix and name.startswith(prefix):
                return cam["archive_folder"]

    if dump_folder_label:
        label = _normalize(dump_folder_label)
        for cam in settings.DEDICATED_CAMERAS:
            hint = cam.get("match_folder_name_contains")
            if hint and hint in label:
                return cam["archive_folder"]

    return None


def classify_smart_device_category(path: Path, meta: CaptureMetadata) -> str:
    """Windows Smart Device Media categories (spec 14.2/14.3), used when
    match_dedicated_camera() returned None."""
    ext = path.suffix.lower()

    if ext in NON_MEDIA_EXTENSIONS:
        return "Non-Media Files"

    is_apple = "apple" in _normalize(meta.make) or _normalize(meta.model).startswith("iphone")
    is_known_non_apple_phone = _normalize(meta.make) in {"google", "samsung"}

    if ext in VIDEO_EXTENSIONS and SCREEN_RECORDING_FILENAME_HINTS.search(path.name):
        return "Screen Recordings"

    if ext in PHOTO_EXTENSIONS and SCREENSHOT_FILENAME_HINTS.search(path.name):
        return "Screenshots"

    if meta.confidence == "confident" and is_apple and ext in (PHOTO_EXTENSIONS | VIDEO_EXTENSIONS):
        return "Camera Imports"

    if meta.confidence == "confident" and is_known_non_apple_phone and ext in (PHOTO_EXTENSIONS | VIDEO_EXTENSIONS):
        return "Non-Apple Device Camera"

    if ext in (PHOTO_EXTENSIONS | VIDEO_EXTENSIONS):
        return "Other Media"

    return "Unknown Files"


@dataclass
class AssetGroup:
    parent: Path
    companions: list = field(default_factory=list)
    low_confidence: bool = False


def group_companions(files: list[Path]) -> tuple[list[AssetGroup], list[Path]]:
    """Groups by filename stem. Returns (groups, apple_double_junk).
    A RAW/photo/video file is treated as a potential parent; XMP/AAE are
    always companions; a same-stem MOV alongside a HEIC is a Live Photo
    pair (companion, not two independent parents).
    """
    junk = [f for f in files if is_apple_double_junk(f)]
    real_files = [f for f in files if not is_apple_double_junk(f)]

    by_stem: dict[str, list[Path]] = {}
    for f in real_files:
        by_stem.setdefault(f.stem.lower(), []).append(f)

    groups = []
    for stem, group_files in by_stem.items():
        sidecars = [f for f in group_files if f.suffix.lower() in SIDECAR_EXTENSIONS]
        live_photo_video = [
            f for f in group_files
            if f.suffix.lower() in VIDEO_EXTENSIONS
            and any(g.suffix.lower() == ".heic" for g in group_files)
        ]
        non_companions = [f for f in group_files if f not in sidecars and f not in live_photo_video]

        if not non_companions:
            # Only sidecars/videos matched this stem with no clear primary -
            # preserve all as their own group rather than dropping anything.
            for f in group_files:
                groups.append(AssetGroup(parent=f, companions=[], low_confidence=True))
            continue

        # Prefer RAW as parent when a RAW+JPEG pair share a stem (spec 9 example).
        raw_parents = [f for f in non_companions if f.suffix.lower() in RAW_EXTENSIONS]
        parent = raw_parents[0] if raw_parents else non_companions[0]
        companions = sidecars + live_photo_video + [f for f in non_companions if f != parent]
        groups.append(AssetGroup(parent=parent, companions=companions, low_confidence=len(group_files) > 1))

    return groups, junk
