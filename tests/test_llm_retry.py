"""Офлайн-тест авторетраю OllamaClient на ConnectionError/Timeout (без мережі/GPU).
Запуск: .venv\\Scripts\\python.exe -m tests.test_llm_retry
"""
from unittest import mock

import requests

from agent.llm import OllamaClient


def _make_client() -> OllamaClient:
    """Клієнт без реального мережевого виклику в __init__ (_ensure_server)."""
    c = OllamaClient.__new__(OllamaClient)
    c.model = "test-model"
    c.host = "http://127.0.0.1:11434"
    c.last_stats = {}
    c.last_error = ""
    return c


def main() -> None:
    # Успіх з першої спроби — без ретраю, без підняття сервера.
    c = _make_client()
    calls = {"post": 0, "ensure": 0}
    c._ensure_server = lambda *a, **kw: calls.__setitem__("ensure", calls["ensure"] + 1)
    with mock.patch("agent.llm.requests.post", side_effect=lambda *a, **kw:
                     calls.__setitem__("post", calls["post"] + 1) or "ok-response"):
        r = c._post_with_retry({"x": 1})
    assert r == "ok-response"
    assert calls["post"] == 1 and calls["ensure"] == 0
    assert c.last_error == ""

    # Сервер впав на 2 спроби (живий кейс: сторонній застосунок вимкнув Ollama),
    # відновився на 3-й -> _ensure_server викликано рівно 2 рази (між невдалими спробами).
    c2 = _make_client()
    calls2 = {"post": 0, "ensure": 0}
    c2._ensure_server = lambda *a, **kw: calls2.__setitem__("ensure", calls2["ensure"] + 1)

    def flaky_post(*a, **kw):
        calls2["post"] += 1
        if calls2["post"] < 3:
            raise requests.exceptions.ConnectionError("refused")
        return "recovered"

    with mock.patch("agent.llm.requests.post", side_effect=flaky_post), \
         mock.patch("agent.llm.time.sleep"):
        r2 = c2._post_with_retry({"x": 1}, max_attempts=3, backoff=0)
    assert r2 == "recovered"
    assert calls2["post"] == 3
    assert calls2["ensure"] == 2
    assert c2.last_error == ""   # очищено після успіху

    # Усі спроби невдалі -> піднімається ОСТАННЯ помилка, last_error збережено
    # (діагностика — щоб GUI/лог міг показати, чому впало).
    c3 = _make_client()
    c3._ensure_server = lambda *a, **kw: None
    raised = False
    with mock.patch("agent.llm.requests.post",
                    side_effect=requests.exceptions.ConnectionError("still down")), \
         mock.patch("agent.llm.time.sleep"):
        try:
            c3._post_with_retry({"x": 1}, max_attempts=3, backoff=0)
        except requests.exceptions.ConnectionError:
            raised = True
    assert raised
    assert "still down" in c3.last_error

    # Timeout теж триґерить ретрай (не лише ConnectionError).
    c4 = _make_client()
    c4._ensure_server = lambda *a, **kw: None
    calls4 = {"post": 0}

    def timeout_then_ok(*a, **kw):
        calls4["post"] += 1
        if calls4["post"] == 1:
            raise requests.exceptions.Timeout("read timed out")
        return "ok-after-timeout"

    with mock.patch("agent.llm.requests.post", side_effect=timeout_then_ok), \
         mock.patch("agent.llm.time.sleep"):
        r4 = c4._post_with_retry({"x": 1}, max_attempts=3, backoff=0)
    assert r4 == "ok-after-timeout"

    print("OK: llm retry — успіх без ретраю, відновлення після ConnectionError/Timeout, "
          "виснаження спроб зберігає last_error")


if __name__ == "__main__":
    main()
