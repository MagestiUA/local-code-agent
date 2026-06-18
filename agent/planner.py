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
    "the file path the step touches, or null. Write each 'description' in the SAME "
    "language as the user's task. Reply ONLY as JSON: {\"steps\":[...]}."
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


# ── Планувальник-діалог (#2 + #5): уточнення й вибір рішень НА РІВНІ планувальника ──
DELIB_SYSTEM = (
    "You are the planning module of a coding agent on a small local model. Decide how "
    "to proceed with the user's task and reply ONLY as JSON.\n"
    "- If the task is AMBIGUOUS (e.g. it does not clearly say WHICH file to change, or "
    "the target is unclear) -> action=\"clarify\": ask ONE short question and give "
    "concrete clickable options (candidate file names from the project structure).\n"
    "- If there are 2-3 genuinely different sensible APPROACHES and the choice matters "
    "-> action=\"choose\": ask which approach; options = the approaches (label + short "
    "detail).\n"
    "- Otherwise -> action=\"plan\": output the final steps. Each step is "
    "{\"kind\":..,\"description\":..,\"target\":..} (kind: llm|deterministic|inline). "
    "Resolve ALL ambiguity HERE — the executor will NOT ask anything.\n"
    "Only ask when it genuinely helps; do NOT invent questions for clear tasks. Include "
    "a short \"reasoning\" (one sentence). Write reasoning/question/options/descriptions "
    "in the SAME language as the user's task."
)

DELIB_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["clarify", "choose", "plan"]},
        "reasoning": {"type": "string"},
        "question": {"type": ["string", "null"]},
        "options": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "detail": {"type": "string"}},
                "required": ["label"],
            },
        },
        "steps": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["llm", "deterministic", "inline"]},
                    "description": {"type": "string"},
                    "target": {"type": ["string", "null"]},
                },
                "required": ["kind", "description"],
            },
        },
    },
    "required": ["action"],
}


def _parse_decision(content: str) -> dict:
    """Розпарсити рішення планувальника. Стійко до обриву: якщо обʼєкт не парситься —
    вертаємо план із врятованих кроків."""
    try:
        d = json.loads(content)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {"action": "plan", "steps": _parse_steps(content)}


def _steps_to_state(task: str, steps: list[dict]) -> TaskState:
    state = TaskState(task=task)
    for st in steps or []:
        kind = st.get("kind", "llm")
        if kind not in VALID_KINDS:
            kind = "llm"
        desc = str(st.get("description", "")).strip()
        if desc:
            state.add_step(kind, desc, st.get("target"))
    return state


def deliberate(task: str, context: str = "", client: OllamaClient | None = None,
               project_doc: str | None = None, history: list | None = None) -> dict:
    """Один хід планувальника-діалогу. Повертає dict:
      action: 'clarify' | 'choose' | 'plan'
      reasoning: коротке пояснення (мова користувача)
      question: текст питання (для clarify/choose)
      options:  [{label, detail}] — клікабельні кандидати/підходи
      state:    TaskState (лише для action='plan'), інакше None
    history — попередні уточнення [{question, answer}], щоб наступний хід їх врахував."""
    client = client or OllamaClient()
    history = history or []
    user = ""
    if project_doc:
        user += f"Контекст проекту (AGENT.md):\n{project_doc}\n\n"
    user += f"Задача:\n{task}"
    if context:
        user += f"\n\nСтруктура/контекст:\n{context}"
    if history:
        qa = "\n".join(f"- Запитав: {h.get('question', '')}\n  Відповідь: {h.get('answer', '')}"
                       for h in history)
        user += f"\n\nВже уточнено з користувачем:\n{qa}"

    msg = client.chat(
        [{"role": "system", "content": DELIB_SYSTEM}, {"role": "user", "content": user}],
        profile=config.EXECUTOR, fmt=DELIB_SCHEMA,
    )
    d = _parse_decision(msg.get("content") or "")
    action = d.get("action") if d.get("action") in {"clarify", "choose", "plan"} else "plan"

    options = []
    for o in (d.get("options") or []):
        if isinstance(o, dict) and o.get("label"):
            options.append({"label": str(o["label"]).strip(), "detail": str(o.get("detail", "")).strip()})

    state = None
    if action == "plan":
        state = _steps_to_state(task, d.get("steps") or [])
        if not state.steps:                              # порожній план -> звичайний make_plan
            state = make_plan(task, context, client, project_doc)

    return {"action": action, "reasoning": str(d.get("reasoning", "")).strip(),
            "question": str(d.get("question", "")).strip(), "options": options, "state": state}


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
