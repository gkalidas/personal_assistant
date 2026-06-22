"""
GK Personal Assistant — centralised logging configuration.

Two entry points:
  setup_logging()          — call once in main.py
  setup_security_logging() — call once in security/guardian.py

Log files produced:
  logs/gk.log                 INFO+  everything from the main assistant process
  logs/errors.log             ERROR+ every component, one place for failures
  logs/security/guardian.log  INFO+  security daemon only

Usage in every module:
  import logging
  log = logging.getLogger(__name__)

Level guide:
  DEBUG   LLM payloads and raw responses (enable: GK_DEBUG=1)
  INFO    routing decision, action dispatched, tool result, task start/end
  WARNING sanitizer block, validator hit, fallback path taken, slow LLM
  ERROR   LLM failure, DB error, uncaught exception
"""

import logging
import logging.config
import os
from pathlib import Path

# Register the custom handler so dictConfig can reference it by class path
from core.log_archiver import ArchivingRotatingHandler  # noqa: F401

_PROJECT  = Path(__file__).parent.parent
_LOG_DIR  = _PROJECT / "logs"
_SEC_DIR  = _LOG_DIR / "security"

_FMT_FILE    = "%(asctime)s [%(levelname)-5s] %(name)-30s %(message)s"
_FMT_CONSOLE = "[%(levelname)-5s] %(name)s: %(message)s"
_DATE        = "%Y-%m-%d %H:%M:%S"


# Third-party libraries to silence to WARNING in every config.
_NOISY_LOGGERS = {
    "httpx":              {"level": "WARNING", "propagate": True},
    "httpcore":           {"level": "WARNING", "propagate": True},
    "urllib3":            {"level": "WARNING", "propagate": True},
    "charset_normalizer": {"level": "WARNING", "propagate": True},
}


def _level() -> str:
    """Return 'DEBUG' when GK_DEBUG is set in the environment, else 'INFO'."""
    return "DEBUG" if os.getenv("GK_DEBUG", "").lower() in ("1", "true", "yes") else "INFO"


def _archiving_handler(filename: Path, max_bytes: int, level: str) -> dict:
    """Build a dictConfig entry for an archiving rotating file handler."""
    return {
        "class":       "core.log_archiver.ArchivingRotatingHandler",
        "filename":    str(filename),
        "maxBytes":    max_bytes,
        "archive_dir": str(_LOG_DIR / "archive"),
        "encoding":    "utf-8",
        "formatter":   "file",
        "level":       level,
    }


def setup_logging() -> None:
    """Configure logging for the main assistant process. Safe to call multiple times."""
    if logging.getLogger().handlers:
        return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    config = {
        "version": 1,
        "disable_existing_loggers": False,

        "formatters": {
            "file": {
                "format":  _FMT_FILE,
                "datefmt": _DATE,
            },
            "console": {
                "format": _FMT_CONSOLE,
            },
        },

        "handlers": {
            "gk_file":     _archiving_handler(_LOG_DIR / "gk.log",     5 * 1024 * 1024, _level()),
            "errors_file": _archiving_handler(_LOG_DIR / "errors.log", 2 * 1024 * 1024, "ERROR"),
            # Console — WARNING+ only, keep the terminal clean
            "console": {
                "class":     "logging.StreamHandler",
                "formatter": "console",
                "level":     "WARNING",
                "stream":    "ext://sys.stderr",
            },
        },

        "root": {
            "level":    _level(),
            "handlers": ["gk_file", "errors_file", "console"],
        },

        # Silence noisy third-party libraries
        "loggers": {**_NOISY_LOGGERS, "matplotlib": {"level": "WARNING", "propagate": True}},
    }

    logging.config.dictConfig(config)
    logging.getLogger("gk").info("=== GK assistant started — log=%s ===", _LOG_DIR / "gk.log")


def setup_security_logging() -> None:
    """Configure logging for the security guardian daemon. Safe to call multiple times."""
    if logging.getLogger().handlers:
        return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _SEC_DIR.mkdir(parents=True, exist_ok=True)

    config = {
        "version": 1,
        "disable_existing_loggers": False,

        "formatters": {
            "file": {
                "format":  _FMT_FILE,
                "datefmt": _DATE,
            },
            "console": {
                "format":  "%(asctime)s [%(levelname)-5s] %(message)s",
                "datefmt": "%H:%M:%S",
            },
        },

        "handlers": {
            "security_file": _archiving_handler(_SEC_DIR / "guardian.log", 5 * 1024 * 1024, "INFO"),
            "errors_file":   _archiving_handler(_LOG_DIR / "errors.log",    2 * 1024 * 1024, "ERROR"),
            # Console — INFO+ for the daemon (it runs in background, stdout goes to file)
            "console": {
                "class":     "logging.StreamHandler",
                "formatter": "console",
                "level":     "INFO",
                "stream":    "ext://sys.stdout",
            },
        },

        "root": {
            "level":    "INFO",
            "handlers": ["security_file", "errors_file", "console"],
        },

        "loggers": dict(_NOISY_LOGGERS),
    }

    logging.config.dictConfig(config)
    logging.getLogger("guardian").info(
        "=== Security guardian started — log=%s ===", _SEC_DIR / "guardian.log"
    )
