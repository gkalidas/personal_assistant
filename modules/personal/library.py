"""
Local music library — scans the user's collection folder (config.MUSIC_DIR) so
GK can play his own songs and prefer them before searching online.

Design notes:
- No hard dependency on a tag library. Titles/artists come from ID3/metadata only
  if `mutagen` happens to be importable; otherwise they're parsed from the
  filename ("Artist - Title.mp3"). Filenames are the common case for a hand-built
  collection, so this stays useful with zero extra installs.
- Track ids are a short stable hash of the path relative to MUSIC_DIR, so the same
  file keeps the same id across scans (the player can resume/seek by id).
- resolve_track() is the security boundary for the file-streaming endpoint: it only
  ever returns a path that is inside MUSIC_DIR and has an audio extension.
"""

import hashlib
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac"}

# Genre/keyword hints so a bare taste like "ghazals, sufi, soft music" can match
# files that use related spellings or live in a themed subfolder.
_TASTE_SYNONYMS = {
    "ghazal": ("ghazal", "gazal", "ghazals", "gazals"),
    "sufi": ("sufi", "qawwali", "qawali", "kalam"),
    "soft": ("soft", "acoustic", "slow", "unplugged", "romantic"),
    "party": ("party", "dance", "remix", "club", "dj"),
}

# ── in-memory cache, invalidated when the folder's mtime changes ──────────────
_cache: list[dict] | None = None
_cache_sig: tuple | None = None


def _music_dir() -> Path:
    """Return the configured collection directory as a resolved Path."""
    from core.config import MUSIC_DIR
    return Path(MUSIC_DIR).expanduser().resolve()


def _dir_signature(root: Path) -> tuple:
    """Cheap fingerprint of the tree (mtimes) so we rescan only when it changes."""
    if not root.exists():
        return ()
    sig = []
    for dirpath, _dirs, files in os.walk(root):
        try:
            sig.append((dirpath, os.stat(dirpath).st_mtime, len(files)))
        except OSError:
            continue
    return tuple(sig)


def _track_id(rel: str) -> str:
    """Short stable id for a track from its relpath."""
    return hashlib.blake2b(rel.encode("utf-8"), digest_size=8).hexdigest()


def _title_artist(path: Path) -> tuple[str, str]:
    """Best-effort (title, artist) from tags if mutagen is present, else filename."""
    try:
        import mutagen  # optional; only used if installed
        meta = mutagen.File(str(path), easy=True)
        if meta:
            title = (meta.get("title") or [""])[0].strip()
            artist = (meta.get("artist") or [""])[0].strip()
            if title:
                return title, artist
    except Exception:
        pass
    # Filename fallback: "Artist - Title" → (Title, Artist); else (stem, "").
    stem = path.stem.strip()
    if " - " in stem:
        left, right = stem.split(" - ", 1)
        return right.strip(), left.strip()
    return stem, ""


def scan_library(force: bool = False) -> list[dict]:
    """Return the collection as [{id, title, artist, rel, subfolder}], newest-first.

    Tolerates a missing folder (returns []). Cached until the tree's mtime changes.
    """
    global _cache, _cache_sig
    root = _music_dir()
    sig = _dir_signature(root)
    if not force and _cache is not None and sig == _cache_sig:
        return _cache

    tracks: list[dict] = []
    if root.exists():
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.startswith(".") or Path(name).suffix.lower() not in AUDIO_EXTS:
                    continue
                p = Path(dirpath) / name
                rel = str(p.relative_to(root))
                title, artist = _title_artist(p)
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    mtime = 0
                tracks.append({
                    "id": _track_id(rel),
                    "title": title,
                    "artist": artist,
                    "rel": rel,
                    "subfolder": str(Path(rel).parent) if Path(rel).parent != Path(".") else "",
                    "_mtime": mtime,
                })
    tracks.sort(key=lambda t: t.get("_mtime", 0), reverse=True)
    for t in tracks:
        t.pop("_mtime", None)

    _cache, _cache_sig = tracks, sig
    return tracks


def resolve_track(track_id: str) -> Path | None:
    """Map a track id to its absolute path, or None.

    Security boundary for the streaming endpoint: the returned path is guaranteed
    to be inside MUSIC_DIR and to have an audio extension.
    """
    if not track_id:
        return None
    root = _music_dir()
    for t in scan_library():
        if t["id"] == track_id:
            p = (root / t["rel"]).resolve()
            try:
                inside = p.is_relative_to(root)   # py3.9+
            except AttributeError:                # very old pythons
                inside = str(p).startswith(str(root) + os.sep)
            if inside and p.suffix.lower() in AUDIO_EXTS and p.is_file():
                return p
            return None
    return None


def local_matches(value: str, limit: int = 50) -> list[dict]:
    """Tracks that match a taste string (e.g. "ghazals, sufi, soft music").

    Matches taste tokens (and their synonyms) against the relpath, subfolder,
    title and artist. With no tokens, returns the whole library (recent-first).
    """
    lib = scan_library()
    tokens = [w for w in _tokenize(value) if len(w) >= 3]
    if not tokens:
        return lib[:limit]

    wanted: set[str] = set()
    for tok in tokens:
        wanted.add(tok)
        for base, syns in _TASTE_SYNONYMS.items():
            if tok == base or tok in syns:
                wanted.update(syns)

    out = []
    for t in lib:
        hay = " ".join((t["rel"], t["subfolder"], t["title"], t["artist"])).lower()
        if any(w in hay for w in wanted):
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _tokenize(value: str) -> list[str]:
    """Lowercase alphabetic tokens from a free-text taste string."""
    import re
    return re.findall(r"[a-z]+", (value or "").lower())
