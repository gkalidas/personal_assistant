"""
Vision captioning — describes what's in each photo using a local Ollama vision model.

Default model: moondream (1.5 GB, fast, CPU-friendly).
Falls back gracefully if the model is not installed.
"""

import base64
import logging
from pathlib import Path

import httpx

from core.config import OLLAMA_URL, VISION_MODEL, VISION_TIMEOUT, VISION_KEEP_ALIVE
from modules.diary.photo_reader import MAX_PHOTOS_PER_DAY

log = logging.getLogger(__name__)

_CAPTION_PROMPT = (
    "Describe what you see in this photo in 2-3 sentences. "
    "Focus on: people, animals, plants/crops, location/setting, activity, weather. "
    "Be specific and factual — no guessing names."
)

# Longest-edge pixels to downscale to before captioning. moondream reasons over
# small image patches, so a full 9 MP iPhone photo is pure waste — it inflates
# inference from seconds to ~1.5 min and was timing out under the idle scheduler.
# 1024 px keeps plenty of detail for a 2-3 sentence caption.
_MAX_EDGE = 1024


def _encode_image(photo_path: Path) -> str:
    """Read a photo, downscale it (respecting EXIF orientation), return base64 JPEG.

    Falls back to the raw file bytes if Pillow is unavailable or can't open the
    format (e.g. HEIC without pillow-heif) — captioning then works as before,
    just without the speed-up.
    """
    try:
        import io
        from PIL import Image, ImageOps

        with Image.open(photo_path) as im:
            im = ImageOps.exif_transpose(im)       # honour camera rotation
            im = im.convert("RGB")
            im.thumbnail((_MAX_EDGE, _MAX_EDGE))    # in-place, preserves aspect ratio
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        log.debug("downscale failed for %s (%s) — using raw bytes", photo_path.name, e)
        return base64.b64encode(photo_path.read_bytes()).decode()


def _is_vision_available() -> bool:
    """Check if the configured vision model is loaded in Ollama."""
    try:
        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
        models = [m["name"].split(":")[0] for m in resp.json().get("models", [])]
        return VISION_MODEL.split(":")[0] in models
    except Exception:
        return False


def prewarm_vision() -> bool:
    """Load the vision model into Ollama now (blocking) so the first real caption
    isn't a ~100s cold load. Safe to call at startup. Returns True if loaded."""
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": VISION_MODEL, "prompt": "ok", "stream": False,
                  "keep_alive": VISION_KEEP_ALIVE, "options": {"num_predict": 1}},
            timeout=VISION_TIMEOUT,
        )
        resp.raise_for_status()
        log.info("vision model '%s' pre-warmed", VISION_MODEL)
        return True
    except Exception as e:
        log.warning("vision pre-warm failed: %s", e)
        return False


def _known_faces_in_photo(photo_path: str) -> list[str]:
    """Return names of identified people in this photo from the face cluster DB."""
    try:
        from modules.faces.db import faces_for_photo
        faces = faces_for_photo(photo_path)
        seen = []
        for f in faces:
            name = f.get("person_name")
            if name and name != "Unknown" and name not in seen:
                seen.append(name)
        return seen
    except Exception:
        return []


def _flag_failure(name: str, reason: str) -> None:
    """Record one captioning failure to the mistake log so the analyser sees it.

    Without this, vision errors only ever hit log.error() (terminal/log file)
    and never reach the structured stores an analyser queries.
    """
    try:
        from core.mistake_log import log_mistake
        log_mistake("vision_caption_failed", module="diary",
                    details=f"{name}: {reason}"[:480], severity="medium")
    except Exception:
        log.debug("could not log caption mistake for %s", name)


def _record_failure_event(attempted: int, failures: list[tuple[str, str]]) -> None:
    """Write one status='error' event for a failed captioning run.

    The mistake log captures per-photo detail; this single events-table row is
    what the anomaly analyser (which counts status='error' events) keys on.
    """
    try:
        from core.memory import log_event
        sample = "; ".join(f"{n}: {r}" for n, r in failures[:3])
        log_event(
            module="diary",
            query="photo captioning",
            response=f"captioning failed for {len(failures)}/{attempted} photo(s): {sample}"[:480],
            metadata={"failures": len(failures), "attempted": attempted},
            status="error",
        )
    except Exception as e:
        log.debug("could not record caption failure event: %s", e)


def _caption_one(photo_path: str | Path, prompt: str = _CAPTION_PROMPT) -> tuple[str, str | None]:
    """Caption a single photo. Returns (caption, error).

    ``error`` is None on success (caption may still be '' if the model returned
    nothing); otherwise a short reason describing a hard failure. Callers decide
    whether/how to flag — keeps this function free of logging-store coupling.
    """
    photo_path = Path(photo_path)
    # Prepend known face names so the vision model can refer to them by name
    known = _known_faces_in_photo(str(photo_path))
    if known:
        prompt = f"People identified in this photo: {', '.join(known)}. " + prompt
    if not photo_path.exists():
        log.warning("caption_photo: file not found %s", photo_path)
        return "", "file not found"

    # Downscale + encode as base64 (full-res photos make moondream time out).
    try:
        image_b64 = _encode_image(photo_path)
    except OSError as e:
        log.error("could not read photo %s: %s", photo_path.name, e)
        return "", f"unreadable file: {e}"

    payload = {
        "model":  VISION_MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "keep_alive": VISION_KEEP_ALIVE,    # keep the model warm between captions
        "options": {"num_predict": 120},    # short caption
    }

    try:
        resp = httpx.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=VISION_TIMEOUT)
        resp.raise_for_status()
        caption = resp.json().get("response", "").strip()
        log.debug("caption %s → %r", photo_path.name, caption[:80])
        return caption, None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            log.warning("vision model '%s' not found — skipping captions (run: ollama pull %s)",
                        VISION_MODEL, VISION_MODEL)
            return "", "vision model not installed"
        log.error("caption request failed for %s: %s", photo_path.name, e)
        return "", f"request failed: {e}"
    except Exception as e:
        log.error("captioning failed for %s: %s", photo_path.name, e)
        return "", f"timed out / error: {e}"


def caption_photo(photo_path: str | Path, prompt: str = _CAPTION_PROMPT) -> str:
    """
    Return a 2-3 sentence description of the photo.
    Returns empty string if vision model is unavailable or the image can't be read.
    A hard failure (timeout / HTTP error / missing model) is flagged to the mistake log.
    """
    caption, err = _caption_one(photo_path, prompt)
    if err and err != "file not found":   # missing file isn't a service failure
        _flag_failure(Path(photo_path).name, err)
    return caption


def caption_batch(photos: list[dict], max_photos: int = MAX_PHOTOS_PER_DAY) -> dict[str, str]:
    """
    Caption a batch of photo metadata dicts (each must have 'path' and 'filename').
    Returns {filename: caption_text}.

    Failures are recorded to BOTH structured stores so the analyser catches them:
    one mistake-log entry per failed photo, plus one status='error' event for the run.
    """
    batch = photos[:max_photos]
    if not _is_vision_available():
        log.info("vision model '%s' not available — diary will use date/EXIF context only", VISION_MODEL)
        if batch:   # only flag when there was actually work to do
            _flag_failure(VISION_MODEL, "vision model unavailable")
            _record_failure_event(len(batch), [("(all)", "vision model unavailable")])
        return {}

    # Load the model once up front. The idle scheduler runs hours apart, so the
    # model has usually unloaded (keep_alive) — without this the FIRST photo pays
    # the cold-load on top of inference and blows the per-photo timeout.
    if batch:
        prewarm_vision()

    captions: dict[str, str] = {}
    failures: list[tuple[str, str]] = []
    for meta in batch:
        cap, err = _caption_one(meta["path"])
        name = meta.get("filename") or meta["path"]
        if err:
            failures.append((name, err))
            _flag_failure(name, err)
        elif cap:
            captions[meta["filename"]] = cap

    log.info("captioned %d/%d photos (%d failed)", len(captions), len(batch), len(failures))
    if failures:
        _record_failure_event(len(batch), failures)
    return captions
