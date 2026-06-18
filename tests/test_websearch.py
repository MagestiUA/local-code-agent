"""Офлайн-тест веб-пошуку (websearch) — підмінюємо провайдера, без мережі.
Запуск:  .venv\\Scripts\\python.exe -m tests.test_websearch
"""
from agent import websearch
from agent.toolkit import ToolContext, default_registry
from pathlib import Path
import tempfile


def main() -> None:
    orig = websearch.PROVIDER
    try:
        # стаб-провайдер: повертає два результати
        websearch.PROVIDER = lambda q, n: [
            {"title": "Result A", "url": "http://a", "snippet": "про " + q},
            {"title": "Result B", "url": "http://b", "snippet": "ще"},
        ][:n]

        res = websearch.search("python asyncio", max_results=5)
        assert len(res) == 2 and res[0]["url"] == "http://a"
        txt = websearch.format_results(res)
        assert "1. Result A" in txt and "http://a" in txt and "про python asyncio" in txt

        # порожній запит -> []
        assert websearch.search("  ") == []

        # max_results обрізає
        assert len(websearch.search("q", max_results=1)) == 1

        # провайдер недоступний (None) -> зрозуміле повідомлення
        websearch.PROVIDER = lambda q, n: None
        assert websearch.search("q") is None
        assert "недоступний" in websearch.format_results(None)

        # помилка провайдера -> None (не падаємо)
        def boom(q, n):
            raise RuntimeError("network down")
        websearch.PROVIDER = boom
        assert websearch.search("q") is None

        # порожній результат
        assert websearch.format_results([]) == "нічого не знайдено"

        # тул у реєстрі + диспетч
        websearch.PROVIDER = lambda q, n: [{"title": "T", "url": "u", "snippet": "s"}]
        reg = default_registry()
        assert "web_search" in reg.names()
        ctx = ToolContext(root=Path(tempfile.mkdtemp()), permissions={"edits": "auto", "shell": "off"})
        out = reg.dispatch("web_search", {"query": "test"}, ctx)
        assert "1. T" in out and "u" in out
    finally:
        websearch.PROVIDER = orig

    print("OK: websearch — пошук, формат, порожній/недоступний/помилка, тул у реєстрі")


if __name__ == "__main__":
    main()
