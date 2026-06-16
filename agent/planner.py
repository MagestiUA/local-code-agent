"""Planner — ріже задачу на кроки й класифікує їх (think=on).

Видає СТРУКТУРОВАНИЙ план у TaskState. Класифікація кроків — ядро ідеї:
  llm           — модель пише/рефакторить код (тіло функції тощо)
  deterministic — механічна операція БЕЗ моделі (файл, переміщення літерала,
                  запуск тестів). Великі опакові дані — завжди сюди.
  inline        — тривіальне, окремий executor не потрібен.

Надійність структури — через format=json (Ollama гарантує валідний JSON).
"""
from __future__ import annotations

import json

from . import config
from .llm import OllamaClient
from .memory import TaskState

VALID_KINDS = {"llm", "deterministic", "inline"}

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
    msg = client.chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        profile=config.PLANNER, fmt=SCHEMA,
    )
    data = json.loads(msg.get("content") or "{}")

    state = TaskState(task=task)
    for st in data.get("steps", []):
        kind = st.get("kind", "llm")
        if kind not in VALID_KINDS:
            kind = "llm"
        state.add_step(kind, st.get("description", "").strip(), st.get("target"))
    return state
