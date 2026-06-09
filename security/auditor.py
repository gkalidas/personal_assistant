"""
Code security auditor.
Runs bandit (static analysis) + custom checks for:
  - SQL injection patterns in source
  - Unsafe file path handling
  - Hardcoded secrets / credentials
  - Overly permissive file permissions on sensitive files
  - Exposed network ports (Ollama)
"""

import json
import os
import re
import socket
import subprocess
import stat
import sys
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger("security.auditor")

PROJECT = Path(__file__).parent.parent

# Files that must have tight permissions (no group/world read)
SENSITIVE_FILES = [
    "user_profile.json",
    ".env",
    "farming.db",
    "finance.db",
    "health.db",
    "personal_assistant.db",
]

# Patterns to flag in Python source code
_CODE_ISSUES = [
    (re.compile(r'eval\s*\(', re.I),               "CRITICAL", "Use of eval() — potential code injection"),
    (re.compile(r'exec\s*\(', re.I),               "CRITICAL", "Use of exec() — potential code injection"),
    (re.compile(r'__import__\s*\(', re.I),         "HIGH",     "Dynamic import via __import__()"),
    (re.compile(r'os\.system\s*\(', re.I),         "HIGH",     "os.system() — prefer subprocess with args list"),
    (re.compile(r'shell\s*=\s*True', re.I),        "HIGH",     "subprocess shell=True — command injection risk"),
    (re.compile(r'f["\'].*\{.*\}.*["\'].*execute', re.I), "HIGH", "f-string in SQL execute() — use parameterized queries"),
    (re.compile(r'%\s*\(.*\)\s*execute', re.I),   "HIGH",     "% formatting in SQL — use parameterized queries"),
    (re.compile(r'password\s*=\s*["\'][^"\']+["\']', re.I), "HIGH", "Hardcoded password in source"),
    (re.compile(r'secret\s*=\s*["\'][^"\']{8,}["\']', re.I), "MEDIUM", "Possible hardcoded secret"),
    (re.compile(r'api_key\s*=\s*["\'][^"\']+["\']', re.I), "HIGH", "Hardcoded API key in source"),
    (re.compile(r'token\s*=\s*["\'][^"\']{20,}["\']', re.I), "MEDIUM", "Possible hardcoded token"),
    (re.compile(r'open\s*\(.*\+.*mode.*w', re.I), "LOW",      "File opened for write with concatenated path"),
    (re.compile(r'pickle\.loads?\(', re.I),        "HIGH",     "pickle.load() — unsafe deserialization"),
    (re.compile(r'yaml\.load\s*\([^)]*\)', re.I), "HIGH",     "yaml.load() without Loader — use yaml.safe_load()"),
    (re.compile(r'verify\s*=\s*False', re.I),      "MEDIUM",   "SSL verification disabled (verify=False)"),
    (re.compile(r'PYTHONDONTWRITEBYTECODE|PYTHONPATH.*=', re.I), "LOW", "Env var manipulation in code"),
]

_SECRET_IN_ENV = re.compile(r'^(PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL)=.{4,}', re.I | re.M)


def _run_bandit() -> list[dict]:
    """Run bandit on our source tree. Returns list of issue dicts."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "bandit", "-r", str(PROJECT), "-f", "json", "-ll",
             "--exclude", ".venv,envs,__pycache__,.git,logs,inputs,security"],
            capture_output=True, text=True, timeout=60,
        )
        if result.stdout and result.stdout.strip():
            data = json.loads(result.stdout)
            return data.get("results", [])
        return []  # bandit found no issues (exit 0, empty output)
    except FileNotFoundError:
        log.warning("bandit not installed — skipping static analysis. Install: pip install bandit")
    except Exception as e:
        log.error(f"bandit failed: {e}")
    return []


def _scan_source_files() -> list[dict]:
    """Custom regex scan of Python source files."""
    issues = []
    # Exclude security/auditor.py itself — its pattern strings trigger false positives
    src_dirs = [PROJECT / "core", PROJECT / "modules", PROJECT / "scripts"]
    for src_dir in src_dirs:
        for py_file in src_dir.rglob("*.py"):
            try:
                text = py_file.read_text(errors="ignore")
                for pattern, severity, desc in _CODE_ISSUES:
                    for m in pattern.finditer(text):
                        line_no = text[:m.start()].count("\n") + 1
                        issues.append({
                            "file":     str(py_file.relative_to(PROJECT)),
                            "line":     line_no,
                            "severity": severity,
                            "issue":    desc,
                            "snippet":  m.group(0)[:80],
                        })
            except Exception:
                pass
    return issues


def _check_file_permissions() -> list[dict]:
    """Verify sensitive files don't have excessive permissions."""
    issues = []
    for fname in SENSITIVE_FILES:
        for path in PROJECT.glob(fname):
            try:
                mode = path.stat().st_mode
                # Warn if group or other can read
                if mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
                    perm_str = oct(mode)[-3:]
                    issues.append({
                        "file":     str(path.relative_to(PROJECT)),
                        "severity": "HIGH",
                        "issue":    f"Sensitive file has permissive permissions: {perm_str}",
                        "fix":      f"chmod 600 {path}",
                    })
            except Exception:
                pass
    return issues


def _fix_file_permissions() -> list[str]:
    """Auto-fix overly permissive files to 600."""
    fixed = []
    for fname in SENSITIVE_FILES:
        for path in PROJECT.glob(fname):
            try:
                current = path.stat().st_mode
                if current & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
                    path.chmod(0o600)
                    fixed.append(str(path.relative_to(PROJECT)))
                    log.info(f"Fixed permissions: {path} → 600")
            except Exception as e:
                log.error(f"Could not fix permissions for {path}: {e}")
    return fixed


def _check_ollama_exposure() -> dict:
    """
    Check if Ollama is bound to 0.0.0.0 (network-exposed) vs 127.0.0.1 (localhost only).
    Reads /proc/net/tcp which shows the actual bound address in hex.
    0100007F = 127.0.0.1 (safe)  |  00000000 = 0.0.0.0 (exposed)
    """
    result = {"exposed": False, "details": "Ollama bound to localhost only (safe)"}
    port_hex = format(11434, '04X')[::-1]  # little-endian hex of port
    # Pad to 4 chars: 11434 = 0x2C8A → hex '2C8A' → reversed per byte '8A2C' → '8A2C'
    port_hex_le = "".join(
        format(11434, '04X')[i:i+2] for i in range(2, -1, -2)
    )
    try:
        tcp_data = Path("/proc/net/tcp").read_text()
        for line in tcp_data.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            local_addr = parts[1]  # e.g. "00000000:2C8A"
            addr_hex, p_hex = local_addr.split(":")
            if p_hex.upper() == port_hex_le.upper() or int(p_hex, 16) == 11434:
                if addr_hex == "00000000":
                    result["exposed"] = True
                    result["details"] = (
                        "Ollama port 11434 is bound to 0.0.0.0 — "
                        "accessible from any network interface. "
                        "Fix: set OLLAMA_HOST=127.0.0.1 in your environment."
                    )
                break
    except Exception:
        # /proc/net/tcp unavailable (non-Linux) — fall back to connection test
        try:
            for host in ["10.0.0.1", "192.168.1.1"]:  # typical LAN addresses
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                if sock.connect_ex((host, 11434)) == 0:
                    result["exposed"] = True
                    result["details"] = f"Ollama reachable at {host}:11434"
                sock.close()
        except Exception:
            pass
    return result


def _check_env_file() -> list[dict]:
    """Check .env file for hardcoded secrets."""
    issues = []
    env_path = PROJECT / ".env"
    if env_path.exists():
        text = env_path.read_text(errors="ignore")
        for m in _SECRET_IN_ENV.finditer(text):
            issues.append({
                "file": ".env",
                "severity": "MEDIUM",
                "issue": "Secret-looking variable in .env file — ensure .env is gitignored",
                "snippet": m.group(0)[:40] + "...",
            })
        # Check it's gitignored
        gitignore = PROJECT / ".gitignore"
        if gitignore.exists() and ".env" not in gitignore.read_text():
            issues.append({
                "file": ".env",
                "severity": "CRITICAL",
                "issue": ".env file exists but is NOT in .gitignore — secrets may be committed to git",
            })
    return issues


def run_audit(auto_fix_permissions: bool = True) -> dict:
    """Full code audit. Returns structured report."""
    log.info("Running code security audit...")

    bandit_issues    = _run_bandit()
    custom_issues    = _scan_source_files()
    perm_issues      = _check_file_permissions()
    ollama_check     = _check_ollama_exposure()
    env_issues       = _check_env_file()
    fixed_perms      = []

    if auto_fix_permissions and perm_issues:
        fixed_perms = _fix_file_permissions()

    all_issues = custom_issues + perm_issues + env_issues
    for b in bandit_issues:
        all_issues.append({
            "file":     b.get("filename", "").replace(str(PROJECT) + "/", ""),
            "line":     b.get("line_number", 0),
            "severity": b.get("issue_severity", "LOW").upper(),
            "issue":    b.get("issue_text", ""),
            "snippet":  b.get("code", "")[:80],
            "source":   "bandit",
        })

    severity_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for iss in all_issues:
        s = iss.get("severity", "LOW")
        severity_counts[s] = severity_counts.get(s, 0) + 1

    return {
        "audited_at":        datetime.now().isoformat(),
        "total_issues":      len(all_issues),
        "severity_counts":   severity_counts,
        "ollama_exposed":    ollama_check["exposed"],
        "ollama_details":    ollama_check["details"],
        "fixed_permissions": fixed_perms,
        "issues":            sorted(all_issues, key=lambda x: {"CRITICAL":0,"HIGH":1,"MEDIUM":2,"LOW":3}.get(x.get("severity","LOW"), 4)),
    }
