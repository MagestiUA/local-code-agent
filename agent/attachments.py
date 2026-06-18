"""Обробка файлів, прикріплених користувачем до повідомлення.

Архітектура (map-reduce):
  1. Користувач прикріплює файли → зберігаються як метадані + вміст (модуль-рівень).
  2. Планувальник бачить ТІЛЬКИ імена і розміри → складає план (кожен файл = крок).
  3. Виконавець кроку отримує вміст ОДНОГО файлу через format_single().
  4. Reduce: планувальник зшиває результати кроків.

classify()        — текст чи бінар.
process()         — декодування + обрізання до бюджету.
format_single()   — блок одного файлу для виконавця кроку.
find_for_step()   — чи згадується якийсь файл у описі кроку.
attachment_summary() — короткий список імен/розмірів для планувальника.
"""
from __future__ import annotations

from pathlib import Path

MAX_FILE_SIZE = 200 * 1024      # 200 KB — ліміт розміру одного файлу
MAX_FILES = 20                  # максимальна кількість вкладень
CHAR_BUDGET = 12_000            # символів на один файл у промпті виконавця

TEXT_EXTENSIONS = {
    ".py", ".pyw", ".pyi",
    ".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".scss", ".sass",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".md", ".txt", ".rst", ".log", ".csv",
    ".sh", ".bash", ".zsh", ".bat", ".ps1", ".cmd",
    ".rs", ".go", ".java", ".c", ".cpp", ".h", ".hpp",
    ".sql", ".xml", ".svg",
    ".gitignore", ".dockerignore",
}


def classify(filename: str, data: bytes) -> str:
    """Повернути "text" якщо файл текстовий, інакше "binary"."""
    ext = Path(filename).suffix.lower()
    if ext in TEXT_EXTENSIONS or ext == "":
        try:
            data.decode("utf-8")
            return "text"
        except (UnicodeDecodeError, ValueError):
            pass
    return "binary"


def process(filename: str, data: bytes) -> dict:
    """Обробити завантажений файл.
    Повертає dict: {name, size, truncated, error}. Вміст НЕ зберігається тут —
    він кладеться окремо в _ATTACHMENT_CONTENT[session_id][name] (lca_web.py).
    """
    size = len(data)
    if size > MAX_FILE_SIZE:
        kb = size // 1024
        return {"name": filename, "size": size, "truncated": False,
                "error": f"завеликий ({kb} KB > {MAX_FILE_SIZE // 1024} KB)"}
    if classify(filename, data) == "binary":
        return {"name": filename, "size": size, "truncated": False,
                "error": "бінарний файл (підтримуються лише текстові)"}
    content = data.decode("utf-8", errors="replace")
    truncated = len(content) > CHAR_BUDGET
    return {"name": filename, "size": size, "truncated": truncated,
            "error": "", "_content": content[:CHAR_BUDGET]}


def format_single(name: str, content: str, truncated: bool = False) -> str:
    """Форматувати вміст одного файлу для виконавця одного кроку."""
    note = " [обрізано]" if truncated else ""
    return f"\n=== Вміст файлу: {name}{note} ===\n{content}\n=== Кінець {name} ===\n"


def find_for_step(step_description: str, meta_list: list[dict]) -> str | None:
    """Повернути ім'я файлу якщо він згадується в описі кроку, інакше None."""
    low = step_description.lower()
    for a in meta_list:
        if a.get("error"):
            continue
        if a["name"].lower() in low:
            return a["name"]
    return None


def attachment_summary(meta_list: list[dict]) -> str:
    """Короткий список вкладень для планувальника (імена + розміри, БЕЗ вмісту)."""
    if not meta_list:
        return ""
    lines = []
    for a in meta_list:
        kb = max(1, a["size"] // 1024)
        note = f" [ПОМИЛКА: {a['error']}]" if a.get("error") else \
               (" [обрізано]" if a.get("truncated") else "")
        lines.append(f"  - {a['name']} ({kb} KB){note}")
    return "Прикріплені файли:\n" + "\n".join(lines)
