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
    """True if any path part is a skip-dir (or an .egg-info)."""
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


def _walk_files(root: Path) -> dict:
    """Walk a tree (skipping ignored dirs/exts), tallying LOC by language and structure.

    Returns {by_lang, structure, py_files, total_files, total_loc}.
    """
    by_lang: dict[str, dict] = defaultdict(lambda: {"files": 0, "loc": 0, "code_loc": 0})
    structure: dict[str, list[str]] = defaultdict(list)
    py_files: list[Path] = []
    total_files = total_loc = 0

    for fpath in root.rglob("*"):
        if fpath.is_dir() or _should_skip(fpath):
            continue
        ext = fpath.suffix.lower()
        if not ext or ext in _SKIP_EXTS:   # skip binaries/data and extensionless files
            continue
        total, code = _count_lines(fpath)
        lang = _EXT_LANG.get(ext, "Other")
        by_lang[lang]["files"] += 1
        by_lang[lang]["loc"] += total
        by_lang[lang]["code_loc"] += code
        total_files += 1
        total_loc += total
        try:
            parts = fpath.relative_to(root).parts
            structure[parts[0] if len(parts) > 1 else "."].append(fpath.name)
        except Exception:
            pass
        if ext == ".py":
            py_files.append(fpath)

    return {"by_lang": by_lang, "structure": structure, "py_files": py_files,
            "total_files": total_files, "total_loc": total_loc}


def _collect_complexity(py_files: list[Path], root: Path) -> list[dict]:
    """Gather the most complex functions across all Python files (top 10)."""
    funcs: list[dict] = []
    for py in py_files:
        for item in _py_complexity(py):
            item["file"] = str(py.relative_to(root))
            funcs.append(item)
    funcs.sort(key=lambda x: -x["complexity"])
    return funcs[:10]


def _collect_security(py_files: list[Path], root: Path) -> list[dict]:
    """Run bandit and normalize its issues (top 20). Empty if no Python files."""
    if not py_files:
        return []
    return [
        {"file": iss.get("filename", "").replace(str(root) + "/", ""),
         "line": iss.get("line_number", 0),
         "severity": iss.get("issue_severity", "LOW"),
         "issue": iss.get("issue_text", "")}
        for iss in _run_bandit_on_dir(root)[:20]
    ]


def _build_llm_context(root: Path, walk: dict, lang_sorted: list[dict],
                       struct_summary: dict, top_complex: list[dict],
                       sec_issues: list[dict]) -> str:
    """Build the compact 'explain this codebase' context string for the LLM."""
    lang_lines = ", ".join(f"{l['language']} ({l['code_loc']} LOC)" for l in lang_sorted[:5])
    top_dirs = ", ".join(list(struct_summary.keys())[:8])
    ctx = (f"Directory: {root.name}  |  {walk['total_files']} files  |  {walk['total_loc']} total lines\n"
           f"Languages: {lang_lines}\nStructure: {top_dirs}\n")
    if top_complex:
        c = top_complex[0]
        ctx += f"Most complex: {c['file']}:{c['function']} (complexity={c['complexity']})\n"
    if sec_issues:
        ctx += f"Security: {len(sec_issues)} bandit issue(s)\n"
    return ctx


def scan_directory(target: str | Path, *, llm_context: bool = True) -> dict[str, Any]:
    """Full directory scan: LOC by language, structure, complexity, and security.

    Returns a dict with path, total_files, total_loc, by_language, structure,
    top_complex_functions, security_issues, and an llm_context string.
    """
    root = Path(target).expanduser().resolve()
    if not root.exists():
        return {"error": f"Directory not found: {target}"}
    if not root.is_dir():
        return {"error": f"Not a directory: {target}"}

    walk = _walk_files(root)
    top_complex = _collect_complexity(walk["py_files"], root)
    sec_issues  = _collect_security(walk["py_files"], root)
    lang_sorted = sorted(
        [{"language": k, **v} for k, v in walk["by_lang"].items()],
        key=lambda x: -x["code_loc"],
    )
    struct_summary = {k: len(v) for k, v in
                      sorted(walk["structure"].items(), key=lambda x: -len(x[1]))}
    ctx = _build_llm_context(root, walk, lang_sorted, struct_summary,
                             top_complex, sec_issues) if llm_context else ""

    return {
        "path": str(root),
        "name": root.name,
        "total_files": walk["total_files"],
        "total_loc": walk["total_loc"],
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
