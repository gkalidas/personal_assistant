"""
Crop disease knowledge base — loaded from crops/*.json.
Provides disease lookup, spray recommendations, and KB context for the LLM.
"""

import json
from pathlib import Path
from typing import Any

_KB_DIR = Path(__file__).parent.parent.parent / "crops"
_kb_cache: dict[str, dict] = {}


def _load(crop: str) -> dict:
    """Load and cache a crop\'s disease KB from crops/<crop>.json ({} if missing)."""
    key = crop.lower()
    if key in _kb_cache:
        return _kb_cache[key]
    path = _KB_DIR / f"{key}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        _kb_cache[key] = data
        return data
    except Exception:
        return {}


def list_crops_with_kb() -> list[str]:
    """Return the crop names that have a disease KB file."""
    return [p.stem for p in _KB_DIR.glob("*.json")]


def get_diseases(crop: str) -> dict[str, Any]:
    """Return all disease entries for a crop."""
    kb = _load(crop)
    return kb.get("diseases", {})


def get_disease(crop: str, condition: str) -> dict[str, Any] | None:
    """Lookup a specific condition.
    Order: exact name match → name contains query → symptom keyword overlap."""
    if not condition:
        return None
    diseases = get_diseases(crop)
    cond_lower = condition.lower()

    # 1. Exact name match
    for name, info in diseases.items():
        if name.lower() == cond_lower:
            return info

    # 2. Name contains query (e.g. "blight" → "Bacterial Blight")
    for name, info in diseases.items():
        if cond_lower in name.lower() or name.lower() in cond_lower:
            return info

    # 3. Symptom keyword overlap — score by how many query words appear in symptoms
    stop = {"the", "a", "an", "is", "my", "on", "in", "of", "and", "or", "with",
            "have", "are", "its", "it", "s", "leaves", "plants", "fruit"}
    words = {w for w in cond_lower.replace("-", " ").split() if len(w) > 2 and w not in stop}
    if not words:
        return None

    best_name, best_score = None, 0
    for name, info in diseases.items():
        if name.lower() == "healthy":
            continue
        text = (
            (info.get("visual_symptoms") or "") + " " +
            (info.get("causal_agent") or "") + " " +
            name
        ).lower()
        score = sum(1 for w in words if w in text)
        if score > best_score:
            best_score, best_name = score, name

    if best_score > 0 and best_name:
        return diseases[best_name]
    return None


def kb_context_for_llm(crop: str, condition: str = "") -> str:
    """
    Format KB as a text block for injection into an LLM prompt.
    If condition is known, narrows to that entry only (prevents mixing spray recipes).
    """
    kb = _load(crop)
    if not kb:
        return ""

    diseases = kb.get("diseases", {})
    lines = [
        f"KNOWLEDGE BASE: {kb.get('crop', crop).upper()}",
        f"Region: {kb.get('primary_region', '')}",
        f"Dominant variety: {kb.get('dominant_variety', 'unknown')}",
    ]

    entries = {condition: diseases[condition]} if (condition and condition in diseases) else diseases

    for disease, info in entries.items():
        lines.append(f"\n--- {disease} ---")
        if info.get("causal_agent"):
            lines.append(f"  Cause: {info['causal_agent']}")
        if info.get("visual_symptoms"):
            lines.append(f"  Symptoms: {info['visual_symptoms']}")
        if info.get("favourable_conditions"):
            lines.append(f"  Triggered by: {info['favourable_conditions']}")
        if info.get("severity_note"):
            lines.append(f"  Severity: {info['severity_note']}")
        if info.get("immediate_actions"):
            lines.append(f"  Immediate actions: {'; '.join(info['immediate_actions'])}")
        if info.get("preventive_measures"):
            lines.append(f"  Prevention: {'; '.join(info['preventive_measures'])}")
        if info.get("do_not"):
            lines.append(f"  Do NOT: {'; '.join(info['do_not'])}")
        if info.get("watch_for"):
            lines.append(f"  Watch for: {'; '.join(info['watch_for'])}")
        if info.get("timeline"):
            lines.append(f"  Timeline: {info['timeline']}")

    return "\n".join(lines)


def format_disease_summary(crop: str, condition: str) -> str:
    """Human-readable one-paragraph summary of a disease."""
    diseases = get_diseases(crop)
    info = get_disease(crop, condition)
    if not info:
        return f"No knowledge base entry found for '{condition}' in {crop}."

    # Find the matched disease name so we can label it
    matched_name = condition
    for name, d in diseases.items():
        if d is info:
            matched_name = name
            break

    header = f"**{matched_name}**" if matched_name.lower() != condition.lower() else f"**{matched_name}**"
    parts = [header]
    if info.get("causal_agent"):
        parts.append(f"Caused by {info['causal_agent']}.")
    if info.get("visual_symptoms"):
        parts.append(f"Symptoms: {info['visual_symptoms']}")
    if info.get("immediate_actions"):
        parts.append(f"Recommended actions: {'; '.join(info['immediate_actions'][:2])}.")
    if info.get("timeline"):
        parts.append(f"Timeline: {info['timeline']}")
    return " ".join(parts)
