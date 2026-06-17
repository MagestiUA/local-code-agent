"""Реєстр інструментів для агентного tool-loop.

Кожен інструмент = {схема, обробник}. Додати новий тул = зареєструвати його, без
змін у циклі. Обробники працюють у ToolContext (root, дозволи, клієнт, confirm).

Файлові операції — окремі тули (write_file/read_file/list_dir), щоб модель не
залежала від PowerShell-cmdlet'ів; run_shell — для запуску програм (python/pytest/git).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import tools as T
from .executor import apply_edit, create_from_source, run_edit
from .project import scan_structure


@dataclass
class ToolContext:
    root: Path
    permissions: dict = field(default_factory=lambda: {"edits": "ask", "shell": "allowlist"})
    client: object = None
    confirm: Callable | None = None     # (text) -> bool


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    required: list
    handler: Callable                    # (args: dict, ctx: ToolContext) -> str


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def schema(self) -> list[dict]:
        """Формат Ollama tools=[...]."""
        return [{
            "type": "function",
            "function": {
                "name": t.name, "description": t.description,
                "parameters": {"type": "object", "properties": t.parameters,
                               "required": t.required},
            },
        } for t in self._tools.values()]

    def dispatch(self, name: str, args: dict, ctx: ToolContext) -> str:
        t = self._tools.get(name)
        if not t:
            return f"невідомий інструмент: {name}"
        try:
            return t.handler(args or {}, ctx)
        except Exception as e:
            return f"помилка інструмента {name}: {e}"


# ── Обробники ────────────────────────────────────────────────────────────────
def _resolve(ctx: ToolContext, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ctx.root / p


def h_list_dir(args, ctx):
    return scan_structure(_resolve(ctx, args.get("path", ".")))


def h_read_file(args, ctx):
    p = _resolve(ctx, args.get("path", ""))
    if not p.is_file():
        return f"файл не знайдено: {args.get('path')}"
    return T.read_file(p)[:8000]


def h_write_file(args, ctx):
    p = _resolve(ctx, args.get("path", ""))
    content = args.get("content", "")
    T.backup_file(p)
    T.write_file(p, content)
    return f"записано {p.name} ({len(content)} символів)"


def h_run_shell(args, ctx):
    cmd = (args.get("command") or "").strip()
    mode = ctx.permissions.get("shell", "allowlist")
    if mode == "off":
        return "консоль вимкнена (дозвіл shell=off)"
    if mode == "ask":
        if ctx.confirm and not ctx.confirm(f"Виконати: {cmd}"):
            return "команду відхилено користувачем"
        r = T.run_shell(cmd, cwd=str(ctx.root), allow_all=True)
    else:  # allowlist
        r = T.run_shell(cmd, cwd=str(ctx.root))
        if not r.allowed:
            return f"заблоковано allow-list: {cmd}"
    return (r.stdout or r.stderr or f"rc={r.returncode}")[:2000]


def _apply_or_reject(res, path, ctx, made: str) -> str:
    if not res.ok:
        return f"помилка: {res.error}"
    if ctx.permissions.get("edits") == "auto" or (ctx.confirm and ctx.confirm(res.diff)):
        apply_edit(path, res)
        return f"{made} {Path(path).name}:\n{res.diff}"
    return f"відхилено. diff:\n{res.diff}"


def h_edit_file(args, ctx):
    p = _resolve(ctx, args.get("path", ""))
    if not p.is_file():
        return f"файл не знайдено: {args.get('path')}"
    res = run_edit(p, args.get("instruction", ""), ctx.client, False)
    return _apply_or_reject(res, p, ctx, "застосовано до")


def h_create_from_source(args, ctx):
    target = _resolve(ctx, args.get("target", ""))
    source = _resolve(ctx, args.get("source", ""))
    if not source.is_file():
        return f"джерело не знайдено: {args.get('source')}"
    res = create_from_source(target, source, args.get("instruction", ""), ctx.client)
    return _apply_or_reject(res, target, ctx, "створено")


def default_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(Tool("list_dir", "Показати структуру файлів проєкту.",
                    {"path": {"type": "string", "description": "тека, типово корінь"}},
                    [], h_list_dir))
    r.register(Tool("read_file", "Прочитати вміст файлу.",
                    {"path": {"type": "string"}}, ["path"], h_read_file))
    r.register(Tool("write_file", "Створити або перезаписати файл із заданим вмістом.",
                    {"path": {"type": "string"}, "content": {"type": "string"}},
                    ["path", "content"], h_write_file))
    r.register(Tool("run_shell", "Запустити програму в консолі Windows (python, pytest, git).",
                    {"command": {"type": "string"}}, ["command"], h_run_shell))
    r.register(Tool("edit_file",
                    "Відрефакторити/змінити наявний .py файл за інструкцією "
                    "(великі дані-літерали зберігаються автоматично).",
                    {"path": {"type": "string"}, "instruction": {"type": "string"}},
                    ["path", "instruction"], h_edit_file))
    r.register(Tool("create_from_source",
                    "Створити НОВИЙ .py файл на основі наявного файлу-джерела.",
                    {"target": {"type": "string"}, "source": {"type": "string"},
                     "instruction": {"type": "string"}},
                    ["target", "source", "instruction"], h_create_from_source))
    return r
