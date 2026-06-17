"""Диспетчер задачі — єдина точка входу для бекенду.

Класифікує намір і маршрутизує:
  answer -> читає контекст і відповідає (read-only)
  shell  -> витягує команду й виконує через allow-list
  edit   -> планувальник + оркестратор (правки файлів з підтвердженням)

GUI може використовувати це для answer/shell, а для edit — свій інтерактивний
цикл підтвердження (передавши confirm).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .answerer import answer, build_context
from .intent import classify_intent, extract_command
from .llm import OllamaClient
from .memory import TaskState, render_for_planner
from .orchestrator import run_plan
from .planner import make_plan
from .project import load_project_doc
from .tools import run_shell


@dataclass
class TaskResult:
    mode: str                       # answer | shell | plan | edit
    text: str                       # відповідь / вивід / підсумок плану
    state: TaskState | None = None  # план (для plan/edit)
    thinking: str = ""              # роздуми моделі (think on)


def handle(task: str, root: str | Path = ".", client: OllamaClient | None = None,
           reference_files=(), confirm=None, det_handler=None,
           plan_first: bool = False) -> TaskResult:
    client = client or OllamaClient()
    mode = classify_intent(task, client)

    if mode == "answer":
        ctx = build_context(root, reference_files, task)
        content, thinking = answer(task, ctx, client)
        return TaskResult("answer", content, thinking=thinking)

    if mode == "shell":
        cmd = extract_command(task, client)
        r = run_shell(cmd, cwd=str(root))
        if not r.allowed:
            return TaskResult("shell", f"Команду заблоковано allow-list: {cmd}")
        body = (r.stdout or r.stderr or "").strip()
        return TaskResult("shell", f"$ {cmd}\n{body}\n(rc={r.returncode})")

    # plan або edit — спершу будуємо план
    doc = load_project_doc(root)
    state = make_plan(task, client=client, project_doc=doc)

    # plan-режим (явний намір або plan_first сесії): показати план, НЕ виконувати
    if mode == "plan" or plan_first:
        return TaskResult("plan", render_for_planner(state), state)

    # edit: виконати одразу
    run_plan(state, client,
             confirm=confirm or (lambda step, diff: True),
             root=str(root), det_handler=det_handler)
    return TaskResult("edit", render_for_planner(state), state)


def execute_plan(state: TaskState, client: OllamaClient | None = None,
                 confirm=None, root: str | Path = ".", det_handler=None) -> TaskResult:
    """Виконати раніше схвалений (pending) план."""
    client = client or OllamaClient()
    run_plan(state, client,
             confirm=confirm or (lambda step, diff: True),
             root=str(root), det_handler=det_handler)
    return TaskResult("edit", render_for_planner(state), state)
