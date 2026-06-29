"""
Photo reader — extracts EXIF metadata and groups photos by date.

Handles iPhone / Android photos (JPEG, PNG, HEIC-renamed-to-jpg).
GPS is optional — many phones strip it on share. Falls back to profile location.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

log = logging.getLogger(__name__)

# EXIF tag IDs we care about
_TAG_DATETIME_ORIGINAL = 36867   # DateTimeOriginal (when shutter fired)
_TAG_DATETIME          = 306     # DateTime (file write time — fallback)
_TAG_MAKE              = 271
_TAG_MODEL             = 272
_TAG_GPS_INFO          = 34853
_TAG_WIDTH             = 40962
_TAG_HEIGHT            = 40963

_PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tiff", ".webp"}
_EXIF_DATE_FMT    = "%Y:%m:%d %H:%M:%S"


def _rational_to_float(r) -> float:
    """Convert a Pillow IFDRational (or (num, den) tuple) to a float."""
    return float(r[0]) / float(r[1]) if isinstance(r, tuple) else float(r)


def _dms_to_deg(dms) -> float:
    """Convert a (degrees, minutes, seconds) GPS triple to decimal degrees."""
    return (_rational_to_float(dms[0]) + _rational_to_float(dms[1]) / 60
            + _rational_to_float(dms[2]) / 3600)


def _parse_gps(gps_data: dict) -> tuple[float, float] | None:
    """Convert raw IFD GPS dict to (lat, lon) floats. Returns None if incomplete."""
    try:
        named = {GPSTAGS.get(k, k): v for k, v in gps_data.items()}
        lat = _dms_to_deg(named["GPSLatitude"])
        lon = _dms_to_deg(named["GPSLongitude"])
        if named.get("GPSLatitudeRef") == "S":
            lat = -lat
        if named.get("GPSLongitudeRef") == "W":
            lon = -lon
        return lat, lon
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def read_exif(photo_path: Path) -> dict[str, Any]:
    """
    Extract metadata from one photo file.

    Returns a dict with keys:
      path, filename, date (datetime), date_str, time_str,
      device, gps (lat,lon or None), width, height, has_gps
    Falls back gracefully when EXIF is absent.
    """
    result: dict[str, Any] = {
        "path":     str(photo_path),
        "filename": photo_path.name,
        "date":     None,
        "date_str": "",
        "time_str": "",
        "device":   "unknown device",
        "gps":      None,
        "has_gps":  False,
        "width":    0,
        "height":   0,
    }

    try:
        img = Image.open(photo_path)
        result["width"], result["height"] = img.size
        raw = img._getexif()
        if raw:
            _apply_exif_fields(raw, photo_path, result)
        else:
            result["date"] = datetime.fromtimestamp(photo_path.stat().st_mtime)
            log.debug("no EXIF in %s — using file mtime", photo_path.name)
    except Exception as e:
        log.warning("could not read EXIF from %s: %s", photo_path.name, e)
        result["date"] = datetime.fromtimestamp(photo_path.stat().st_mtime)

    if result["date"]:
        result["date_str"] = result["date"].strftime("%Y-%m-%d")
        result["time_str"] = result["date"].strftime("%H:%M")
    return result


def _apply_exif_fields(raw: dict, photo_path: Path, result: dict) -> None:
    """Fill date/device/gps into ``result`` from a photo's raw EXIF dict."""
    dt_str = raw.get(_TAG_DATETIME_ORIGINAL) or raw.get(_TAG_DATETIME)
    try:
        result["date"] = datetime.strptime(dt_str, _EXIF_DATE_FMT) if dt_str else None
    except ValueError:
        result["date"] = None
    if result["date"] is None:
        result["date"] = datetime.fromtimestamp(photo_path.stat().st_mtime)

    make, model = raw.get(_TAG_MAKE, ""), raw.get(_TAG_MODEL, "")
    if make or model:
        result["device"] = f"{make} {model}".strip()

    if _TAG_GPS_INFO in raw:
        coords = _parse_gps(raw[_TAG_GPS_INFO])
        if coords:
            result["gps"], result["has_gps"] = coords, True


def scan_photos(directory: str | Path, max_per_day: int | None = 12) -> dict[str, list[dict]]:
    """
    Scan a directory for photos and group them by date.

    Returns: {
        "2025-08-02": [photo_meta, photo_meta, ...],
        "2025-08-03": [...],
        ...
    }
    Photos within each day are sorted by time. At most max_per_day per day
    (keeps token usage bounded for captioning and diary writing). Pass
    max_per_day=None to disable the cap (callers that cap AFTER filtering out
    already-processed photos use this so >cap/day photos aren't orphaned).
    """
    directory = Path(directory).expanduser()
    if not directory.exists():
        log.warning("photo directory not found: %s", directory)
        return {}

    # Collect all photo files
    photo_files = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in _PHOTO_EXTENSIONS
    )

    if not photo_files:
        log.info("no photos found in %s", directory)
        return {}

    log.info("scanning %d photos in %s", len(photo_files), directory)
    by_date = _group_photos_by_date(photo_files, max_per_day)
    log.info("grouped into %d days: %s", len(by_date), sorted(by_date.keys()))
    return by_date


def _group_photos_by_date(photo_files: list[Path], max_per_day: int | None) -> dict[str, list[dict]]:
    """Read each photo's EXIF, group by date, sort by time, and optionally cap per day."""
    by_date: dict[str, list[dict]] = {}
    for p in photo_files:
        meta = read_exif(p)
        by_date.setdefault(meta["date_str"] or "unknown", []).append(meta)
    for day, metas in by_date.items():
        metas.sort(key=lambda m: m["time_str"])
        if max_per_day is not None and len(metas) > max_per_day:
            log.info("day %s has %d photos — keeping first %d", day, len(metas), max_per_day)
            by_date[day] = metas[:max_per_day]
    return by_date


def cap_per_day(by_date: dict[str, list[dict]], max_per_day: int) -> dict[str, list[dict]]:
    """Return by_date with at most max_per_day photos per day (chronological first N)."""
    capped: dict[str, list[dict]] = {}
    for day, metas in by_date.items():
        if len(metas) > max_per_day:
            log.info("day %s has %d unprocessed photos — captioning first %d this cycle",
                     day, len(metas), max_per_day)
        capped[day] = metas[:max_per_day]
    return capped


def default_photo_dir(profile: dict) -> Path:
    """Return the configured photo directory from profile, or ~/Uploads as fallback."""
    prefs = profile.get("preferences", {})
    configured = prefs.get("photo_dir")
    if configured:
        return Path(configured).expanduser()
    # Try common locations
    for candidate in ["~/Uploads", "~/Pictures", "~/Photos", "~/DCIM"]:
        p = Path(candidate).expanduser()
        if p.exists():
            return p
    return Path("~/Uploads").expanduser()
