"""Офлайн-тест agent/settings.py.
Запуск: .venv\\Scripts\\python.exe -m tests.test_settings
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import agent.settings as S


def main():
    with tempfile.TemporaryDirectory() as td:
        fake_file = Path(td) / "settings.json"

        with patch.object(S, "SETTINGS_FILE", fake_file):
            # 1. Відсутній файл → defaults
            d = S.load()
            assert d["theme"] == S.DEFAULTS["theme"]
            assert d["font_chat"] == S.DEFAULTS["font_chat"]

            # 2. save → load round-trip
            S.save({"theme": "light", "font_chat": 16, "font_ui": 12})
            d2 = S.load()
            assert d2["theme"] == "light"
            assert d2["font_chat"] == 16
            assert d2["font_ui"] == 12

            # 3. Невалідна тема → fallback до default
            fake_file.write_text(json.dumps({"theme": "INVALID", "font_chat": 14, "font_ui": 13}))
            d3 = S.load()
            assert d3["theme"] == S.DEFAULTS["theme"], f"очікували default тему, отримали {d3['theme']}"

            # 4. Шрифт за межами → clamp
            S.save({"theme": "dark", "font_chat": 99, "font_ui": 0})
            d4 = S.load()
            assert d4["font_chat"] == 20, f"очікували max 20, отримали {d4['font_chat']}"
            assert d4["font_ui"] == 11, f"очікували min 11, отримали {d4['font_ui']}"

    print("OK: settings — defaults, round-trip, невалідна тема, clamp шрифтів")


if __name__ == "__main__":
    main()
