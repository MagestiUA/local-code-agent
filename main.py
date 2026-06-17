"""Точка входу для запуску веб-інтерфейсу local-code-agent.

Зручно з PyCharm: відкрий цей файл і натисни ▶ Run (інтерпретатор — .venv проєкту).
Робить те саме, що `reflex run`: підіймає фронтенд на http://localhost:3000
і бекенд на :8000. Зупинка — Stop у PyCharm або Ctrl+C.

Аргументи передаються далі в reflex, напр.:  Run config → Parameters: `--env prod`
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    # reflex має запускатись із кореня проєкту (поряд з rxconfig.py)
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    # тиша телеметрії + коректний UTF-8 у консолі Windows/PyCharm
    os.environ.setdefault("REFLEX_TELEMETRY_ENABLED", "false")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # Викликаємо CLI reflex у цьому ж процесі — логи й Stop працюють напряму.
    from reflex.reflex import cli

    extra = sys.argv[1:]
    sys.argv = ["reflex", "run", *extra]
    cli()


if __name__ == "__main__":
    main()
