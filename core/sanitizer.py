"""
Input sanitizer and PII redactor.

sanitize_input()         — cleans user query before it reaches the LLM.
sanitize_external_text() — scrubs injection patterns from external API/web content.
redact_pii()             — strips personal identifiers before writing to logs/DB.
validate_action()        — checks LLM JSON output has safe, expected field types.

Injection patterns are loaded from security/patterns.json (auto-updated by
the security guardian). Falls back to the built-in base set if not present.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_QUERY_LEN = 1000

# Unambiguous shell-injection syntax that must never appear in LLM action
# field values. Deliberately narrow: these fields feed parameterized SQLite
# queries (never a shell), so this is defense-in-depth. We match only syntax
# with no legitimate use in farm/health/finance free text — NOT bare "&&"/"||"
# or plain command words, which false-positive on natural descriptions
# (e.g. "bought seeds && fertilizer", "use python for analysis").
_SHELL_META_RE = re.compile(
    r'(?:'
    r'\$\([^)]*\)|`[^`]*`'                       # command substitution $(...) or `...`
    r'|;\s*(?:rm|chmod|chown|curl|wget|nc|bash|sh|eval)\b'  # command chained after ';'
    r'|\|\s*(?:bash|sh|nc|python|perl)\b'        # piped into an interpreter
    r'|\.\./\.\.'                                # path traversal
    r')',
    re.IGNORECASE,
)

# Built-in base patterns — always active even if patterns.json is missing
_BASE_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"ignore\s+prior",
    r"forget\s+(everything|all)(\s+(you|i)\s+(know|said|told))?",
    r"you\s+are\s+now\s+a?\s*(different|new|another)?\s*(assistant|ai|model|bot|system|dan)",
    r"\byou\s+are\s+now\s+a?\s*DAN\b",
    r"new\s+(system\s+)?instructions?\s*:",
    r"disregard\s+(?:all\s+|the\s+)?(prior|previous|above)",
    r"override\s+(previous\s+)?instructions?",
    r"act\s+as\s+if\s+you\s+(have\s+no|don'?t\s+have)",
    r"from\s+now\s+on\s+you\s+(must|will|should)",
    r"your\s+new\s+(role|persona|identity|task)\s+is",
    r"<\s*system\s*>",
    r"\[system\]",
    r"###\s*(system|instruction|prompt)",
]

_PATTERNS_FILE = Path(__file__).parent.parent / "security" / "patterns.json"


def _load_injection_re() -> re.Pattern:
    """Load all patterns: base set + any additions from security/patterns.json."""
    patterns = list(_BASE_INJECTION_PATTERNS)
    try:
        if _PATTERNS_FILE.exists():
            data = json.loads(_PATTERNS_FILE.read_text())
            for p in data.get("injection_patterns", []):
                pat = p.get("pattern", "")
                if pat and pat not in patterns:
                    patterns.append(pat)
    except Exception:
        pass  # silently use base set if file malformed
    return re.compile("|".join(patterns), re.IGNORECASE)


_INJECTION_RE: re.Pattern = _load_injection_re()


def reload_patterns() -> int:
    """Hot-reload injection patterns from security/patterns.json. Returns count."""
    global _INJECTION_RE
    _INJECTION_RE = _load_injection_re()
    # Count active patterns by splitting alternatives
    return len(_INJECTION_RE.pattern.split("|"))

# PII patterns for India.
# Order matters: more-specific patterns first to prevent overlap.
# Aadhaar requires a space between each group (xxxx xxxx xxxx) to avoid
# colliding with 12-digit bank account numbers (no spaces).
_PII_PATTERNS = [
    (re.compile(r"\b[6-9]\d{9}\b"),              "[PHONE]"),       # 10-digit mobile (starts 6-9)
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),      "[PAN]"),         # PAN card AAAAA0000A
    (re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b"),     "[AADHAAR]"),     # Aadhaar WITH spaces only
    (re.compile(r"\b\d{9,18}\b"),                 "[ACCOUNT_NO]"),  # bank account 9–18 digits
]

# Expected field types per action (module → action → {field: allowed_types})
# Fields listed here are REQUIRED — None is only valid if type(None) is in allowed_types.
_ACTION_SCHEMA: dict[str, dict[str, dict[str, tuple]]] = {
    "finance": {
        "log":          {"amount": (int, float), "type": (str,),
                         "category": (str,),     "description": (str, type(None))},
        "summary":      {"month": (str, type(None))},
        "set_budget":   {"category": (str,), "monthly_cap": (int, float)},
        "add_goal":     {"name": (str,), "target": (int, float)},
        "budget_status":{"month": (str, type(None))},
        "list_goals":   {},
        "chat":         {"reply": (str,)},
    },
    "farming": {
        "add_plot":       {"name": (str,)},
        "list_plots":     {},
        "plant_crop":     {"plot": (str,), "crop": (str,)},
        "list_crops":     {},
        "harvest_crop":   {"crop_id": (int,)},
        "log_spray":      {"plot": (str,), "chemical": (str,)},
        "spray_history":  {"plot": (str,)},
        "log_observation":{"plot": (str,), "description": (str,)},
        "open_observations": {},
        "disease_info":   {"crop": (str,)},
        "diagnose_photo": {"image_path": (str,)},
        "mandi_price":    {"commodity": (str,)},
        "weather_now":    {},
        "weather_forecast":{},
        "spray_safe_tomorrow": {},
        "rainfall_history": {},
        "crop_history":   {},
        "soil_data":      {},
        "season_summary": {},
        "summary":        {},
        "ndvi_health":    {"location": (str, type(None)), "plot": (str, type(None))},
        "analysis_history": {"limit": (int, float, type(None))},
        "chat":           {"reply": (str,)},
    },
    "health": {
        "log_bp":     {"systolic": (int, float), "diastolic": (int, float),
                       "notes": (str, type(None))},
        "log_steps":  {"count": (int, float)},
        "log_weight": {"kg": (int, float)},
        "log_sleep":  {"hours": (int, float)},
        "log_sugar":  {"mg_dl": (int, float), "meal_state": (str, type(None))},
        "history":    {"type": (str,), "days": (int, float, type(None))},
        "summary":    {},
        "trend":      {"type": (str,), "days": (int, float, type(None))},
        "set_goal":   {"type": (str,), "target": (int, float)},
        "nutrition":  {"topic": (str,)},
        "chat":       {"reply": (str,)},
    },
}

# Fields that are REQUIRED (cannot be None). All fields in the schema above
# are required unless type(None) is explicitly listed in their allowed_types.
def _is_required(allowed_types: tuple) -> bool:
    """Return True if a field is mandatory (None is not an allowed type)."""
    return type(None) not in allowed_types


# ── Input sanitizer ───────────────────────────────────────────────────────────

@dataclass
class SanitizeResult:
    query: str
    original: str
    warnings: list[str] = field(default_factory=list)
    blocked: bool = False
    block_reason: str = ""


def sanitize_input(query: str) -> SanitizeResult:
    """Clean a user query. Returns cleaned query + any warnings."""
    original = query
    warnings: list[str] = []

    # 1. Strip control characters (null bytes, escape sequences, etc.)
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", query)
    if cleaned != query:
        warnings.append("control characters stripped from input")
    query = cleaned

    # 2. Enforce max length
    if len(query) > MAX_QUERY_LEN:
        query = query[:MAX_QUERY_LEN]
        warnings.append(f"query truncated to {MAX_QUERY_LEN} characters")

    # 3. Prompt injection check — warn but don't block (user is trusted locally)
    injections = _INJECTION_RE.findall(query)
    if injections:
        warnings.append(f"possible prompt injection pattern detected in query: '{injections[0][:60]}'")

    # 4. Repeated token flooding (e.g. "DAN DAN DAN DAN DAN")
    tokens = query.split()
    if len(tokens) >= 6:
        most_common = max(set(tokens), key=tokens.count)
        if tokens.count(most_common) > len(tokens) * 0.6:
            warnings.append(f"unusual repetition detected: '{most_common}' repeated {tokens.count(most_common)} times")

    return SanitizeResult(query=query, original=original, warnings=warnings)


def sanitize_external_text(text: str, label: str = "external") -> str:
    """
    Scrub injection patterns from text that arrived from an external source
    (API responses, web search results, geocoding names, etc.).

    Unlike sanitize_input() this does not block or truncate — it silently
    replaces any matching injection with [BLOCKED] and logs a warning so the
    surrounding content still flows through cleanly.
    """
    if not text:
        return text
    cleaned = _INJECTION_RE.sub("[BLOCKED]", text)
    if cleaned != text:
        log.warning("Injection pattern scrubbed from %s content", label)
    return cleaned


# ── PII redactor ──────────────────────────────────────────────────────────────

def redact_pii(text: str) -> str:
    """Replace PII patterns with safe placeholders before storing in logs."""
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ── LLM action validator ──────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    valid: bool
    action: dict
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _coerce_numeric(val: str, allowed_types: tuple) -> float | None:
    """Coerce a numeric-looking string to a float for int/float fields.

    Strips commas, the rupee sign, and surrounding whitespace (so "₹1,200"
    becomes 1200.0). Returns None if the field does not accept numbers or the
    string is not a valid number — the caller then reports a type error.
    """
    if not (int in allowed_types or float in allowed_types):
        return None
    try:
        return float(val.replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None


def _scan_string_field(fname: str, val: str) -> list[str]:
    """Return error messages if a string field contains unsafe content.

    Checks for prompt-injection patterns first, then shell-injection syntax.
    Returns an empty list when the value is clean.
    """
    if _INJECTION_RE.search(val):
        return [f"field '{fname}' contains injection pattern: {repr(val[:80])}"]
    if _SHELL_META_RE.search(val):
        return [f"field '{fname}' contains shell metacharacters: {repr(val[:80])}"]
    return []


def _validate_field(
    fname: str, allowed_types: tuple, action: dict
) -> tuple[list[str], list[str]]:
    """Validate a single action field in place.

    Performs, in order: required/None check, explicit bool rejection for
    numeric fields, type check with numeric-string coercion (writing the
    coerced value back into ``action``), and an injection/shell scan of any
    string value. Returns ``(errors, warnings)`` for this field.
    """
    val = action.get(fname)

    # Required check — None is only allowed if type(None) is in allowed_types.
    if val is None:
        if _is_required(allowed_types):
            return [f"required field '{fname}' is missing or null"], []
        return [], []

    # bool is a subclass of int in Python — reject it for numeric fields.
    if isinstance(val, bool) and (int in allowed_types or float in allowed_types):
        return [f"field '{fname}' must be a number, got bool={repr(val)}"], []

    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(val, allowed_types):
        coerced = _coerce_numeric(val, allowed_types) if isinstance(val, str) else None
        if coerced is not None:
            action[fname] = coerced
            warnings.append(f"field '{fname}' coerced from string '{val}' to number {coerced}")
            return errors, warnings
        errors.append(
            f"field '{fname}' has wrong type: expected {[t.__name__ for t in allowed_types]}, "
            f"got {type(val).__name__} = {repr(val)[:60]}"
        )

    if isinstance(val, str):
        errors.extend(_scan_string_field(fname, val))

    return errors, warnings


def validate_action(module: str, action: dict) -> ValidationResult:
    """Validate an LLM-generated action dict before it is executed.

    Blocks the action outright if it names an action not in the module's
    schema. Otherwise validates each schema field via :func:`_validate_field`,
    accumulating errors and warnings. Returns a :class:`ValidationResult` whose
    ``action`` carries any coerced field values.
    """
    action = dict(action)
    a = action.get("action", "")
    module_schema = _ACTION_SCHEMA.get(module, {})

    if a not in module_schema:
        return ValidationResult(
            valid=False, action=action,
            errors=[f"action '{a}' is not an allowed action for module '{module}' — blocked"],
        )

    errors: list[str] = []
    warnings: list[str] = []
    for fname, allowed_types in module_schema[a].items():
        f_errors, f_warnings = _validate_field(fname, allowed_types, action)
        errors.extend(f_errors)
        warnings.extend(f_warnings)

    return ValidationResult(valid=not errors, action=action, warnings=warnings, errors=errors)
