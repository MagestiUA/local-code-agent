"""Запуск local-code-agent як легкого десктоп-застосунку (pywebview) — нативне
вікно замість вкладки браузера. Піднімає звичайний Reflex-сервер (main.py) як
ОКРЕМИЙ ПРОЦЕС (не потік — Reflex/Granian реєструє signal-handler, що працює
лише в головному потоці головного процесу; у фоновому потоці це падає з
"signal only works in main thread of the main interpreter"), чекає поки
фронтенд відповість, відкриває вікно з ним.

На Windows pywebview використовує WebView2 (рушій Edge, без бандлу Chromium —
на відміну від Electron) через pythonnet/clr_loader, які встановлюються разом
із pywebview.

Закриття вікна зупиняє дочірній сервер-процес — це самостійний застосунок, а
не ще одна вкладка до вже запущеного сервера. Для звичної розробки в браузері
з devtools — далі лишається main.py/run_web.ps1.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = "http://localhost:3000"
STARTUP_TIMEOUT = 60.0


def _start_server() -> subprocess.Popen:
    env = {**os.environ, "REFLEX_TELEMETRY_ENABLED": "false", "PYTHONIOENCODING": "utf-8"}
    return subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=str(ROOT), env=env)


def _wait_for_server(url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.5)
    return False


def _stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> None:
    print("Піднімаю Reflex-сервер...")
    proc = _start_server()
    try:
        if not _wait_for_server(URL, STARTUP_TIMEOUT):
            print(f"Сервер не відповів за {STARTUP_TIMEOUT:.0f}с — дивіться консоль вище на помилки.")
            return
        import webview
        webview.create_window("local-code-agent", URL, width=1280, height=860, min_size=(800, 600))
        webview.start()
    finally:
        _stop_server(proc)


if __name__ == "__main__":
    main()
