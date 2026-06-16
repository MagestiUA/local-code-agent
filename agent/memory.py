"""Стан задачі на диску — структурований, не «LLM-summary».

Ключова ідея: планувальник бачить КОМПАКТНИЙ зріз (план зі статусами + факти +
стислі результати кроків), а не сиру історію. Так контекст не роздувається й
переживає перезапуск. Що зберігати — вирішує СХЕМА, а не модель (керована втрата).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_DIR = ".agent"


@dataclass
class Step:
    id: int
    kind: str                 # llm | deterministic | inline
    description: str
    target: str | None = None
    status: str = "pending"   # pending | done | failed
    result: str = ""          # СТИСЛИЙ результат (один рядок), не сирий вивід


@dataclass
class TaskState:
    task: str
    steps: list[Step] = field(default_factory=list)
    facts: dict = field(default_factory=dict)   # артефакти: BODY_1 -> parser.py:25
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def add_step(self, kind: str, description: str, target: str | None = None) -> Step:
        s = Step(id=len(self.steps) + 1, kind=kind, description=description, target=target)
        self.steps.append(s)
        self.updated = time.time()
        return s

    def set_result(self, step_id: int, status: str, result: str = "") -> None:
        for s in self.steps:
            if s.id == step_id:
                s.status, s.result = status, result
        self.updated = time.time()

    def next_pending(self) -> Step | None:
        return next((s for s in self.steps if s.status == "pending"), None)


def state_path(root: str | Path = ".", name: str = "current") -> Path:
    d = Path(root) / STATE_DIR
    d.mkdir(exist_ok=True)
    return d / f"task_{name}.json"


def save(state: TaskState, root: str | Path = ".", name: str = "current") -> Path:
    p = state_path(root, name)
    p.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load(root: str | Path = ".", name: str = "current") -> TaskState:
    data = json.loads(state_path(root, name).read_text(encoding="utf-8"))
    steps = [Step(**s) for s in data.pop("steps")]
    return TaskState(steps=steps, **data)


def render_for_planner(state: TaskState) -> str:
    """Компактний зріз для планувальника. БЕЗ сирих виходів — лише статуси,
    стислі результати й факти. Це й тримає контекст вузьким."""
    mark = {"done": "[x]", "failed": "[!]", "pending": "[ ]"}
    lines = [f"Задача: {state.task}", "", "План:"]
    for s in state.steps:
        tgt = f" ({s.target})" if s.target else ""
        lines.append(f"  {mark.get(s.status, '[ ]')} #{s.id} [{s.kind}]{tgt}: {s.description}")
        if s.result:
            lines.append(f"        -> {s.result}")
    if state.facts:
        lines.append("\nФакти/артефакти:")
        for k, v in state.facts.items():
            lines.append(f"  - {k}: {v}")
    return "\n".join(lines)
