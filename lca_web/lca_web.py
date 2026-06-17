"""local-code-agent — веб-інтерфейс на Reflex (каркас у стилі Claude).

G2: статична розкладка (сайдбар зі структурою + greeting + поле вводу).
Прив'язки до бекенду ще немає (G3+).
"""
import reflex as rx

# Тепла темна палітра, близька до Claude.ai
BG = "#262624"        # основний фон
PANEL = "#171615"     # сайдбар (темніший за основний)
INPUT = "#30302e"     # поле вводу
BORDER = "border-white/10"
SERIF = {"fontFamily": "Newsreader, Georgia, serif"}


def nav_item(icon: str, label: str) -> rx.Component:
    return rx.hstack(
        rx.icon(icon, size=16, class_name="text-gray-400"),
        rx.text(label, class_name="text-sm text-gray-200"),
        class_name="items-center gap-3 px-2 py-1.5 rounded-lg hover:bg-white/5 "
                   "cursor-pointer w-full",
    )


def sidebar() -> rx.Component:
    return rx.flex(
        nav_item("plus", "Новий чат"),
        nav_item("settings", "Налаштування"),
        rx.text("Recents", class_name="text-xs text-gray-500 uppercase tracking-wide "
                                       "mt-5 mb-1 px-2"),
        rx.vstack(
            rx.text("Поки немає чатів", class_name="text-sm text-gray-400 px-2"),
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


def controls_bar() -> rx.Component:
    return rx.hstack(
        rx.button(rx.icon("paperclip", size=15), variant="ghost", size="1",
                  class_name="text-gray-400"),
        rx.button(rx.icon("folder", size=14), "тека", variant="ghost", size="1",
                  class_name="text-gray-400 gap-1"),
        rx.select(["ask", "auto"], default_value="ask", size="1", variant="soft",
                  width="5rem"),
        rx.select(["allowlist", "ask", "off"], default_value="allowlist", size="1",
                  variant="soft", width="6.2rem"),
        rx.spacer(),
        rx.text("план наперед", class_name="text-xs text-gray-500"),
        rx.switch(size="1"),
        rx.button(rx.icon("arrow-up", size=16), size="1", radius="full",
                  class_name="bg-white text-black ml-1"),
        class_name="w-full items-center mt-2 gap-2",
    )


def input_box() -> rx.Component:
    return rx.box(
        rx.text_area(
            placeholder="Опишіть задачу...",
            class_name="w-full bg-transparent text-gray-100 placeholder:text-gray-500 "
                       "resize-none outline-none border-none text-base",
            rows="2",
        ),
        controls_bar(),
        rx.hstack(
            rx.icon("lock", size=12, class_name="text-gray-500"),
            rx.text("джерела: нема", class_name="text-xs text-gray-500"),
            rx.button(rx.icon("plus", size=12), "файл", variant="ghost", size="1",
                      class_name="text-gray-400"),
            class_name="items-center gap-2 mt-1",
        ),
        class_name="w-full max-w-2xl rounded-2xl p-3 border " + BORDER,
        style={"backgroundColor": INPUT},
    )


def main_area() -> rx.Component:
    return rx.center(
        rx.vstack(
            rx.heading("Back at it, Микола",
                       class_name="text-4xl text-gray-200 mb-2", style=SERIF),
            input_box(),
            spacing="5",
            class_name="w-full max-w-2xl items-center px-4",
        ),
        class_name="flex-1 h-full",
    )


def index() -> rx.Component:
    return rx.hstack(
        sidebar(),
        main_area(),
        class_name="h-screen w-screen overflow-hidden",
        style={"backgroundColor": BG},
        spacing="0",
    )


app = rx.App(
    theme=rx.theme(appearance="dark"),
    stylesheets=["https://fonts.googleapis.com/css2?family=Newsreader:ital@0;1&display=swap"],
)
app.add_page(index, title="local-code-agent")
