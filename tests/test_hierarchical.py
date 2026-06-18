"""Офлайн-тест ПРОТОТИПУ R1 (hierarchical) зі стаб-моделлю.
Запуск:  .venv\\Scripts\\python.exe -m tests.test_hierarchical
Перевіряємо детекцію переповнення + контроль-флоу (map -> reduce -> redo).
"""
import json
import tempfile
from pathlib import Path

from agent import hierarchical as H
from agent.toolkit import ToolContext


class SeqStub:
    """Повертає заскриптовані відповіді по черзі (як OllamaClient.chat).
    Рядок -> {'content': ...}; dict -> віддається як message як є."""
    def __init__(self, scripted):
        self.scripted = scripted
        self.i = 0
        self.last_stats = {}

    def chat(self, messages, tools=None, profile=None, fmt=None):
        x = self.scripted[min(self.i, len(self.scripted) - 1)]
        self.i += 1
        return {"content": x} if isinstance(x, str) else x


def main() -> None:
    # ── детекція переповнення ────────────────────────────────────────────────
    assert abs(H.context_usage(32768, 65536) - 0.5) < 1e-9
    assert not H.is_overflow(40000, 65536)              # ~0.61 < 0.85
    assert H.is_overflow(60000, 65536)                  # ~0.92 >= 0.85
    assert H.context_usage(10, 0) == 0.0                # без ділення на нуль

    root = Path(tempfile.mkdtemp())
    ctx = ToolContext(root=root, permissions={"edits": "auto", "shell": "off"})

    # Сценарій А: план з 2 кроків -> обидва виконуються -> звірка ok.
    plan = {"steps": [{"kind": "llm", "description": "крок1", "target": "a.py"},
                      {"kind": "llm", "description": "крок2", "target": "b.py"}]}
    stub = SeqStub([
        json.dumps(plan),                               # make_plan
        {"content": "зробив крок1", "tool_calls": []},  # run_step #1 (одразу фініш)
        {"content": "зробив крок2", "tool_calls": []},  # run_step #2
        json.dumps({"ok": True}),                       # check_consistency
    ])
    state = H.solve("задача", ctx, client=stub)
    assert len(state.steps) == 2
    assert all(s.status == "done" for s in state.steps), [s.status for s in state.steps]

    # Сценарій Б: звірка каже redo кроку 2 -> переобдумування -> потім ok.
    stub2 = SeqStub([
        json.dumps(plan),                               # make_plan
        {"content": "к1", "tool_calls": []},            # step1
        {"content": "к2", "tool_calls": []},            # step2
        json.dumps({"ok": False, "step_id": 2, "note": "не узгоджено з к1"}),  # redo step2
        {"content": "к2-виправлено", "tool_calls": []}, # rerun step2
        json.dumps({"ok": True}),                       # звірка ok
    ])
    state2 = H.solve("задача", ctx, client=stub2)
    assert state2.steps[1].result == "к2-виправлено", state2.steps[1].result

    # Сценарій В: звірка зациклюється (завжди redo) -> ліміт раундів тримає.
    stub3 = SeqStub([
        json.dumps({"steps": [{"kind": "llm", "description": "s", "target": "x.py"}]}),
        {"content": "ok", "tool_calls": []},            # step1
        json.dumps({"ok": False, "step_id": 1, "note": "ще"}),  # redo (раунд 1)
        {"content": "ok2", "tool_calls": []},
        json.dumps({"ok": False, "step_id": 1, "note": "ще"}),  # redo (раунд 2)
        {"content": "ok3", "tool_calls": []},
        json.dumps({"ok": False, "step_id": 1, "note": "ще"}),  # (не дійде — ліміт=2)
    ])
    state3 = H.solve("задача", ctx, client=stub3, max_rounds=2)
    assert len(state3.steps) == 1   # не впало, завершилось за лімітом

    print("OK: hierarchical — переповнення, map->reduce, redo-крок, ліміт раундів")


if __name__ == "__main__":
    main()
