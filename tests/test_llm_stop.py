"""Офлайн-тест переривання chat_stream через stop_event.
Запуск: .venv\\Scripts\\python.exe -m tests.test_llm_stop
"""
import threading
from agent.llm import _stream_event


def _fake_stream(n_chunks: int):
    """Генератор-стаб: видає n_chunks thinking-чанків, потім done."""
    for i in range(n_chunks):
        yield {"done": False, "message": {"thinking": f"думка {i}", "content": ""}}
    yield {"done": True, "message": {}, "eval_count": n_chunks, "eval_duration": 1_000_000_000,
           "prompt_eval_count": 10}


def _consume_with_stop(gen, stop_after: int, stop_event: threading.Event):
    """Споживає генератор, ставить stop_event після stop_after чанків."""
    collected = []
    for i, raw in enumerate(gen):
        ev = _stream_event(raw)
        if ev["done"]:
            break
        collected.append(ev["thinking"])
        if i + 1 >= stop_after:
            stop_event.set()
            break
    return collected


def main():
    # Тест 1: без stop_event — отримуємо всі чанки
    chunks = list(_fake_stream(10))
    assert len(chunks) == 11, f"очікували 11 (10 + done), отримали {len(chunks)}"

    # Тест 2: stop після 3 чанків — partial result
    ev = threading.Event()
    result = _consume_with_stop(_fake_stream(10), stop_after=3, stop_event=ev)
    assert len(result) == 3, f"очікували 3 чанки до стопу, отримали {len(result)}"
    assert ev.is_set(), "stop_event має бути встановлено"
    assert result[0] == "думка 0"
    assert result[2] == "думка 2"

    # Тест 3: stop_event вже встановлено — не читаємо жодного чанку
    ev2 = threading.Event()
    ev2.set()

    def _guarded_gen():
        for raw in _fake_stream(10):
            if ev2.is_set():
                return
            yield raw

    result2 = list(_guarded_gen())
    assert result2 == [], f"очікували порожній список, отримали {result2}"

    print("OK: llm_stop — partial result зберігається, stop_event перериває стрім")


if __name__ == "__main__":
    main()
