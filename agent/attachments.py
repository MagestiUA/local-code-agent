"""Обробка файлів, прикріплених користувачем до повідомлення.

Архітектура (файли на диску):
  1. Користувач прикріплює файли → валідуються (classify/process) і ЗБЕРІГАЮТЬСЯ
     на диск у теці сесії: ~/.local-code-agent/attachments/<session_id>/.
  2. Моделі в контекст передається лише ШЛЯХ до теки + список імен (attachments_note),
     БЕЗ вмісту. Модель сама читає потрібні файли через тул read_file/list_dir.
  3. Чистка — ручна (видалення чіпа = видалення файлу з диска); тека сесії живе,
     поки користувач її не прибере.

classify()           — текст чи бінар.
process()            — декодування + обрізання до бюджету (вміст у `_content`).
session_dir()        — тека вкладень сесії на диску.
save()/remove()/clear()/rename_session() — операції з файлами на диску.
list_saved()         — метадані файлів, що вже лежать у теці (для відновлення chips).
attachments_note()   — шлях до теки + список повних шляхів для контексту моделі.
"""
from __future__ import annotations

import shutil
from pathlib import Path

MAX_FILE_SIZE = 200 * 1024      # 200 KB — ліміт розміру одного файлу
MAX_FILES = 20                  # максимальна кількість вкладень
CHAR_BUDGET = 12_000            # символів на один файл (обрізання при збереженні)

# Тека вкладень — у корені репозиторію (.attachments/<sid>/), щоб файли було легко
# знайти. attachments.py лежить у <repo>/agent/, тож корінь = parent.parent.
# Ім'я з крапкою — щоб Reflex dev-watcher НЕ робив hot-reload при записі файлів сюди
# (is_excluded_by_default виключає теки на "."); на Windows тека все одно видима.
ATTACHMENTS_ROOT = Path(__file__).resolve().parent.parent / ".attachments"

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
    """Валідувати й декодувати завантажений файл.
    Повертає dict: {name, size, truncated, error, _content}. Поле `_content`
    (обрізаний до CHAR_BUDGET текст) caller дістає й пише на диск через save();
    при помилці (`error` != "") поля `_content` немає.
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


def _safe_name(name: str) -> str:
    """Тільки базове ім'я файлу — захист від traversal (../, абсолютні шляхи)."""
    return Path(name).name


def session_dir(session_id: str) -> Path:
    """Тека вкладень для сесії на диску."""
    return ATTACHMENTS_ROOT / session_id


def save(session_id: str, name: str, content: str) -> Path:
    """Зберегти текстовий вміст файлу в теку сесії. Повертає шлях до файлу."""
    d = session_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / _safe_name(name)
    p.write_text(content, encoding="utf-8")
    return p


def remove(session_id: str, name: str) -> None:
    """Видалити один файл вкладення з диска (ручна чистка користувачем)."""
    try:
        (session_dir(session_id) / _safe_name(name)).unlink()
    except OSError:
        pass


def clear(session_id: str) -> None:
    """Видалити всю теку вкладень сесії (напр. при видаленні чату)."""
    shutil.rmtree(session_dir(session_id), ignore_errors=True)


def rename_session(old_id: str, new_id: str) -> None:
    """Перенести теку вкладень old_id → new_id (міграція пре-сесійного `_tmp`)."""
    src = session_dir(old_id)
    if not src.is_dir():
        return
    dst = session_dir(new_id)
    if dst.exists():
        for f in src.iterdir():                 # злити: перенести файли
            f.replace(dst / f.name)
        shutil.rmtree(src, ignore_errors=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dst)


def list_saved(session_id: str) -> list[dict]:
    """Метадані файлів, що лежать у теці сесії (для відновлення chips при виборі сесії).
    На диску лежать лише валідні текстові файли, тож error завжди ''."""
    d = session_dir(session_id)
    if not d.is_dir():
        return []
    return [{"name": p.name, "size": p.stat().st_size, "truncated": False, "error": ""}
            for p in sorted(d.iterdir()) if p.is_file()]


def attachments_note(session_id: str, meta_list: list[dict]) -> str:
    """Контекстний блок для моделі: імена прикріплених файлів (БЕЗ вмісту). Модель
    читає потрібні через тул read_attachment(name) — він знаходить файл за іменем у
    теці сесії (нечіткий пошук), тож копіювати довгі шляхи не треба."""
    files = [a for a in meta_list if not a.get("error")]
    if not files:
        return ""
    lines = [f"  - {a['name']}" for a in files]
    return ("Користувач прикріпив до цієї розмови такі файли (доступні через тул). Щоб "
            "побачити вміст — виклич read_attachment(name) з іменем файлу (можна кілька "
            "викликів одним ходом). НЕ проси користувача надсилати їх ще раз — вони вже "
            "є на диску й читаються тулом:\n" + "\n".join(lines))
