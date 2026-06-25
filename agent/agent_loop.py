"""Per-step tool-loop: дає моделі інструменти й крутить цикл на ОДИН крок плану.

Модель сама вирішує, які тули викликати; ми виконуємо й повертаємо результат, поки
вона не завершить крок (без виклику тула) або не вичерпає ліміт ітерацій. Контекст
вузький (один крок), цикл короткий — безпечно для слабкої локальної моделі.
"""
from __future__ import annotations

import json
import re

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
    "If a run_shell command FAILS, read the error and issue a CORRECTED command — do "
    "NOT repeat the same failing command. (E.g. git: stage with 'git add -A', then "
    "commit with 'git commit -m \"message\"' — '-m' belongs to commit, NOT to add.)\n"
    "NEVER invent data, lists, or mechanisms that aren't shown in your context (e.g. a "
    "hardcoded list of app names/IDs when the project already has a search/lookup "
    "mechanism for that). If the step implies an existing project mechanism should be "
    "reused, use read_file/list_dir FIRST to find and use the real one — do not fall "
    "back to a made-up substitute.\n"
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


def _estimate_iters(step_text: str, context: str, client: OllamaClient,
                    profile: dict = config.EXECUTOR) -> int:
    """Спитати модель скільки тул-викликів потрібно для кроку → повернути n*2 (мін 6).
    profile: chat-режим передає CHAT_EXECUTOR (gemma)."""
    user = (f"Context:\n{context}\n\n" if context else "") + f"Step:\n{step_text}"
    try:
        msg = client.chat(
            [{"role": "system", "content": _ESTIMATE_SYSTEM},
             {"role": "user", "content": user}],
            profile=profile, fmt=_ESTIMATE_SCHEMA,
        )
        n = json.loads(msg.get("content") or "{}").get("n", 6)
        # Запас на самокорекцію: слабка модель часто витрачає 2-3 ітерації на невдалі
        # спроби (напр. плутає `git add -m` з `git commit -m`), тож множник і підлога
        # вищі — інакше крок упирається в ліміт ще до завершення (як було з commit).
        return max(10, int(n) * 3)
    except Exception:
        return 15


# Деякі моделі (помічено на qwen3-coder) під складеними інструкціями плутають тег
# самого виклику (<tool_call>) із тегом опису сигнатур (<tools>) і виводять спробу
# викликати тул як звичайний текст замість structured tool_calls. Без цієї перевірки
# крок "тихо" завершується без жодної дії (calls=[] виглядає як "модель закінчила").
_LEAKED_CALL_RE = re.compile(r'<tool_call>|<tools>|"arguments"\s*:\s*\{', re.IGNORECASE)


def _looks_like_leaked_tool_call(content: str) -> bool:
    return bool(content) and bool(_LEAKED_CALL_RE.search(content))


def _extract_leaked_tool_call(content: str) -> dict | None:
    """Розпарсити просочений виклик тула з тексту (XML-теги чи голий JSON) у
    справжній tool_call dict — і ВИКОНАТИ дію одразу, замість ще одного nudge-раунду.
    Живий кейс: модель повторила ТОЙ САМИЙ просочений формат і після nudge (2/2),
    включно з малформованим зайвим '}' наприкінці — звичайний retry тут не зарадить,
    бо модель стабільно ламає формат саме так. Парсинг стійкий до:
    - обгортки <tool_call>/<tools> (чи без неї — голий JSON);
    - зайвих символів/дужок ПІСЛЯ валідного JSON-обʼєкта (рахуємо глибину дужок і
      зупиняємось на першому балансі, ігноруючи хвіст)."""
    if not content:
        return None
    text = re.sub(r'</?tool_call>|</?tools>', '', content, flags=re.IGNORECASE).strip()
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    end = None
    for i, ch in enumerate(text[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        obj = json.loads(text[start:end])
    except Exception:
        return None
    name = obj.get("name")
    args = obj.get("arguments", {})
    if not isinstance(name, str) or not name or not isinstance(args, dict):
        return None
    return {"function": {"name": name, "arguments": args}}


def recover_tool_calls(msg: dict) -> tuple[list[dict], bool]:
    """Повертає (tool_calls, recovered). Якщо модель віддала справжні structured
    tool_calls — повертає їх як є (recovered=False). Якщо ні, але контент містить
    розпарсюваний просочений виклик — повертає його як один tool_call
    (recovered=True), щоб виконати ту саму дію без додаткового раунду."""
    calls = msg.get("tool_calls") or []
    if calls:
        return calls, False
    leaked = _extract_leaked_tool_call((msg.get("content") or "").strip())
    return ([leaked], True) if leaked else ([], False)


# Спільна для УСІХ tool-loop'ів (agent_loop.run_step, lca_web._run_tool_step,
# lca_web._run_tool_chat) перевірка "чи варто підштовхнути модель ще раз замість
# того, щоб прийняти цю відповідь як фінальну". Раніше кожен loop мав свою копію
# цієї перевірки — фікс одного й того ж бага (порожня/просочена відповідь без
# tool_calls тихо приймається як "крок завершено") довелось вносити в трьох місцях
# окремо. Differences між циклами (sync/async виклик моделі, continue циклу чи
# перехід в іншу фазу) лишаються в кожному місці — тут лише ОДНЕ рішення + текст.
NUDGE_TEXT = (
    "Виклич потрібний інструмент як справжній tool/function call (structured), НЕ "
    "як текст і НЕ в XML-тегах у відповіді."
)


def should_nudge(content: str, already_nudged: bool) -> bool:
    """True, якщо модель не виконала дію (content порожній, або спроба виклику тула
    просочилась як текст замість structured tool_calls — плутанина тегів
    <tool_call>/<tools>, бачено на qwen3-coder) і ще НЕ нюджили цього разу (рівно
    один nudge — щоб не зациклитись, якщо модель стабільно ламає формат)."""
    return not already_nudged and (not content or _looks_like_leaked_tool_call(content))


# Останній read_attachment повертає явну підказку "лишилось N символів, виклич
# read_attachment(name='X', offset=N)" (toolkit.h_read_attachment). Якщо модель після
# цього НЕ зробила той виклик, а написала прозою щось типу "продовжую читати" — текстовий
# nudge тут НЕ зарадить: живцем бачили модель, що 15+ разів підряд повторює ТОЧНО ту саму
# фразу, ігноруючи nudge. Маємо вже все потрібне (ім'я файлу + offset) з власної ж
# підказки — тож ВИКОНУЄМО продовження самі, без участі моделі: гарантований прогрес
# замість сподівання на співпрацю.
_PENDING_CONTINUATION_RE = re.compile(r"read_attachment\(name='([^']+)',\s*offset=(\d+)\)\]\s*$")


def pending_continuation(messages: list[dict]) -> dict | None:
    """Якщо останній РЕАЛЬНИЙ tool-результат в історії містить незавершену пагінацію
    read_attachment — повертає синтетичний tool_call (той самий формат, що й
    recover_tool_calls) для автопродовження. Йдемо назад, пропускаючи власні
    nudge-повідомлення ('user') — інакше після ПЕРШОГО nudge перевірка вже не бачить
    tool-результат і мовчки здається."""
    for m in reversed(messages):
        role = m.get("role")
        if role == "tool":
            match = _PENDING_CONTINUATION_RE.search((m.get("content") or "").strip())
            if not match:
                return None
            return {"function": {"name": "read_attachment",
                                 "arguments": {"name": match.group(1), "offset": int(match.group(2))}}}
        if role == "user":
            continue   # пропускаємо власні nudge-репліки, шукаємо далі назад
        return None   # assistant (реальний виклик уже відбувся) чи system — не pending
    return None


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
             max_iters: int | None = None, on_tool=None, stats_sink: list | None = None,
             stop_event=None) -> tuple[str, list[str]]:
    """Виконати один крок через tool-loop.
    max_iters=None → авто-оцінка моделлю (n*2); явне число скасовує оцінку.
    on_tool(name, args, result) — необовʼязковий колбек для UI-журналу.
    stats_sink — якщо передано, у нього додаються client.last_stats кожного виклику
    моделі (для лічильника токенів кроку).
    stop_event: threading.Event — якщо встановлено, цикл переривається між
    ітераціями (поточний частковий результат повертається).
    Повертає (фінальний_текст, журнал_дій)."""
    client = client or OllamaClient()
    registry = registry or default_registry()
    if max_iters is None:
        max_iters = _estimate_iters(step_text, context, client)
    user = (f"Контекст:\n{context}\n\n" if context else "") + f"Крок:\n{step_text}"
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": user}]
    log: list[str] = []
    nudged = False

    for _ in range(max_iters):
        if stop_event is not None and stop_event.is_set():
            return "зупинено користувачем", log
        msg = client.chat(messages, tools=registry.schema(), profile=config.EXECUTOR)
        if stats_sink is not None and client.last_stats:
            stats_sink.append(dict(client.last_stats))
        calls, recovered = recover_tool_calls(msg)
        if not calls:
            content = (msg.get("content") or "").strip()
            # Великий файл дочитується частинами (read_attachment offset) — якщо
            # лишився непрочитаний хвіст, а модель не продовжила виклик, ДОЧИТУЄМО
            # САМІ (текстовий nudge тут ненадійний — бачили живцем 15+ ідентичних
            # повторів "продовжую читати" без жодної дії). Див. pending_continuation.
            cont = pending_continuation(messages)
            if cont is not None:
                calls, recovered = [cont], True
            # НЕ повертаємо зламаний/порожній текст назад як assistant-повідомлення —
            # інакше модель копіює власну попередню відповідь і застрягає в тому
            # самому виводі навіть при повторі (перевірено живцем). Див. should_nudge.
            elif should_nudge(content, nudged):
                nudged = True
                messages.append({"role": "user", "content": NUDGE_TEXT})
                continue
            else:
                return content, log

        # recovered=True: викликали тул з розпарсеного просоченого тексту — НЕ ехо-имо
        # сам зламаний текст назад (той самий анти-анкоринг, що й для nudge).
        messages.append({"role": "assistant", "content": "" if recovered else msg.get("content", ""),
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
