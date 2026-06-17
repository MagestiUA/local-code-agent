"""Офлайн-тест диспетчера (стаб маршрутизує відповіді за системним промптом).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_runner
"""
import tempfile
from pathlib import Path

from agent.runner import handle


class RouteStub:
    """Стаб LLM: відповідає залежно від того, який системний промпт прийшов."""
    def __init__(self, mode: str, answer_text: str = "(ans)"):
        self.mode = mode
        self.answer_text = answer_text

    def chat(self, messages, tools=None, profile=None, fmt=None):
        sysmsg = messages[0]["content"]
        if "into exactly one mode" in sysmsg:   # унікально для intent-класифікатора
            return {"content": f'{{"mode": "{self.mode}"}}'}
        if "code analysis assistant" in sysmsg:
            return {"content": self.answer_text}
        if "run a command" in sysmsg:
            return {"content": "pytest -q"}
        if "planning module" in sysmsg:
            return {"content": '{"steps":[{"kind":"llm","description":"do x","target":"x.py"}]}'}
        return {"content": ""}


def main() -> None:
    root = Path(tempfile.mkdtemp())
    (root / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    # answer
    r1 = handle("що робить x.py?", root=root, client=RouteStub("answer", "Повертає 1."))
    assert r1.mode == "answer" and r1.text == "Повертає 1.", r1

    # shell (команда дозволена -> виконується; pytest у порожньому проекті дасть rc!=0, ок)
    r2 = handle("прожени тести", root=root, client=RouteStub("shell"))
    assert r2.mode == "shell" and "pytest -q" in r2.text, r2

    # plan: план повертається, але файл НЕ змінюється
    before = (root / "x.py").read_text(encoding="utf-8")
    r3 = handle("давай спланую рефакторинг x.py", root=root, client=RouteStub("plan"))
    assert r3.mode == "plan" and r3.state is not None and len(r3.state.steps) == 1, r3
    assert (root / "x.py").read_text(encoding="utf-8") == before, "plan не повинен правити файли"

    # execute_plan: виконання готового плану (deterministic, без LLM)
    from agent.memory import TaskState
    from agent.runner import execute_plan
    st = TaskState(task="t")
    st.add_step("deterministic", "зробити Х")
    r4 = execute_plan(st, client=RouteStub("edit"), root=str(root),
                      det_handler=lambda step: "готово")
    assert r4.mode == "edit" and st.steps[0].status == "done", r4

    print("OK: диспетчер маршрутизує answer / shell / plan; execute_plan виконує план")
    print(f"  answer -> {r1.text}")
    print(f"  plan   -> {len(r3.state.steps)} крок, файл не змінено")


if __name__ == "__main__":
    main()
