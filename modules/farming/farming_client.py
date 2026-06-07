"""
Bridge to the standalone farming FastAPI server (/home/ganesh/projects/farming).
Used for photo-based disease diagnosis — the farming server handles vision model + ONNX.
GK calls it when the user provides an image for crop analysis.

If the farming server is not running, GK falls back to text-based KB advice.
"""

import os
from pathlib import Path
from typing import Any

import httpx

FARMING_SERVER = os.getenv("FARMING_SERVER_URL", "http://localhost:5002")
_TIMEOUT = 120.0  # vision model inference is slow


def is_running() -> bool:
    """Check if the farming server is up."""
    try:
        r = httpx.get(f"{FARMING_SERVER}/api/status", timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False


def analyse_photo(
    image_path: str,
    crop: str,
    location: str = "",
    plot_id: int | None = None,
) -> dict[str, Any]:
    """
    Send a photo to the farming server for disease diagnosis.
    Returns structured result: condition, severity, immediate_actions, spray_timing, etc.
    """
    path = Path(image_path)
    if not path.exists():
        return {"error": f"Image not found: {image_path}"}

    if not is_running():
        return {
            "error": "Farming server not running",
            "hint": f"Start it with: cd /home/ganesh/projects/farming && python main.py",
        }

    try:
        with open(path, "rb") as f:
            files = {"image": (path.name, f, "image/jpeg")}
            data  = {"crop": crop, "location": location}
            if plot_id:
                data["plot_id"] = str(plot_id)

            r = httpx.post(
                f"{FARMING_SERVER}/api/analyse",
                files=files,
                data=data,
                timeout=_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as e:
        return {"error": f"Farming server error: {e.response.status_code}"}
    except Exception as e:
        return {"error": f"Could not reach farming server: {e}"}


def format_diagnosis(result: dict) -> str:
    """Format a farming server diagnosis result into a readable response."""
    if "error" in result:
        return f"Disease analysis unavailable: {result['error']}"
        if result.get("hint"):
            return f"Disease analysis unavailable: {result['error']}\nHint: {result['hint']}"

    lines = []
    condition = result.get("condition", "Unknown")
    severity  = result.get("severity", "unknown")
    conf      = result.get("confidence", "low")
    lines.append(f"Diagnosis: {condition} [{severity} severity, {conf} confidence]")

    if result.get("immediate_actions"):
        lines.append("\nImmediate actions:")
        for a in result["immediate_actions"]:
            lines.append(f"  • {a}")

    if result.get("spray_timing"):
        lines.append(f"\nSpray timing: {result['spray_timing']}")

    if result.get("weather_note"):
        lines.append(f"\nWeather note: {result['weather_note']}")

    if result.get("soil_note"):
        lines.append(f"Soil note: {result['soil_note']}")

    if result.get("do_not"):
        lines.append("\nDo NOT:")
        for d in result["do_not"]:
            lines.append(f"  ✗ {d}")

    if result.get("watch_for"):
        lines.append("\nWatch for:")
        for w in result["watch_for"]:
            lines.append(f"  → {w}")

    if result.get("timeline"):
        lines.append(f"\nTimeline: {result['timeline']}")

    return "\n".join(lines)
