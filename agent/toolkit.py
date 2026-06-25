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
    attachments: list = field(default_factory=list)   # метадані вкладень (без вмісту)
    attachments_dir: Path | None = None  # тека з файлами вкладень сесії (для read_attachment)


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
    """Резолвити шлях відносно кореня проєкту й боронити вихід за його межі
    (traversal через `..` або абсолютний шлях). Без цього модель — особливо при
    edits=auto — могла б читати/писати будь-де на диску (напр. ~/.ssh, поза проєктом).
    Кидає ValueError; ToolRegistry.dispatch ловить його й віддає текст моделі."""
    root = ctx.root.resolve()
    p = Path(path)
    full = (p if p.is_absolute() else root / p).resolve()
    if full != root and root not in full.parents:
        raise ValueError(f"шлях поза межами проєкту заборонено: {path}")
    return full


def h_list_dir(args, ctx):
    return scan_structure(_resolve(ctx, args.get("path", ".")))


def h_read_file(args, ctx):
    p = _resolve(ctx, args.get("path", ""))
    if not p.is_file():
        return f"файл не знайдено: {args.get('path')}"
    text = T.read_file(p)
    if len(text) > 8000:
        return (text[:8000] +
                f"\n\n…[обрізано: показано 8000 з {len(text)} символів]. НЕ копіюй вміст "
                "вручну — для дублювання у новий файл клич create_from_source, для зміни на "
                "місці — edit_file (вони зберігають великі дані-літерали байт-у-байт).")
    return text


# Розмір шматка на один read_attachment-виклик. На num_ctx=131072 з резервом
# ~20k токенів на system/tools/історію/відповідь лишається ~110k токенів ≈ 275k
# символів (кирилиця ~2.5 симв./токен) на сам вміст в одній розмові — тобто
# 32000 далеко не межа контексту; це баланс "менше раундів" (386KB → ~12 читань
# замість 48 по 8000) проти "лишити моделі простір подумати над шматком".
ATTACHMENT_CHUNK_SIZE = 32_000


def h_read_attachment(args, ctx):
    """Прочитати прикріплений користувачем файл за іменем (нечіткий пошук у теці
    вкладень сесії). Прощає неточності в довгому імені — exact → ci → підрядок.
    offset — позиція в символах, з якої читати (для великих файлів читай частинами:
    кожен виклик повертає ATTACHMENT_CHUNK_SIZE символів і явно каже, який offset
    передати в НАСТУПНОМУ виклику, поки файл не дочитано)."""
    d = ctx.attachments_dir
    if not d or not Path(d).is_dir():
        return "немає прикріплених файлів"
    files = [p for p in Path(d).iterdir() if p.is_file()]
    if not files:
        return "немає прикріплених файлів"
    q = Path((args.get("name") or "").strip()).name      # лише базове ім'я
    match = next((p for p in files if p.name == q), None)
    if not match and q:
        ql = q.lower()
        cand = ([p for p in files if p.name.lower() == ql]
                or [p for p in files if ql in p.name.lower()]
                or [p for p in files if p.name.lower() in ql])
        match = cand[0] if cand else None
    if not match:
        names = ", ".join(p.name for p in files)
        return f"файл '{q}' не знайдено серед вкладень. Доступні: {names}"
    text = T.read_file(match)
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    total = len(text)
    chunk = text[offset:offset + ATTACHMENT_CHUNK_SIZE]
    end = offset + len(chunk)
    header = f"=== {match.name} ({offset}-{end} з {total}) ===" if total > ATTACHMENT_CHUNK_SIZE else f"=== {match.name} ==="
    if end < total:
        chunk += (f"\n\n…[лишилось {total - end} символів. Щоб продовжити, виклич "
                  f"read_attachment(name='{match.name}', offset={end})]")
    return f"{header}\n{chunk}"


def h_read_topic(args, ctx):
    """Прочитати нотатку теми (chat-режим, спільна памʼять між чатами — agent.topics).
    Нечіткий пошук за іменем, як read_attachment."""
    from . import topics
    q = (args.get("name") or "").strip()
    existing = topics.list_topics()
    if not existing:
        return "немає збережених тем"
    match = next((t for t in existing if t == q), None)
    if not match and q:
        ql = q.lower()
        cand = ([t for t in existing if t.lower() == ql]
                or [t for t in existing if ql in t.lower()]
                or [t for t in existing if t.lower() in ql])
        match = cand[0] if cand else None
    if not match:
        return f"тема '{q}' не знайдена. Існуючі теми: {', '.join(existing)}"
    content = topics.load_topic(match)
    if len(content) > 8000:
        content = content[:8000] + f"\n\n…[обрізано: показано 8000 з {len(content)} символів]"
    return f"=== тема: {match} ===\n{content}"


def h_write_file(args, ctx):
    rel = (args.get("path") or "").strip()
    if not rel:
        return "помилка: не вказано path для write_file"
    p = _resolve(ctx, rel)
    if p.is_dir():
        return f"помилка: '{rel}' — це тека, а не файл"
    content = args.get("content", "")
    p.parent.mkdir(parents=True, exist_ok=True)
    T.backup_file(p)
    T.write_file(p, content)
    return f"записано {p.name} ({len(content)} символів)"


def h_run_shell(args, ctx):
    from .shell_guard import classify
    cmd = (args.get("command") or "").strip()
    mode = ctx.permissions.get("shell", "allowlist")
    if mode == "off":
        return "консоль вимкнена (дозвіл shell=off)"
    if mode == "ask":
        if ctx.confirm and not ctx.confirm(f"Виконати: {cmd}"):
            return "команду відхилено користувачем"
        r = T.run_shell(cmd, cwd=str(ctx.root), allow_all=True)
    elif mode == "smart":
        if classify(cmd) == "danger":
            if ctx.confirm and not ctx.confirm(f"⚠ Небезпечна команда:\n{cmd}"):
                return "команду відхилено користувачем"
        r = T.run_shell(cmd, cwd=str(ctx.root), allow_all=True)
    elif mode == "auto":
        r = T.run_shell(cmd, cwd=str(ctx.root), allow_all=True)
    else:  # allowlist
        r = T.run_shell(cmd, cwd=str(ctx.root))
        if not r.allowed:
            return f"заблоковано allowlist: {cmd}"
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


def h_web_search(args, ctx):
    from . import websearch
    res = websearch.search(args.get("query", ""),
                           int(args.get("max_results", websearch.DEFAULT_MAX) or websearch.DEFAULT_MAX))
    return websearch.format_results(res)


def default_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(Tool("list_dir", "Показати структуру файлів проєкту.",
                    {"path": {"type": "string", "description": "тека, типово корінь"}},
                    [], h_list_dir))
    r.register(Tool("read_file", "Прочитати вміст файлу.",
                    {"path": {"type": "string"}}, ["path"], h_read_file))
    r.register(Tool("read_attachment",
                    "Прочитати файл, ПРИКРІПЛЕНИЙ користувачем до повідомлення. Передай "
                    "ім'я файлу зі списку прикріплених (можна частину імені — пошук "
                    "нечіткий). Використовуй ЦЕ замість read_file для вкладень. Великі "
                    "файли повертаються частинами (~32000 символів) — якщо у відповіді "
                    "є позначка 'лишилось N символів... offset=X', виклич знову з тим "
                    "offset, щоб дочитати решту.",
                    {"name": {"type": "string", "description": "ім'я прикріпленого файлу"},
                     "offset": {"type": "integer",
                                "description": "з якого символу читати (0 — спочатку); "
                                                "брати зі значення offset у позначці "
                                                "'лишилось...' попереднього виклику"}},
                    ["name"], h_read_attachment))
    r.register(Tool("write_file",
                    "Створити або перезаписати файл із заданим вмістом. "
                    "Батьківські директорії створюються АВТОМАТИЧНО — не потрібно окремо "
                    "викликати mkdir чи run_shell для створення тек.",
                    {"path": {"type": "string", "description": "шлях до файлу (не тека)"},
                     "content": {"type": "string"}},
                    ["path", "content"], h_write_file))
    r.register(Tool("run_shell",
                    "Запустити команду в консолі Windows. cwd вже встановлено в корінь "
                    "проєкту. Використовуй для: python, pip install, pytest, git, "
                    "python -m venv .venv тощо. НЕ використовуй для створення тек або "
                    "копіювання файлів — для цього є write_file і create_from_source.",
                    {"command": {"type": "string", "description": "команда для виконання"}},
                    ["command"], h_run_shell))
    r.register(Tool("edit_file",
                    "Відрефакторити/змінити наявний .py файл за інструкцією "
                    "(великі дані-літерали зберігаються автоматично).",
                    {"path": {"type": "string"}, "instruction": {"type": "string"}},
                    ["path", "instruction"], h_edit_file))
    r.register(Tool("create_from_source",
                    "Створити НОВИЙ файл як копію наявного файлу-джерела з опційним "
                    "рефакторингом. ЦЕ ЄДИНИЙ ПРАВИЛЬНИЙ спосіб скопіювати/продублювати "
                    "файл у новий: великі дані-літерали копіюються байт-у-байт. Використовуй "
                    "замість ручного read_file+write_file і замість shell-copy.",
                    {"target": {"type": "string", "description": "новий файл"},
                     "source": {"type": "string", "description": "наявний файл-джерело"},
                     "instruction": {"type": "string",
                                     "description": "що змінити; '' якщо просто копія"}},
                    ["target", "source", "instruction"], h_create_from_source))
    r.register(Tool("web_search",
                    "Пошук в інтернеті актуальної/зовнішньої інформації (документація, "
                    "помилки, новини). Повертає короткі сніпети (заголовок, URL, опис).",
                    {"query": {"type": "string", "description": "пошуковий запит"},
                     "max_results": {"type": "integer", "description": "скільки результатів, типово 5"}},
                    ["query"], h_web_search))
    r.register(Tool("read_topic",
                    "Прочитати нотатку попередньої аналітичної розмови за темою (chat-режим, "
                    "СПІЛЬНА памʼять між усіма чатами). Викликай, коли ЦЯ розмова продовжує "
                    "тему зі списку доступних тем у контексті.",
                    {"name": {"type": "string", "description": "назва теми"}},
                    ["name"], h_read_topic))
    return r
