"""Константи й профілі агента.

Два профілі ролей (вивід замірів на gemma4:26b-a4b-it-qat):
  EXECUTOR — механічне виконання: думання ВИМКНЕНО, увесь бюджет на код, швидко.
  PLANNER  — декомпозиція й аналіз: думання УВІМКНЕНО.

num_ctx=65536 — заміряна солодка точка (≈107 t/s, майже все на GPU).
Не варіюємо num_ctx між профілями — інакше Ollama перевантажує модель.
"""
from __future__ import annotations

HOST  = "http://127.0.0.1:11434"
MODEL = "gemma4:26b-a4b-it-qat"

REQUEST_TIMEOUT = 600

# run_shell allow-list: дозволені префікси команд (точний токен або "<prefix> ...").
# Усе інше блокується БЕЗ виконання. Виконуємо через shell=False (без ланцюжків).
ALLOWED_SHELL = (
    "python", "py", "pip list", "pip show",
    "pytest", "ruff", "flake8", "mypy",
    "git status", "git diff", "git log",
)
SHELL_TIMEOUT = 120

# Профілі ролей: (think, num_ctx, temperature)
EXECUTOR = {"think": False, "num_ctx": 65536, "temperature": 0.2}
PLANNER  = {"think": True,  "num_ctx": 65536, "temperature": 0.3}
