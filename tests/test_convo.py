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

    # kind="chat" використовує ОКРЕМИЙ (не кодинг-орієнтований) промпт — перевіряємо,
    # що системний промпт дійсно різний для code/chat (а не просто ігнорується kind).
    captured = {}
    class CapturingStub:
        def chat(self, messages, tools=None, profile=None, fmt=None):
            captured["system"] = messages[0]["content"]
            return {"content": "епізод"}
    convo.summarize_turn("питання", "відповідь", client=CapturingStub(), kind="chat")
    chat_sys = captured["system"]
    convo.summarize_turn("питання", "відповідь", client=CapturingStub(), kind="code")
    code_sys = captured["system"]
    assert chat_sys != code_sys
    assert "files" not in chat_sys.lower() and "function" not in chat_sys.lower()
    assert "files" in code_sys.lower() or "functions" in code_sys.lower()

    # update_digest: перший шматок -> дайджест; другий шматок + дайджест -> новий дайджест
    d1 = convo.update_digest("", "шматок 1: подія А сталась через Б",
                             client=Stub(["Подія А сталась через Б."]))
    assert "Подія А" in d1

    # модель ЛУНАЄ заголовок промпту замість чистого тексту (живий кейс) -> обрізаємо
    d_echo = convo.update_digest("", "шматок", client=Stub(["Поточний дайджест:\nЧистий текст."]))
    assert d_echo == "Чистий текст.", repr(d_echo)

    # той самий echo, але обгорнутий у markdown (живий кейс: "**Поточна нотатка:**")
    d_echo_md = convo.update_digest("", "шматок",
                                    client=Stub(["**Поточний дайджест:**\n\nЧистий текст."]))
    assert d_echo_md == "Чистий текст.", repr(d_echo_md)

    # модель повертає лише наш ВЛАСНИЙ плейсхолдер "(порожньо...)" -> трактуємо як
    # "нічого корисного", лишаємо попередній дайджест (живий кейс: сміттєвий файл
    # теми з вмістом буквально "(порожньо)")
    d_junk = convo.update_digest("старий дайджест", "куций хід",
                                 client=Stub(["(порожньо — це перший шматок)"]))
    assert d_junk == "старий дайджест", repr(d_junk)
    d2 = convo.update_digest(d1, "шматок 2: подія В сталась через А",
                             client=Stub(["Подія А -> Б -> В."]))
    assert "В" in d2
    # переповнення бюджету дайджесту -> другий (стискаючий) прохід
    d3 = convo.update_digest("x" * 200, "ще шматок",
                             client=Stub(["y" * 200_000, "СТИСНУТО"]), budget=1000)
    assert d3 == "СТИСНУТО", repr(d3)

    print("OK: convo — епізоди, дописування, стиснення за бюджетом, порожній епізод, as_context, "
          "chat/code kind різні промпти, update_digest шматків")


if __name__ == "__main__":
    main()
