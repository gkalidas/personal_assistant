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


def _parse_gps(gps_data: dict) -> tuple[float, float] | None:
    """Convert raw IFD GPS dict to (lat, lon) floats. Returns None if incomplete."""
    try:
        def _rational_to_float(r):
            # Pillow returns IFDRational objects
            return float(r[0]) / float(r[1]) if isinstance(r, tuple) else float(r)

        def _dms_to_deg(dms) -> float:
            d = _rational_to_float(dms[0])
            m = _rational_to_float(dms[1])
            s = _rational_to_float(dms[2])
            return d + m / 60 + s / 3600

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
        if not raw:
            # No EXIF — fall back to file mtime
            mtime = photo_path.stat().st_mtime
            result["date"] = datetime.fromtimestamp(mtime)
            log.debug("no EXIF in %s — using file mtime", photo_path.name)
        else:
            # Date
            dt_str = raw.get(_TAG_DATETIME_ORIGINAL) or raw.get(_TAG_DATETIME)
            if dt_str:
                try:
                    result["date"] = datetime.strptime(dt_str, _EXIF_DATE_FMT)
                except ValueError:
                    result["date"] = datetime.fromtimestamp(photo_path.stat().st_mtime)
            else:
                result["date"] = datetime.fromtimestamp(photo_path.stat().st_mtime)

            # Device
            make  = raw.get(_TAG_MAKE, "")
            model = raw.get(_TAG_MODEL, "")
            if make or model:
                result["device"] = f"{make} {model}".strip()

            # GPS
            if _TAG_GPS_INFO in raw:
                coords = _parse_gps(raw[_TAG_GPS_INFO])
                if coords:
                    result["gps"]     = coords
                    result["has_gps"] = True

    except Exception as e:
        log.warning("could not read EXIF from %s: %s", photo_path.name, e)
        result["date"] = datetime.fromtimestamp(photo_path.stat().st_mtime)

    if result["date"]:
        result["date_str"] = result["date"].strftime("%Y-%m-%d")
        result["time_str"] = result["date"].strftime("%H:%M")

    return result


def scan_photos(directory: str | Path, max_per_day: int = 12) -> dict[str, list[dict]]:
    """
    Scan a directory for photos and group them by date.

    Returns: {
        "2025-08-02": [photo_meta, photo_meta, ...],
        "2025-08-03": [...],
        ...
    }
    Photos within each day are sorted by time. At most max_per_day per day
    (keeps token usage bounded for captioning and diary writing).
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

    # Read EXIF and group by date
    by_date: dict[str, list[dict]] = {}
    for p in photo_files:
        meta = read_exif(p)
        day = meta["date_str"] or "unknown"
        by_date.setdefault(day, []).append(meta)

    # Sort each day's photos by time, keep at most max_per_day
    for day in by_date:
        by_date[day].sort(key=lambda m: m["time_str"])
        if len(by_date[day]) > max_per_day:
            log.info("day %s has %d photos — keeping first %d",
                     day, len(by_date[day]), max_per_day)
            by_date[day] = by_date[day][:max_per_day]

    log.info("grouped into %d days: %s", len(by_date), sorted(by_date.keys()))
    return by_date


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
