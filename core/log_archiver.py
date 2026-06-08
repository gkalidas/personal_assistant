"""
Log archiver — runs automatically when a log file hits its size limit.

Instead of keeping multiple .1/.2/.3 backup copies of the full file,
the archiver extracts only the meaningful lines and writes them to a
compact archive, then deletes the original.

Archive location:  logs/archive/<stem>_YYYYMMDD_HHMMSS.log

Lines kept in archive:
  • Every ERROR and WARNING line
  • INFO lines that carry real signal: query IDs, routing decisions,
    action dispatches, latency readings, security alerts, task starts
  • The startup banner (===)

Lines dropped:
  • DEBUG lines (LLM payload dumps, sub-second noise)
  • Boilerplate INFO that carries no decision value

This keeps the audit trail intact (timestamps preserved) while
staying well under 100 KB per archive vs 5 MB of raw logs.
"""

import re
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


# ── Which lines are worth keeping ────────────────────────────────────────────

# Always keep ERROR and WARNING lines.
# The format "%(levelname)-5s" pads short names: INFO→"INFO ", but WARNING
# stays "WARNING" (7 chars, not truncated), so we match the prefix only.
_LEVEL_KEEP_RE = re.compile(r"\[(ERROR|WARN)")

# INFO lines worth keeping — these carry routing or action decisions
_INFO_KEEP_RE = re.compile(
    r"query id=|"           # every user query
    r"route →|"             # router decision
    r"action=\w|"           # module dispatched an action
    r"latency=\d|"          # end-to-end latency recorded
    r"warmup \w|"           # model load on startup
    r"=== GK|"              # assistant startup banner
    r"=== Security|"        # guardian startup banner
    r"task_\w|"             # guardian background task
    r"VULNERABLE|"          # threat intel hit
    r"ALERT|"               # anomaly or CVE alert
    r"CVE|"                 # CVE finding
    r"sanitizer:|"          # sanitizer block
    r"action blocked|"      # validator block
    r"LLM call failed|"     # LLM error (also captured as ERROR)
    r"Patched|"             # auto-patch applied
    r"slow LLM"             # performance warning
)


def _keep(line: str) -> bool:
    """Return True if this log line belongs in the archive."""
    if _LEVEL_KEEP_RE.search(line):
        return True
    # For INFO lines, only keep those with real signal
    if "[INFO ]" in line:
        return bool(_INFO_KEEP_RE.search(line))
    return False


# ── Core archive function ─────────────────────────────────────────────────────

def archive_log(log_path: Path, archive_dir: Path) -> Path | None:
    """
    Extract meaningful lines from log_path, write to archive_dir, delete original.
    Returns the archive path, or None if the log was empty/unreadable.
    """
    if not log_path.exists() or log_path.stat().st_size == 0:
        return None

    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    kept = [line for line in lines if _keep(line)]

    archive_path = None
    if kept:
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = archive_dir / f"{log_path.stem}_{stamp}.log"

        header = (
            f"# Archive of {log_path.name}  —  {datetime.now().isoformat(timespec='seconds')}\n"
            f"# Original lines: {len(lines)}   Kept: {len(kept)}\n"
        )
        archive_path.write_text(header + "\n".join(kept) + "\n", encoding="utf-8")

    # Delete the original so disk usage stays flat
    try:
        log_path.unlink()
    except OSError:
        pass

    return archive_path


# ── Custom rotating handler ───────────────────────────────────────────────────

class ArchivingRotatingHandler(RotatingFileHandler):
    """
    Drop-in replacement for RotatingFileHandler.

    On rollover:
      1. Flush and close the current file.
      2. Archive meaningful lines → logs/archive/<stem>_<timestamp>.log
      3. Delete the original log file.
      4. Reopen a fresh empty log file.

    backupCount is unused (no .1/.2/.3 files are created).
    """

    def __init__(self, filename: str, archive_dir: str | Path | None = None, **kwargs):
        # backupCount=0 because we don't use the rename chain
        kwargs["backupCount"] = 0
        super().__init__(filename, **kwargs)

        if archive_dir is not None:
            self._archive_dir = Path(archive_dir)
        else:
            # Default: logs/archive/ next to the log file
            self._archive_dir = Path(filename).parent / "archive"

    def doRollover(self) -> None:
        """Archive key lines then start a fresh file."""
        # Close the stream before touching the file
        if self.stream:
            self.stream.flush()
            self.stream.close()
            self.stream = None

        archive_log(Path(self.baseFilename), self._archive_dir)

        # Reopen — creates a new empty file since the original was deleted
        self.stream = self._open()
