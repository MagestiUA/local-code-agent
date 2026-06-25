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

    # Порожня відповідь без tool_calls (модель "видихнулась") -> nudge -> реальна дія.
    empty_then_call = LoopStub([
        {"content": "", "tool_calls": []},
        {"tool_calls": [{"function": {"name": "write_file",
                                      "arguments": {"path": "after_nudge.txt", "content": "ok"}}}]},
        {"content": "Готово", "tool_calls": []},
    ])
    final3, log3 = run_step("створи after_nudge.txt", ctx, client=empty_then_call, max_iters=5)
    assert (root / "after_nudge.txt").read_text(encoding="utf-8") == "ok", "nudge не витягнув крок"
    assert "Готово" in final3

    # Просочений виклик тула як текст (<tool_call>/<tools> у content) -> nudge -> дія.
    leaked_then_call = LoopStub([
        {"content": '<tools>\n{"name": "write_file"}\n</tools>', "tool_calls": []},
        {"tool_calls": [{"function": {"name": "write_file",
                                      "arguments": {"path": "after_leak.txt", "content": "ok"}}}]},
        {"content": "Готово", "tool_calls": []},
    ])
    final4, log4 = run_step("створи after_leak.txt", ctx, client=leaked_then_call, max_iters=5)
    assert (root / "after_leak.txt").read_text(encoding="utf-8") == "ok", "nudge не витягнув крок (leak)"
    assert "Готово" in final4

    # Просочений виклик з ПОВНИМИ аргументами -> розпарсити й ВИКОНАТИ напряму,
    # без nudge-раунду взагалі (живий кейс: модель повторює той самий зламаний
    # формат і ПІСЛЯ nudge — retry не зарадить, бо вже маємо все потрібне з тексту).
    leaked_full_args = LoopStub([
        {"content": '{"name": "write_file", "arguments": '
                    '{"path": "after_recover.txt", "content": "recovered"}}', "tool_calls": []},
        {"content": "Готово", "tool_calls": []},
    ])
    final5, log5 = run_step("створи after_recover.txt", ctx, client=leaked_full_args, max_iters=5)
    assert (root / "after_recover.txt").read_text(encoding="utf-8") == "recovered", "не розпарсило й не виконало"
    assert any("write_file" in entry for entry in log5)
    assert "Готово" in final5

    # Той самий розпарсюваний випадок, але з малформованим зайвим '}' наприкінці
    # (живий кейс на qwen3-coder) — брейс-каунтинг має зупинитись на балансі й
    # ігнорувати хвіст.
    leaked_malformed = LoopStub([
        {"content": '{"name": "write_file", "arguments": '
                    '{"path": "after_malformed.txt", "content": "ok2"}}}', "tool_calls": []},
        {"content": "Готово", "tool_calls": []},
    ])
    final6, log6 = run_step("створи after_malformed.txt", ctx, client=leaked_malformed, max_iters=5)
    assert (root / "after_malformed.txt").read_text(encoding="utf-8") == "ok2", "не впоралось з малформованим хвостом"
    assert "Готово" in final6

    print("OK: tool-loop виконує тули через реєстр, завершується, ліміт ітерацій тримає, nudge витягує порожні/просочені відповіді, парсинг просочених викликів виконує дію напряму")
    print(f"  журнал кроку 1: {log}")


if __name__ == "__main__":
    main()
