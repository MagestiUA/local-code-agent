"""Клієнт Ollama: керування сервером, chat із tool-calling і перемикачем think.

Патерни старту/зупинки сервера — з NewsAnalizer (перевірені).
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import requests

from . import config


def _stream_event(obj: dict) -> dict:
    """Нормалізувати один JSON-чанк потоку /api/chat у подію.
    Проміжний чанк -> дельта content/thinking; фінальний (done) -> stats+tool_calls.
    stats: prompt (токени контексту), out (токени виводу), eval_ns (час генерації, нс)."""
    msg = obj.get("message", {}) or {}
    if obj.get("done"):
        return {"content": "", "thinking": "", "done": True,
                "tool_calls": msg.get("tool_calls"),
                "stats": {"prompt": obj.get("prompt_eval_count", 0) or 0,
                          "out": obj.get("eval_count", 0) or 0,
                          "eval_ns": obj.get("eval_duration", 0) or 0}}
    return {"content": msg.get("content", "") or "",
            "thinking": msg.get("thinking", "") or "", "done": False}


class OllamaClient:
    def __init__(self, model: str = config.MODEL, host: str = config.HOST):
        self.model = model
        self.host = host.rstrip("/")
        self.last_stats: dict = {}      # stats останнього chat() — для лічильника токенів
        self.last_error: str = ""       # остання помилка з'єднання (діагностика)
        self._ensure_server()

    # ── Сервер ──────────────────────────────────────────────────────────────
    def _ensure_server(self, retries: int = 20, delay: float = 1.5) -> None:
        try:
            requests.get(f"{self.host}/api/tags", timeout=3)
            return
        except Exception:
            pass
        # q4_0 KV-cache + flash attention — за бенчмарком дають практично безкоштовне
        # 2x контексту (gemma4 113.5→108.5 t/s на 65536→131072); дефолт для УСІХ моделей.
        # Діє лише коли МИ піднімаємо сервер — якщо ollama serve вже запущено ззовні
        # (напр. іншим клієнтом), його env не змінюємо.
        env = {**os.environ, "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q4_0"}
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=env,
        )
        for _ in range(retries):
            time.sleep(delay)
            try:
                requests.get(f"{self.host}/api/tags", timeout=3)
                return
            except Exception:
                pass
        raise RuntimeError("Ollama не відповіла після запуску")

    def _post_with_retry(self, payload: dict, stream: bool = False,
                         max_attempts: int = 3, backoff: float = 3.0):
        """POST /api/chat з автовідновленням. Живий кейс: сторонній застосунок чи
        користувач вимкнув Ollama-сервер посеред сесії — наступний запит раніше
        падав з ConnectionError назавжди, поки хтось не піднімав сервер вручну.
        Тепер: на ConnectionError/Timeout — спроба підняти сервер знову
        (_ensure_server) і повтор із паузою, до max_attempts разів."""
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                r = requests.post(f"{self.host}/api/chat", json=payload, stream=stream,
                                  timeout=config.REQUEST_TIMEOUT)
                self.last_error = ""
                return r
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_exc = e
                self.last_error = str(e)
                if attempt < max_attempts:
                    try:
                        self._ensure_server()
                    except Exception:
                        pass
                    time.sleep(backoff)
        raise last_exc

    def unload(self) -> None:
        """Вивантажити модель з VRAM (keep_alive=0)."""
        try:
            requests.post(f"{self.host}/api/chat",
                          json={"model": self.model, "messages": [], "keep_alive": 0},
                          timeout=10)
        except Exception:
            pass

    # ── Запит ───────────────────────────────────────────────────────────────
    def chat(self, messages: list[dict], tools: list | None = None,
             profile: dict = config.EXECUTOR, fmt: str | dict | None = None) -> dict:
        """Один виклик /api/chat. Повертає message (dict) з полями
        content / thinking / tool_calls (за наявності).
        fmt="json" або JSON-schema -> примусово валідний JSON у content.
        Модель беремо з profile["model"], якщо є (EXECUTOR/PLANNER можуть мати різні
        моделі) — інакше дефолт клієнта (self.model)."""
        model = profile.get("model") or self.model
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": profile["think"],
            "options": {
                "temperature": profile["temperature"],
                "num_ctx": profile["num_ctx"],
                **profile.get("options", {}),     # доп. семплінг профілю (напр. DRY)
            },
        }
        if tools:
            payload["tools"] = tools
        if fmt:
            payload["format"] = fmt
        r = self._post_with_retry(payload)
        r.raise_for_status()
        data = r.json()
        self.last_stats = {"prompt": data.get("prompt_eval_count", 0) or 0,
                           "out": data.get("eval_count", 0) or 0,
                           "eval_ns": data.get("eval_duration", 0) or 0,
                           "model": model}
        return data["message"]

    def chat_stream(self, messages: list[dict], tools: list | None = None,
                    profile: dict = config.EXECUTOR, fmt: str | dict | None = None,
                    stop_event=None):
        """Стрімовий виклик /api/chat. Генератор подій (див. _stream_event):
        проміжні чанки -> дельти content/thinking; фінальний -> done зі stats.
        stop_event: threading.Event — якщо встановлено, стрім переривається після
        поточного чанку (частковий результат зберігається)."""
        model = profile.get("model") or self.model
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": profile["think"],
            "options": {
                "temperature": profile["temperature"],
                "num_ctx": profile["num_ctx"],
                **profile.get("options", {}),     # доп. семплінг профілю (напр. DRY)
            },
        }
        if tools:
            payload["tools"] = tools
        if fmt:
            payload["format"] = fmt
        with self._post_with_retry(payload, stream=True) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if stop_event and stop_event.is_set():
                    break
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ev = _stream_event(obj)
                if ev["done"]:
                    self.last_stats = {**ev["stats"], "model": model}
                yield ev
                if stop_event and stop_event.is_set():
                    break
