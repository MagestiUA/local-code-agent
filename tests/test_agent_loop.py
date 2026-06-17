"""Офлайн-тест tool-loop (стаб моделі повертає заскриптовані виклики тулів).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_agent_loop
"""
import tempfile
from pathlib import Path

from agent.agent_loop import run_step
from agent.toolkit import ToolContext


class LoopStub:
    """Підставна модель: повертає заздалегідь задані відповіді по черзі."""
    def __init__(self, scripted: list[dict]):
        self.scripted = scripted
        self.i = 0

    def chat(self, messages, tools=None, profile=None, fmt=None):
        m = self.scripted[min(self.i, len(self.scripted) - 1)]
        self.i += 1
        return m


def main() -> None:
    root = Path(tempfile.mkdtemp())
    ctx = ToolContext(root=root, permissions={"edits": "auto", "shell": "allowlist"})

    # Сценарій: модель кличе write_file, потім завершує без виклику.
    stub = LoopStub([
        {"tool_calls": [{"function": {"name": "write_file",
                                      "arguments": {"path": "new.txt", "content": "hello"}}}]},
        {"content": "Готово: створив new.txt", "tool_calls": []},
    ])
    final, log = run_step("створи new.txt", ctx, client=stub, max_iters=5)
    assert (root / "new.txt").read_text(encoding="utf-8") == "hello", "файл не створено циклом"
    assert "Готово" in final
    assert any("write_file" in entry for entry in log)

    # Ліміт ітерацій: модель щоразу кличе тул і не завершує.
    inf = LoopStub([{"tool_calls": [{"function": {"name": "list_dir", "arguments": {}}}]}])
    final2, log2 = run_step("безкінечно", ctx, client=inf, max_iters=3)
    assert "ліміт" in final2 and len(log2) == 3, (final2, log2)

    print("OK: tool-loop виконує тули через реєстр, завершується, ліміт ітерацій тримає")
    print(f"  журнал кроку 1: {log}")


if __name__ == "__main__":
    main()
