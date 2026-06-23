"""Інтеграційний тест код-флоу: планувальник -> виконання кроків плану.
Стаб моделі (GPU-free). Ловить регресії класу «падає після планування /
кроки не виконуються». Запуск: .venv\\Scripts\\python.exe -m tests.test_code_flow
"""
import tempfile
from pathlib import Path

from agent.agent_loop import run_step
from agent.memory import TaskState, render_for_planner, state_from_dict
from agent.toolkit import ToolContext
from dataclasses import asdict


class ToolStub:
    """Підставна модель: повертає заскриптовані відповіді по черзі (як LoopStub)."""
    def __init__(self, scripted):
        self.scripted = scripted
        self.i = 0
        self.last_stats = None

    def chat(self, messages, tools=None, profile=None, fmt=None):
        m = self.scripted[min(self.i, len(self.scripted) - 1)]
        self.i += 1
        return m


def _exec_plan(plan, ctx, client_for_step):
    """Аналог _execute_steps без Reflex: проганяє кроки плану через run_step.
    Повертає список (step_id, status). Кидає виняток, якщо щось ламається —
    саме це ми й хочемо ловити в тесті."""
    results = []
    for step in plan.steps:
        if step.kind == "inline":
            plan.set_result(step.id, "done", "inline")
            results.append((step.id, "done"))
            continue
        final, log = run_step(step.description, ctx, client=client_for_step(step),
                              context=f"Project root: {ctx.root}", max_iters=5)
        plan.set_result(step.id, "done" if log else "failed", final or "—")
        results.append((step.id, "done" if log else "failed"))
    return results


def main():
    root = Path(tempfile.mkdtemp(prefix="lca_flow_"))
    ctx = ToolContext(root=root, permissions={"edits": "auto", "shell": "auto"})

    # 1) Планувальник (емуляція результату deliberate=plan): 2 кроки + inline.
    plan = TaskState(task="створити два файли")
    plan.add_step("deterministic", "Створити a.py", target="a.py")
    plan.add_step("llm", "Створити b.py з функцією")
    plan.add_step("inline", "нічого не робити")
    assert len(plan.steps) == 3

    # 2) План переживає round-trip через сесію (asdict <-> state_from_dict).
    plan = state_from_dict(asdict(plan))
    assert [s.kind for s in plan.steps] == ["deterministic", "llm", "inline"]

    # 3) Виконання: кожен llm/deterministic-крок кличе write_file, тоді завершує.
    def client_for_step(step):
        name = "a.py" if "a.py" in step.description else "b.py"
        body = "x = 1\n" if name == "a.py" else "def f():\n    return 1\n"
        return ToolStub([
            {"tool_calls": [{"function": {"name": "write_file",
                                          "arguments": {"path": name, "content": body}}}]},
            {"content": f"Готово: {name}", "tool_calls": []},
        ])

    results = _exec_plan(plan, ctx, client_for_step)

    # 4) Перевірки: обидва файли створені, inline помічений done, винятку нема.
    assert (root / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    assert "def f" in (root / "b.py").read_text(encoding="utf-8")
    assert dict(results) == {1: "done", 2: "done", 3: "done"}

    # 5) render_for_planner на виконаному плані не падає.
    out = render_for_planner(plan)
    assert "a.py" in out or "створити" in out.lower()

    print("OK: code-flow — план (2 llm/det + inline) round-trip, виконання кроків, "
          "write_file спрацював, статуси done, render_for_planner")


if __name__ == "__main__":
    main()
