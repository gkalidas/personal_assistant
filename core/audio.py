"""
Audio input: record from microphone → transcribe with local Whisper.

Usage:
    from core.audio import record_and_transcribe, is_available

    if is_available():
        text = record_and_transcribe(seconds=5)
        print(text)

Requires:
    pip install openai-whisper
    arecord  (alsa-utils, usually pre-installed on Linux)

Note: ffmpeg is optional — WAV input from arecord works without it.
      With ffmpeg installed, any audio format is supported.
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_WHISPER_MODEL = "base"  # tiny(37MB) / base(141MB) / small(461MB)
_SAMPLE_RATE   = 16000   # Whisper expects 16 kHz
_CHANNELS      = 1

_model_cache = None  # loaded once, reused


def is_available() -> bool:
    """Return True if whisper and arecord are both present."""
    try:
        import whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _load_model():
    global _model_cache
    if _model_cache is None:
        import whisper
        log.info("loading whisper model: %s", _WHISPER_MODEL)
        _model_cache = whisper.load_model(_WHISPER_MODEL)
        log.info("whisper model loaded")
    return _model_cache


def record_and_transcribe(
    seconds: int = 5,
    device: str | None = None,
    lang: str | None = None,
) -> str:
    """Record `seconds` of audio, return transcribed text.

    Args:
        seconds: Recording duration.
        device: ALSA device name (default: system default).
        lang: ISO-639-1 language hint, e.g. 'hi' for Hindi, 'mr' for Marathi.
              None = auto-detect.

    Returns:
        Transcribed text, or empty string on failure.
    """
    if not is_available():
        log.error("whisper not installed — run: pip install openai-whisper")
        return ""

    # Record to a temp WAV file
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name

    try:
        arecord_cmd = [
            "arecord",
            "-q",                   # quiet
            "-f", "S16_LE",         # 16-bit signed little-endian
            "-r", str(_SAMPLE_RATE),
            "-c", str(_CHANNELS),
            "-d", str(seconds),     # duration
            tmp_path,
        ]
        if device:
            arecord_cmd += ["-D", device]

        log.info("recording %ds audio → %s", seconds, tmp_path)
        result = subprocess.run(
            arecord_cmd, capture_output=True, timeout=seconds + 10,
        )
        if result.returncode != 0:
            log.error("arecord failed: %s", result.stderr.decode())
            return ""

        model = _load_model()
        opts = {}
        if lang:
            opts["language"] = lang

        log.info("transcribing %s", tmp_path)
        out = model.transcribe(tmp_path, **opts)
        text = out.get("text", "").strip()
        log.info("transcribed: %r", text[:80])
        return text

    except subprocess.TimeoutExpired:
        log.error("arecord timed out")
        return ""
    except Exception as e:
        log.error("transcription failed: %s", e)
        return ""
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def transcribe_file(path: str | Path, lang: str | None = None) -> str:
    """Transcribe an existing audio file (WAV, MP3, etc.)."""
    if not is_available():
        return ""
    try:
        model = _load_model()
        opts = {}
        if lang:
            opts["language"] = lang
        out = model.transcribe(str(path), **opts)
        return out.get("text", "").strip()
    except Exception as e:
        log.error("transcribe_file failed: %s", e)
        return ""
