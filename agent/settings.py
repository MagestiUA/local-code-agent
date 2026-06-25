from pathlib import Path
import json

SETTINGS_FILE = Path.home() / ".local-code-agent" / "settings.json"
DEFAULT = {
    "theme": "auto",
    "font_chat": 14,
    "font_ui": 13,
    "last_session_id": "",     # відновлюємо останній відкритий чат після перезапуску
}

def load() -> dict:
    """Завантажити налаштування; відсутній файл або невалідний → defaults."""
    if not SETTINGS_FILE.exists():
        return DEFAULT.copy()
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        # Об'єднуємо дефолтні значення з завантаженими, зберігаючи всі ключі з файлу
        result = DEFAULT.copy()
        result.update(data)
        return result
    except Exception:
        return DEFAULT.copy()

def save(d: dict) -> None:
    """Зберегти словник налаштувань у файл."""
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Зберігаємо весь словник, щоб не втрачати додаткові (екстеншн) ключі
        SETTINGS_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
