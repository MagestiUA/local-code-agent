"""Проекти chat-режиму — спільний розділ чатів зі своїм системним промптом
(аналог Projects у Claude/ChatGPT). Лише для chat-режиму (легкий чат) —
код-режим уже має власну прив'язку до робочої теки (Session.project_root).

Незалежна фіча від agent.topics (auto-класифікація розмови в спільну памʼять):
теми модель створює сама автоматично; проекти користувач створює й веде вручну,
кожен зі своїм промптом, що йде в КОЖНЕ повідомлення чатів цього проекту.

Зберігаються глобально (як сесії, agent/session.py) — один JSON на проект.
Чат належить щонайбільше одному проекту (Session.project_id, порожньо — жодному).
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

PROJECTS_DIR = Path.home() / ".local-code-agent" / "chat_projects"
PROMPT_MAX_CHARS = 5000


@dataclass
class ChatProject:
    id: str
    name: str
    prompt: str = ""
    created: float = 0.0
    updated: float = 0.0


def _dir() -> Path:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    return PROJECTS_DIR


def new_project(name: str, prompt: str = "") -> ChatProject:
    now = time.time()
    return ChatProject(id=uuid.uuid4().hex[:12], name=name.strip() or "Новий проект",
                       prompt=prompt[:PROMPT_MAX_CHARS], created=now, updated=now)


def save_project(p: ChatProject) -> Path:
    p.prompt = p.prompt[:PROMPT_MAX_CHARS]
    p.updated = time.time()
    path = _dir() / f"{p.id}.json"
    path.write_text(json.dumps(asdict(p), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_project(pid: str) -> ChatProject | None:
    if not pid:
        return None
    path = _dir() / f"{pid}.json"
    if not path.is_file():
        return None
    return ChatProject(**json.loads(path.read_text(encoding="utf-8")))


def delete_project(pid: str) -> None:
    path = _dir() / f"{pid}.json"
    if path.exists():
        path.unlink()


def list_projects() -> list[dict]:
    """Короткий перелік для сайдбара, за іменем (без чутливості до регістру)."""
    out = []
    for p in _dir().glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": d["id"], "name": d["name"], "prompt": d.get("prompt", "")})
        except Exception:
            pass
    out.sort(key=lambda d: d["name"].lower())
    return out


def as_system_block(prompt: str) -> str:
    """Блок промпту проекту для вшивання в системний промпт чату (порожній рядок,
    якщо проекту/промпту нема)."""
    return f"Інструкції проекту:\n{prompt}\n\n" if prompt.strip() else ""
