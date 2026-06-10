"""
Code directory analyzer.

Scans a directory and returns:
  - File count + LOC by language
  - Top-level structure summary
  - Python complexity metrics (AST-based cyclomatic complexity)
  - Security issues via bandit (if available)
  - LLM-ready context string for "explain this codebase" queries
"""

import ast
import logging
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Language detection by extension
_EXT_LANG: dict[str, str] = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript", ".jsx": "JavaScript", ".html": "HTML",
    ".css": "CSS", ".scss": "CSS", ".json": "JSON", ".yaml": "YAML",
    ".yml": "YAML", ".sh": "Shell", ".bash": "Shell", ".md": "Markdown",
    ".sql": "SQL", ".rs": "Rust", ".go": "Go", ".java": "Java",
    ".c": "C", ".cpp": "C++", ".h": "C/C++ Header", ".rb": "Ruby",
    ".tf": "Terraform", ".toml": "TOML", ".ini": "Config", ".cfg": "Config",
}

# Dirs to always skip
_SKIP_DIRS = {
    ".git", ".venv", "venv", "envs", "__pycache__", "node_modules",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".tox",
    "site-packages", ".eggs", "logs",
}

# File extensions that are binary/data — never count as code LOC
_SKIP_EXTS = {
    ".pyc", ".pyo", ".pyd",            # Python bytecode
    ".log", ".jsonl",                   # Runtime logs / event journals
    ".db", ".sqlite", ".sqlite3",       # Databases
    ".enc",                             # Encrypted backups
    ".png", ".jpg", ".jpeg", ".gif",    # Images
    ".ico", ".svg", ".webp", ".heic",   # More images
    ".woff", ".woff2", ".ttf", ".otf",  # Fonts
    ".mp3", ".wav", ".ogg", ".mp4",     # Media
    ".zip", ".tar", ".gz", ".bz2",      # Archives
    ".bin", ".so", ".dylib", ".dll",    # Binaries
    ".model", ".onnx", ".pt",           # ML model files
    ".sample",                          # Git sample files
    ".TAG",                             # Tag files
}


def _should_skip(path: Path) -> bool:
    return any(part in _SKIP_DIRS or part.endswith(".egg-info")
               for part in path.parts)


def _count_lines(path: Path) -> tuple[int, int]:
    """Return (total_lines, code_lines) — code excludes blank + comment lines."""
    try:
        text = path.read_text(errors="ignore")
        lines = text.splitlines()
        total = len(lines)
        code = sum(1 for l in lines if l.strip() and not l.strip().startswith(("#", "//", "*", "/*", "*/")))
        return total, code
    except Exception:
        return 0, 0


def _py_complexity(path: Path) -> list[dict]:
    """AST-based cyclomatic complexity for Python files (no radon needed)."""
    try:
        tree = ast.parse(path.read_text(errors="ignore"))
    except SyntaxError:
        return []
    results = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Count branch points: if/elif/for/while/except/with/assert/comprehension
        complexity = 1
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler,
                                  ast.With, ast.Assert, ast.comprehension)):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
        if complexity >= 5:  # only report non-trivial functions
            results.append({
                "function": node.name,
                "line": node.lineno,
                "complexity": complexity,
                "risk": "HIGH" if complexity >= 10 else "MEDIUM" if complexity >= 7 else "LOW",
            })
    return sorted(results, key=lambda x: -x["complexity"])


def _run_bandit_on_dir(target: Path) -> list[dict]:
    """Run bandit on target dir, return issues list."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "bandit", "-r", str(target), "-f", "json", "-ll",
             "--exclude", "__pycache__,.git,.venv,node_modules"],
            capture_output=True, text=True, timeout=30,
        )
        raw = result.stdout or ""
        idx = raw.find("{")
        if idx >= 0:
            import json
            data = json.loads(raw[idx:])
            return data.get("results", [])
    except Exception:
        pass
    return []


def scan_directory(target: str | Path, *, llm_context: bool = True) -> dict[str, Any]:
    """
    Full directory scan.

    Returns dict with:
      path, total_files, total_loc, by_language, structure,
      top_complex_functions, security_issues, llm_context
    """
    root = Path(target).expanduser().resolve()
    if not root.exists():
        return {"error": f"Directory not found: {target}"}
    if not root.is_dir():
        return {"error": f"Not a directory: {target}"}

    by_lang: dict[str, dict] = defaultdict(lambda: {"files": 0, "loc": 0, "code_loc": 0})
    all_py_files: list[Path] = []
    structure: dict[str, list[str]] = defaultdict(list)
    total_files = 0
    total_loc = 0

    for fpath in root.rglob("*"):
        if fpath.is_dir() or _should_skip(fpath):
            continue
        ext = fpath.suffix.lower()
        if ext in _SKIP_EXTS:
            continue
        # Skip extensionless files (binary model caches, git objects, etc.)
        if not ext:
            continue
        lang = _EXT_LANG.get(ext, "Other")
        total, code = _count_lines(fpath)
        by_lang[lang]["files"] += 1
        by_lang[lang]["loc"] += total
        by_lang[lang]["code_loc"] += code
        total_files += 1
        total_loc += total
        # Top-level structure: first 2 levels
        try:
            rel = fpath.relative_to(root)
            parts = rel.parts
            top = parts[0] if len(parts) > 1 else "."
            structure[top].append(fpath.name)
        except Exception:
            pass
        if ext == ".py":
            all_py_files.append(fpath)

    # Python complexity — top 10 most complex functions across all .py files
    complex_funcs: list[dict] = []
    for py in all_py_files:
        for item in _py_complexity(py):
            item["file"] = str(py.relative_to(root))
            complex_funcs.append(item)
    complex_funcs.sort(key=lambda x: -x["complexity"])
    top_complex = complex_funcs[:10]

    # Security scan (bandit) on Python files
    sec_issues = []
    if all_py_files:
        raw_issues = _run_bandit_on_dir(root)
        for iss in raw_issues[:20]:
            sec_issues.append({
                "file": iss.get("filename", "").replace(str(root) + "/", ""),
                "line": iss.get("line_number", 0),
                "severity": iss.get("issue_severity", "LOW"),
                "issue": iss.get("issue_text", ""),
            })

    # Sort languages by code LOC descending
    lang_sorted = sorted(
        [{"language": k, **v} for k, v in by_lang.items()],
        key=lambda x: -x["code_loc"],
    )

    # Structure summary: top dirs + file count
    struct_summary = {
        k: len(v) for k, v in sorted(structure.items(), key=lambda x: -len(x[1]))
    }

    # LLM-ready context string
    ctx = ""
    if llm_context:
        lang_lines = ", ".join(
            f"{l['language']} ({l['code_loc']} LOC)" for l in lang_sorted[:5]
        )
        top_dirs = ", ".join(list(struct_summary.keys())[:8])
        ctx = (
            f"Directory: {root.name}  |  {total_files} files  |  {total_loc} total lines\n"
            f"Languages: {lang_lines}\n"
            f"Structure: {top_dirs}\n"
        )
        if top_complex:
            ctx += f"Most complex: {top_complex[0]['file']}:{top_complex[0]['function']} (complexity={top_complex[0]['complexity']})\n"
        if sec_issues:
            ctx += f"Security: {len(sec_issues)} bandit issue(s)\n"

    return {
        "path": str(root),
        "name": root.name,
        "total_files": total_files,
        "total_loc": total_loc,
        "by_language": lang_sorted,
        "structure": struct_summary,
        "top_complex_functions": top_complex,
        "security_issues": sec_issues,
        "llm_context": ctx,
    }


def format_scan_report(scan: dict) -> str:
    """Human-readable summary of scan_directory output."""
    if "error" in scan:
        return f"Error: {scan['error']}"

    lines = [
        f"◈ {scan['name']}  —  {scan['total_files']} files  ·  {scan['total_loc']:,} lines",
        "",
        "LANGUAGES",
    ]
    for lang in scan["by_language"][:8]:
        bar = "█" * min(20, lang["code_loc"] // max(1, scan["total_loc"] // 20))
        lines.append(f"  {lang['language']:<14} {lang['files']:>4} files  {lang['code_loc']:>6} LOC  {bar}")

    lines += ["", "STRUCTURE"]
    for d, count in list(scan["structure"].items())[:8]:
        lines.append(f"  {d:<20} {count} files")

    if scan["top_complex_functions"]:
        lines += ["", "COMPLEX FUNCTIONS  (cyclomatic complexity ≥ 5)"]
        for fn in scan["top_complex_functions"][:5]:
            lines.append(f"  [{fn['risk']:<6}] {fn['file']}:{fn['line']} {fn['function']}()  cc={fn['complexity']}")

    if scan["security_issues"]:
        lines += ["", f"SECURITY ISSUES  ({len(scan['security_issues'])} found)"]
        for iss in scan["security_issues"][:5]:
            lines.append(f"  [{iss['severity']}] {iss['file']}:{iss['line']} — {iss['issue']}")
    else:
        lines += ["", "SECURITY  ✓ bandit: no issues"]

    return "\n".join(lines)
