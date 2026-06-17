"""Planner — ріже задачу на кроки й класифікує їх.

Структурований вивід генеруємо з think=OFF: у gemma думання плутає й обриває
JSON. Парсинг стійкий до обриву (_parse_steps вихоплює окремі кроки).

Видає СТРУКТУРОВАНИЙ план у TaskState. Класифікація кроків — ядро ідеї:
  llm           — модель пише/рефакторить код (тіло функції тощо)
  deterministic — механічна операція БЕЗ моделі (файл, переміщення літерала,
                  запуск тестів). Великі опакові дані — завжди сюди.
  inline        — тривіальне, окремий executor не потрібен.

Надійність структури — через format=json (Ollama гарантує валідний JSON).
"""
from __future__ import annotations

import json
import re

from . import config
from .llm import OllamaClient
from .memory import TaskState

VALID_KINDS = {"llm", "deterministic", "inline"}


def _parse_steps(content: str) -> list[dict]:
    """Дістати кроки з відповіді. Стійко до обірваного JSON: якщо весь обʼєкт не
    парситься — вихоплюємо окремі step-обʼєкти (вони пласкі)."""
    try:
        data = json.loads(content)
        if isinstance(data, dict) and isinstance(data.get("steps"), list):
            return data["steps"]
    except Exception:
        pass
    steps = []
    for m in re.findall(r"\{[^{}]*\}", content):
        try:
            obj = json.loads(m)
            if isinstance(obj, dict) and obj.get("description"):
                steps.append(obj)
        except Exception:
            pass
    return steps

SYSTEM = (
    "You are the planning module of a coding agent that runs on a small local model. "
    "Decompose the user's task into a SHORT list of coarse steps (aim 2-6; do NOT "
    "over-split into micro-steps). Classify each step's kind:\n"
    "- 'llm': needs the model to write or refactor code (e.g. rewrite a function body).\n"
    "- 'deterministic': a mechanical op done WITHOUT the model — create/rename/delete a "
    "file, move/copy a large data literal, run tests or a linter. Large opaque literals "
    "(protobufs, payloads) are ALWAYS deterministic; never make the model retype them.\n"
    "- 'inline': trivial; no separate executor step needed.\n"
    "Each step is an object {\"kind\":..,\"description\":..,\"target\":..} where target is "
    "the file path the step touches, or null. Reply ONLY as JSON: {\"steps\":[...]}."
)

# JSON-schema для format -> жорстка структура відповіді.
SCHEMA = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["llm", "deterministic", "inline"]},
                    "description": {"type": "string"},
                    "target": {"type": ["string", "null"]},
                },
                "required": ["kind", "description"],
            },
        }
    },
    "required": ["steps"],
}


def make_plan(task: str, context: str = "", client: OllamaClient | None = None,
              project_doc: str | None = None) -> TaskState:
    client = client or OllamaClient()
    user = ""
    if project_doc:
        user += f"Контекст проекту (AGENT.md):\n{project_doc}\n\n"
    user += f"Задача:\n{task}"
    if context:
        user += f"\n\nКонтекст:\n{context}"
    # think=OFF для структурованого виводу: думання плутає/обриває JSON у gemma.
    msg = client.chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        profile=config.EXECUTOR, fmt=SCHEMA,
    )

    state = TaskState(task=task)
    for st in _parse_steps(msg.get("content") or ""):
        kind = st.get("kind", "llm")
        if kind not in VALID_KINDS:
            kind = "llm"
        state.add_step(kind, str(st.get("description", "")).strip(), st.get("target"))
    return state
