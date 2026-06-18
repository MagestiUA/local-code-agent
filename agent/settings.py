"""Глобальні налаштування застосунку (тема, шрифти).
Зберігаються в ~/.local-code-agent/settings.json.
"""
from __future__ import annotations

import json
from pathlib import Path

SETTINGS_FILE = Path.home() / ".local-code-agent" / "settings.json"

DEFAULTS: dict = {
    "theme": "dark",       # "light" | "dark" | "system"
    "font_chat": 14,       # розмір шрифту повідомлень чату (px)
    "font_ui": 13,         # розмір шрифту інтерфейсу (px)
}

VALID_THEMES = {"light", "dark", "system"}


def load() -> dict:
    """Завантажити налаштування; відсутній файл або невалідні поля → defaults."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return dict(DEFAULTS)
    result = dict(DEFAULTS)
    if data.get("theme") in VALID_THEMES:
        result["theme"] = data["theme"]
    try:
        result["font_chat"] = max(12, min(20, int(data["font_chat"])))
    except Exception:
        pass
    try:
        result["font_ui"] = max(11, min(16, int(data["font_ui"])))
    except Exception:
        pass
    return result


def save(d: dict) -> None:
    """Зберегти налаштування на диск."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = dict(DEFAULTS)
    if d.get("theme") in VALID_THEMES:
        out["theme"] = d["theme"]
    try:
        out["font_chat"] = max(12, min(20, int(d["font_chat"])))
    except Exception:
        pass
    try:
        out["font_ui"] = max(11, min(16, int(d["font_ui"])))
    except Exception:
        pass
    SETTINGS_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
