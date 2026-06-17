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
        if "Classify" in sysmsg:
            return {"content": f'{{"mode": "{self.mode}"}}'}
        if "code analysis assistant" in sysmsg:
            return {"content": self.answer_text}
        if "run a command" in sysmsg:
            return {"content": "pytest -q"}
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

    print("OK: диспетчер маршрутизує answer і shell")
    print(f"  answer -> {r1.text}")
    print(f"  shell  -> {r2.text.splitlines()[0]}")


if __name__ == "__main__":
    main()
