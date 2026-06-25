"""Офлайн-тест спільних тем chat-режиму (topics) зі стаб-моделлю.
Запуск:  .venv\\Scripts\\python.exe -m tests.test_topics
"""
import shutil

from agent import topics


class Stub:
    def __init__(self, scripted):
        self.scripted = scripted
        self.i = 0

    def chat(self, messages, tools=None, profile=None, fmt=None):
        c = self.scripted[min(self.i, len(self.scripted) - 1)]
        self.i += 1
        return {"content": c}


def main() -> None:
    shutil.rmtree(topics.TOPICS_ROOT, ignore_errors=True)
    try:
        assert topics.list_topics() == []

        # нова тема
        t1 = topics.classify_topic(
            "що з фронтом біля Костянтинівки", "обговорили оточення",
            [], client=Stub(['{"topic": "Костянтинівка", "is_new": true}']),
        )
        assert t1 == "Костянтинівка"
        note1 = topics.update_topic_note("", "що з фронтом", "оточення міста",
                                         client=Stub(["Місто в оточенні."]))
        topics.save_topic(t1, note1)
        assert topics.list_topics() == ["Костянтинівка"]
        assert topics.load_topic(t1) == "Місто в оточенні."

        # модель ЛУНАЄ заголовок промпту замість чистого тексту (живий кейс) -> обрізаємо
        note_echo = topics.update_topic_note("", "хід", "відповідь",
                                             client=Stub(["Поточна нотатка:\nЧистий текст."]))
        assert note_echo == "Чистий текст.", repr(note_echo)

        # той самий хід належить до ІСНУЮЧОЇ теми
        t2 = topics.classify_topic(
            "а що по 156 бригаді", "слабка ланка", ["Костянтинівка"],
            client=Stub(['{"topic": "Костянтинівка", "is_new": false}']),
        )
        assert t2 == "Костянтинівка"
        note2 = topics.update_topic_note(note1, "156 бригада", "слабка ланка",
                                         client=Stub(["Місто в оточенні; 156 бригада — слабка ланка."]))
        topics.save_topic(t2, note2)
        assert "156 бригада" in topics.load_topic(t2)

        # нова, ІНША тема
        t3 = topics.classify_topic(
            "що по нафті РФ", "ціна може впасти", ["Костянтинівка"],
            client=Stub(['{"topic": "Економіка РФ", "is_new": true}']),
        )
        assert t3 == "Економіка РФ"
        topics.save_topic(t3, topics.update_topic_note("", "нафта", "ціна впаде",
                                                        client=Stub(["Ціна нафти може впасти."])))
        assert set(topics.list_topics()) == {"Костянтинівка", "Економіка РФ"}

        # переповнення бюджету -> другий (стискаючий) прохід
        compressed = topics.update_topic_note("x" * 200, "ще щось", "ще щось",
                                               client=Stub(["y" * 200_000, "СТИСНУТО"]), budget=1000)
        assert compressed == "СТИСНУТО"

        # невалідний JSON від моделі -> fallback "загальне"
        t4 = topics.classify_topic("щось", "щось", [], client=Stub(["не json взагалі"]))
        assert t4 == "загальне"

        # available_topics_note
        assert topics.available_topics_note([]) == ""
        assert "Костянтинівка" in topics.available_topics_note(["Костянтинівка", "Економіка РФ"])

        # traversal-захист у назві теми (слаг)
        topics.save_topic("../../escape", "x")
        assert not (topics.TOPICS_ROOT.parent.parent / "escape.txt").exists()

        print("OK: topics — класифікація (нова/існуюча), злиття нотатки, бюджет, "
              "fallback на невалідний JSON, topics_note, захист імені файлу")
    finally:
        shutil.rmtree(topics.TOPICS_ROOT, ignore_errors=True)


if __name__ == "__main__":
    main()
