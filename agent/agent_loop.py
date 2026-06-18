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
    "You have tools — pick the RIGHT one:\n"
    "- To DUPLICATE an existing file into a NEW file (copy, optionally refactored): call "
    "create_from_source ONCE (target=new file, source=existing file, instruction=what to "
    "change or '' for a plain copy). It copies large data literals byte-for-byte. NEVER "
    "read a big file and rewrite it by hand, and NEVER use shell copy/cp for this.\n"
    "- To CHANGE an existing .py in place: call edit_file (path, instruction).\n"
    "- To create a small brand-new file from scratch: call write_file (path, content) — "
    "always pass a non-empty path that points to a FILE, not a folder.\n"
    "- To inspect: list_dir, read_file. To run programs (python/pytest/git): run_shell.\n"
    "Use the file tools for file operations, not shell cmdlets. When the step is done, "
    "reply with a SHORT confirmation and NO tool call. Reply in the user's language."
)


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
             max_iters: int = 5, on_tool=None, stats_sink: list | None = None) -> tuple[str, list[str]]:
    """Виконати один крок через tool-loop.
    on_tool(name, args, result) — необовʼязковий колбек для UI-журналу.
    stats_sink — якщо передано, у нього додаються client.last_stats кожного виклику
    моделі (для лічильника токенів кроку).
    Повертає (фінальний_текст, журнал_дій)."""
    client = client or OllamaClient()
    registry = registry or default_registry()
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
