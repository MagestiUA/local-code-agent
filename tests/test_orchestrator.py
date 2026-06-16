"""Офлайн-тест оркестратора (БЕЗ Ollama — через стаб-клієнт).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_orchestrator
"""
import tempfile
from pathlib import Path

from agent.memory import TaskState
from agent.orchestrator import run_plan


class StubClient:
    """Підставний LLM: повертає заздалегідь задану відповідь, GPU не чіпає."""
    def __init__(self, reply: str):
        self.reply = reply

    def chat(self, messages, tools=None, profile=None, fmt=None):
        return {"content": self.reply}


def main() -> None:
    d = Path(tempfile.mkdtemp())
    f = d / "mod.py"
    f.write_text("def f():\n    return 1\n", encoding="utf-8")

    stub = StubClient("```python\ndef f():\n    return 2  # changed\n```")

    st = TaskState(task="змінити f на 2")
    st.add_step("llm", "змінити повернення на 2", target="mod.py")
    st.add_step("inline", "тривіальна дрібниця")
    st.add_step("deterministic", "прогнати тести")

    st = run_plan(
        st, client=stub,
        confirm=lambda step, diff: True,          # авто-згода
        root=str(d),
        det_handler=lambda step: "виконано (заглушка)",
    )

    assert "return 2" in f.read_text(encoding="utf-8"), "llm-крок не записався"
    assert st.steps[0].status == "done", st.steps[0].result
    assert st.steps[1].status == "done"   # inline
    assert st.steps[2].status == "done"   # deterministic через обробник
    # бекап оригіналу створено
    assert any(p.name.startswith("mod.py.bak") for p in d.iterdir()), "бекап не створено"

    print("OK: оркестратор маршрутизує llm/inline/deterministic, пише з бекапом, пам'ять оновлена")
    for s in st.steps:
        print(f"  #{s.id} [{s.kind}] {s.status}: {s.result}")


if __name__ == "__main__":
    main()
