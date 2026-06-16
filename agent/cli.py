"""Мінімальний CLI: задача -> план -> виконання з підтвердженням у консолі.

Запуск:  .venv\\Scripts\\python.exe -m agent.cli "твоя задача"
(Потребує Ollama — запускає локальну модель.)
"""
from __future__ import annotations

import sys

from .llm import OllamaClient
from .memory import render_for_planner
from .orchestrator import run_plan
from .planner import make_plan
from .tools import run_shell


def console_confirm(step, diff: str) -> bool:
    print(f"\n--- Крок #{step.id}: {step.description} ---")
    print(diff or "(порожній diff)")
    return input("Застосувати? [y/N] ").strip().lower() == "y"


def det_handler(step) -> str:
    """Найпростіший обробник deterministic-кроків: якщо схоже на запуск тестів —
    проганяємо pytest. Решта — поки що для ручного виконання."""
    desc = step.description.lower()
    if "тест" in desc or "test" in desc or "pytest" in desc:
        r = run_shell("pytest -q")
        return f"pytest rc={r.returncode}" if r.allowed else r.stderr
    return "deterministic-крок: виконати вручну (поки не автоматизовано)"


def main() -> None:
    task = " ".join(sys.argv[1:]).strip() or input("Задача: ")
    client = OllamaClient()
    state = make_plan(task, client=client)
    print("\n" + render_for_planner(state) + "\n")
    run_plan(state, client, confirm=console_confirm, det_handler=det_handler)
    print("\n=== Підсумок ===\n" + render_for_planner(state))


if __name__ == "__main__":
    main()
