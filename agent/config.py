"""Константи й профілі агента.

Два профілі ролей (вивід замірів на gemma4:26b-a4b-it-qat):
  EXECUTOR — механічне виконання: думання ВИМКНЕНО, увесь бюджет на код, швидко.
  PLANNER  — декомпозиція й аналіз: думання УВІМКНЕНО.

num_ctx=131072 — sweet spot за бенчмарком q4_0+flash attention (gemma4 113.5→108.5
t/s, лише -4% проти 65536; за 262144 падіння вже різке — лишаємо як опцію не дефолт).
Не варіюємо num_ctx між профілями — інакше Ollama перевантажує модель.
"""
from __future__ import annotations

HOST  = "http://127.0.0.1:11434"
MODEL = "gemma4:26b-a4b-it-qat"

REQUEST_TIMEOUT = 600

# run_shell allow-list: дозволені префікси команд (точний токен або "<prefix> ...").
# Усе інше блокується БЕЗ виконання. Виконуємо через shell=False (без ланцюжків).
ALLOWED_SHELL = (
    # Python / pip
    "python", "py",
    "pip install", "pip uninstall", "pip list", "pip show", "pip freeze",
    # linters / tests / formatters
    "pytest", "ruff", "flake8", "mypy", "black", "isort",
    # git (безпечні операції)
    "git status", "git diff", "git log", "git show",
    "git add", "git commit", "git pull", "git push",
    "git checkout", "git branch", "git merge", "git stash",
    "git init", "git clone",
    # інші мови / пакетні менеджери
    "npm", "node", "npx",
    "cargo", "rustc",
    "go",
)
SHELL_TIMEOUT = 120

# DRY-семплінг (Don't Repeat Yourself) — лише для EXECUTOR (тул-цикл): карає повтор
# цілих ФРАЗ/послідовностей (напр. та сама невдала git-команда раз за разом), не
# окремих токенів — тож не псує код, як грубий repeat_penalty. М'який multiplier
# (емпірично 1.8 repeat_penalty ламав слова; DRY 0.3 зберігає зв'язність). Виклики з
# format=schema захищені граматикою — DRY не зробить JSON невалідним. PLANNER (проза,
# think=on) лишаємо без DRY. options мерджаться в payload у llm.chat/chat_stream.
_DRY = {"dry_multiplier": 0.3, "dry_base": 1.75, "dry_allowed_length": 2}

# Профілі ролей: (think, num_ctx, temperature, [options])
EXECUTOR = {"think": False, "num_ctx": 131072, "temperature": 0.2, "options": _DRY}
PLANNER  = {"think": True,  "num_ctx": 131072, "temperature": 0.3}