"""Оркестратор — крутить план: маршрутизує кроки за типом, оновлює пам'ять,
питає підтвердження перед записом.

  llm           -> executor.run_edit (strip/LLM/restore) + confirm + apply
  deterministic -> зовнішній обробник (run_shell, переміщення літерала тощо)
  inline        -> пропуск (зміна тривіальна)

LLM-клієнт інжектиться (DI), тож оркестрацію можна тестувати стабом без GPU.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .executor import apply_edit, run_edit
from .memory import TaskState, save


def default_confirm(step, diff: str) -> bool:
    """Безпечний дефолт: нічого не писати, поки виклик не передасть свій confirm."""
    return False


def run_plan(state: TaskState, client, confirm: Callable = default_confirm,
             root: str | Path = ".", det_handler: Callable | None = None) -> TaskState:
    root = Path(root)
    for step in state.steps:
        if step.status != "pending":
            continue

        if step.kind == "inline":
            state.set_result(step.id, "done", "inline (без окремого виконання)")

        elif step.kind == "deterministic":
            if det_handler:
                state.set_result(step.id, "done", det_handler(step))
            else:
                state.set_result(step.id, "failed", "deterministic: немає обробника")

        elif step.kind == "llm":
            if not step.target:
                state.set_result(step.id, "failed", "llm-крок без target-файлу")
            else:
                res = run_edit(root / step.target, step.description, client=client)
                if not res.ok:
                    state.set_result(step.id, "failed", res.error)
                elif confirm(step, res.diff):
                    apply_edit(root / step.target, res)
                    state.set_result(step.id, "done",
                                     f"застосовано, ~{res.diff.count(chr(10))} рядків diff")
                else:
                    state.set_result(step.id, "failed", "відхилено користувачем")

        try:
            save(state, root)
        except Exception:
            pass
    return state
