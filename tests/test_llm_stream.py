"""Офлайн-тест парсингу стріму Ollama (_stream_event) + математики tok/s.
Запуск:  .venv\\Scripts\\python.exe -m tests.test_llm_stream
Без HTTP/GPU — годуємо парсер зразковими чанками.
"""
from agent.llm import _stream_event


def main() -> None:
    # Проміжний чанк контенту
    e = _stream_event({"message": {"role": "assistant", "content": "Hel"}, "done": False})
    assert e["content"] == "Hel" and e["thinking"] == "" and not e["done"]

    # Проміжний чанк роздумів (think on)
    e = _stream_event({"message": {"role": "assistant", "thinking": "мір"}, "done": False})
    assert e["thinking"] == "мір" and e["content"] == "" and not e["done"]

    # Порожні поля не падають
    e = _stream_event({"message": {}, "done": False})
    assert e["content"] == "" and e["thinking"] == ""

    # Фінальний чанк зі stats
    fin = _stream_event({
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "prompt_eval_count": 1200,
        "eval_count": 600,
        "eval_duration": 30_000_000_000,   # 30 секунд у наносекундах
    })
    assert fin["done"] is True
    assert fin["stats"] == {"prompt": 1200, "out": 600, "eval_ns": 30_000_000_000}

    # tok/s = out / (eval_ns / 1e9) = 600 / 30 = 20.0
    tps = fin["stats"]["out"] / (fin["stats"]["eval_ns"] / 1e9)
    assert abs(tps - 20.0) < 1e-6, tps

    # Фінальний чанк з tool_calls (стрім із тулами)
    tc = _stream_event({
        "message": {"tool_calls": [{"function": {"name": "web_search"}}]},
        "done": True,
    })
    assert tc["tool_calls"] and tc["stats"]["out"] == 0

    # Симуляція повного потоку: накопичення тексту
    chunks = [
        {"message": {"content": "при"}, "done": False},
        {"message": {"content": "віт"}, "done": False},
        {"message": {"content": "!"}, "done": False},
        {"message": {}, "done": True, "prompt_eval_count": 5,
         "eval_count": 3, "eval_duration": 1_000_000_000},
    ]
    text, stats = "", None
    for c in chunks:
        ev = _stream_event(c)
        if ev["done"]:
            stats = ev["stats"]
        else:
            text += ev["content"]
    assert text == "привіт!" and stats["out"] == 3

    print("OK: стрім — дельти content/thinking, фінал зі stats, tok/s, tool_calls, накопичення")


if __name__ == "__main__":
    main()
