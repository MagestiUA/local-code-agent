"""Офлайн-тест класифікації наміру (стаб замість Ollama).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_intent
"""
from agent.intent import classify_intent


class Stub:
    def __init__(self, reply: str):
        self.reply = reply

    def chat(self, messages, tools=None, profile=None, fmt=None):
        return {"content": self.reply}


def main() -> None:
    assert classify_intent("відрефактори parser.py", Stub('{"mode": "edit"}')) == "edit"
    assert classify_intent("що робить ця функція?", Stub('{"mode": "answer"}')) == "answer"
    assert classify_intent("запусти тести", Stub('{"mode": "shell"}')) == "shell"
    # нерозбірливе / зламане -> безпечний дефолт answer
    assert classify_intent("???", Stub("not json")) == "answer"
    assert classify_intent("???", Stub('{"mode": "wat"}')) == "answer"
    print("OK: класифікація наміру edit/answer/shell + дефолт answer")


if __name__ == "__main__":
    main()
