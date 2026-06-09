"""
Code Module — analyze directories and answer questions about code.

Commands:
  "analyze this project"              → scan current project
  "analyze /path/to/dir"             → scan specific directory
  "what does this codebase do"       → LLM explanation
  "show complex functions"           → complexity report
  "security scan /path"              → bandit security scan
  "how many lines of code"           → LOC summary
"""

import logging
import re
from pathlib import Path
from typing import Any

from core.base_module import BaseModule, ModuleResponse
from modules.code.analyzer import scan_directory, format_scan_report

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).parent.parent.parent

_PATH_RE = re.compile(r"(/[\w/._~-]+|~/[\w/._-]+|\./[\w/._-]+)", re.IGNORECASE)
_ANALYZE_WORDS = {"analyze", "analyse", "scan", "inspect", "check", "review", "audit", "explore", "examine"}
_EXPLAIN_WORDS = {"explain", "what does", "what is", "describe", "tell me about", "overview", "summary of"}
_COMPLEX_WORDS = {"complex", "complexity", "hard to read", "spaghetti", "difficult"}
_LOC_WORDS     = {"lines", "loc", "how big", "how many files", "size of"}
_SECURITY_WORDS = {"security", "vulnerabilities", "bandit", "insecure", "issues"}


def _intent(query: str) -> str:
    q = query.lower()
    if any(w in q for w in _EXPLAIN_WORDS):
        return "explain"
    if any(w in q for w in _COMPLEX_WORDS):
        return "complexity"
    if any(w in q for w in _SECURITY_WORDS):
        return "security"
    if any(w in q for w in _LOC_WORDS):
        return "loc"
    if any(w in q for w in _ANALYZE_WORDS):
        return "analyze"
    return "analyze"


def _resolve_path(query: str) -> Path:
    """Extract path from query or default to project root."""
    q = query.lower()
    # Special shortcuts
    if "this project" in q or "our project" in q or "gk" in q:
        return _PROJECT_ROOT
    if "farming" in q:
        return _PROJECT_ROOT / "modules" / "farming"
    if "finance" in q:
        return _PROJECT_ROOT / "modules" / "finance"
    if "dashboard" in q:
        return _PROJECT_ROOT / "dashboard"
    if "security" in q or "guardian" in q:
        return _PROJECT_ROOT / "security"
    if "diary" in q:
        return _PROJECT_ROOT / "modules" / "diary"
    m = _PATH_RE.search(query)
    if m:
        return Path(m.group(1)).expanduser()
    return _PROJECT_ROOT


def _do_analyze(query: str) -> tuple[str, dict | None]:
    target = _resolve_path(query)
    log.info("code analyze: %s", target)
    scan = scan_directory(target)
    if "error" in scan:
        return scan["error"], None
    report = format_scan_report(scan)
    return report, {"path": str(target), "files": scan["total_files"], "loc": scan["total_loc"]}


def _do_explain(query: str) -> tuple[str, dict | None]:
    target = _resolve_path(query)
    scan = scan_directory(target)
    if "error" in scan:
        return scan["error"], None

    ctx = scan["llm_context"]
    try:
        from core.llm import generate
        prompt = (
            f"{ctx}\n\n"
            f"In 3-4 sentences, explain what this codebase does, its main purpose, "
            f"and the key components. Be specific to the directory name and languages found."
        )
        explanation = generate(prompt)
        return f"◈ {target.name}\n\n{explanation}\n\n---\n{format_scan_report(scan)}", {"path": str(target)}
    except Exception as e:
        log.warning("LLM explain failed: %s — returning scan only", e)
        return format_scan_report(scan), {"path": str(target)}


def _do_complexity(query: str) -> tuple[str, dict | None]:
    target = _resolve_path(query)
    scan = scan_directory(target)
    if "error" in scan:
        return scan["error"], None
    funcs = scan["top_complex_functions"]
    if not funcs:
        return f"No complex functions found in {target.name} (all cyclomatic complexity < 5).", None
    lines = [f"Complex functions in {target.name} (cyclomatic complexity):"]
    for fn in funcs[:15]:
        lines.append(f"  [{fn['risk']:<6}] cc={fn['complexity']:>2}  {fn['file']}:{fn['line']}  {fn['function']}()")
    lines.append(f"\nTotal: {len(funcs)} function(s) with cc ≥ 5")
    return "\n".join(lines), {"count": len(funcs)}


def _do_security(query: str) -> tuple[str, dict | None]:
    target = _resolve_path(query)
    scan = scan_directory(target)
    if "error" in scan:
        return scan["error"], None
    issues = scan["security_issues"]
    if not issues:
        return f"✓ No security issues found by bandit in {target.name}.", {"issues": 0}
    lines = [f"Security issues in {target.name} ({len(issues)} found):"]
    for iss in issues:
        lines.append(f"  [{iss['severity']}] {iss['file']}:{iss['line']} — {iss['issue']}")
    return "\n".join(lines), {"issues": len(issues)}


def _do_loc(query: str) -> tuple[str, dict | None]:
    target = _resolve_path(query)
    scan = scan_directory(target)
    if "error" in scan:
        return scan["error"], None
    lines = [f"Code size: {target.name}  ({scan['total_files']} files, {scan['total_loc']:,} total lines)\n"]
    for lang in scan["by_language"]:
        pct = round(lang["code_loc"] / max(1, scan["total_loc"]) * 100)
        lines.append(f"  {lang['language']:<14} {lang['files']:>4} files  {lang['code_loc']:>7,} LOC  ({pct}%)")
    return "\n".join(lines), {"total_loc": scan["total_loc"], "files": scan["total_files"]}


class CodeModule(BaseModule):
    name = "code"
    description = (
        "Analyzes code directories: file counts, LOC by language, "
        "cyclomatic complexity, security scan, LLM explanation of codebase."
    )

    def handle(self, query: str, context: dict[str, Any]) -> ModuleResponse:
        intent = _intent(query)
        log.info("code intent=%s q=%r", intent, query[:80])

        if intent == "explain":
            text, data = _do_explain(query)
            follow_up = "Say 'show complex functions' to see which parts are hardest to maintain."
        elif intent == "complexity":
            text, data = _do_complexity(query)
            follow_up = "Say 'security scan' to check for vulnerabilities."
        elif intent == "security":
            text, data = _do_security(query)
            follow_up = None
        elif intent == "loc":
            text, data = _do_loc(query)
            follow_up = "Say 'analyze this project' for full breakdown including complexity."
        else:
            text, data = _do_analyze(query)
            follow_up = "Say 'explain this codebase' for an AI-written overview."

        return ModuleResponse(text=text, module=self.name, data=data, follow_up=follow_up)
