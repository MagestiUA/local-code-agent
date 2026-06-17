"""Офлайн-тест відповідей + збору контексту (стаб замість Ollama).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_answer
"""
import tempfile
from pathlib import Path

from agent.answerer import answer, build_context


class CapStub:
    def __init__(self, reply: str):
        self.reply = reply
        self.captured = None

    def chat(self, messages, tools=None, profile=None, fmt=None):
        self.captured = messages
        return {"content": self.reply}


def main() -> None:
    root = Path(tempfile.mkdtemp())
    (root / "parser.py").write_text("def parse():\n    return 42\n", encoding="utf-8")
    (root / "util.py").write_text("X = 1\n", encoding="utf-8")

    ctx = build_context(root, question="що робить parser.py?")
    assert "Структура проекту" in ctx
    assert "parser.py" in ctx and "return 42" in ctx
    # util.py не згадувався у питанні -> його вмісту в контексті нема (лише у структурі)
    assert "X = 1" not in ctx

    cap = CapStub("Парсер повертає 42.")
    out, _thinking = answer("що робить parser.py?", ctx, cap)
    assert out == "Парсер повертає 42."
    joined = " ".join(m["content"] for m in cap.captured)
    assert "return 42" in joined, "контекст не дійшов до моделі"

    print("OK: build_context тягне згаданий файл + структуру; answer повертає текст")


if __name__ == "__main__":
    main()
