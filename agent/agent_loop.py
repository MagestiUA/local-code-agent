"""Per-step tool-loop: дає моделі інструменти й крутить цикл на ОДИН крок плану.

Модель сама вирішує, які тули викликати; ми виконуємо й повертаємо результат, поки
вона не завершить крок (без виклику тула) або не вичерпає ліміт ітерацій. Контекст
вузький (один крок), цикл короткий — безпечно для слабкої локальної моделі.
"""
from __future__ import annotations

import json

from . import config
from .llm import OllamaClient
from .toolkit import ToolContext, ToolRegistry, default_registry

SYSTEM = (
    "You are an execution agent for ONE task step on a Windows machine (PowerShell). "
    "Working directory (project root) is provided in context — all relative paths are "
    "relative to it; run_shell cwd is already set to it.\n\n"
    "You have tools — pick the RIGHT one:\n"
    "- write_file(path, content): create a brand-new file. PARENT DIRECTORIES ARE CREATED "
    "AUTOMATICALLY — never call mkdir or run_shell just to create a folder. Pass a file "
    "path (not a folder). Example: write_file('src/utils/helper.py', '...').\n"
    "- edit_file(path, instruction): refactor/change an existing file in place.\n"
    "- create_from_source(target, source, instruction): duplicate an existing file into a "
    "new file (copy + optional refactor). Copies large data literals byte-for-byte. NEVER "
    "use shell copy or read+write manually for this.\n"
    "- read_file(path), list_dir(path): inspect files. list_dir with no path shows project root.\n"
    "- run_shell(command): run programs — python, pip, pytest, git, etc. cwd=project root. "
    "Use for: 'python -m venv .venv', 'pip install -r requirements.txt', 'pytest', etc. "
    "For venv pip use: '.venv\\Scripts\\pip install ...' (Windows) or '.venv/bin/pip install ...' (Linux). "
    "Do NOT use run_shell to create directories or copy files — use write_file instead.\n"
    "- web_search(query): look up docs, errors, external info.\n\n"
    "When the step is done, reply with a SHORT confirmation and NO tool call. "
    "Reply in the user's language."
)


_ESTIMATE_SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
}
_ESTIMATE_SYSTEM = (
    "You are a planning assistant. Given a task step description, estimate the minimum "
    "number of tool calls needed to complete it (write_file, run_shell, read_file, etc.). "
    "Count each file creation/edit as 1, each shell command as 1. Reply ONLY as JSON: {\"n\": <integer>}."
)


def _estimate_iters(step_text: str, context: str, client: OllamaClient) -> int:
    """Спитати модель скільки тул-викликів потрібно для кроку → повернути n*2 (мін 4)."""
    user = (f"Context:\n{context}\n\n" if context else "") + f"Step:\n{step_text}"
    try:
        msg = client.chat(
            [{"role": "system", "content": _ESTIMATE_SYSTEM},
             {"role": "user", "content": user}],
            profile=config.EXECUTOR, fmt=_ESTIMATE_SCHEMA,
        )
        n = json.loads(msg.get("content") or "{}").get("n", 6)
        return max(6, int(n) * 2)
    except Exception:
        return 12


def _args(call: dict) -> dict:
    a = call.get("function", {}).get("arguments", {})
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except Exception:
            a = {}
    return a or {}


def run_step(step_text: str, ctx: ToolContext, client: OllamaClient | None = None,
             registry: ToolRegistry | None = None, context: str = "",
             max_iters: int | None = None, on_tool=None, stats_sink: list | None = None) -> tuple[str, list[str]]:
    """Виконати один крок через tool-loop.
    max_iters=None → авто-оцінка моделлю (n*2); явне число скасовує оцінку.
    on_tool(name, args, result) — необовʼязковий колбек для UI-журналу.
    stats_sink — якщо передано, у нього додаються client.last_stats кожного виклику
    моделі (для лічильника токенів кроку).
    Повертає (фінальний_текст, журнал_дій)."""
    client = client or OllamaClient()
    registry = registry or default_registry()
    if max_iters is None:
        max_iters = _estimate_iters(step_text, context, client)
    user = (f"Контекст:\n{context}\n\n" if context else "") + f"Крок:\n{step_text}"
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user}]
    log: list[str] = []

    for _ in range(max_iters):
        msg = client.chat(messages, tools=registry.schema(), profile=config.EXECUTOR)
        if stats_sink is not None and client.last_stats:
            stats_sink.append(dict(client.last_stats))
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip(), log

        messages.append({"role": "assistant", "content": msg.get("content", ""),
                         "tool_calls": calls})
        for call in calls:
            name = call.get("function", {}).get("name", "")
            args = _args(call)
            result = registry.dispatch(name, args, ctx)
            log.append(f"{name}({', '.join(args)}) -> {result[:80]}")
            if on_tool:
                on_tool(name, args, result)
            messages.append({"role": "tool", "content": result})

    return "досягнуто ліміту ітерацій", log
