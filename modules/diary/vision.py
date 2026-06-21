"""
Vision captioning — describes what's in each photo using a local Ollama vision model.

Default model: moondream (1.5 GB, fast, CPU-friendly).
Falls back gracefully if the model is not installed.
"""

import base64
import logging
from pathlib import Path

import httpx

from core.config import OLLAMA_URL, VISION_MODEL

log = logging.getLogger(__name__)

_CAPTION_PROMPT = (
    "Describe what you see in this photo in 2-3 sentences. "
    "Focus on: people, animals, plants/crops, location/setting, activity, weather. "
    "Be specific and factual — no guessing names."
)


def _is_vision_available() -> bool:
    """Check if the configured vision model is loaded in Ollama."""
    try:
        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5.0)
        models = [m["name"].split(":")[0] for m in resp.json().get("models", [])]
        return VISION_MODEL.split(":")[0] in models
    except Exception:
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


def caption_photo(photo_path: str | Path, prompt: str = _CAPTION_PROMPT) -> str:
    """
    Return a 2-3 sentence description of the photo.
    Returns empty string if vision model is unavailable or the image can't be read.
    """
    photo_path = Path(photo_path)
    # Prepend known face names so the vision model can refer to them by name
    known = _known_faces_in_photo(str(photo_path))
    if known:
        prompt = f"People identified in this photo: {', '.join(known)}. " + prompt
    if not photo_path.exists():
        log.warning("caption_photo: file not found %s", photo_path)
        return ""

    # Encode image as base64
    try:
        image_b64 = base64.b64encode(photo_path.read_bytes()).decode()
    except OSError as e:
        log.error("could not read photo %s: %s", photo_path.name, e)
        return ""

    payload = {
        "model":  VISION_MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "options": {"num_predict": 120},   # short caption
    }

    try:
        resp = httpx.post(f"{OLLAMA_URL}/api/generate", json=payload, timeout=60.0)
        resp.raise_for_status()
        caption = resp.json().get("response", "").strip()
        log.debug("caption %s → %r", photo_path.name, caption[:80])
        return caption
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            log.warning("vision model '%s' not found — skipping captions (run: ollama pull %s)",
                        VISION_MODEL, VISION_MODEL)
        else:
            log.error("caption request failed for %s: %s", photo_path.name, e)
        return ""
    except Exception as e:
        log.error("captioning failed for %s: %s", photo_path.name, e)
        return ""


def caption_batch(photos: list[dict], max_photos: int = 10) -> dict[str, str]:
    """
    Caption a batch of photo metadata dicts (each must have 'path' and 'filename').
    Returns {filename: caption_text}.
    Skips gracefully if vision model not available.
    """
    if not _is_vision_available():
        log.info("vision model '%s' not available — diary will use date/EXIF context only", VISION_MODEL)
        return {}

    captions: dict[str, str] = {}
    for meta in photos[:max_photos]:
        cap = caption_photo(meta["path"])
        if cap:
            captions[meta["filename"]] = cap

    log.info("captioned %d/%d photos", len(captions), min(len(photos), max_photos))
    return captions
