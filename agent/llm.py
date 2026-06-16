"""Клієнт Ollama: керування сервером, chat із tool-calling і перемикачем think.

Патерни старту/зупинки сервера — з NewsAnalizer (перевірені).
"""
from __future__ import annotations

import subprocess
import time

import requests

from . import config


class OllamaClient:
    def __init__(self, model: str = config.MODEL, host: str = config.HOST):
        self.model = model
        self.host = host.rstrip("/")
        self._ensure_server()

    # ── Сервер ──────────────────────────────────────────────────────────────
    def _ensure_server(self, retries: int = 20, delay: float = 1.5) -> None:
        try:
            requests.get(f"{self.host}/api/tags", timeout=3)
            return
        except Exception:
            pass
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for _ in range(retries):
            time.sleep(delay)
            try:
                requests.get(f"{self.host}/api/tags", timeout=3)
                return
            except Exception:
                pass
        raise RuntimeError("Ollama не відповіла після запуску")

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
             profile: dict = config.EXECUTOR) -> dict:
        """Один виклик /api/chat. Повертає message (dict) з полями
        content / thinking / tool_calls (за наявності)."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": profile["think"],
            "options": {
                "temperature": profile["temperature"],
                "num_ctx": profile["num_ctx"],
            },
        }
        if tools:
            payload["tools"] = tools
        r = requests.post(f"{self.host}/api/chat", json=payload,
                          timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()["message"]
