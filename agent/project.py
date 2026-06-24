"""Проектне ЗНАННЯ (на відміну від історії): AGENT.md — курована довідка для
моделі, що вшивається в кожну задачу. Тримати коротко (йде в кожен промпт).

  load_project_doc   — прочитати AGENT.md, якщо є
  scan_structure     — детерміновано (без LLM) зібрати дерево файлів
  draft_project_doc  — згенерувати чернетку через LLM (потребує клієнта/GPU)
  ensure_project_doc — є файл? повернути; інакше за init_scan -> згенерувати+зберегти
"""
from __future__ import annotations

import os
from pathlib import Path

from . import config

DOC_NAME = "AGENT.md"
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".idea", ".agent",
    "dist", "build", ".mypy_cache", ".pytest_cache", "scratch", ".web", ".states",
    "reflex.lock", ".attachments",
}

DRAFT_SYSTEM = (
    "You document a code project for a coding agent. Given the file structure and key "
    "file excerpts, write a CONCISE AGENT.md (<= ~400 words): purpose, main components "
    "and entry points, conventions, how to run and test, and what the agent must NOT "
    "break. Markdown only, no fluff."
)


def doc_path(root: str | Path) -> Path:
    return Path(root) / DOC_NAME


def load_project_doc(root: str | Path) -> str | None:
    p = doc_path(root)
    return p.read_text(encoding="utf-8") if p.exists() else None


def scan_structure(root: str | Path, max_entries: int = 300) -> str:
    """Детерміновано: відносні шляхи файлів, з відсіканням службових тек."""
    root = Path(root)
    lines: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel = Path(dirpath).relative_to(root)
        for f in sorted(filenames):
            lines.append((rel / f).as_posix())
            if len(lines) >= max_entries:
                lines.append("... (обрізано)")
                return "\n".join(lines)
    return "\n".join(lines)


def _key_excerpts(root: str | Path, limit: int = 2000) -> str:
    out = []
    for n in ("README.md", "README", "pyproject.toml", "package.json"):
        p = Path(root) / n
        if p.exists():
            out.append(f"--- {n} ---\n{p.read_text(encoding='utf-8', errors='replace')[:limit]}")
    return "\n\n".join(out)


def draft_project_doc(root: str | Path, client) -> str:
    """Генерує чернетку AGENT.md (LLM, потребує GPU)."""
    user = f"File structure:\n{scan_structure(root)}\n\nKey files:\n{_key_excerpts(root)}"
    msg = client.chat(
        [{"role": "system", "content": DRAFT_SYSTEM}, {"role": "user", "content": user}],
        profile=config.PLANNER,
    )
    return (msg.get("content") or "").strip()


def ensure_project_doc(root: str | Path, client=None, init_scan: bool = False) -> str | None:
    """Є AGENT.md -> повернути. Інакше, якщо init_scan і є клієнт -> згенерувати,
    зберегти й повернути. Інакше None (працюємо без проектного контексту)."""
    existing = load_project_doc(root)
    if existing is not None:
        return existing
    if init_scan and client is not None:
        draft = draft_project_doc(root, client)
        if draft:
            doc_path(root).write_text(draft, encoding="utf-8")
        return draft or None
    return None
