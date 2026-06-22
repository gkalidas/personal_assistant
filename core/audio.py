"""
Audio input: record from microphone → transcribe with local Whisper.

Uses faster-whisper (CTranslate2 backend) — no PyTorch required.
Model weights auto-downloaded from HuggingFace on first use (~74MB for 'base').

Usage:
    from core.audio import record_and_transcribe, is_available

    if is_available():
        text = record_and_transcribe(seconds=5)

Requires:
    pip install faster-whisper
    arecord  (alsa-utils, usually pre-installed on Linux)
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_WHISPER_MODEL = "base"   # tiny(~40MB) / base(~74MB) / small(~244MB)
_SAMPLE_RATE   = 16000
_CHANNELS      = 1

_model_cache = None


def is_available() -> bool:
    """True if faster-whisper is installed (so transcription can run)."""
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def _load_model():
    """Return the cached WhisperModel, loading it (CPU int8) on first use."""
    global _model_cache
    if _model_cache is None:
        from faster_whisper import WhisperModel
        log.info("loading faster-whisper model: %s", _WHISPER_MODEL)
        # device="cpu", compute_type="int8" — fast + low RAM on CPU
        _model_cache = WhisperModel(_WHISPER_MODEL, device="cpu", compute_type="int8")
        log.info("faster-whisper model loaded")
    return _model_cache


def record_and_transcribe(
    seconds: int = 5,
    device: str | None = None,
    lang: str | None = None,
) -> str:
    """Record `seconds` from mic, return transcribed text.

    Args:
        seconds: Recording duration.
        device:  ALSA device name (default: system default).
        lang:    ISO-639-1 hint ('hi'=Hindi, 'mr'=Marathi). None = auto-detect.
    """
    if not is_available():
        log.error("faster-whisper not installed — run: pip install faster-whisper")
        return ""

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name
    try:
        if not _record_wav(seconds, device, tmp_path):
            return ""
        return _transcribe(tmp_path, lang)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _record_wav(seconds: int, device: str | None, out_path: str) -> bool:
    """Record mono 16-bit WAV from the mic via arecord. Returns True on success."""
    cmd = [
        "arecord", "-q", "-f", "S16_LE",
        "-r", str(_SAMPLE_RATE), "-c", str(_CHANNELS),
        "-d", str(seconds), out_path,
    ]
    if device:
        cmd += ["-D", device]
    log.info("recording %ds → %s", seconds, out_path)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=seconds + 10)
    except subprocess.TimeoutExpired:
        log.error("arecord timed out")
        return False
    except Exception as e:
        log.error("recording failed: %s", e)
        return False
    if r.returncode != 0:
        log.error("arecord failed: %s", r.stderr.decode())
        return False
    return True


def transcribe_file(path: str | Path, lang: str | None = None) -> str:
    """Transcribe an existing audio file (WAV, MP3, etc.)."""
    if not is_available():
        return ""
    return _transcribe(str(path), lang)


def _transcribe(path: str, lang: str | None) -> str:
    """Transcribe an audio file with Whisper; returns the text ('' on failure)."""
    try:
        model = _load_model()
        opts = {}
        if lang:
            opts["language"] = lang
        segments, info = model.transcribe(path, beam_size=3, **opts)
        text = " ".join(s.text for s in segments).strip()
        log.info("transcribed lang=%s: %r", info.language, text[:80])
        return text
    except Exception as e:
        log.error("transcription failed: %s", e)
        return ""
