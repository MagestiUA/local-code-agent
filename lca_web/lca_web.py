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
from reflex.components.radix.themes.color_mode import set_color_mode

from agent import config
from agent import convo
from agent import session as sess
from agent.agent_loop import run_step
from agent.answerer import answer_stream, build_context
from agent.intent import classify_intent
from agent.llm import OllamaClient
from agent.memory import render_for_planner
from agent.planner import deliberate
from agent.project import load_project_doc, scan_structure
from agent.toolkit import ToolContext, default_registry

CHAT_SYSTEM = (
    "You are a helpful thinking assistant. You can search the web with the web_search "
    "tool when you need current or external information. Answer in the user's language."
)

_CLIENT = None

# Очікувані confirm-и shell=ask, поза State (threading.Event непіклиться -> інакше
# StateSerializationError). Ключ — id сесії. {sid: {"event": Event, "ok": bool}}
_CONFIRMS: dict = {}

# Stop-події для переривання стріму/виконання. Ключ — id сесії.
_STOP_EVENTS: dict = {}

# Вміст прикріплених файлів поза State (великі рядки не сериалізуємо).
# {session_id: {filename: content_str}}
_ATTACHMENT_CONTENT: dict = {}


def get_client() -> OllamaClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = OllamaClient()
    return _CLIENT


SCROLL_SETUP_JS = """
(() => {
  const c = document.getElementById('chat-scroll');
  if (!c || c._lcaObs) return;
  let stick = true;
  const nearBottom = () => c.scrollHeight - c.scrollTop - c.clientHeight < 120;
  c.addEventListener('scroll', () => { stick = nearBottom(); });
  const obs = new MutationObserver(() => { if (stick) c.scrollTop = c.scrollHeight; });
  obs.observe(c, {childList: true, subtree: true, characterData: true});
  c._lcaObs = obs;
  c.scrollTop = c.scrollHeight;
})();
"""
SCROLL_BOTTOM_JS = "var c=document.getElementById('chat-scroll'); if(c){c.scrollTop=c.scrollHeight;}"


BG = "#262624"
PANEL = "#171615"
INPUT = "#30302e"
BORDER = "border-white/10"
SERIF = {"fontFamily": "Newsreader, Georgia, serif"}


class State(rx.State):
    sessions: list[dict] = []
    mode: str = "code"                     # активний режим: "code" (агент) | "chat" (легкий чат)
    current_id: str = ""
    title: str = ""
    project_root: str = ""
    edits: str = "ask"
    shell: str = "smart"
    plan_first: bool = False
    references: list[str] = []
    folder_input: str = ""
    ref_input: str = ""
    task: str = ""
    messages: list[dict] = []
    has_pending: bool = False
    queued_text: str = ""                  # повідомлення, що чекає, поки модель зайнята
    stopping: bool = False                 # стоп запитано, чекаємо переривання
    context_summary: str = ""              # контекст-памʼять розмови (підсумок)
    # Вкладення (#10) — тільки метадані, вміст у _ATTACHMENT_CONTENT
    attachments: list[dict] = []
    # Налаштування (#9)
    settings_open: bool = False
    theme: str = "dark"
    font_chat: int = 14
    font_ui: int = 13
    # планувальник-діалог (#2+#5): питання/вибір, що очікує відповіді
    has_question: bool = False
    q_text: str = ""
    q_reasoning: str = ""
    q_options: list[dict] = []
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
    # лічильник токенів поточного запиту (над полем вводу; скидається на новий запит)
    tok_prompt: int = 0
    tok_out: int = 0
    tok_eval_ns: int = 0
    tok_tps: float = 0.0

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

    # ── Лічильник токенів (викликати лише всередині `async with self`) ─────────
    def _reset_tokens(self):
        self.tok_prompt = self.tok_out = self.tok_eval_ns = 0
        self.tok_tps = 0.0

    def _recompute_tps(self):
        self.tok_tps = round(self.tok_out / (self.tok_eval_ns / 1e9), 1) if self.tok_eval_ns else 0.0

    def _add_stats(self, stats: list[dict]):
        self.tok_prompt += sum(s.get("prompt", 0) for s in stats)
        self.tok_out += sum(s.get("out", 0) for s in stats)
        self.tok_eval_ns += sum(s.get("eval_ns", 0) for s in stats)
        self._recompute_tps()

    @rx.var
    def tokens_label(self) -> str:
        # ctx (prompt_eval_count) Ollama віддає лише у фінальному чанку — поки невідомо
        # й модель ще працює, показуємо «…», а не оманливий 0.
        ctx = "…" if (self.busy and self.tok_prompt == 0) else str(self.tok_prompt)
        return f"↑{ctx} ctx · ↓{self.tok_out} out · {self.tok_tps} tok/s"

    @rx.var
    def ui_font_js(self) -> str:
        """JS, що масштабує кореневий шрифт <html> — усі rem-based Tailwind-розміри
        (text-xs/sm/4xl) інтерфейсу скейляться пропорційно. Чат має власний px-розмір."""
        return f"document.documentElement.style.fontSize='{self.font_ui}px'"

    @rx.var
    def chat_font_px(self) -> str:
        """Розмір шрифту повідомлень чату в px (абсолютний, не залежить від font_ui)."""
        return f"{self.font_chat}px"

    @rx.var
    def folder_name(self) -> str:
        return Path(self.project_root).name if self.project_root else "тека"

    @rx.var
    def has_current(self) -> bool:
        return self.current_id != ""

    @rx.var
    def is_chat(self) -> bool:
        return self.mode == "chat"

    @rx.var
    def visible_sessions(self) -> list[dict]:
        """Чати поточного режиму (Чат/Код роздільні списки)."""
        return [s for s in self.sessions if s.get("kind", "code") == self.mode]

    def _clear_attachments(self):
        """Скинути вкладення поточного запиту: chips зі State + вміст із модуль-дикту."""
        _ATTACHMENT_CONTENT.pop(self.current_id, None)
        self.attachments = []

    def _clear_view(self):
        _ATTACHMENT_CONTENT.pop(self.current_id, None)
        self.current_id = ""
        self.title = ""
        self.project_root = ""
        self.messages = []
        self.references = []
        self.has_pending = False
        self.queued_text = ""
        self.context_summary = ""
        self.attachments = []
        self._load_question(None)

    @rx.event
    def set_mode(self, m: str):
        if m == self.mode:
            return
        self.mode = m
        cur = next((s for s in self.sessions if s["id"] == self.current_id), None)
        if not cur or cur.get("kind", "code") != m:   # відкритий чат іншого типу -> згорнути
            self._clear_view()

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
        self.mode = s.kind
        self.title = s.title
        self.project_root = s.project_root
        self.edits = s.permissions.get("edits", "ask")
        self.shell = s.permissions.get("shell", "smart")
        self.plan_first = s.plan_first
        self.references = list(s.reference_files)
        self.messages = [{"thinking": "", **m} for m in s.messages]
        self.has_pending = s.pending_plan is not None
        self.context_summary = s.context_summary
        self._load_question(s.pending_question)

    def _load_question(self, q: dict | None):
        """Відновити стан питання планувальника зі збереженого pending_question."""
        if q:
            self.has_question = True
            self.q_text = q.get("question", "")
            self.q_reasoning = q.get("reasoning", "")
            self.q_options = list(q.get("options", []))
        else:
            self.has_question = False
            self.q_text = ""
            self.q_reasoning = ""
            self.q_options = []

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
        s = sess.new_session("Новий чат", "", kind=self.mode)
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
            self._clear_view()
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

    # ── Налаштування (#9) ──────────────────────────────────────────────────────
    @rx.event
    def toggle_settings(self):
        self.settings_open = not self.settings_open

    @rx.event
    def load_settings(self):
        from agent import settings as S
        d = S.load()
        self.theme = d["theme"]
        self.font_chat = d["font_chat"]
        self.font_ui = d["font_ui"]
        # застосувати збережену тему + масштаб шрифту інтерфейсу на старті
        return [set_color_mode(self.theme), rx.call_script(self.ui_font_js)]

    def _save_settings(self):
        from agent import settings as S
        S.save({"theme": self.theme, "font_chat": self.font_chat, "font_ui": self.font_ui})

    @rx.event
    def set_theme(self, v: str):
        self.theme = v
        self._save_settings()
        return set_color_mode(v)                    # реально перемкнути світлу/темну/авто

    @rx.event
    def set_font_chat(self, v: int):
        self.font_chat = max(12, min(20, int(v)))
        self._save_settings()

    @rx.event
    def set_font_ui(self, v: int):
        self.font_ui = max(11, min(16, int(v)))
        self._save_settings()
        return rx.call_script(self.ui_font_js)     # застосувати масштаб інтерфейсу негайно

    # ── Вкладення (#10) ────────────────────────────────────────────────────────
    @rx.event
    async def handle_upload(self, files: list[rx.UploadFile]):
        from agent.attachments import process, MAX_FILES
        cid = self.current_id or "_tmp"
        bucket = _ATTACHMENT_CONTENT.setdefault(cid, {})
        for file in files:
            if len(self.attachments) >= MAX_FILES:
                break
            if any(a["name"] == file.filename for a in self.attachments):
                continue                           # вже є — пропустити дублікат
            data = await file.read()
            result = process(file.filename, data)
            content = result.pop("_content", "")  # вміст окремо від metadata
            if content:
                bucket[file.filename] = content
            self.attachments = self.attachments + [result]

    @rx.event
    def remove_attachment(self, name: str):
        cid = self.current_id or "_tmp"
        _ATTACHMENT_CONTENT.get(cid, {}).pop(name, None)
        self.attachments = [a for a in self.attachments if a["name"] != name]

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
        from agent.attachments import find_for_step, format_single
        ctx = self._exec_ctx(root, client)
        async with self:
            att_meta = list(self.attachments)
            cid = self.current_id
        att_bucket = _ATTACHMENT_CONTENT.get(cid, {})
        stop_ev = _STOP_EVENTS.get(cid)
        stopped = False
        for step in plan.steps:
            if stop_ev is not None and stop_ev.is_set():       # перервано між кроками
                stopped = True
                break
            async with self:
                self.status = f"Крок #{step.id}: {step.description[:50]}"
            if step.kind == "inline":
                plan.set_result(step.id, "done", "inline")
                continue
            step_ctx = f"Project root: {root}"
            if self.context_summary:
                step_ctx += f"\n{convo.as_context(self.context_summary)}"
            # Додаємо вміст одного файлу, якщо він згаданий у кроці (map-reduce)
            fname = find_for_step(step.description, att_meta)
            if fname and fname in att_bucket:
                meta = next((a for a in att_meta if a["name"] == fname), {})
                step_ctx += "\n" + format_single(fname, att_bucket[fname],
                                                 meta.get("truncated", False))
            final, log = await self._run_tool_step(step.description, ctx,
                                                   title=step.description[:60],
                                                   context=step_ctx, stop_event=stop_ev)
            plan.set_result(step.id, "done" if log else "failed", final or "—")
        async with self:
            self._append("assistant", "⛔ Зупинено" if stopped else "Готово ✓", "note")
            self._clear_attachments()                  # вкладення спожиті/скинуті
        return render_for_planner(plan)               # outcome для фонового підсумку

    def _exec_ctx(self, root: str, client) -> ToolContext:
        """ToolContext для виконання: edits=auto (план/запит уже схвалено),
        shell передаємо як є; для shell=ask confirm показує модальний попап."""
        return ToolContext(root=Path(root),
                           permissions={"edits": "auto", "shell": self.shell},
                           client=client,
                           confirm=self._make_confirm(asyncio.get_running_loop()))

    async def _run_tool_step(self, step_text: str, ctx: ToolContext, title: str = "",
                             context: str = "", stop_event=None):
        """Один прохід tool-loop. У чат додає ОДНЕ повідомлення на крок: видимий
        заголовок + короткий підсумок, а всі дії моделі (списки файлів, diff-и,
        вивід команд) ховає під кат «хід виконання» (kind=step). Повертає (final, log)."""
        actions: list[tuple[str, str]] = []   # (tool, result) — заповнює on_tool у потоці
        stats: list[dict] = []                # client.last_stats кожного виклику моделі
        final, log = await asyncio.to_thread(
            lambda: run_step(step_text, ctx, ctx.client, context=context, stats_sink=stats,
                             on_tool=lambda n, a, r: actions.append((n, r)),
                             stop_event=stop_event))

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
        async with self:
            self._add_stats(stats)                       # лічильник над полем вводу
            self._append("assistant", content, "step", thinking=log_md)
        return final, log

    async def _run_stream(self, gen):
        """Прокрутити стрім із живим лічильником токенів. Повідомлення НЕ додає —
        повертає (body, thinking, tool_calls). Спільне для answer і chat-режиму.
        Підтримує переривання через _STOP_EVENTS[current_id]."""
        async with self:
            self.streaming = True
            self.stream_content = ""
            self.stream_thinking = ""
            self.status = ""
            base_out = self.tok_out
            cid = self.current_id
        t0 = time.time()
        stats = None
        tool_calls = None
        counted = 0
        while True:
            if _STOP_EVENTS.get(cid, None) and _STOP_EVENTS[cid].is_set():
                break
            ev = await asyncio.to_thread(lambda g=gen: next(g, None))
            if ev is None:
                break
            if ev["done"]:
                stats = ev.get("stats")
                tool_calls = ev.get("tool_calls")
                break
            async with self:
                self.stream_content += ev["content"]
                self.stream_thinking += ev["thinking"]
                if ev["content"]:
                    counted += 1
                    self.tok_out = base_out + counted
                    el = time.time() - t0
                    if el > 0:
                        self.tok_tps = round(self.tok_out / el, 1)
        async with self:
            if stats:
                self.tok_prompt += stats.get("prompt", 0)
                self.tok_out = base_out + stats.get("out", counted)
                self.tok_eval_ns += stats.get("eval_ns", 0)
                self._recompute_tps()
            body = (self.stream_content or "").strip()
            thinking = self.stream_thinking
            self.streaming = False
            self.stopping = False
            _STOP_EVENTS.pop(cid, None)
        return body, thinking, tool_calls

    async def _stream_answer(self, text: str, ctx: str, client):
        """Стрімить відповідь answer-режиму (без тулів)."""
        body, thinking, _ = await self._run_stream(answer_stream(text, ctx, client))
        async with self:
            self._append("assistant", body, "answer", thinking=thinking)
        return body

    async def _chat_reply(self, text: str, client) -> str:
        """Чат-режим: стрім думаючої моделі + web_search. Стрімимо з тулами; якщо у
        фінальному чанку є tool_calls — виконуємо пошук, дописуємо й стрімимо далі."""
        reg = default_registry()
        ws = [t for t in reg.schema() if t["function"]["name"] == "web_search"]
        tctx = ToolContext(root=Path.home(), permissions={"edits": "off", "shell": "off"})
        async with self:
            history = [{"role": m["role"], "content": m["content"]}
                       for m in self.messages
                       if m["role"] in ("user", "assistant")
                       and m.get("kind", "text") in ("text", "answer") and m["content"]][-20:]
        msgs = [{"role": "system", "content": CHAT_SYSTEM}] + history
        body = ""
        for _ in range(3):
            body, thinking, tool_calls = await self._run_stream(
                client.chat_stream(msgs, tools=ws, profile=config.PLANNER))
            if not tool_calls:
                async with self:
                    self._append("assistant", body, "answer", thinking=thinking)
                break
            msgs.append({"role": "assistant", "content": body, "tool_calls": tool_calls})
            for call in tool_calls:
                args = call.get("function", {}).get("arguments", {})
                if isinstance(args, str):
                    import json as _json
                    try:
                        args = _json.loads(args)
                    except Exception:
                        args = {}
                query = args.get("query", "")
                async with self:
                    self.status = f"Шукаю: {query}"
                result = await asyncio.to_thread(lambda a=args: reg.dispatch("web_search", a, tctx))
                msgs.append({"role": "tool", "content": result})
                async with self:
                    self._append("assistant", f"🔎 {query}", "step", thinking=result)
        return body

    async def _update_summary(self, user_text: str, outcome: str):
        """Оновлення контекст-памʼяті (LLM-підсумок) після завершеного запиту. Модель
        однопотокова, тож це триває в межах busy; нові повідомлення стають у чергу
        (queued_text) і виконаються після — вже зі свіжим стисненим контекстом.
        cid-гард: якщо користувач перемкнув чат, пишемо в сесію-походження."""
        if not (outcome or "").strip():
            return
        client = await asyncio.to_thread(get_client)
        async with self:
            cur = self.context_summary
            cid = self.current_id
        new = await asyncio.to_thread(
            lambda: convo.update_summary(cur, user_text, outcome, client))
        async with self:
            if self.current_id == cid:                # та сама сесія — оновити й у State
                self.context_summary = new
            if cid:                                   # у файл — завжди в сесію-походження
                s = sess.load_session(cid)
                s.context_summary = new
                sess.save_session(s)

    @rx.event
    def stop_generation(self):
        """Перервати поточну генерацію: встановити stop_event для активної сесії."""
        import threading
        cid = self.current_id
        if not cid:
            return
        ev = _STOP_EVENTS.get(cid)
        if ev:
            ev.set()
        else:
            # якщо stop_event ще не зареєстровано — створюємо вже встановленим
            e = threading.Event()
            e.set()
            _STOP_EVENTS[cid] = e
        self.stopping = True

    @rx.event(background=True)
    async def send_task(self, form_data: dict | None = None):
        """Надсилання. Модель однопотокова: якщо зайнята (busy) — повідомлення стає в
        чергу (додається до наявного) і виконається після з уже свіжим контекстом."""
        async with self:
            if not self.current_id:
                s = sess.new_session("Новий чат", "", kind=self.mode)
                sess.save_session(s)
                self.sessions = sess.list_sessions()
                self._select(s.id)
                if "_tmp" in _ATTACHMENT_CONTENT:       # перенести пре-сесійні вкладення
                    _ATTACHMENT_CONTENT[s.id] = _ATTACHMENT_CONTENT.pop("_tmp")
            if self.mode != "chat" and not self.project_root:
                self._append("assistant", "Спершу вкажіть робочу теку.", "note")
                return
            text = self.task.strip()
            if not text:
                return
            self.task = ""
            self._append("user", text)
            if self.busy:                              # модель зайнята -> у чергу
                self.queued_text = (self.queued_text + "\n\n" + text).strip() if self.queued_text else text
                return
            self.busy = True
        await self._process_one(text)
        await self._drain_and_unbusy()

    async def _drain_and_unbusy(self):
        """Після завершення запиту: якщо в черзі є повідомлення — виконати його (busy
        тримається), інакше зняти busy. Перевірка + зняття — атомарні, без втрати черги."""
        while True:
            async with self:
                if self.queued_text:
                    nxt = self.queued_text
                    self.queued_text = ""
                    self.status = "Обробка запиту з черги…"
                else:
                    self.busy = False
                    self.status = ""
                    self.stopping = False                 # скидаємо стоп-стан на всіх шляхах
                    _STOP_EVENTS.pop(self.current_id, None)
                    return
            await self._process_one(nxt)

    async def _process_one(self, text: str):
        """Обробити ОДНЕ повідомлення (user-бульбашку вже додано). Включає фінальний
        LLM-підсумок контексту. busy не чіпає — ним керує send_task/_drain_and_unbusy."""
        async with self:
            self._reset_tokens()
            # Свіжа стоп-подія на кожен запит, щоб stop_generation і виконавець кроків
            # завжди працювали з одним і тим самим обʼєктом (а не None, захопленим зарано).
            _STOP_EVENTS[self.current_id] = threading.Event()
            chat_mode = self.mode == "chat"
            root = self.project_root
            refs = [str(x) for x in self.references]
            plan_first = self.plan_first or self.edits == "ask"
            summary = self.context_summary
            answering = self.has_question
            if answering:
                self.has_question = False
                self.status = "Обмірковую відповідь…"
            else:
                self.status = "Думаю…" if chat_mode else "Визначаю тип запиту…"

        client = await asyncio.to_thread(get_client)
        su, so = text, ""

        if chat_mode:
            so = await self._chat_reply(text, client)
        elif answering:
            su, so = await self._resume_planning(text, root, plan_first, client)
        else:
            mode = await asyncio.to_thread(lambda: classify_intent(text, client))
            if mode == "answer":
                async with self:
                    self.status = "Аналізую код…"
                ctx = await asyncio.to_thread(lambda: build_context(root, refs, text))
                so = await self._stream_answer(text, convo.as_context(summary) + ctx, client)
            elif mode == "shell":
                async with self:
                    self.status = "Виконую запит…"
                ctx = self._exec_ctx(root, client)
                shell_ctx = f"Project root: {root}\n{convo.as_context(summary)}" if summary else f"Project root: {root}"
                final, _ = await self._run_tool_step(text, ctx, context=shell_ctx,
                                                     stop_event=_STOP_EVENTS.get(self.current_id))
                so = final or "виконано дії інструментами"
            else:  # plan / edit -> планувальник-діалог
                async with self:
                    self.status = "Обмірковую план…"
                doc = await asyncio.to_thread(lambda: load_project_doc(root))
                struct = await asyncio.to_thread(lambda: scan_structure(root))
                su, so = await self._deliberate_flow(
                    text, convo.as_context(summary) + struct, doc, [], root, plan_first, client)

        if (so or "").strip():
            async with self:
                self.status = "Оновлюю памʼять…"
            await self._update_summary(su, so)
        # Вкладення спожиті — чистимо, якщо запит не на паузі (план/уточнення триває)
        async with self:
            if not (self.has_pending or self.has_question):
                self._clear_attachments()

    @rx.event(background=True)
    async def answer_planner(self, value: str):
        """Відповідь на питання планувальника кліком по кандидату."""
        async with self:
            if not self.has_question or self.busy or not self.current_id:
                return
            self.busy = True
            self._reset_tokens()
            _STOP_EVENTS[self.current_id] = threading.Event()   # свіжа стоп-подія на запит
            self._append("user", value)
            self.has_question = False
            self.status = "Обмірковую відповідь…"
            root = self.project_root
            plan_first = self.plan_first or self.edits == "ask"
        client = await asyncio.to_thread(get_client)
        task, outcome = await self._resume_planning(value, root, plan_first, client)
        if (outcome or "").strip():
            async with self:
                self.status = "Оновлюю памʼять…"
            await self._update_summary(task, outcome)
        await self._drain_and_unbusy()

    async def _resume_planning(self, value: str, root: str, plan_first: bool, client):
        """Продовжити планування після відповіді користувача: додати її в history,
        повторити deliberate. Повертає (task, outcome)."""
        async with self:
            s = sess.load_session(self.current_id)
            q = s.pending_question or {}
        history = list(q.get("history") or []) + [{"question": q.get("question", ""), "answer": value}]
        task = q.get("task", value)
        return await self._deliberate_flow(task, q.get("context", ""), q.get("doc"),
                                           history, root, plan_first, client)

    async def _deliberate_flow(self, task: str, context: str, doc, history: list,
                               root: str, plan_first: bool, client):
        """Один хід планувальника-діалогу: clarify/choose -> питання+кнопки (пауза);
        plan -> готовий план (виконати або показати на підтвердження).
        Повертає (task, outcome) — outcome='' якщо пауза/не виконано (підсумок не потрібен)."""
        from agent.attachments import attachment_summary
        async with self:
            att_meta = list(self.attachments)
        att_ctx = attachment_summary(att_meta)
        full_context = (context + "\n\n" + att_ctx).strip() if att_ctx else context
        result = await asyncio.to_thread(
            lambda: deliberate(task, full_context, client, doc, history))
        action = result["action"]

        if action in ("clarify", "choose"):
            q = {"task": task, "context": context, "doc": doc, "history": history,
                 "question": result["question"], "reasoning": result["reasoning"],
                 "options": result["options"]}
            async with self:
                head = (result["reasoning"] + "\n\n") if result["reasoning"] else ""
                self._append("assistant", f"{head}**{result['question']}**", "plan")
                self.q_text = result["question"]
                self.q_reasoning = result["reasoning"]
                self.q_options = result["options"]
                self.has_question = True
                s = sess.load_session(self.current_id)
                s.set_pending_question(q)
                s.clear_pending_plan()
                sess.save_session(s)
            return task, ""

        # action == "plan"
        state = result["state"]
        async with self:
            s = sess.load_session(self.current_id)
            s.clear_pending_question()
            sess.save_session(s)
            self._load_question(None)
        if not state or not state.steps:
            async with self:
                self._append("assistant", "Не вдалося скласти план.", "note")
            return task, ""
        if plan_first:
            async with self:
                self._append("assistant", render_for_planner(state), "plan")
                s = sess.load_session(self.current_id)
                s.set_pending_plan(state)
                sess.save_session(s)
                self.has_pending = True
            return task, ""                           # виконається пізніше -> підсумок там
        outcome = await self._execute_steps(state, root, client)
        return task, outcome

    @rx.event(background=True)
    async def execute_pending(self):
        async with self:
            if not self.has_pending or not self.current_id or self.busy:
                return
            self.busy = True
            self.status = "Виконую план…"
            _STOP_EVENTS[self.current_id] = threading.Event()   # свіжа стоп-подія на запит
            root, cid = self.project_root, self.current_id

        client = await asyncio.to_thread(get_client)
        s = sess.load_session(cid)
        plan = s.get_pending_plan()
        outcome = await self._execute_steps(plan, root, client)

        async with self:
            s2 = sess.load_session(self.current_id)
            s2.clear_pending_plan()
            sess.save_session(s2)
            self.has_pending = False
        if (outcome or "").strip():
            async with self:
                self.status = "Оновлюю памʼять…"
            await self._update_summary(plan.task, outcome)
        await self._drain_and_unbusy()

    @rx.event
    def discard_pending(self):
        if self.current_id:
            s = sess.load_session(self.current_id)
            s.clear_pending_plan()
            sess.save_session(s)
        self.has_pending = False

    @rx.event
    def cancel_question(self):
        if self.current_id:
            s = sess.load_session(self.current_id)
            s.clear_pending_question()
            sess.save_session(s)
        self._load_question(None)


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


def mode_switch() -> rx.Component:
    """Верхній перемикач режимів Чат | Код."""
    def tab(label: str, m: str, icon: str) -> rx.Component:
        active = State.mode == m
        return rx.hstack(
            rx.icon(icon, size=14),
            rx.text(label, class_name="text-sm"),
            on_click=lambda: State.set_mode(m),
            class_name=rx.cond(active, "bg-white/10 text-gray-100", "text-gray-400 hover:bg-white/5")
            + " items-center justify-center gap-1.5 grow rounded-md py-1.5 cursor-pointer",
        )
    return rx.hstack(
        tab("Чат", "chat", "message-circle"),
        tab("Код", "code", "code"),
        class_name="w-full gap-1 p-1 rounded-lg bg-white/[0.03] mb-1",
    )


def settings_dialog() -> rx.Component:
    def font_ctrl(label, value, on_dec, on_inc, min_v, max_v):
        return rx.hstack(
            rx.text(label, class_name="text-sm text-gray-300 w-36"),
            rx.icon_button(rx.icon("minus", size=12), on_click=on_dec,
                           size="1", variant="soft",
                           disabled=value <= min_v),
            rx.text(value, class_name="text-sm text-gray-100 w-6 text-center"),
            rx.icon_button(rx.icon("plus", size=12), on_click=on_inc,
                           size="1", variant="soft",
                           disabled=value >= max_v),
            class_name="items-center gap-2",
        )

    return rx.dialog.root(
        rx.dialog.content(
            rx.vstack(
                rx.hstack(
                    rx.heading("Налаштування", size="4"),
                    rx.spacer(),
                    rx.dialog.close(
                        rx.icon_button(rx.icon("x", size=16), variant="ghost", size="1"),
                    ),
                    class_name="w-full items-center mb-4",
                ),
                # Тема
                rx.text("Тема", class_name="text-xs text-gray-500 uppercase tracking-wide mb-1"),
                rx.segmented_control.root(
                    rx.segmented_control.item("Світла", value="light"),
                    rx.segmented_control.item("Темна", value="dark"),
                    rx.segmented_control.item("Авто", value="system"),
                    value=State.theme,
                    on_change=State.set_theme,
                    size="1", class_name="mb-4",
                ),
                # Шрифти
                rx.text("Шрифти", class_name="text-xs text-gray-500 uppercase tracking-wide mb-2"),
                font_ctrl("Чат (повідомлення)", State.font_chat,
                          State.set_font_chat(State.font_chat - 1),
                          State.set_font_chat(State.font_chat + 1), 12, 20),
                font_ctrl("Інтерфейс", State.font_ui,
                          State.set_font_ui(State.font_ui - 1),
                          State.set_font_ui(State.font_ui + 1), 11, 16),
                gap="2", class_name="min-w-80",
            ),
            style={"backgroundColor": PANEL},
        ),
        open=State.settings_open,
        on_open_change=State.toggle_settings,
    )


def sidebar() -> rx.Component:
    return rx.flex(
        settings_dialog(),
        mode_switch(),
        nav_item("plus", "Новий чат", State.new_chat),
        rx.text("Recents", class_name="text-xs text-gray-500 uppercase tracking-wide "
                                       "mt-5 mb-1 px-2"),
        rx.vstack(
            rx.cond(
                State.visible_sessions,
                rx.foreach(State.visible_sessions, session_item),
                rx.text("Поки немає чатів", class_name="text-sm text-gray-400 px-2"),
            ),
            class_name="flex-1 w-full gap-0.5 overflow-y-auto",
        ),
        rx.spacer(),
        nav_item("settings", "Налаштування", State.toggle_settings),
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


def _code_controls() -> rx.Component:
    """Контроли лише для Код-режиму: тека, дозволи, план наперед."""
    return rx.hstack(
        folder_dialog(),
        rx.hstack(
            rx.text("правки", class_name="text-xs text-gray-500"),
            rx.select(["ask", "auto"], value=State.edits, on_change=State.set_edits,
                      size="1", variant="soft", width="5rem"),
            class_name="items-center gap-1",
        ),
        rx.hstack(
            rx.text("консоль", class_name="text-xs text-gray-500"),
            rx.select(
                [rx.select.item("smart", value="smart"),
                 rx.select.item("ask", value="ask"),
                 rx.select.item("auto", value="auto"),
                 rx.select.item("allowlist", value="allowlist"),
                 rx.select.item("off", value="off")],
                value=State.shell, on_change=State.set_shell,
                size="1", variant="soft", width="6.2rem"),
            class_name="items-center gap-1",
        ),
        class_name="items-center gap-2",
    )


def attachment_chips() -> rx.Component:
    """Рядок чіпів прикріплених файлів під textarea."""
    def chip(a: dict) -> rx.Component:
        has_error = a["error"] != ""
        return rx.hstack(
            rx.icon("file", size=12,
                    class_name=rx.cond(has_error, "text-red-400", "text-gray-400")),
            rx.text(a["name"], class_name=rx.cond(has_error, "text-red-300", "text-gray-300")
                    + " text-xs truncate max-w-28"),
            rx.cond(has_error,
                    rx.text(a["error"], class_name="text-xs text-red-400 truncate max-w-24"),
                    rx.fragment()),
            rx.icon("x", size=11,
                    class_name="text-gray-500 hover:text-gray-200 cursor-pointer shrink-0",
                    on_click=lambda: State.remove_attachment(a["name"])),
            class_name="items-center gap-1 px-2 py-0.5 rounded-full border "
                       + rx.cond(has_error, "border-red-500/30 bg-red-900/20",
                                 "border-white/10 bg-white/5"),
        )
    return rx.cond(
        State.attachments,
        rx.hstack(
            rx.foreach(State.attachments, chip),
            class_name="flex-wrap gap-1.5 mt-2",
        ),
        rx.fragment(),
    )


def controls_bar() -> rx.Component:
    return rx.hstack(
        rx.upload(
            rx.icon_button(rx.icon("paperclip", size=15), type="button", variant="ghost",
                           size="1", class_name="text-gray-400"),
            id="file_upload",
            multiple=True,
            on_drop=State.handle_upload(rx.upload_files(upload_id="file_upload")),
            accept={
                "text/plain": [".txt", ".md", ".csv", ".log", ".rst"],
                "text/x-python": [".py", ".pyw", ".pyi"],
                "application/json": [".json", ".jsonc"],
                "text/javascript": [".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"],
                "text/html": [".html", ".htm"],
                "text/css": [".css", ".scss", ".sass"],
                "text/yaml": [".yaml", ".yml"],
                "application/toml": [".toml"],
                "text/x-sh": [".sh", ".bash", ".zsh", ".bat", ".ps1"],
                "application/xml": [".xml", ".svg"],
                "application/sql": [".sql"],
            },
            no_drag=True,
        ),
        rx.cond(State.is_chat, rx.fragment(), _code_controls()),
        rx.spacer(),
        rx.cond(
            State.is_chat,
            rx.hstack(rx.icon("globe", size=14, class_name="text-gray-500"),
                      rx.text("веб-пошук", class_name="text-xs text-gray-500"),
                      class_name="items-center gap-1"),
            rx.hstack(
                rx.text("план наперед", class_name="text-xs text-gray-500"),
                rx.switch(checked=State.plan_first, on_change=State.set_plan_first, size="1"),
                class_name="items-center gap-2",
            ),
        ),
        rx.cond(
            State.busy & (State.queued_text == "") & ~State.stopping,
            # Зайнята + черга порожня + ще не зупиняємось → кнопка Стоп
            rx.button(
                rx.icon("square", size=16),
                on_click=State.stop_generation,
                size="1", radius="full",
                class_name="bg-red-500 text-white ml-1 hover:bg-red-600",
            ),
            # Інакше → submit (send або + в чергу)
            rx.button(
                rx.cond(State.busy, rx.icon("plus", size=16), rx.icon("arrow-up", size=16)),
                type="submit",
                size="1", radius="full", class_name="bg-white text-black ml-1",
            ),
        ),
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


def token_bar() -> rx.Component:
    """Лічильник токенів поточного запиту + індикатор черги — над полем вводу."""
    return rx.cond(
        (State.tok_out > 0) | State.busy,
        rx.hstack(
            rx.cond(State.busy, rx.spinner(size="1"), rx.fragment()),
            rx.text(State.tokens_label, class_name="text-xs text-gray-500 font-mono"),
            rx.cond(
                State.queued_text != "",
                rx.hstack(rx.icon("clock", size=12, class_name="text-amber-400"),
                          rx.text("у черзі", class_name="text-xs text-amber-400"),
                          class_name="items-center gap-1"),
                rx.fragment(),
            ),
            class_name="items-center gap-2 self-start px-1 mb-1",
        ),
        rx.box(class_name="h-0"),
    )


def input_box() -> rx.Component:
    return rx.form(
        rx.vstack(
            token_bar(),
            rx.box(
                rx.text_area(
                    placeholder=rx.cond(
                        State.is_chat,
                        "Напишіть повідомлення…  (Enter — надіслати, Shift+Enter — новий рядок)",
                        "Опишіть задачу...  (Enter — надіслати, Shift+Enter — новий рядок)"),
                    value=State.task,
                    on_change=State.set_task,
                    enter_key_submit=True,
                    class_name="w-full bg-transparent text-gray-100 placeholder:text-gray-500 "
                               "resize-none outline-none border-none text-base",
                    rows="2",
                ),
                controls_bar(),
                rx.cond(State.is_chat, rx.fragment(), references_row()),
                attachment_chips(),
                class_name="w-full rounded-2xl p-3 border " + BORDER,
                style={"backgroundColor": INPUT},
            ),
            class_name="w-full max-w-2xl gap-0",
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
            rx.text(m["content"], class_name="whitespace-pre-wrap text-gray-100",
                    style={"fontSize": State.chat_font_px}),
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
                style={"fontSize": State.chat_font_px},
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


def q_option(opt: dict) -> rx.Component:
    """Клікабельний кандидат/підхід — відповідь на питання планувальника."""
    return rx.box(
        rx.text(opt["label"], class_name="text-sm text-gray-100 font-medium"),
        rx.cond(
            opt["detail"] != "",
            rx.text(opt["detail"], class_name="text-xs text-gray-400"),
            rx.fragment(),
        ),
        on_click=lambda: State.answer_planner(opt["label"]),
        class_name="cursor-pointer rounded-lg px-3 py-2 bg-white/5 hover:bg-white/10 "
                   "border " + BORDER,
    )


def question_bar() -> rx.Component:
    """Питання планувальника: клікабельні кандидати + підказка, що можна й вписати."""
    return rx.vstack(
        rx.hstack(rx.foreach(State.q_options, q_option),
                  class_name="flex-wrap gap-2 items-stretch"),
        rx.hstack(
            rx.text("…або впишіть відповідь у поле нижче",
                    class_name="text-xs text-gray-500"),
            rx.button("Скасувати", on_click=State.cancel_question,
                      variant="ghost", size="1", class_name="text-gray-400"),
            class_name="items-center gap-2",
        ),
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
    """Тимчасова бульбашка під час стріму: роздуми під катом + контент.
    Лічильник токенів — над полем вводу (token_bar)."""
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
        class_name="self-start bg-white/[0.03] rounded-2xl px-4 py-2.5 max-w-[85%] text-sm",
    )


def chat_view() -> rx.Component:
    return rx.vstack(
        rx.foreach(State.messages, message_bubble),
        rx.cond(State.streaming, streaming_bubble(), rx.fragment()),
        status_line(),
        rx.cond(State.has_pending, pending_bar(), rx.fragment()),
        rx.cond(State.has_question, question_bar(), rx.fragment()),
        id="chat-scroll",
        on_mount=rx.call_script(SCROLL_SETUP_JS),
        class_name="w-full max-w-2xl mx-auto flex-1 overflow-y-auto px-4 py-6 gap-3",
        style={"fontSize": State.chat_font_px},
    )


def scroll_down_button() -> rx.Component:
    """Кнопка-стрілка: промотати чат донизу."""
    return rx.button(
        rx.icon("arrow-down", size=18),
        on_click=rx.call_script(SCROLL_BOTTOM_JS),
        radius="full", size="2",
        class_name="absolute bottom-4 right-4 bg-white/10 hover:bg-white/20 text-gray-100 "
                   "shadow-lg backdrop-blur z-10",
    )


def main_area() -> rx.Component:
    return rx.cond(
        State.messages,
        rx.vstack(
            chat_view(),
            scroll_down_button(),
            rx.box(input_box(), class_name="w-full px-4 pb-4 flex justify-center"),
            class_name="flex-1 h-full w-full relative",
        ),
        rx.center(
            rx.vstack(
                rx.heading(
                    rx.cond(State.has_current, State.title, "Що робимо?"),
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
app.add_page(index, title="local-code-agent",
             on_load=[State.load_sessions, State.load_settings])
