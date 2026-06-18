"""local-code-agent — веб-інтерфейс на Reflex (стиль Claude).

G3: керування сесіями через бекенд (agent/session.py): створення/список/вибір,
тека, дозволи, план-наперед, джерела — зберігаються в сесію.
Прив'язки runner (answer/shell/plan/edit) — G4.
"""
import asyncio
import threading
import time
from pathlib import Path

import reflex as rx

from agent import session as sess
from agent.agent_loop import run_step
from agent.answerer import answer_stream, build_context
from agent.intent import classify_intent
from agent.llm import OllamaClient
from agent.memory import render_for_planner
from agent.planner import make_plan
from agent.project import load_project_doc
from agent.toolkit import ToolContext

_CLIENT = None

# Очікувані confirm-и shell=ask, поза State (threading.Event непіклиться -> інакше
# StateSerializationError). Ключ — id сесії. {sid: {"event": Event, "ok": bool}}
_CONFIRMS: dict = {}


def get_client() -> OllamaClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = OllamaClient()
    return _CLIENT


def _fmt_tokens(stats: list[dict]) -> str:
    """Рядок-футер лічильника з агрегату stats (сума по викликах моделі кроку)."""
    prompt = sum(s.get("prompt", 0) for s in stats)
    out = sum(s.get("out", 0) for s in stats)
    ns = sum(s.get("eval_ns", 0) for s in stats)
    tps = round(out / (ns / 1e9), 1) if ns else 0.0
    return f"`↑{prompt} ctx · ↓{out} out · {tps} tok/s`"


BG = "#262624"
PANEL = "#171615"
INPUT = "#30302e"
BORDER = "border-white/10"
SERIF = {"fontFamily": "Newsreader, Georgia, serif"}


class State(rx.State):
    sessions: list[dict] = []
    current_id: str = ""
    title: str = ""
    project_root: str = ""
    edits: str = "ask"
    shell: str = "allowlist"
    plan_first: bool = False
    references: list[str] = []
    folder_input: str = ""
    ref_input: str = ""
    task: str = ""
    messages: list[dict] = []
    has_pending: bool = False
    busy: bool = False
    status: str = ""
    rename_open: bool = False
    rename_id: str = ""
    rename_input: str = ""
    confirm_open: bool = False
    confirm_text: str = ""
    # живий стрім (#1): оновлюється по ходу роздумів/відповіді
    streaming: bool = False
    stream_content: str = ""
    stream_thinking: str = ""
    stream_prompt_tokens: int = 0
    stream_out_tokens: int = 0
    stream_tps: float = 0.0

    @rx.event
    def set_task(self, v: str):
        self.task = v

    def _resolve_confirm(self, ok: bool):
        c = _CONFIRMS.get(self.current_id)
        if c is not None:
            c["ok"] = ok
            c["event"].set()
        self.confirm_open = False

    @rx.event
    def confirm_yes(self):
        self._resolve_confirm(True)

    @rx.event
    def confirm_no(self):
        self._resolve_confirm(False)

    @rx.var
    def folder_name(self) -> str:
        return Path(self.project_root).name if self.project_root else "тека"

    @rx.var
    def has_current(self) -> bool:
        return self.current_id != ""

    @rx.event
    def set_folder_input(self, v: str):
        self.folder_input = v

    @rx.event
    def set_ref_input(self, v: str):
        self.ref_input = v

    @rx.event
    def load_sessions(self):
        self.sessions = sess.list_sessions()

    def _select(self, sid: str):
        s = sess.load_session(sid)
        self.current_id = s.id
        self.title = s.title
        self.project_root = s.project_root
        self.edits = s.permissions.get("edits", "ask")
        self.shell = s.permissions.get("shell", "allowlist")
        self.plan_first = s.plan_first
        self.references = list(s.reference_files)
        self.messages = [{"thinking": "", **m} for m in s.messages]
        self.has_pending = s.pending_plan is not None

    def _append(self, role: str, content: str, kind: str = "text", thinking: str = ""):
        self.messages = self.messages + [
            {"role": role, "content": content, "kind": kind, "thinking": thinking}]
        if self.current_id:
            s = sess.load_session(self.current_id)
            s.messages = [dict(m) for m in self.messages]   # розгорнути Reflex-проксі
            sess.save_session(s)

    def _save_current(self):
        if not self.current_id:
            return
        s = sess.load_session(self.current_id)
        s.title = self.title or s.title
        s.project_root = self.project_root
        s.permissions = {"edits": self.edits, "shell": self.shell}
        s.plan_first = self.plan_first
        s.reference_files = [str(x) for x in self.references]   # розгорнути проксі
        sess.save_session(s)
        self.sessions = sess.list_sessions()

    @rx.event
    def new_chat(self):
        s = sess.new_session("Новий чат", "")
        sess.save_session(s)
        self.sessions = sess.list_sessions()
        self._select(s.id)

    @rx.event
    def select_chat(self, sid: str):
        self._select(sid)

    @rx.event
    def set_rename_input(self, v: str):
        self.rename_input = v

    @rx.event
    def set_rename_open(self, v: bool):
        self.rename_open = v

    @rx.event
    def open_rename(self, sid: str, title: str):
        self.rename_id = sid
        self.rename_input = title
        self.rename_open = True

    @rx.event
    def save_rename(self):
        if self.rename_id and self.rename_input.strip():
            s = sess.load_session(self.rename_id)
            s.title = self.rename_input.strip()
            sess.save_session(s)
            if self.current_id == self.rename_id:
                self.title = s.title
            self.sessions = sess.list_sessions()
        self.rename_open = False

    @rx.event
    def delete_chat(self, sid: str):
        sess.delete_session(sid)
        if self.current_id == sid:
            self.current_id = ""
            self.title = ""
            self.project_root = ""
            self.messages = []
            self.references = []
            self.has_pending = False
        self.sessions = sess.list_sessions()

    @rx.event
    def on_enter(self, key: str):
        if key == "Enter":
            return State.send_task

    @rx.event
    def set_edits(self, v: str):
        self.edits = v
        self._save_current()

    @rx.event
    def set_shell(self, v: str):
        self.shell = v
        self._save_current()

    @rx.event
    def set_plan_first(self, v: bool):
        self.plan_first = v
        self._save_current()

    @rx.event
    def save_folder(self):
        self.project_root = self.folder_input.strip()
        if self.project_root and self.title in ("", "Новий чат"):
            self.title = Path(self.project_root).name
        self._save_current()

    @rx.event
    def add_ref(self):
        r = self.ref_input.strip()
        if r and r not in self.references:
            self.references = self.references + [r]
        self.ref_input = ""
        self._save_current()

    @rx.event
    def remove_ref(self, r: str):
        self.references = [x for x in self.references if x != r]
        self._save_current()

    def _make_confirm(self, loop):
        """Збудувати confirm(text)->bool для shell=ask. Викликається із воркер-потоку
        (asyncio.to_thread), тож показ діалогу планує корутину на головний цикл через
        run_coroutine_threadsafe, а саме очікування — на threading.Event (блокує лише
        воркер, не цикл). Event тримаємо в модульному _CONFIRMS (поза State — інакше
        непіклиться). Закриття без відповіді неможливе (діалог керований кнопками);
        таймаут 300с -> відмова, щоб воркер не завис назавжди."""
        cid = self.current_id

        def confirm(text: str) -> bool:
            ev = threading.Event()
            _CONFIRMS[cid] = {"event": ev, "ok": False}

            async def _show():
                async with self:
                    self.confirm_text = text
                    self.confirm_open = True

            asyncio.run_coroutine_threadsafe(_show(), loop).result()
            ev.wait(timeout=300)
            return _CONFIRMS.pop(cid, {}).get("ok", False)
        return confirm

    async def _execute_steps(self, plan, root: str, client):
        """Виконати кроки плану через per-step tool-loop: модель сама обирає тули
        (read/write/edit/run_shell), ми лише показуємо її дії та diff у чаті.

        ToolContext: edits=auto (план уже схвалено — через plan_first або edits=auto).
        shell передаємо як є; для shell=ask confirm показує модальний попап."""
        ctx = self._exec_ctx(root, client)
        for step in plan.steps:
            async with self:
                self.status = f"Крок #{step.id}: {step.description[:50]}"
            if step.kind == "inline":
                plan.set_result(step.id, "done", "inline")
                continue
            final, log = await self._run_tool_step(step.description, ctx, title=step.description[:60])
            plan.set_result(step.id, "done" if log else "failed", final or "—")
        async with self:
            self._append("assistant", "Готово ✓", "note")

    def _exec_ctx(self, root: str, client) -> ToolContext:
        """ToolContext для виконання: edits=auto (план/запит уже схвалено),
        shell передаємо як є; для shell=ask confirm показує модальний попап."""
        return ToolContext(root=Path(root),
                           permissions={"edits": "auto", "shell": self.shell},
                           client=client,
                           confirm=self._make_confirm(asyncio.get_running_loop()))

    async def _run_tool_step(self, step_text: str, ctx: ToolContext, title: str = ""):
        """Один прохід tool-loop. У чат додає ОДНЕ повідомлення на крок: видимий
        заголовок + короткий підсумок, а всі дії моделі (списки файлів, diff-и,
        вивід команд) ховає під кат «хід виконання» (kind=step). Повертає (final, log)."""
        actions: list[tuple[str, str]] = []   # (tool, result) — заповнює on_tool у потоці
        stats: list[dict] = []                # client.last_stats кожного виклику моделі
        final, log = await asyncio.to_thread(
            lambda: run_step(step_text, ctx, ctx.client, stats_sink=stats,
                             on_tool=lambda n, a, r: actions.append((n, r))))

        parts: list[str] = []
        for name, result in actions:
            if name in ("edit_file", "create_from_source"):
                parts.append(f"**{name}**\n```diff\n{result}\n```")
            elif name == "run_shell":
                parts.append(f"**run_shell**\n```\n{result}\n```")
            else:   # list_dir / read_file тощо
                parts.append(f"**{name}**\n```\n{result[:4000]}\n```")
        log_md = "\n\n".join(parts)

        head = title or step_text[:80]
        content = f"**{head}**"
        if final:
            content += f"\n\n{final}"
        content += "\n\n" + _fmt_tokens(stats)
        async with self:
            self._append("assistant", content, "step", thinking=log_md)
        return final, log

    async def _stream_answer(self, text: str, ctx: str, client):
        """Стрімить відповідь (answer-режим) з живим лічильником токенів + tok/s.
        Блокуючий генератор тягнемо по чанку через to_thread, щоб цикл лишався
        живим; кожен чанк оновлює stream_*; у фіналі — точні stats із метаданих."""
        async with self:
            self.streaming = True
            self.stream_content = ""
            self.stream_thinking = ""
            self.stream_prompt_tokens = 0
            self.stream_out_tokens = 0
            self.stream_tps = 0.0
            self.status = ""
        gen = answer_stream(text, ctx, client)
        t0 = time.time()
        stats = None
        while True:
            ev = await asyncio.to_thread(lambda g=gen: next(g, None))
            if ev is None:
                break
            if ev["done"]:
                stats = ev.get("stats")
                break
            async with self:
                self.stream_content += ev["content"]
                self.stream_thinking += ev["thinking"]
                if ev["content"]:                       # рахуємо лише токени виводу
                    self.stream_out_tokens += 1
                    el = time.time() - t0
                    if el > 0:
                        self.stream_tps = round(self.stream_out_tokens / el, 1)
        async with self:
            if stats:                                    # замінюємо приблизне точним
                self.stream_prompt_tokens = stats.get("prompt", 0)
                self.stream_out_tokens = stats.get("out", self.stream_out_tokens)
                ns = stats.get("eval_ns", 0)
                if ns:
                    self.stream_tps = round(self.stream_out_tokens / (ns / 1e9), 1)
            meta = (f"`↑{self.stream_prompt_tokens} ctx · ↓{self.stream_out_tokens} out "
                    f"· {self.stream_tps} tok/s`")
            body = (self.stream_content or "").strip() + "\n\n" + meta
            self._append("assistant", body, "answer", thinking=self.stream_thinking)
            self.streaming = False

    @rx.event(background=True)
    async def send_task(self, form_data: dict | None = None):
        async with self:
            if not self.current_id:
                return
            if not self.project_root:
                self._append("assistant", "Спершу вкажіть робочу теку.", "note")
                return
            text = self.task.strip()
            if not text or self.busy:
                return
            self.task = ""
            self._append("user", text)
            self.busy = True
            self.status = "Визначаю тип запиту…"
            root = self.project_root
            refs = [str(x) for x in self.references]
            plan_first = self.plan_first or self.edits == "ask"

        client = await asyncio.to_thread(get_client)
        mode = await asyncio.to_thread(lambda: classify_intent(text, client))

        if mode == "answer":
            async with self:
                self.status = "Аналізую код…"
            ctx = await asyncio.to_thread(lambda: build_context(root, refs, text))
            await self._stream_answer(text, ctx, client)

        elif mode == "shell":
            async with self:
                self.status = "Виконую запит…"
            ctx = self._exec_ctx(root, client)
            await self._run_tool_step(text, ctx)

        else:  # plan / edit
            async with self:
                self.status = "Складаю план…"
            doc = await asyncio.to_thread(lambda: load_project_doc(root))
            plan = await asyncio.to_thread(lambda: make_plan(text, "", client, doc))
            if plan_first:
                async with self:
                    self._append("assistant", render_for_planner(plan), "plan")
                    s = sess.load_session(self.current_id)
                    s.set_pending_plan(plan)
                    sess.save_session(s)
                    self.has_pending = True
            else:
                await self._execute_steps(plan, root, client)

        async with self:
            self.busy = False
            self.status = ""

    @rx.event(background=True)
    async def execute_pending(self):
        async with self:
            if not self.has_pending or not self.current_id or self.busy:
                return
            self.busy = True
            self.status = "Виконую план…"
            root, cid = self.project_root, self.current_id

        client = await asyncio.to_thread(get_client)
        s = sess.load_session(cid)
        plan = s.get_pending_plan()
        await self._execute_steps(plan, root, client)

        async with self:
            self.busy = False
            self.status = ""
            s2 = sess.load_session(self.current_id)
            s2.clear_pending_plan()
            sess.save_session(s2)
            self.has_pending = False

    @rx.event
    def discard_pending(self):
        if self.current_id:
            s = sess.load_session(self.current_id)
            s.clear_pending_plan()
            sess.save_session(s)
        self.has_pending = False


def nav_item(icon: str, label: str, on_click=None) -> rx.Component:
    return rx.hstack(
        rx.icon(icon, size=16, class_name="text-gray-400"),
        rx.text(label, class_name="text-sm text-gray-200"),
        on_click=on_click,
        class_name="items-center gap-3 px-2 py-1.5 rounded-lg hover:bg-white/5 "
                   "cursor-pointer w-full",
    )


def session_item(s: dict) -> rx.Component:
    active = State.current_id == s["id"]
    return rx.hstack(
        rx.hstack(
            rx.icon("message-square", size=14, class_name="text-gray-500 shrink-0"),
            rx.text(s["title"], class_name="text-sm text-gray-200 truncate"),
            on_click=lambda: State.select_chat(s["id"]),
            class_name="items-center gap-2 grow min-w-0 cursor-pointer",
        ),
        rx.menu.root(
            rx.menu.trigger(
                rx.icon("ellipsis", size=14,
                        class_name="text-gray-500 hover:text-gray-200 cursor-pointer shrink-0"),
            ),
            rx.menu.content(
                rx.menu.item("Перейменувати",
                             on_click=lambda: State.open_rename(s["id"], s["title"])),
                rx.menu.item("Видалити", on_click=lambda: State.delete_chat(s["id"]),
                             class_name="text-red-400"),
            ),
        ),
        class_name=rx.cond(active, "bg-white/10", "hover:bg-white/5")
        + " items-center gap-1 px-2 py-1.5 rounded-lg w-full",
    )


def rename_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title("Перейменувати чат"),
            rx.input(value=State.rename_input, on_change=State.set_rename_input,
                     class_name="w-full mt-2"),
            rx.flex(
                rx.button("Скасувати", variant="soft",
                          on_click=lambda: State.set_rename_open(False)),
                rx.button("Зберегти", on_click=State.save_rename),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.rename_open,
        on_open_change=State.set_rename_open,
    )


def confirm_dialog() -> rx.Component:
    """Попап для shell=ask: підтвердити запуск команди. Керований лише кнопками
    (без on_open_change), щоб закриття не лишило воркер у вічному очікуванні."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title("Виконати команду в консолі?"),
            rx.code_block(State.confirm_text, language="powershell",
                          class_name="w-full mt-2"),
            rx.flex(
                rx.button("Відхилити", variant="soft", color_scheme="red",
                          on_click=State.confirm_no),
                rx.button("Виконати", on_click=State.confirm_yes),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.confirm_open,
    )


def sidebar() -> rx.Component:
    return rx.flex(
        nav_item("plus", "Новий чат", State.new_chat),
        nav_item("settings", "Налаштування"),
        rx.text("Recents", class_name="text-xs text-gray-500 uppercase tracking-wide "
                                       "mt-5 mb-1 px-2"),
        rx.vstack(
            rx.cond(
                State.sessions,
                rx.foreach(State.sessions, session_item),
                rx.text("Поки немає чатів", class_name="text-sm text-gray-400 px-2"),
            ),
            class_name="flex-1 w-full gap-0.5 overflow-y-auto",
        ),
        rx.spacer(),
        rx.hstack(
            rx.box("М", class_name="w-7 h-7 rounded-full bg-white/10 text-gray-200 "
                                   "flex items-center justify-center text-xs"),
            rx.text("Микола", class_name="text-sm text-gray-200"),
            class_name="items-center gap-2 px-2 py-2 mt-2 border-t " + BORDER,
        ),
        direction="column",
        class_name="w-64 h-full p-2 gap-0.5",
        style={"backgroundColor": PANEL, "borderRight": "1px solid rgba(255,255,255,0.08)"},
    )


def folder_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.trigger(
            rx.button(rx.icon("folder", size=14), State.folder_name, type="button",
                      variant="ghost", size="1", class_name="text-gray-300 gap-1"),
        ),
        rx.dialog.content(
            rx.dialog.title("Робоча тека проєкту"),
            rx.input(placeholder="Абсолютний шлях до папки",
                     value=State.folder_input, on_change=State.set_folder_input,
                     class_name="w-full mt-2"),
            rx.flex(
                rx.dialog.close(rx.button("Скасувати", variant="soft")),
                rx.dialog.close(rx.button("Зберегти", on_click=State.save_folder)),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
    )


def ref_dialog() -> rx.Component:
    return rx.dialog.root(
        rx.dialog.trigger(
            rx.button(rx.icon("plus", size=12), "файл", type="button", variant="ghost",
                      size="1", class_name="text-gray-400"),
        ),
        rx.dialog.content(
            rx.dialog.title("Файл-джерело (read-only)"),
            rx.input(placeholder="Абсолютний шлях до файлу",
                     value=State.ref_input, on_change=State.set_ref_input,
                     class_name="w-full mt-2"),
            rx.flex(
                rx.dialog.close(rx.button("Скасувати", variant="soft")),
                rx.dialog.close(rx.button("Додати", on_click=State.add_ref)),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
    )


def controls_bar() -> rx.Component:
    return rx.hstack(
        rx.button(rx.icon("paperclip", size=15), type="button", variant="ghost", size="1",
                  class_name="text-gray-400"),
        folder_dialog(),
        rx.hstack(
            rx.text("правки", class_name="text-xs text-gray-500"),
            rx.select(["ask", "auto"], value=State.edits, on_change=State.set_edits,
                      size="1", variant="soft", width="5rem"),
            class_name="items-center gap-1",
        ),
        rx.hstack(
            rx.text("консоль", class_name="text-xs text-gray-500"),
            rx.select(["allowlist", "ask", "off"], value=State.shell, on_change=State.set_shell,
                      size="1", variant="soft", width="6.2rem"),
            class_name="items-center gap-1",
        ),
        rx.spacer(),
        rx.text("план наперед", class_name="text-xs text-gray-500"),
        rx.switch(checked=State.plan_first, on_change=State.set_plan_first, size="1"),
        rx.button(
            rx.cond(State.busy, rx.spinner(size="1"), rx.icon("arrow-up", size=16)),
            type="submit", disabled=State.busy,
            size="1", radius="full", class_name="bg-white text-black ml-1"),
        class_name="w-full items-center mt-2 gap-2",
    )


def references_row() -> rx.Component:
    return rx.hstack(
        rx.icon("lock", size=12, class_name="text-gray-500"),
        rx.text("джерела:", class_name="text-xs text-gray-500"),
        rx.foreach(
            State.references,
            lambda r: rx.hstack(
                rx.text(r, class_name="text-xs text-gray-300 truncate max-w-40"),
                rx.icon("x", size=11, class_name="text-gray-500 cursor-pointer",
                        on_click=lambda: State.remove_ref(r)),
                class_name="items-center gap-1 px-1.5 py-0.5 rounded bg-white/5",
            ),
        ),
        ref_dialog(),
        class_name="items-center gap-2 mt-1 flex-wrap",
    )


def input_box() -> rx.Component:
    return rx.form(
        rx.box(
            rx.text_area(
                placeholder="Опишіть задачу...  (Enter — надіслати, Shift+Enter — новий рядок)",
                value=State.task,
                on_change=State.set_task,
                enter_key_submit=True,
                class_name="w-full bg-transparent text-gray-100 placeholder:text-gray-500 "
                           "resize-none outline-none border-none text-base",
                rows="2",
            ),
            controls_bar(),
            references_row(),
            class_name="w-full max-w-2xl rounded-2xl p-3 border " + BORDER,
            style={"backgroundColor": INPUT},
        ),
        on_submit=State.send_task,
        reset_on_submit=False,
        class_name="w-full flex justify-center",
    )


def message_bubble(m: dict) -> rx.Component:
    mine = m["role"] == "user"
    return rx.box(
        rx.cond(
            mine,
            rx.text(m["content"], class_name="whitespace-pre-wrap text-gray-100 text-sm"),
            rx.box(
                rx.cond(
                    m["thinking"] != "",
                    rx.el.details(
                        rx.el.summary(
                            rx.cond(m["kind"] == "step", "хід виконання", "Роздуми моделі"),
                            class_name="text-xs text-gray-500 cursor-pointer select-none"),
                        rx.markdown(m["thinking"]),
                        class_name="mb-2 border-l-2 border-white/10 pl-2 max-h-80 overflow-y-auto",
                    ),
                    rx.fragment(),
                ),
                rx.markdown(m["content"]),
                class_name="text-sm",
            ),
        ),
        class_name=rx.cond(mine, "self-end bg-white/10", "self-start bg-white/[0.03]")
        + " rounded-2xl px-4 py-2.5 max-w-[85%]",
    )


def pending_bar() -> rx.Component:
    return rx.hstack(
        rx.button("Виконати план", on_click=State.execute_pending,
                  class_name="bg-white text-black rounded-lg px-3 py-1.5 text-sm"),
        rx.button("Відхилити", on_click=State.discard_pending, variant="soft", size="2"),
        class_name="self-start gap-2 mt-1",
    )


def status_line() -> rx.Component:
    return rx.cond(
        State.busy & ~State.streaming,
        rx.hstack(
            rx.spinner(size="1"),
            rx.text(State.status, class_name="text-sm text-gray-400 italic"),
            class_name="self-start items-center gap-2 px-1",
        ),
        rx.fragment(),
    )


def streaming_bubble() -> rx.Component:
    """Тимчасова бульбашка під час стріму: роздуми під катом + контент + живий
    лічильник токенів/швидкості, що оновлюється по ходу генерації."""
    return rx.box(
        rx.cond(
            State.stream_thinking != "",
            rx.el.details(
                rx.el.summary("Роздуми моделі",
                              class_name="text-xs text-gray-500 cursor-pointer select-none"),
                rx.markdown(State.stream_thinking),
                class_name="mb-2 border-l-2 border-white/10 pl-2 max-h-80 overflow-y-auto",
                open=True,
            ),
            rx.fragment(),
        ),
        rx.markdown(State.stream_content),
        rx.hstack(
            rx.spinner(size="1"),
            rx.text(
                "↑" + State.stream_prompt_tokens.to_string() + " ctx · ↓"
                + State.stream_out_tokens.to_string() + " out · "
                + State.stream_tps.to_string() + " tok/s",
                class_name="text-xs text-gray-500 font-mono"),
            class_name="items-center gap-2 mt-1",
        ),
        class_name="self-start bg-white/[0.03] rounded-2xl px-4 py-2.5 max-w-[85%] text-sm",
    )


def chat_view() -> rx.Component:
    return rx.vstack(
        rx.foreach(State.messages, message_bubble),
        rx.cond(State.streaming, streaming_bubble(), rx.fragment()),
        status_line(),
        rx.cond(State.has_pending, pending_bar(), rx.fragment()),
        class_name="w-full max-w-2xl mx-auto flex-1 overflow-y-auto px-4 py-6 gap-3",
    )


def main_area() -> rx.Component:
    return rx.cond(
        State.messages,
        rx.vstack(
            chat_view(),
            rx.box(input_box(), class_name="w-full px-4 pb-4 flex justify-center"),
            class_name="flex-1 h-full w-full",
        ),
        rx.center(
            rx.vstack(
                rx.heading(
                    rx.cond(State.has_current, State.title, "Back at it, Микола"),
                    class_name="text-4xl text-gray-200 mb-2", style=SERIF,
                ),
                input_box(),
                spacing="5",
                class_name="w-full max-w-2xl items-center px-4",
            ),
            class_name="flex-1 h-full",
        ),
    )


def index() -> rx.Component:
    return rx.hstack(
        sidebar(),
        main_area(),
        rename_dialog(),
        confirm_dialog(),
        class_name="h-screen w-screen overflow-hidden",
        style={"backgroundColor": BG},
        spacing="0",
    )


app = rx.App(
    theme=rx.theme(appearance="dark"),
    stylesheets=["https://fonts.googleapis.com/css2?family=Newsreader:ital@0;1&display=swap"],
)
app.add_page(index, title="local-code-agent", on_load=State.load_sessions)
