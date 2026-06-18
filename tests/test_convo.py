"""Офлайн-тест контекст-памʼяті розмови (convo) зі стаб-моделлю.
Запуск:  .venv\\Scripts\\python.exe -m tests.test_convo
"""
from agent import convo


class Stub:
    """Повертає заскриптовані відповіді по черзі (як OllamaClient.chat)."""
    def __init__(self, scripted):
        self.scripted = scripted
        self.i = 0

    def chat(self, messages, tools=None, profile=None, fmt=None):
        c = self.scripted[min(self.i, len(self.scripted) - 1)]
        self.i += 1
        return {"content": c}


def main() -> None:
    # перший епізод -> підсумок з пункту
    s = convo.update_summary("", "додай функцію", "створено f() у a.py",
                             client=Stub(["створено f() у a.py"]))
    assert s == "- створено f() у a.py", repr(s)

    # другий епізод дописується
    s = convo.update_summary(s, "виклич f", "додано виклик f() у a.py",
                             client=Stub(["додано виклик f() у a.py"]))
    assert "створено f()" in s and "додано виклик" in s
    assert s.count("- ") == 2

    # перевищення бюджету -> стиснення (стаб вертає короткий стиск)
    big = "x" * 50  # епізод
    s2 = convo.update_summary("y" * 40000, "щось", "результат",
                              client=Stub([big, "СТИСНУТО"]), budget=1000)
    assert s2 == "СТИСНУТО", repr(s2)

    # порожній епізод -> підсумок без змін
    s3 = convo.update_summary("- наявне", "пусто", "", client=Stub([""]))
    assert s3 == "- наявне"

    # as_context
    assert convo.as_context("") == ""
    assert "підсумок" in convo.as_context("- щось")

    print("OK: convo — епізоди, дописування, стиснення за бюджетом, порожній епізод, as_context")


if __name__ == "__main__":
    main()
