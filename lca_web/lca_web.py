"""local-code-agent — веб-інтерфейс на Reflex (стиль Claude).

G3: керування сесіями через бекенд (agent/session.py): створення/список/вибір,
тека, дозволи, план-наперед, джерела — зберігаються в сесію.
Прив'язки runner (answer/shell/plan/edit) — G4.
"""
import asyncio
import re
import threading
import time
from dataclasses import replace
from pathlib import Path

import reflex as rx
from reflex_base.style import set_color_mode

from agent import attachments as A
from agent import config
from agent import convo
from agent import chat_projects as CP
from agent import session as sess
from agent import topics
from agent.agent_loop import (_estimate_iters, NUDGE_TEXT, pending_continuation,
                              recover_tool_calls, should_nudge)
from agent.answerer import build_context
from agent.intent import classify_intent
from agent.llm import OllamaClient
from agent.memory import render_for_planner
from agent.planner import deliberate
from agent.project import load_project_doc, scan_structure
from agent.toolkit import ATTACHMENT_CHUNK_SIZE, ToolContext, default_registry

CHAT_SYSTEM = (
    "You are a helpful thinking assistant. You can search the web with the web_search "
    "tool when you need current or external information. If the user attached files, "
    "their names are listed in context — read any you need with the read_attachment(name) "
    "tool (NEVER ask the user to paste or resend them; they are already available). "
    "Answer in the user's language."
)

_CLIENT = None

# Очікувані confirm-и shell=ask, поза State (asyncio.Event не серіалізується). Ключ —
# id сесії. {sid: {"event": asyncio.Event, "ok": bool}}. Резолвить confirm_yes/no.
_CONFIRMS: dict = {}

# Stop-події для переривання стріму/виконання. Ключ — id сесії.
_STOP_EVENTS: dict = {}

# Вкладення зберігаються на диску (agent/attachments.py), не в пам'яті.


def get_client() -> OllamaClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = OllamaClient()
    return _CLIENT


SCROLL_SETUP_JS = """
(() => {
  // Виміряти ширину скролбара -> CSS-змінна (для вирівнювання поля вводу зі
  // скрольованим чатом: чат втрачає ширину на скролбар, поле вводу — ні).
  const sd = document.createElement('div');
  sd.style.cssText = 'overflow:scroll;width:50px;height:50px;position:absolute;top:-9999px';
  document.body.appendChild(sd);
  document.documentElement.style.setProperty('--sbw', (sd.offsetWidth - sd.clientWidth) + 'px');
  document.body.removeChild(sd);

  const c = document.getElementById('chat-scroll');
  if (!c || c._lcaObs) return;
  let stick = true;
  const nearBottom = () => c.scrollHeight - c.scrollTop - c.clientHeight < 120;
  const btn = document.getElementById('scroll-down-btn');
  const updBtn = () => { if (btn) btn.style.display = nearBottom() ? 'none' : 'inline-flex'; };
  c.addEventListener('scroll', () => { stick = nearBottom(); updBtn(); });
  const obs = new MutationObserver(() => { if (stick) c.scrollTop = c.scrollHeight; updBtn(); });
  obs.observe(c, {childList: true, subtree: true, characterData: true});
  c._lcaObs = obs;
  c.scrollTop = c.scrollHeight;
  updBtn();
})();
"""
SCROLL_BOTTOM_JS = "var c=document.getElementById('chat-scroll'); if(c){c.scrollTop=c.scrollHeight;}"

# Вставка файлів через Ctrl+V: перенаправляємо файли з буфера обміну у прихований
# input компонента rx.upload → спрацьовує наявний handle_upload. Якщо у буфері лише
# текст (files порожній) — нічого не робимо, звичайна вставка в textarea працює як є.
PASTE_SETUP_JS = """
(() => {
  if (window._lcaPasteBound) return;
  window._lcaPasteBound = true;
  document.addEventListener('paste', (e) => {
    const cd = e.clipboardData;
    if (!cd) return;
    const files = Array.from(cd.files || []);
    if (!files.length) return;
    const input = document.querySelector('.rx-Upload input[type="file"]');
    if (!input) return;
    e.preventDefault();
    const dt = new DataTransfer();
    files.forEach(f => dt.items.add(f));
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
})();
"""


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
    session_topics: list[str] = []         # теми (chat), що траплялись у цій сесії
    # Проекти chat-режиму (аналог Claude/ChatGPT Projects) — лише для mode="chat".
    projects: list[dict] = []
    current_project_id: str = ""           # фільтр сайдбара: "" = чати без проекту
    chat_project_id: str = ""              # проект ВІДКРИТОГО чату (джерело правди для промпту)
    project_dialog_open: bool = False
    project_edit_id: str = ""              # "" = створення нового проекту
    project_name_input: str = ""
    project_prompt_input: str = ""
    move_dialog_open: bool = False
    move_chat_id: str = ""                 # чат, який зараз переносимо між проектами
    # Вкладення (#10) — лише метадані для chips; вміст файлів лежить на диску
    # (agent/attachments.py, тека сесії). Модель читає їх через read_file.
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
    tok_model: str = ""

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
        self.tok_model = ""

    def _recompute_tps(self):
        self.tok_tps = round(self.tok_out / (self.tok_eval_ns / 1e9), 1) if self.tok_eval_ns else 0.0

    def _add_stats(self, stats: list[dict]):
        self.tok_prompt += sum(s.get("prompt", 0) for s in stats)
        self.tok_out += sum(s.get("out", 0) for s in stats)
        self.tok_eval_ns += sum(s.get("eval_ns", 0) for s in stats)
        for s in stats:
            if s.get("model"):
                self.tok_model = s["model"]      # остання модель, що відповідала
        self._recompute_tps()

    @rx.var
    def tokens_label(self) -> str:
        # ctx (prompt_eval_count) Ollama віддає лише у фінальному чанку — поки невідомо
        # й модель ще працює, показуємо «…», а не оманливий 0.
        ctx = "…" if (self.busy and self.tok_prompt == 0) else str(self.tok_prompt)
        model = f"{self.tok_model} · " if self.tok_model else ""
        return f"{model}↑{ctx} ctx · ↓{self.tok_out} out · {self.tok_tps} tok/s"

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
        """Чати поточного режиму (Чат/Код роздільні списки). У chat-режимі додатково
        фільтруємо за поточним проектом сайдбара (current_project_id="" — чати без
        проекту, як і код-режим, де проектів нема взагалі)."""
        base = [s for s in self.sessions if s.get("kind", "code") == self.mode]
        if self.mode != "chat":
            return base
        return [s for s in base if (s.get("project_id") or "") == self.current_project_id]

    def _attachments_note(self) -> str:
        """Контекстний блок для моделі: імена файлів сесії з ДИСКА (не зі стейджинг-чіпів,
        бо ті зникають після відправки). Модель читає їх через read_attachment."""
        return A.attachments_note(self.current_id, A.list_saved(self.current_id))

    def _clear_view(self):
        # Файли вкладень лишаються на диску (ручна чистка) — скидаємо лише State-chips.
        self.current_id = ""
        self.title = ""
        self.project_root = ""
        self.messages = []
        self.references = []
        self.has_pending = False
        self.queued_text = ""
        self.context_summary = ""
        self.session_topics = []
        self.chat_project_id = ""
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
    def select_project(self, pid: str):
        """Фільтр сайдбара (chat-режим): показати чати лише цього проекту
        ("" — без проекту). Якщо відкритий чат не належить новому фільтру —
        згортаємо вид (той самий патерн, що й set_mode)."""
        if pid == self.current_project_id:
            return
        self.current_project_id = pid
        cur = next((s for s in self.sessions if s["id"] == self.current_id), None)
        if not cur or (cur.get("project_id") or "") != pid:
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
        self.projects = CP.list_projects()
        # Самозцілення: чат може лишитись з посиланням на проект, якого вже
        # нема на диску (живий кейс: чат "зник" із сайдбара, бо ховався за
        # фільтром неіснуючого проекту — і "чатів", і "проектів" виглядало
        # порожньо одночасно). Звіряємо й чистимо осиротілі project_id.
        valid_ids = {p["id"] for p in self.projects}
        for s_meta in self.sessions:
            pid = s_meta.get("project_id") or ""
            if pid and pid not in valid_ids:
                s = sess.load_session(s_meta["id"])
                s.project_id = ""
                sess.save_session(s)
                s_meta["project_id"] = ""

    def _select(self, sid: str):
        s = sess.load_session(sid)
        self.current_id = s.id
        self.mode = s.kind
        self.title = s.title
        self.project_root = s.project_root
        self.chat_project_id = s.project_id
        self.edits = s.permissions.get("edits", "ask")
        self.shell = s.permissions.get("shell", "smart")
        self.plan_first = s.plan_first
        self.references = list(s.reference_files)
        self.messages = [{"thinking": "", "attachments": [], **m} for m in s.messages]
        self.has_pending = s.pending_plan is not None
        self.context_summary = s.context_summary
        self.session_topics = list(s.topics)
        self.attachments = []     # чіпи — стейджинг до відправки; вкладення сесії на диску
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

    def _append(self, role: str, content: str, kind: str = "text", thinking: str = "",
                attachments: list | None = None):
        self.messages = self.messages + [
            {"role": role, "content": content, "kind": kind, "thinking": thinking,
             "attachments": attachments or []}]
        if self.current_id:
            s = sess.load_session(self.current_id)
            # Розгорнути Reflex-проксі, ВКЛЮЧНО з вкладеним списком attachments
            # (інакше asdict() при збереженні падає на MutableProxy).
            s.messages = [
                {"role": m["role"], "content": m["content"],
                 "kind": m.get("kind", "text"), "thinking": m.get("thinking", ""),
                 "attachments": list(m.get("attachments", []) or [])}
                for m in self.messages]
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
        if self.mode == "chat" and self.current_project_id:
            s.project_id = self.current_project_id
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

    # ── Проекти chat-режиму ──────────────────────────────────────────────────
    @rx.event
    def open_move_dialog(self, sid: str):
        self.move_chat_id = sid
        self.move_dialog_open = True

    @rx.event
    def set_move_dialog_open(self, v: bool):
        self.move_dialog_open = v

    @rx.event
    def move_chat_to_project(self, pid: str):
        """Перенести чат у проект pid ("" — винести з проекту/без проекту)."""
        if self.move_chat_id:
            s = sess.load_session(self.move_chat_id)
            s.project_id = pid
            sess.save_session(s)
            if self.current_id == self.move_chat_id:
                self.chat_project_id = pid
            self.sessions = sess.list_sessions()
        self.move_dialog_open = False
        self.move_chat_id = ""

    @rx.event
    def open_new_project(self):
        self.project_edit_id = ""
        self.project_name_input = ""
        self.project_prompt_input = ""
        self.project_dialog_open = True

    @rx.event
    def open_edit_project(self, pid: str):
        p = CP.load_project(pid)
        if not p:
            return
        self.project_edit_id = p.id
        self.project_name_input = p.name
        self.project_prompt_input = p.prompt
        self.project_dialog_open = True

    @rx.event
    def set_project_dialog_open(self, v: bool):
        self.project_dialog_open = v

    @rx.event
    def set_project_name_input(self, v: str):
        self.project_name_input = v

    @rx.event
    def set_project_prompt_input(self, v: str):
        self.project_prompt_input = v[:CP.PROMPT_MAX_CHARS]

    @rx.event
    def save_project_dialog(self):
        name = self.project_name_input.strip()
        if not name:
            return
        if self.project_edit_id:
            p = CP.load_project(self.project_edit_id)
            if p:
                p.name = name
                p.prompt = self.project_prompt_input
                CP.save_project(p)
        else:
            CP.save_project(CP.new_project(name, self.project_prompt_input))
        self.projects = CP.list_projects()
        self.project_dialog_open = False

    @rx.event
    def delete_project_event(self, pid: str):
        """Видалити проект. Чати, що належали йому, лишаються — просто без
        проекту (НЕ видаляємо їхній вміст)."""
        for s_meta in self.sessions:
            if (s_meta.get("project_id") or "") == pid:
                s = sess.load_session(s_meta["id"])
                s.project_id = ""
                sess.save_session(s)
        CP.delete_project(pid)
        if self.current_project_id == pid:
            self.current_project_id = ""
        if self.chat_project_id == pid:
            self.chat_project_id = ""
        self.projects = CP.list_projects()
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
        self.theme = d.get("theme", "auto")
        self.font_chat = d.get("font_chat", 14)
        self.font_ui = d.get("font_ui", 13)
        # Тему застосовує next-themes сам (зберігає в localStorage між перезавантаженнями);
        # set_color_mode НЕ можна викликати з бекенд-хендлера — лише шрифт через DOM.
        # PASTE_SETUP_JS — одноразово вішає document-listener вставки файлів (Ctrl+V).
        return [rx.call_script(self.ui_font_js), rx.call_script(PASTE_SETUP_JS)]

    def _persist_settings(self):
        from agent import settings as S
        S.save({
            "theme": self.theme,
            "font_chat": self.font_chat,
            "font_ui": self.font_ui
        })

    @rx.event
    def save_settings(self):
        self._persist_settings()

    @rx.event
    def set_theme(self, v: str | list[str]):
        # Лише зберігаємо вибір; реальне перемикання робить set_color_mode,
        # підвʼязаний до фронтенд-тригера сегмент-контролу (див. settings_dialog).
        if isinstance(v, list):
            v = v[0] if v else self.theme
        self.theme = v
        self._persist_settings()

    @rx.event
    def set_font_chat(self, v: str | list[str] | list[int]):
        val = v[0] if isinstance(v, list) else v
        # Кламп МУСИТЬ збігатися з min/max слайдера (12..32), інакше повзунок застрягає
        # посеред шкали (хендлер ріже значення раніше за кінець доріжки).
        self.font_chat = max(12, min(32, int(val)))
        self._persist_settings()

    @rx.event
    def set_font_ui(self, v: str | list[str] | list[int]):
        val = v[0] if isinstance(v, list) else v
        self.font_ui = max(11, min(24, int(val)))     # збігається зі слайдером 11..24
        self._persist_settings()
        return rx.call_script(self.ui_font_js)

    # ── Вкладення (#10) ────────────────────────────────────────────────────────
    @rx.event
    async def handle_upload(self, files: list[rx.UploadFile]):
        cid = self.current_id or "_tmp"     # до створення сесії — у тимчасову теку _tmp
        for file in files:
            if len(self.attachments) >= A.MAX_FILES:
                break
            if any(a["name"] == file.filename for a in self.attachments):
                continue                           # вже є — пропустити дублікат
            data = await file.read()
            result = A.process(file.filename, data)
            content = result.pop("_content", "")  # вміст не тримаємо в State
            if not result["error"] and content:
                A.save(cid, file.filename, content)   # валідний → на диск
            self.attachments = self.attachments + [result]

    @rx.event
    def remove_attachment(self, name: str):
        # Видалення чіпа = ручна чистка файлу з диска.
        A.remove(self.current_id or "_tmp", name)
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

    async def _confirm_async(self, text: str) -> bool:
        """Показати діалог підтвердження З КОНТЕКСТУ background-події (через `async with
        self`, тож дельта confirm_open реально доходить до фронта — на відміну від
        старого _show через run_coroutine_threadsafe, що не мав EventContext) і чекати
        клік через asyncio.Event БЕЗ блокування воркер-потоку. Резолвиться
        confirm_yes/confirm_no; таймаут 300с -> відмова."""
        ev = asyncio.Event()
        _CONFIRMS[self.current_id] = {"event": ev, "ok": False}
        async with self:
            self.confirm_text = text
            self.confirm_open = True
        try:
            await asyncio.wait_for(ev.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass
        async with self:
            self.confirm_open = False
        return _CONFIRMS.pop(self.current_id, {}).get("ok", False)

    async def _execute_steps(self, plan, root: str, client):
        """Виконати кроки плану через per-step tool-loop: модель сама обирає тули
        (read/write/edit/run_shell), ми лише показуємо її дії та diff у чаті.

        ToolContext: edits=auto (план уже схвалено — через plan_first або edits=auto).
        shell передаємо як є; для shell=ask confirm показує модальний попап."""
        ctx = self._exec_ctx(root, client)
        async with self:
            cid = self.current_id
            att_note = self._attachments_note()    # шляхи файлів; виконавець читає read_file
        stop_ev = _STOP_EVENTS.get(cid)
        stopped = False
        # Огляд усього плану — щоб виконавець кожного кроку бачив МЕТУ й сусідні кроки,
        # а не отримував голий опис кроку без контексту (інакше на розмитому кроці модель
        # «розмірковує» замість дії й крок тихо провалюється).
        plan_overview = "\n".join(
            f"  {'[x]' if s.status == 'done' else '[!]' if s.status == 'failed' else '[ ]'} "
            f"#{s.id} [{s.kind}]: {s.description}" for s in plan.steps)
        for step in plan.steps:
            if stop_ev is not None and stop_ev.is_set():       # перервано між кроками
                stopped = True
                break
            async with self:
                self.status = f"Крок #{step.id}: {step.description[:50]}"
            if step.kind == "inline":
                plan.set_result(step.id, "done", "inline")
                continue
            step_ctx = (f"Project root: {root}\n\nЗагальна задача: {plan.task}\n"
                        f"Повний план (ти виконуєш ЛИШЕ крок #{step.id}):\n{plan_overview}")
            if self.context_summary:
                step_ctx += f"\n{convo.as_context(self.context_summary)}"
            if att_note:
                step_ctx += "\n" + att_note
            final, log = await self._run_tool_step(step.description, ctx,
                                                   title=step.description[:60],
                                                   context=step_ctx, stop_event=stop_ev)
            plan.set_result(step.id, "done" if log else "failed", final or "—")
        async with self:
            self._append("assistant", "⛔ Зупинено" if stopped else "Готово ✓", "note")
        return render_for_planner(plan)               # outcome для фонового підсумку

    def _exec_ctx(self, root: str, client) -> ToolContext:
        """ToolContext для виконання: edits=auto (план/запит уже схвалено). Підтвердження
        shell тепер робить async-шар (_dispatch_tool/_confirm_async), тож confirm=None —
        самі тули блокуючого попапу більше не викликають."""
        return ToolContext(root=Path(root),
                           permissions={"edits": "auto", "shell": self.shell},
                           client=client, confirm=None,
                           attachments_dir=A.session_dir(self.current_id))

    async def _dispatch_tool(self, reg, name: str, args: dict, ctx: ToolContext) -> str:
        """Виконати тул. run_shell, що потребує підтвердження (shell=ask, або smart+
        небезпечна), ставимо на ASYNC-паузу з діалогом (без блокування воркера); решта —
        у потоці. Схвалену команду виконуємо з shell=auto, щоб тул не питав удруге."""
        if name == "run_shell":
            from agent.shell_guard import classify
            cmd = (args.get("command") or "").strip()
            mode = ctx.permissions.get("shell", "allowlist")
            if mode == "off":
                return "консоль вимкнена (дозвіл shell=off)"
            danger = mode == "smart" and classify(cmd) == "danger"
            if mode == "ask" or danger:
                prompt = f"⚠ Небезпечна команда:\n{cmd}" if danger else f"Виконати: {cmd}"
                if not await self._confirm_async(prompt):
                    return "команду відхилено користувачем"
                ctx = replace(ctx, permissions={**ctx.permissions, "shell": "auto"})
        return await asyncio.to_thread(lambda: reg.dispatch(name, args, ctx))

    async def _run_tool_step(self, step_text: str, ctx: ToolContext, title: str = "",
                             context: str = "", stop_event=None):
        """Async per-step tool-loop: модель кличе тули, ми диспетчеримо їх (виклик моделі
        — per-iteration через to_thread). run_shell, що потребує підтвердження, ставимо
        на async-паузу — діалог показується з контексту ЦІЄЇ події (дельта доходить), а
        не з відірваної корутини. Додає ОДНЕ повідомлення-крок із катом «хід виконання».
        Логіку дублюємо з agent_loop.run_step (той лишається для headless/тестів)."""
        from agent.agent_loop import SYSTEM as EXEC_SYSTEM, _args
        reg = default_registry()
        client = ctx.client
        user = (f"Контекст:\n{context}\n\n" if context else "") + f"Крок:\n{step_text}"
        messages = [{"role": "system", "content": EXEC_SYSTEM},
                    {"role": "user", "content": user}]
        max_iters = await asyncio.to_thread(lambda: _estimate_iters(step_text, context, client))
        actions: list[tuple[str, str]] = []   # (tool, result)
        stats: list[dict] = []                # client.last_stats кожного виклику моделі
        final = "досягнуто ліміту ітерацій"
        nudged = False
        for _ in range(max_iters):
            if stop_event is not None and stop_event.is_set():
                final = "зупинено користувачем"; break
            msg = await asyncio.to_thread(
                lambda: client.chat(messages, tools=reg.schema(), profile=config.EXECUTOR))
            if client.last_stats:
                stats.append(dict(client.last_stats))
            calls, recovered = recover_tool_calls(msg)
            if not calls:
                content = (msg.get("content") or "").strip()
                # Великий файл дочитується частинами — якщо модель не продовжила
                # пагінацію сама, дочитуємо БЕЗ її участі (текстовий nudge тут
                # ненадійний, бачили живцем 15+ ідентичних повторів). Див. pending_continuation.
                cont = pending_continuation(messages)
                if cont is not None:
                    calls, recovered = [cont], True
                # Підштовхуємо РІВНО ОДИН раз, незалежно від того, чи вже були дії
                # раніше у цьому кроці (живий кейс на account_test_14.py: модель
                # почитала файл, потім видала порожню відповідь). НЕ ехо-имо
                # зламаний/порожній текст назад як assistant. Див. agent_loop.should_nudge.
                elif should_nudge(content, nudged):
                    nudged = True
                    messages.append({"role": "user", "content": NUDGE_TEXT})
                    continue
                else:
                    final = content; break
            messages.append({"role": "assistant", "content": "" if recovered else msg.get("content", ""),
                             "tool_calls": calls})
            for call in calls:
                name = call.get("function", {}).get("name", "")
                args = _args(call)
                result = await self._dispatch_tool(reg, name, args, ctx)
                actions.append((name, result))
                messages.append({"role": "tool", "content": result})
        log = [f"{n} -> {r[:80]}" for n, r in actions]

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
                if stats.get("model"):
                    self.tok_model = stats["model"]
                self._recompute_tps()
            body = (self.stream_content or "").strip()
            thinking = self.stream_thinking
            self.streaming = False
            self.stopping = False
            _STOP_EVENTS.pop(cid, None)
        return body, thinking, tool_calls

    # Тули, доступні read-only режимам (чат / answer): пошук + читання файлів/вкладень.
    _READONLY_TOOLS = ("web_search", "read_file", "list_dir", "read_attachment", "read_topic")

    # Заголовок read_attachment, коли файл читається частинами (toolkit.h_read_attachment):
    # "=== name (OFFSET-END з TOTAL) ===" — з'являється лише коли total > CHUNK_SIZE.
    _PAGINATED_HEADER_RE = re.compile(r"^=== (.+?) \(\d+-\d+ з \d+\) ===\n", re.MULTILINE)
    # Хвіст-підказка "лишилось N символів... offset=X)]" — лишаємо як є, щоб
    # agent_loop.pending_continuation продовжував працювати й після заміни на дайджест.
    _CONTINUATION_SUFFIX_RE = re.compile(r"\n\n…\[лишилось .*?\]$", re.DOTALL)

    async def _digest_attachment_chunk(self, name: str, raw_result: str,
                                       memory: dict[str, str], client, profile: dict) -> str:
        """Якщо raw_result — шматок ВЕЛИКОГО вкладення (пагінація), зливає його в
        накопичувальний дайджест (agent.convo.update_digest) і повертає дайджест +
        той самий continuation-хвіст замість сирого шматка — щоб контекст не
        роздувався накопиченням сирих шматків, а pending_continuation і далі бачив
        offset-підказку. Для звичайних (непагінованих) результатів — повертає
        raw_result без змін. profile: та сама модель, що й gather-фаза (chat -> gemma)."""
        header_match = self._PAGINATED_HEADER_RE.match(raw_result)
        if not header_match:
            return raw_result
        body = raw_result[header_match.end():]
        suffix_match = self._CONTINUATION_SUFFIX_RE.search(body)
        suffix = suffix_match.group(0) if suffix_match else ""
        chunk_text = body[:suffix_match.start()] if suffix_match else body
        prev = memory.get(name, "")
        digest = await asyncio.to_thread(
            lambda: convo.update_digest(prev, chunk_text, client, profile=profile))
        memory[name] = digest
        return f"=== {name} (дайджест прочитаного дотепер) ===\n{digest}{suffix}"

    async def _dispatch_tool_calls(self, tool_calls: list, msgs: list, reg, tctx,
                                   digest_memory: dict[str, str] | None = None, client=None,
                                   digest_profile: dict = config.EXECUTOR):
        """Виконати tool_calls: дописати результати в msgs + показати кроки в чаті.
        digest_memory (опційно): якщо передано — шматки великих вкладень (read_attachment
        пагінація) зливаються в дайджест (convo.update_digest) замість накопичення сирими
        шматками в контексті. Без цього параметра — поведінка як була."""
        import json as _json
        for call in tool_calls:
            name = call.get("function", {}).get("name", "")
            args = call.get("function", {}).get("arguments", {})
            if isinstance(args, str):
                try:
                    args = _json.loads(args)
                except Exception:
                    args = {}
            label = args.get("name") or args.get("query") or args.get("path") or name
            async with self:
                self.status = f"{name}: {label}"[:60]
            result = await asyncio.to_thread(lambda n=name, a=args: reg.dispatch(n, a, tctx))
            shown = result
            if digest_memory is not None and name == "read_attachment" and client is not None:
                async with self:
                    self.status = f"Узагальнюю прочитане ({label})…"
                shown = await self._digest_attachment_chunk(label, result, digest_memory, client,
                                                             digest_profile)
            msgs.append({"role": "tool", "content": shown})
            async with self:
                self._append("assistant", f"🔧 {name} · {label}", "step", thinking=shown)

    async def _synthesize_with_retry(self, msgs: list, client,
                                     profile: dict = config.PLANNER) -> tuple[str, str]:
        """Стрім фінальної відповіді (think=on). Живий кейс: модель «застрягла» в
        роздумах (видно довгий thinking-монолог) і так і не дійшла до фінального
        контенту — body порожній, хоч thinking — ні. Той самий клас бага, що
        порожні tool_calls у фазі збору (agent_loop.should_nudge), але тут це
        ОКРЕМА точка (синтез, не збір), тож власний, простіший retry: один
        повторний прохід з прямим нагадуванням відповісти, перш ніж здаватись."""
        body, thinking, _ = await self._run_stream(client.chat_stream(msgs, profile=profile))
        if not (body or "").strip():
            msgs.append({"role": "user", "content":
                "Ти не дав фінальної відповіді (лише міркував). Дай КОНКРЕТНУ "
                "відповідь користувачу зараз, без подальших роздумів."})
            body, thinking, _ = await self._run_stream(client.chat_stream(msgs, profile=profile))
        return body, thinking

    async def _run_tool_chat(self, msgs: list, tctx: ToolContext, client,
                             max_rounds: int = 6, gather_first: bool = False,
                             gather_profile: dict = config.EXECUTOR) -> str:
        """Тул-цикл для read-only режимів.

        gather_first=True (є прикріплені файли): фаза ЗБОРУ йде з think=OFF
        (gather_profile — за дефолтом EXECUTOR/Qwen для answer-режиму /КОД-аналізу;
        chat-режим передає config.CHAT_EXECUTOR/gemma — інакше chat випадково
        тягнув би кодинг-модель лише тому, що think=off надійніший для tool_calls)
        — модель надійно емітить tool_calls (read_attachment), а не «думає» про них
        без виклику; потім ОДИН фінальний синтез з think=ON (PLANNER/gemma, стрім,
        живий лічильник).

        gather_first=False (простий чат/web_search): think=ON стрім-цикл як є; якщо
        раунди вичерпано на тулах — форсований фінал без тулів."""
        reg = default_registry()
        tools = [t for t in reg.schema() if t["function"]["name"] in self._READONLY_TOOLS]

        if gather_first:
            nudged = False
            digest_memory: dict[str, str] = {}   # name -> накопичувальний дайджест великого файлу
            for _ in range(max_rounds):
                msg = await asyncio.to_thread(
                    lambda: client.chat(msgs, tools=tools, profile=gather_profile))
                tool_calls, recovered = recover_tool_calls(msg)
                if not tool_calls:
                    content = (msg.get("content") or "").strip()
                    # Великий файл читається частинами (read_attachment offset-пагінація,
                    # toolkit.ATTACHMENT_CHUNK_SIZE) — якщо модель не продовжила сама,
                    # дочитуємо БЕЗ її участі (текстовий nudge ненадійний, бачили живцем
                    # 15+ ідентичних повторів "продовжую читати" без жодної дії).
                    cont = pending_continuation(msgs)
                    if cont is not None:
                        tool_calls, recovered = [cont], True
                    # Якщо контент непорожній і не просочений — це справді "досить
                    # зібрано, йду відповідати" (валідний ранній вихід). Інакше раніше
                    # цикл просто `break`-ався без жодних зібраних даних (типово —
                    # вкладення взагалі не прочитане), звідси "(порожня відповідь)" на
                    # живому кейсі з великим paste-вкладенням. Див. agent_loop.should_nudge.
                    elif should_nudge(content, nudged):
                        nudged = True
                        msgs.append({"role": "user", "content": NUDGE_TEXT})
                        continue
                    else:
                        break
                msgs.append({"role": "assistant", "content": "" if recovered else msg.get("content", ""),
                             "tool_calls": tool_calls})
                await self._dispatch_tool_calls(tool_calls, msgs, reg, tctx,
                                                digest_memory=digest_memory, client=client,
                                                digest_profile=gather_profile)
            async with self:
                self.status = "Формулюю відповідь…"
            body, thinking = await self._synthesize_with_retry(msgs, client)
            async with self:
                self._append("assistant", body or "(порожня відповідь)", "answer",
                             thinking=thinking)
            return body

        for _ in range(max_rounds):
            body, thinking, tool_calls = await self._run_stream(
                client.chat_stream(msgs, tools=tools, profile=config.PLANNER))
            if not tool_calls:                       # модель завершила — це фінальна відповідь
                if not (body or "").strip():
                    # Завершила без жодного тула, але й без контенту — той самий
                    # "застрягла в роздумах" живий кейс, тут без передісторії тулів.
                    body, thinking = await self._synthesize_with_retry(msgs, client)
                async with self:
                    self._append("assistant", body, "answer", thinking=thinking)
                return body
            msgs.append({"role": "assistant", "content": body, "tool_calls": tool_calls})
            await self._dispatch_tool_calls(tool_calls, msgs, reg, tctx)
        # Раунди вичерпано на викликах тулів → змушуємо модель відповісти зібраним (без тулів).
        async with self:
            self.status = "Формулюю відповідь…"
        msgs.append({"role": "user",
                     "content": "Достатньо читання. Дай повну відповідь українською за "
                                "зібраними даними, більше не викликай тулів."})
        body, thinking = await self._synthesize_with_retry(msgs, client)
        async with self:
            self._append("assistant", body or "(не вдалося сформувати відповідь)",
                         "answer", thinking=thinking)
        return body

    async def _estimate_rounds(self, text: str, names: list, client,
                               profile: dict = config.EXECUTOR, cid: str = "") -> int:
        """Адаптивний ліміт раундів тул-циклу. Коли є прикріплені файли — оцінюємо
        через _estimate_iters (модель каже n тул-викликів → max(6, n*2)), щоб ліміт
        масштабувався під кількість/складність файлів, а не впирався в хардкод. Без
        файлів — дефолт 6 (ліміт усе одно не зв'язує простий чат: він завершується
        першим раундом без тулів). profile: chat-режим передає CHAT_EXECUTOR (gemma).

        Модель не знає РЕАЛЬНОГО розміру файлу (бачить лише імʼя), тож її власна
        оцінка систематично занижена для великих вкладень — живий кейс: цикл
        вичерпав раунди на півдорозі через 386KB файл і пішов на синтез із
        недочитаним дайджестом (обірвана відповідь). Розмір ми знаємо ДЕТЕРМІНОВАНО
        (без LLM) — рахуємо мінімум раундів від нього напряму й беремо максимум з
        оцінкою моделі, а не покладаємось лише на її вгадування."""
        if not names:
            return 6
        size_floor = 6
        if cid:
            sizes = {n["name"]: n["size"] for n in A.list_saved(cid)}
            total_chunks = sum(-(-sizes.get(n, 0) // ATTACHMENT_CHUNK_SIZE) for n in names)
            if total_chunks:
                size_floor = total_chunks + 4   # +4 запас на нюджі/нечитальні раунди
        model_estimate = await asyncio.to_thread(
            lambda: _estimate_iters(text, "files: " + "; ".join(names), client, profile=profile))
        return max(model_estimate, size_floor)

    @staticmethod
    def _inject_files(content: str, cur_names: list, other_names: list) -> str:
        """Вшити в текст повідомлення ДВА переліки файлів, щоб модель зорієнтувалась:
        - cur_names: прикріплені САМЕ до цього повідомлення → опрацювати;
        - other_names: інші файли розмови → перечитати через read_attachment ЛИШЕ якщо
          запит стосується саме їх (інакше вони вже в контексті через підсумок).
        Усі читаються тулом read_attachment(name)."""
        cur = [n for n in (cur_names or []) if n]
        others = [n for n in (other_names or []) if n]
        if not cur and not others:
            return content
        lines = ["[Доступні файли — читай тулом read_attachment(name):"]
        if cur:
            lines.append("• Додані до ЦЬОГО повідомлення (опрацюй): " + "; ".join(cur))
        if others:
            lines.append("• Інші файли розмови (перечитуй ЛИШЕ якщо запит саме про них): "
                         + "; ".join(others))
        return content + "\n\n" + "\n".join(lines) + "]"

    async def _chat_reply(self, text: str, client, att_note: str = "") -> str:
        """Чат-режим: думаюча модель + read-only тули. У поточну user-репліку вшиваємо
        імена файлів, прикріплених САМЕ до неї (m['attachments']); модель читає їх через
        read_attachment (attachments_dir = тека вкладень сесії)."""
        async with self:
            cid = self.current_id
            proj_id = self.chat_project_id
            cur_atts = list(self.messages[-1].get("attachments", []) or []) if self.messages else []
            history = [{"role": m["role"], "content": m["content"]}
                       for m in self.messages
                       if m["role"] in ("user", "assistant")
                       and m.get("kind", "text") in ("text", "answer") and m["content"]][-20:]
        proj = CP.load_project(proj_id) if proj_id else None
        project_block = CP.as_system_block(proj.prompt) if proj else ""
        all_names = [n["name"] for n in A.list_saved(cid)]
        other_names = [n for n in all_names if n not in cur_atts]
        # Вшити переліки файлів у ОСТАННЮ user-репліку (поточний хід), якщо файли є.
        if all_names and history and history[-1]["role"] == "user":
            history[-1] = {**history[-1],
                           "content": self._inject_files(history[-1]["content"], cur_atts, other_names)}
        tctx = ToolContext(root=A.session_dir(cid),
                           permissions={"edits": "off", "shell": "off"}, client=client,
                           attachments_dir=A.session_dir(cid))
        all_topics = await asyncio.to_thread(topics.list_topics)
        topics_note = topics.available_topics_note(all_topics)
        # project_block ПЕРШИМ — інструкції проекту задають рамку розмови, тема й
        # вкладення йдуть як додатковий контекст усередині цієї рамки.
        system = project_block + CHAT_SYSTEM + ("\n\n" + topics_note if topics_note else "")
        msgs = [{"role": "system", "content": system}] + history
        rounds = await self._estimate_rounds(text, cur_atts or all_names, client,
                                             profile=config.CHAT_EXECUTOR, cid=cid)
        # think=off-збір, коли в сесії Є файли ЧИ доступні теми минулих розмов — щоб
        # модель НАДІЙНО викликала read_attachment/read_topic (think=on часто «думає»
        # про тул, але не викликає). Чистий чат без файлів/тем → think=on-стрім без
        # зайвого збору. gather_profile=CHAT_EXECUTOR (gemma) — chat-режим не торкає
        # кодинг-модель.
        return await self._run_tool_chat(msgs, tctx, client, rounds,
                                         gather_first=bool(all_names or all_topics),
                                         gather_profile=config.CHAT_EXECUTOR)

    # Системний промпт answer-режиму В ТУЛ-ЦИКЛІ: на відміну від answerer.SYSTEM, явно
    # каже про read_file/list_dir — інакше модель «просить користувача прислати файли»
    # замість того, щоб прочитати їх самій (той самий баг, що зупиняв аналіз після плану).
    _ANSWER_TOOLS_SYSTEM = (
        "You are a code analysis assistant for a local project. You have tools: "
        "read_file(path) and list_dir(path) to inspect ANY file in the project, plus "
        "web_search. The project structure is in the context. To answer ACCURATELY you "
        "MUST read the relevant files yourself with read_file — NEVER ask the user to "
        "paste or provide file contents, you can read them directly. Read the files you "
        "need, then answer concisely, citing the file/function. Do NOT propose code edits "
        "unless asked. Answer in the user's language.\n"
        "IMPORTANT about your capabilities: this is the read-only ANALYSIS mode, so here "
        "you only have read tools. But the agent CAN edit files and run shell commands "
        "(git commit/push, run tests, etc.) via run_shell/edit_file in its EXECUTION mode. "
        "So if the user asks you to DO something (commit, push, run, modify a file), do NOT "
        "say you are unable — instead tell them to phrase it as a task (e.g. 'commit and "
        "push the changes') so the agent runs it in execution mode."
    )

    async def _answer_reply(self, text: str, ctx: str, att_note: str, root: str, client) -> str:
        """Answer-режим (аналіз коду): думаюча модель + read-only тули. root = корінь
        проєкту, тож модель читає і файли проєкту, і прикріплені (за повним шляхом)."""
        ANSWER_SYSTEM = self._ANSWER_TOOLS_SYSTEM
        async with self:
            cid = self.current_id
            cur_atts = list(self.messages[-1].get("attachments", []) or []) if self.messages else []
        tctx = ToolContext(root=Path(root) if root else Path.home(),
                           permissions={"edits": "off", "shell": "off"}, client=client,
                           attachments_dir=A.session_dir(cid))
        all_names = [n["name"] for n in A.list_saved(cid)]
        other_names = [n for n in all_names if n not in cur_atts]
        question = self._inject_files(text, cur_atts, other_names)
        parts = [f"Контекст:\n{ctx}" if ctx else "", f"Питання:\n{question}"]
        user = "\n\n".join(p for p in parts if p)
        msgs = [{"role": "system", "content": ANSWER_SYSTEM},
                {"role": "user", "content": user}]
        rounds = await self._estimate_rounds(text, cur_atts or all_names, client, cid=cid)
        # answer-режим = аналіз коду: ЗАВЖДИ think=off-збір, щоб модель надійно читала
        # файли проєкту (read_file/list_dir). Без цього вона лише «планує» прозою й
        # зупиняється, нічого не прочитавши (саме той баг «стоп після планування»).
        # Фінальний синтез усе одно think=on (у _run_tool_chat).
        return await self._run_tool_chat(msgs, tctx, client, rounds, gather_first=True)

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
            chat = self.mode == "chat"
            kind = "chat" if chat else "code"
            cur_topics = list(self.session_topics)
        # chat: загальний підсумок розмови менше потрібен у деталях (деталі живуть у
        # темах нижче) — зменшуємо бюджет умовно втричі, як домовлено з користувачем.
        budget = convo.BUDGET_CHARS // 3 if chat else convo.BUDGET_CHARS
        new = await asyncio.to_thread(
            lambda: convo.update_summary(cur, user_text, outcome, client, budget=budget, kind=kind))
        new_topics = cur_topics
        if chat:
            new_topics = await asyncio.to_thread(
                lambda: self._update_topic(user_text, outcome, cur_topics, client))
        async with self:
            if self.current_id == cid:                # та сама сесія — оновити й у State
                self.context_summary = new
                self.session_topics = new_topics
            if cid:                                   # у файл — завжди в сесію-походження
                s = sess.load_session(cid)
                s.context_summary = new
                s.topics = new_topics
                sess.save_session(s)

    def _update_topic(self, user_text: str, outcome: str, cur_topics: list[str], client) -> list[str]:
        """Класифікувати цей хід розмови за темою (нова чи існуюча — список тем
        СПІЛЬНИЙ для всього chat-режиму, не лише цієї сесії), злити в нотатку теми
        й зберегти на диск. Повертає оновлений список тем ЦІЄЇ сесії (для підказки
        моделі в наступних ходах). Синхронна (LLM-виклики) — викликати з to_thread."""
        existing = topics.list_topics()
        name = topics.classify_topic(user_text, outcome, existing, client, profile=config.CHAT_EXECUTOR)
        prev = topics.load_topic(name)
        note = topics.update_topic_note(prev, user_text, outcome, client, profile=config.CHAT_EXECUTOR)
        if not note.strip():
            # Куций хід (нема що класифікувати) -> модель не дала нічого корисного
            # (update_topic_note вже відкинула луна-плейсхолдери) — НЕ створюємо
            # сміттєвий файл теми. Живий кейс: "Новий topic.txt" з вмістом "(порожньо)".
            return cur_topics
        topics.save_topic(name, note)
        return cur_topics if name in cur_topics else [*cur_topics, name]

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
                if self.mode == "chat" and self.current_project_id:
                    s.project_id = self.current_project_id
                sess.save_session(s)
                self.sessions = sess.list_sessions()
                pre = list(self.attachments)            # chips, прикріплені до створення сесії
                self._select(s.id)
                A.rename_session("_tmp", s.id)          # перенести файли _tmp → сесія
                self.attachments = pre                  # _select затер chips — повертаємо
            # Тека, введена в полі на головній сторінці, ще не збережена в сесію
            # (save_folder окремо не викликали). Застосовуємо ДО перевірки — інакше
            # надсилання з головної з уведеною текою хибно скаржиться «вкажіть теку».
            if not self.project_root and self.folder_input.strip():
                self.project_root = self.folder_input.strip()
                if self.title in ("", "Новий чат"):
                    self.title = Path(self.project_root).name
                self._save_current()
            if self.mode != "chat" and not self.project_root:
                self._append("assistant", "Спершу вкажіть робочу теку.", "note")
                return
            text = self.task.strip()
            if not text:
                return
            self.task = ""
            att_names = [a["name"] for a in self.attachments if not a.get("error")]
            # Великий вставлений/набраний текст у полі вводу не має жодного ліміту
            # (на відміну від файлових вкладень — MAX_FILE_SIZE) і летить прямо в
            # промпт, переповнюючи контекст (живий кейс: 687k символів — модель
            # падає). Той самий ліміт, що й для файлів — понад нього зберігаємо як
            # вкладення на диск; модель читає частинами через read_attachment, а не
            # отримує суцільний блок у контексті.
            if len(text) > A.MAX_FILE_SIZE:
                kb = len(text) // 1024
                paste_name = f"paste_{int(time.time())}.txt"
                A.save(self.current_id, paste_name, text)
                att_names.append(paste_name)
                text = (f"[Вставлений текст ({kb} KB) занадто великий для прямого "
                        f"включення — збережено як вкладення '{paste_name}'. Прочитай "
                        f"його частинами через read_attachment('{paste_name}').]")
            # Імена прикріплених файлів → у саме повідомлення (видно в чаті), потім
            # стейджинг-чіпи прибираємо з-під інпута. Файли лишаються на диску.
            self._append("user", text, attachments=att_names)
            self.attachments = []
            if self.busy or self.has_pending or self.has_question:                              # модель зайнята або чекає на дію користувача -> у чергу
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
        LLM-підсумок контексту. busy не чіпає — ним керує send_task/_drain_and_unbusy.

        Винятки (обрив зʼєднання з Ollama, кривий JSON тощо) ловимо тут і показуємо в
        чаті — інакше виняток вилітає з background-події ДО того, як has_pending/
        has_question/busy встигають скинутись, і кнопки/черга висять назавжди."""
        try:
            await self._process_one_inner(text)
        except Exception as e:
            async with self:
                self.has_pending = False
                self.has_question = False
                self._append("assistant", f"⚠ Помилка: {e}", "note")

    async def _process_one_inner(self, text: str):
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
            att_note = self._attachments_note()     # шляхи прикріплених файлів (модель читає сама)
            answering = self.has_question
            if answering:
                self.has_question = False
                self.status = "Обмірковую відповідь…"
            else:
                self.status = "Думаю…" if chat_mode else "Визначаю тип запиту…"

        client = await asyncio.to_thread(get_client)
        su, so = text, ""

        if chat_mode:
            so = await self._chat_reply(text, client, att_note)
        elif answering:
            su, so = await self._resume_planning(text, root, plan_first, client)
        else:
            mode = await asyncio.to_thread(lambda: classify_intent(text, client))
            if mode == "answer":
                async with self:
                    self.status = "Аналізую код…"
                ctx = await asyncio.to_thread(lambda: build_context(root, refs, text))
                so = await self._answer_reply(text, convo.as_context(summary) + ctx,
                                              att_note, root, client)
            elif mode == "shell":
                async with self:
                    self.status = "Виконую запит…"
                ctx = self._exec_ctx(root, client)
                shell_ctx = f"Project root: {root}\n{convo.as_context(summary)}" if summary else f"Project root: {root}"
                if att_note:
                    shell_ctx += "\n" + att_note
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
        # Файли вкладень лишаються на диску (ручна чистка) — нічого не скидаємо.

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
        task, outcome = value, ""
        try:
            task, outcome = await self._resume_planning(value, root, plan_first, client)
        except Exception as e:
            async with self:
                self.has_pending = False
                self.has_question = False
                self._append("assistant", f"⚠ Помилка: {e}", "note")
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
        async with self:
            att_note = self._attachments_note()
        full_context = (context + "\n\n" + att_note).strip() if att_note else context
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
            # Кнопки «Виконати/Відхилити» прив'язані до has_pending — ховаємо їх ОДРАЗУ
            # на старті (план уже беремо з диска нижче), інакше вони висять увесь час
            # виконання й зникають лише наприкінці.
            self.has_pending = False
            s0 = sess.load_session(cid)
            plan = s0.get_pending_plan()
            s0.clear_pending_plan()
            sess.save_session(s0)

        client = await asyncio.to_thread(get_client)
        outcome = ""
        try:
            outcome = await self._execute_steps(plan, root, client)
        except Exception as e:
            async with self:
                self._append("assistant", f"⚠ Помилка виконання: {e}", "note")
        if (outcome or "").strip():
            async with self:
                self.status = "Оновлюю памʼять…"
            await self._update_summary(plan.task, outcome)
        await self._drain_and_unbusy()

    @rx.event(background=True)
    async def discard_pending(self):
        async with self:
            if self.current_id:
                s = sess.load_session(self.current_id)
                s.clear_pending_plan()
                sess.save_session(s)
            self.has_pending = False
        await self._drain_and_unbusy()           # черга могла накопичитись поки план чекав

    @rx.event(background=True)
    async def cancel_question(self):
        async with self:
            if self.current_id:
                s = sess.load_session(self.current_id)
                s.clear_pending_question()
                sess.save_session(s)
            self._load_question(None)
        await self._drain_and_unbusy()            # те саме для скасованого питання


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
                rx.cond(
                    s["kind"] == "chat",
                    rx.menu.item("Перенести…", on_click=lambda: State.open_move_dialog(s["id"])),
                    rx.fragment(),
                ),
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


def project_item(p: dict) -> rx.Component:
    """Рядок проекту в сайдбарі: клік — фільтр чатів цього проекту; 3-крапки —
    редагувати промпт / видалити."""
    active = State.current_project_id == p["id"]
    return rx.hstack(
        rx.hstack(
            rx.icon("folder", size=14, class_name="text-gray-500 shrink-0"),
            rx.text(p["name"], class_name="text-sm text-gray-200 truncate"),
            on_click=lambda: State.select_project(p["id"]),
            class_name="items-center gap-2 grow min-w-0 cursor-pointer",
        ),
        rx.menu.root(
            rx.menu.trigger(
                rx.icon("ellipsis", size=14,
                        class_name="text-gray-500 hover:text-gray-200 cursor-pointer shrink-0"),
            ),
            rx.menu.content(
                rx.menu.item("Редагувати", on_click=lambda: State.open_edit_project(p["id"])),
                rx.menu.item("Видалити", on_click=lambda: State.delete_project_event(p["id"]),
                             class_name="text-red-400"),
            ),
        ),
        class_name=rx.cond(active, "bg-white/10", "hover:bg-white/5")
        + " items-center gap-1 px-2 py-1.5 rounded-lg w-full",
    )


def project_dialog() -> rx.Component:
    """Створення/редагування проекту: назва + промпт (до 5000 символів), що
    вшивається в системний промпт кожного повідомлення чатів цього проекту."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title(rx.cond(State.project_edit_id != "", "Редагувати проект", "Новий проект")),
            rx.input(placeholder="Назва проекту", value=State.project_name_input,
                     on_change=State.set_project_name_input, class_name="w-full mt-2"),
            rx.text_area(
                placeholder="Інструкції проекту — додаються до кожного повідомлення в чатах "
                            "цього проекту (до 5000 символів)",
                value=State.project_prompt_input, on_change=State.set_project_prompt_input,
                max_length=CP.PROMPT_MAX_CHARS, rows="8", class_name="w-full mt-2",
            ),
            rx.text("До 5000 символів — зайве буде обрізано",
                    class_name="text-xs text-gray-500 mt-1"),
            rx.flex(
                rx.button("Скасувати", variant="soft",
                          on_click=lambda: State.set_project_dialog_open(False)),
                rx.button("Зберегти", on_click=State.save_project_dialog),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.project_dialog_open,
        on_open_change=State.set_project_dialog_open,
    )


def move_dialog() -> rx.Component:
    """Перенести чат у проект (чи "без проекту")."""
    return rx.dialog.root(
        rx.dialog.content(
            rx.dialog.title("Перенести чат у проект"),
            rx.vstack(
                rx.box(
                    rx.text("Без проекту", class_name="text-sm text-gray-200"),
                    on_click=lambda: State.move_chat_to_project(""),
                    class_name="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer w-full",
                ),
                rx.foreach(
                    State.projects,
                    lambda p: rx.box(
                        rx.text(p["name"], class_name="text-sm text-gray-200"),
                        on_click=lambda: State.move_chat_to_project(p["id"]),
                        class_name="px-3 py-2 rounded-lg hover:bg-white/5 cursor-pointer w-full",
                    ),
                ),
                class_name="w-full gap-1 mt-2 max-h-72 overflow-y-auto",
            ),
            rx.flex(
                rx.button("Скасувати", variant="soft",
                          on_click=lambda: State.set_move_dialog_open(False)),
                spacing="2", justify="end", class_name="mt-3",
            ),
        ),
        open=State.move_dialog_open,
        on_open_change=State.set_move_dialog_open,
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
    def font_slider(label, value, on_change, min_v, max_v):
        return rx.hstack(
            rx.text(label, class_name="text-sm text-gray-300 w-24 shrink-0"),
            rx.slider(
                value=[value],
                on_change=on_change,
                min=min_v,
                max=max_v,
                step=1,
                class_name="flex-1",
            ),
            rx.text(value, "px", class_name="text-sm text-gray-400 w-10 shrink-0 text-right"),
            class_name="items-center gap-3 w-full",
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
                    # set_color_mode — фронтенд-тригер, що реально перемикає тему (next-themes).
                    # set_theme лише зберігає вибір у State/на диск; без set_color_mode тема
                    # візуально не мінялась (це й був баг «перемикачі нічого не роблять»).
                    on_change=lambda v: [State.set_theme(v), set_color_mode(v)],
                    size="2", class_name="mb-4 w-full",
                ),
                # Шрифти
                rx.text("Шрифти", class_name="text-xs text-gray-500 uppercase tracking-wide mb-2"),
                font_slider("Чат", State.font_chat,
                            lambda v: State.set_font_chat(v), 12, 32),
                font_slider("Інтерфейс", State.font_ui,
                            lambda v: State.set_font_ui(v), 11, 24),
                gap="4", class_name="w-full px-2",
            ),
            style={"backgroundColor": PANEL, "maxWidth": "560px", "width": "92vw"},
        ),
        open=State.settings_open,
        on_open_change=State.toggle_settings,
    )


def project_section() -> rx.Component:
    """Проекти chat-режиму (аналог Claude/ChatGPT Projects): список + "+ Новий
    проект". Лише mode="chat" — код-режим має власну прив'язку до робочої теки."""
    return rx.cond(
        State.is_chat,
        rx.fragment(
            rx.hstack(
                rx.text("Проекти", class_name="text-xs text-gray-500 uppercase tracking-wide"),
                rx.spacer(),
                rx.icon("plus", size=13, on_click=State.open_new_project,
                        class_name="text-gray-500 hover:text-gray-200 cursor-pointer"),
                class_name="items-center mt-5 mb-1 px-2",
            ),
            rx.cond(
                State.projects,
                rx.vstack(rx.foreach(State.projects, project_item), class_name="w-full gap-0.5"),
                rx.text("Поки немає проектів", class_name="text-sm text-gray-500 px-2"),
            ),
        ),
        rx.fragment(),
    )


def sidebar() -> rx.Component:
    return rx.flex(
        settings_dialog(),
        mode_switch(),
        nav_item("plus", "Новий чат", State.new_chat),
        project_section(),
        rx.cond(
            State.is_chat & (State.current_project_id != ""),
            rx.hstack(
                rx.icon("arrow-left", size=12),
                rx.text("Усі чати", class_name="text-xs"),
                on_click=lambda: State.select_project(""),
                class_name="items-center gap-1 text-gray-500 hover:text-gray-200 cursor-pointer "
                           "px-2 mt-3",
            ),
            rx.fragment(),
        ),
        rx.text("Recents", class_name=rx.cond(State.is_chat, "mt-2", "mt-5")
                + " text-xs text-gray-500 uppercase tracking-wide mb-1 px-2"),
        rx.vstack(
            rx.cond(
                State.visible_sessions,
                rx.foreach(State.visible_sessions, session_item),
                rx.text("Поки немає чатів", class_name="text-sm text-gray-400 px-2"),
            ),
            class_name="flex-1 w-full gap-0.5 overflow-y-auto",
        ),
        rx.spacer(),
        # self-start: не розтягувати на всю ширину колонки (інакше контур-пігулка йде
        # через увесь сайдбар). icon_button — квадрат за розміром іконки, ліворуч.
        rx.icon_button(
            rx.icon("settings", size=18),
            on_click=State.toggle_settings,
            variant="ghost",
            color_scheme="gray",
            size="2",
            class_name="self-start mb-2 ml-1 rounded-full hover:bg-white/10 text-gray-400",
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
            rx.select.root(
                rx.select.trigger(variant="soft", width="6.2rem"),
                rx.select.content(
                    rx.select.item("smart", value="smart"),
                    rx.select.item("ask", value="ask"),
                    rx.select.item("auto", value="auto"),
                    rx.select.item("allowlist", value="allowlist"),
                    rx.select.item("off", value="off"),
                ),
                value=State.shell, on_change=State.set_shell,
                size="1",
            ),
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
            # Прибрати дефолтну рамку/padding rx.upload (вони задаються як ПРЯМІ
            # props через setdefault, не через style) — лишити тільки іконку-кнопку.
            border="none",
            padding="0",
            width="fit-content",
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
                               "resize-none outline-none border-none",
                    style={"fontSize": State.chat_font_px},
                    rows="2",
                ),
                controls_bar(),
                rx.cond(State.is_chat, rx.fragment(), references_row()),
                attachment_chips(),
                class_name="w-full rounded-2xl p-3 border " + BORDER,
                style={"backgroundColor": INPUT},
            ),
            class_name="w-full gap-0",      # ширину задає батьківський контейнер
        ),
        on_submit=State.send_task,
        reset_on_submit=False,
        class_name="w-full",
    )


def message_bubble(m: dict) -> rx.Component:
    mine = m["role"] == "user"
    return rx.box(
        rx.cond(
            mine,
            rx.vstack(
                rx.text(m["content"], class_name="whitespace-pre-wrap text-gray-100",
                        style={"fontSize": State.chat_font_px}),
                rx.cond(
                    m["attachments"].to(list[str]).length() > 0,
                    rx.hstack(
                        rx.foreach(
                            m["attachments"].to(list[str]),
                            lambda n: rx.hstack(
                                rx.icon("file", size=11, class_name="text-gray-400"),
                                rx.text(n, class_name="text-xs text-gray-300 truncate max-w-40"),
                                class_name="items-center gap-1 px-2 py-0.5 rounded-full bg-black/20"),
                        ),
                        class_name="flex-wrap gap-1 mt-1 justify-end",
                    ),
                    rx.fragment(),
                ),
                class_name="items-end gap-0",
            ),
            rx.box(
                rx.cond(
                    m["thinking"] != "",
                    rx.el.details(
                        rx.el.summary(
                            rx.cond(m["kind"] == "step", "хід виконання", "Роздуми моделі"),
                            class_name="text-xs text-gray-500 cursor-pointer select-none"),
                        rx.markdown(m["thinking"], class_name="max-w-full overflow-x-auto break-words"),
                        class_name="mb-2 border-l-2 border-white/10 pl-2 max-h-80 max-w-full "
                                   "overflow-y-auto overflow-x-hidden",
                    ),
                    rx.fragment(),
                ),
                rx.markdown(m["content"], class_name="max-w-full overflow-x-auto break-words"),
                style={"fontSize": State.chat_font_px},
                class_name="min-w-0 max-w-full",
            ),
        ),
        # user — компактна бульбашка справа; assistant — на всю ширину (вирівняно з полем вводу).
        class_name=rx.cond(
            mine,
            "self-end bg-white/10 max-w-[85%]",
            "self-start bg-white/[0.03] w-full min-w-0",
        ) + " rounded-2xl px-4 py-2.5",
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
                rx.markdown(State.stream_thinking, class_name="max-w-full overflow-x-auto break-words"),
                class_name="mb-2 border-l-2 border-white/10 pl-2 max-h-80 max-w-full "
                           "overflow-y-auto overflow-x-hidden",
            ),
            rx.fragment(),
        ),
        rx.markdown(State.stream_content, class_name="max-w-full overflow-x-auto break-words"),
        class_name="self-start bg-white/[0.03] rounded-2xl px-4 py-2.5 w-full min-w-0",
        style={"fontSize": State.chat_font_px},
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
        class_name="w-full flex-1 overflow-y-auto overflow-x-hidden min-w-0 px-[1cm] py-6 gap-3",
        # scrollbar-gutter: stable — скролбар завжди резервує місце праворуч, тож ширина
        # контенту стабільна; поле вводу компенсує ту саму ширину через --sbw (нижче).
        style={"fontSize": State.chat_font_px, "scrollbarGutter": "stable"},
    )


def scroll_down_button() -> rx.Component:
    """Кнопка-стрілка: промотати чат донизу. По центру, над полем вводу."""
    return rx.button(
        rx.icon("arrow-down", size=18),
        on_click=rx.call_script(SCROLL_BOTTOM_JS),
        radius="full", size="2",
        id="scroll-down-btn",
        # Початково сховано; SCROLL_SETUP_JS показує лише коли чат прокручено вгору.
        style={"display": "none"},
        class_name="absolute left-1/2 -translate-x-1/2 bottom-full mb-3 bg-white/10 "
                   "hover:bg-white/20 text-gray-100 shadow-lg backdrop-blur z-10",
    )


def main_area() -> rx.Component:
    return rx.cond(
        State.messages,
        rx.vstack(
            chat_view(),
            rx.box(
                scroll_down_button(),
                input_box(),
                # Праворуч додаємо ширину скролбара (--sbw), щоб рамка поля вводу
                # збігалася з правим краєм відповіді (чат має скролбар, інпут — ні).
                class_name="w-full pl-[1cm] pr-[calc(1cm+var(--sbw,0px))] pb-4 relative",
            ),
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
        project_dialog(),
        move_dialog(),
        confirm_dialog(),
        class_name="h-screen w-screen overflow-hidden",
        style={"backgroundColor": BG},
        spacing="0",
    )


app = rx.App(
    stylesheets=["https://fonts.googleapis.com/css2?family=Newsreader:ital@0;1&display=swap"],
)
app.add_page(index, title="local-code-agent",
             on_load=[State.load_sessions, State.load_settings])
