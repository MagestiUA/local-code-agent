"""Сесія = «чат» Claude-Code-стилю: прив'язана до проекту, з дозволами та
історією. Зберігається глобально (один JSON на сесію), тож сайдбар бачить усі
чати з усіх проектів.

Дозволи (керують confirm-колбеком та run_shell):
  edits: "ask" | "auto"
  shell: "off" | "allowlist" | "ask"
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

SESSIONS_DIR = Path.home() / ".local-code-agent" / "sessions"

DEFAULT_PERMISSIONS = {"edits": "ask", "shell": "allowlist"}


@dataclass
class Session:
    id: str
    title: str
    project_root: str
    permissions: dict = field(default_factory=lambda: dict(DEFAULT_PERMISSIONS))
    init_scan: bool = False
    plan_first: bool = False                              # завжди планувати наперед
    reference_files: list = field(default_factory=list)   # read-only джерела (абс. шляхи)
    pending_plan: dict | None = None                      # план, що очікує виконання
    messages: list = field(default_factory=list)          # [{role, content, kind, meta}]
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def add_message(self, role: str, content: str, kind: str = "text", meta: dict | None = None) -> None:
        self.messages.append({"role": role, "content": content, "kind": kind, "meta": meta or {}})
        self.updated = time.time()

    def add_reference(self, path: str) -> None:
        p = str(path)
        if p not in self.reference_files:
            self.reference_files.append(p)
            self.updated = time.time()

    def remove_reference(self, path: str) -> None:
        if str(path) in self.reference_files:
            self.reference_files.remove(str(path))
            self.updated = time.time()

    def set_pending_plan(self, state) -> None:
        self.pending_plan = asdict(state)
        self.updated = time.time()

    def get_pending_plan(self):
        from .memory import state_from_dict
        return state_from_dict(self.pending_plan) if self.pending_plan else None

    def clear_pending_plan(self) -> None:
        self.pending_plan = None
        self.updated = time.time()


def _dir(base: str | Path | None = None) -> Path:
    d = Path(base) if base else SESSIONS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_session(title: str, project_root: str | Path,
                permissions: dict | None = None, init_scan: bool = False) -> Session:
    return Session(
        id=uuid.uuid4().hex[:12],
        title=title,
        project_root=str(project_root),
        permissions=permissions or dict(DEFAULT_PERMISSIONS),
        init_scan=init_scan,
    )


def save_session(s: Session, base: str | Path | None = None) -> Path:
    p = _dir(base) / f"{s.id}.json"
    p.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_session(sid: str, base: str | Path | None = None) -> Session:
    data = json.loads((_dir(base) / f"{sid}.json").read_text(encoding="utf-8"))
    return Session(**data)


def list_sessions(base: str | Path | None = None) -> list[dict]:
    """Короткий перелік для сайдбара (нові згори)."""
    out = []
    for p in sorted(_dir(base).glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": d["id"], "title": d["title"],
                        "project_root": d["project_root"], "updated": d.get("updated")})
        except Exception:
            pass
    return out
