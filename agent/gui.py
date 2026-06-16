"""NiceGUI веб-інтерфейс (M6b — каркас).

Працює керування сесіями (створити / список / обрати / дозволи / джерела).
Прив'язки агента ще НЕМАЄ (M6c) — надсилання задачі лише пише повідомлення.
Запуск моделі НЕ відбувається.

Запуск:  .venv\\Scripts\\python.exe -m agent.gui   (відкриє http://localhost:8080)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from nicegui import run, ui

from . import session as sess
from .executor import apply_edit, run_edit
from .llm import OllamaClient
from .memory import render_for_planner
from .planner import make_plan
from .project import ensure_project_doc
from .tools import read_file, run_shell

PERM_EDITS = ["ask", "auto"]
PERM_SHELL = ["allowlist", "ask", "off"]


@ui.page("/")
def index() -> None:
    dark = ui.dark_mode(value=True)
    ui.colors(
        primary="#6b7280", secondary="#4b5563", accent="#7c83d3",
        positive="#3f9168", negative="#b0504d",
        dark="#1b1c1f", dark_page="#141517",
    )
    cur: dict = {"s": None}   # поточна сесія

    # ── Сайдбар: список чатів ────────────────────────────────────────────────
    @ui.refreshable
    def sidebar() -> None:
        items = sess.list_sessions()
        if not items:
            ui.label("Поки немає чатів").classes("text-xs text-grey q-pa-sm")
        for it in items:
            active = cur["s"] and cur["s"].id == it["id"]
            with ui.card().tight().classes(
                "w-full cursor-pointer q-pa-sm " + ("bg-grey-3 dark:bg-grey-9" if active else "")
            ).on("click", lambda i=it: open_session(i["id"])):
                ui.label(it["title"]).classes("text-sm")
                ui.label(Path(it["project_root"]).name).classes("text-xs text-grey")

    # ── Чат ──────────────────────────────────────────────────────────────────
    @ui.refreshable
    def chat_view() -> None:
        s = cur["s"]
        if not s:
            ui.label("Створіть або оберіть чат зліва").classes("text-grey q-pa-md")
            return
        for m in s.messages:
            mine = m["role"] == "user"
            with ui.row().classes("w-full " + ("justify-end" if mine else "justify-start")):
                ui.label(m["content"]).classes(
                    "q-pa-sm rounded-borders " + ("bg-grey-3 dark:bg-grey-8" if mine else "")
                ).style("max-width:80%; white-space:pre-wrap")

    # ── Низові контролі (дрібні, другорядні) ─────────────────────────────────
    @ui.refreshable
    def controls() -> None:
        s = cur["s"]
        with ui.row().classes("items-center gap-3").style("font-size:11px"):
            if not s:
                ui.label("оберіть чат, щоб задати параметри").classes("text-xs text-grey")
                return
            # тека
            with ui.row().classes("items-center gap-1"):
                ui.icon("folder", size="14px")
                ui.label(Path(s.project_root).name).classes("text-grey")
            # джерела (read-only)
            with ui.row().classes("items-center gap-1"):
                ui.icon("lock", size="13px")
                ui.label("джерела").classes("text-grey")
                for rf in s.reference_files:
                    with ui.element("span").classes(
                        "row items-center q-px-xs rounded-borders bg-grey-3 dark:bg-grey-8"
                    ):
                        ui.label(Path(rf).name)
                        ui.icon("close", size="13px").classes("cursor-pointer").on(
                            "click", lambda r=rf: remove_ref(r))
                ui.button("файл", icon="add", on_click=add_ref_dialog) \
                    .props("flat dense size=sm")
            # дозволи
            ui.select(PERM_EDITS, value=s.permissions.get("edits", "ask"), label="правки") \
                .props("dense options-dense").style("min-width:90px") \
                .on_value_change(lambda e: set_perm("edits", e.value))
            ui.select(PERM_SHELL, value=s.permissions.get("shell", "allowlist"), label="консоль") \
                .props("dense options-dense").style("min-width:110px") \
                .on_value_change(lambda e: set_perm("shell", e.value))

    # ── Дії ──────────────────────────────────────────────────────────────────
    def open_session(sid: str) -> None:
        cur["s"] = sess.load_session(sid)
        sidebar.refresh(); chat_view.refresh(); controls.refresh()

    def set_perm(key: str, val: str) -> None:
        if cur["s"]:
            cur["s"].permissions[key] = val
            sess.save_session(cur["s"])

    def remove_ref(path: str) -> None:
        cur["s"].remove_reference(path)
        sess.save_session(cur["s"]); controls.refresh()

    def add_ref_dialog() -> None:
        with ui.dialog() as dlg, ui.card():
            ui.label("Додати файл-джерело (read-only)")
            inp = ui.input("Абсолютний шлях до файлу").classes("w-96")
            with ui.row():
                ui.button("Скасувати", on_click=dlg.close).props("flat")
                def add() -> None:
                    if inp.value.strip():
                        cur["s"].add_reference(inp.value.strip())
                        sess.save_session(cur["s"]); controls.refresh()
                    dlg.close()
                ui.button("Додати", on_click=add)
        dlg.open()

    def new_chat_dialog() -> None:
        with ui.dialog() as dlg, ui.card():
            ui.label("Новий чат").classes("text-h6")
            folder = ui.input("Папка проекту").classes("w-96")
            edits = ui.select(PERM_EDITS, value="ask", label="Правки файлів")
            shell = ui.select(PERM_SHELL, value="allowlist", label="Консоль")
            scan = ui.checkbox("Init scan — оглянути репо й створити чернетку AGENT.md")
            with ui.row():
                ui.button("Скасувати", on_click=dlg.close).props("flat")
                def create() -> None:
                    root = folder.value.strip()
                    if not root:
                        ui.notify("Вкажіть папку проекту", type="warning"); return
                    s = sess.new_session(
                        Path(root).name or "Новий чат", root,
                        permissions={"edits": edits.value, "shell": shell.value},
                        init_scan=scan.value,
                    )
                    sess.save_session(s)
                    cur["s"] = s
                    sidebar.refresh(); chat_view.refresh(); controls.refresh()
                    dlg.close()
                ui.button("Створити чат", on_click=create)
        dlg.open()

    def get_client() -> OllamaClient:
        if not cur.get("client"):
            cur["client"] = OllamaClient()
        return cur["client"]

    def build_context(s) -> str:
        """AGENT.md (+ авто-чернетка за init_scan) і вміст read-only джерел."""
        parts = []
        doc = ensure_project_doc(
            s.project_root,
            client=get_client() if s.init_scan else None,
            init_scan=s.init_scan,
        )
        if doc:
            parts.append(f"AGENT.md:\n{doc}")
        for rf in s.reference_files:
            try:
                parts.append(f"Reference {Path(rf).name} (read-only):\n{read_file(rf)[:4000]}")
            except Exception:
                pass
        return "\n\n".join(parts)

    async def ask_approval(diff: str) -> bool:
        work_area.clear()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        with work_area:
            ui.label("Запропоновані зміни").classes("text-sm")
            ui.code(diff or "(порожній diff)").classes("w-full")
            with ui.row():
                ui.button("Прийняти", on_click=lambda: fut.set_result(True)).props("color=positive")
                ui.button("Відхилити", on_click=lambda: fut.set_result(False)).props("color=negative")
        result = await fut
        work_area.clear()
        return result

    def log(content: str, kind: str = "note") -> None:
        cur["s"].add_message("assistant", content, kind=kind)
        sess.save_session(cur["s"])
        chat_view.refresh()

    async def run_task(text: str) -> None:
        s = cur["s"]
        s.add_message("user", text)
        sess.save_session(s)
        task.value = ""
        chat_view.refresh()

        client = await run.io_bound(get_client)
        context = await run.io_bound(build_context, s)

        log("Планую…")
        state = await run.io_bound(make_plan, text, "", client, context)
        log(render_for_planner(state), kind="plan")

        for step in state.steps:
            if step.kind == "inline":
                continue
            if step.kind == "deterministic":
                d = step.description.lower()
                if ("test" in d or "тест" in d) and s.permissions.get("shell") != "off":
                    r = await run.io_bound(run_shell, "pytest -q")
                    log(f"Тести: rc={r.returncode}\n{(r.stdout or r.stderr)[:800]}")
                else:
                    log(f"Детермінований крок (вручну): {step.description}")
                continue
            # llm
            if not step.target:
                log(f"Пропущено (немає файлу): {step.description}"); continue
            target = Path(s.project_root) / step.target
            if not target.exists():
                log(f"Файл не знайдено: {step.target}"); continue
            res = await run.io_bound(run_edit, target, step.description, client, False, context)
            if not res.ok:
                log(f"Помилка кроку #{step.id}: {res.error}"); continue
            approve = True if s.permissions.get("edits") == "auto" else await ask_approval(res.diff)
            if approve:
                await run.io_bound(apply_edit, target, res)
                log(f"Застосовано до {step.target}:\n{res.diff}", kind="diff")
            else:
                log(f"Відхилено: {step.target}")
        log("Готово.")

    async def send() -> None:
        if cur.get("s") and task.value.strip():
            await run_task(task.value.strip())

    # ── Розкладка ────────────────────────────────────────────────────────────
    with ui.header().classes("items-center justify-between bg-grey-10 text-grey-4") \
            .style("border-bottom:1px solid rgba(255,255,255,0.08)"):
        ui.label("local-code-agent").classes("text-subtitle1")
        with ui.button(icon="settings").props("flat round").classes("text-grey-4"):
            with ui.menu():
                ui.menu_item("Світла тема", lambda: dark.set_value(False))
                ui.menu_item("Темна тема", lambda: dark.set_value(True))
                ui.menu_item("Як у системі", lambda: dark.set_value(None))

    with ui.left_drawer(value=True).props("width=240 bordered").classes("gap-2 bg-grey-10"):
        ui.button("Новий чат", icon="add", on_click=new_chat_dialog).props("outline").classes("w-full")
        ui.label("Чати").classes("text-xs text-grey q-mt-sm")
        sidebar()

    with ui.column().classes("w-full q-pa-md gap-3"):
        chat_view()
        work_area = ui.column().classes("w-full")

    with ui.footer().classes("column gap-2 q-pa-sm bg-grey-10") \
            .style("border-top:1px solid rgba(255,255,255,0.08)"):
        with ui.row().classes("w-full items-center gap-2"):
            task = ui.input(placeholder="Опишіть задачу...").props("outlined dense").classes("flex-grow") \
                .on("keydown.enter", send)
            ui.button(icon="send", on_click=send).props("round flat").classes("text-grey-4")
        controls()


def main() -> None:
    ui.run(title="local-code-agent", port=8080, reload=False, show=True)


if __name__ in {"__main__", "__mp_main__"}:
    main()
