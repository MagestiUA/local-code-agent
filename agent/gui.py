"""NiceGUI веб-інтерфейс (M6b — каркас).

Працює керування сесіями (створити / список / обрати / дозволи / джерела).
Прив'язки агента ще НЕМАЄ (M6c) — надсилання задачі лише пише повідомлення.
Запуск моделі НЕ відбувається.

Запуск:  .venv\\Scripts\\python.exe -m agent.gui   (відкриє http://localhost:8080)
"""
from __future__ import annotations

from pathlib import Path

from nicegui import ui

from . import session as sess

PERM_EDITS = ["ask", "auto"]
PERM_SHELL = ["allowlist", "ask", "off"]


@ui.page("/")
def index() -> None:
    dark = ui.dark_mode(value=True)
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

    def send() -> None:
        s = cur["s"]
        if not s or not task.value.strip():
            return
        s.add_message("user", task.value.strip())
        s.add_message("assistant", "(агент буде підключено в M6c)", kind="note")
        sess.save_session(s)
        task.value = ""
        chat_view.refresh()

    # ── Розкладка ────────────────────────────────────────────────────────────
    with ui.header().classes("items-center justify-between"):
        ui.label("local-code-agent").classes("text-subtitle1")
        with ui.button(icon="settings").props("flat round"):
            with ui.menu():
                ui.menu_item("Світла тема", lambda: dark.set_value(False))
                ui.menu_item("Темна тема", lambda: dark.set_value(True))
                ui.menu_item("Як у системі", lambda: dark.set_value(None))

    with ui.left_drawer(value=True).props("width=240").classes("gap-2"):
        ui.button("Новий чат", icon="add", on_click=new_chat_dialog).classes("w-full")
        ui.label("Чати").classes("text-xs text-grey q-mt-sm")
        sidebar()

    with ui.column().classes("w-full q-pa-md gap-3"):
        chat_view()

    with ui.footer().classes("column gap-2 q-pa-sm"):
        with ui.row().classes("w-full items-center gap-2"):
            task = ui.input(placeholder="Опишіть задачу...").classes("flex-grow") \
                .on("keydown.enter", send)
            ui.button(icon="send", on_click=send).props("round")
        controls()


def main() -> None:
    ui.run(title="local-code-agent", port=8080, reload=False, show=True)


if __name__ in {"__main__", "__mp_main__"}:
    main()
