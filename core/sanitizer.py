"""
Input sanitizer and PII redactor.

sanitize_input()  — cleans user query before it reaches the LLM.
redact_pii()      — strips personal identifiers before writing to logs/DB.
validate_action() — checks LLM JSON output has safe, expected field types.
"""

import re
from dataclasses import dataclass, field

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_QUERY_LEN = 1000

# Same injection patterns as guardrails.py (applied to user input too)
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions?",
    r"forget\s+(everything|all)\s+(you|i)\s+(know|said|told)",
    r"you\s+are\s+now\s+a?\s*(different|new|another)?\s*(assistant|ai|model|bot|system)",
    r"new\s+(system\s+)?instructions?\s*:",
    r"disregard\s+(all\s+)?(prior|previous|above)",
    r"override\s+(previous\s+)?instructions?",
    r"act\s+as\s+if\s+you\s+(have\s+no|don'?t\s+have)",
    r"from\s+now\s+on\s+you\s+(must|will|should)",
    r"your\s+new\s+(role|persona|identity|task)\s+is",
    r"<\s*system\s*>",
    r"\[system\]",
    r"###\s*(system|instruction|prompt)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

# PII patterns for India
_PII_PATTERNS = [
    (re.compile(r"\b[6-9]\d{9}\b"),          "[PHONE]"),        # mobile
    (re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\b"), "[AADHAAR]"),    # Aadhaar
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),   "[PAN]"),         # PAN card
    (re.compile(r"\b\d{9,18}\b"),              "[ACCOUNT_NO]"),  # bank account
]

# Expected field types per action (module → action → {field: allowed_types})
_ACTION_SCHEMA: dict[str, dict[str, dict[str, tuple]]] = {
    "finance": {
        "log":          {"amount": (int, float), "type": (str,),
                         "category": (str,),     "description": (str,)},
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
        "weather_now":    {},
        "weather_forecast":{},
        "spray_safe_tomorrow": {},
        "rainfall_history": {},
        "crop_history":   {},
        "soil_data":      {},
        "season_summary": {},
        "chat":           {"reply": (str,)},
    },
}


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


def validate_action(module: str, action: dict) -> ValidationResult:
    """
    Validate that an LLM-generated action dict has safe field types
    before it is executed. Returns corrected action if fixable,
    or marks invalid if not.
    """
    warnings: list[str] = []
    errors: list[str] = []
    action = dict(action)

    a = action.get("action", "")
    module_schema = _ACTION_SCHEMA.get(module, {})

    if a not in module_schema:
        # Unknown action — pass through with a warning (new actions may not be in schema yet)
        warnings.append(f"action '{a}' not in validation schema for module '{module}'")
        return ValidationResult(valid=True, action=action, warnings=warnings)

    field_schema = module_schema[a]
    for fname, allowed_types in field_schema.items():
        val = action.get(fname)
        if val is None:
            continue  # optional fields are fine

        if not isinstance(val, allowed_types):
            # Try to coerce numbers
            if (int in allowed_types or float in allowed_types) and isinstance(val, str):
                try:
                    coerced = float(val.replace(",", "").replace("₹", "").strip())
                    action[fname] = coerced
                    warnings.append(f"field '{fname}' coerced from string '{val}' to number {coerced}")
                    continue
                except ValueError:
                    pass
            errors.append(
                f"field '{fname}' has wrong type: expected {[t.__name__ for t in allowed_types]}, "
                f"got {type(val).__name__} = {repr(val)[:60]}"
            )

        # Check for injection in string fields
        if isinstance(val, str) and _INJECTION_RE.search(val):
            errors.append(f"field '{fname}' contains injection pattern: {repr(val[:80])}")

    valid = len(errors) == 0
    return ValidationResult(valid=valid, action=action, warnings=warnings, errors=errors)
