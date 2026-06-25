"""Точка входу local-code-agent: піднімає сервер і відкриває застосунок у
нативному вікні (pywebview, WebView2 на Windows) — без вкладки браузера.

Зручно з PyCharm: відкрий цей файл і натисни ▶ Run (інтерпретатор — .venv проєкту).

Сервер (Reflex) піднімається як ОКРЕМИЙ ПРОЦЕС, не потік: Granian/Reflex
реєструє signal-handler, що працює лише в головному потоці головного
інтерпретатора — у фоновому потоці це падає з "signal only works in main
thread of the main interpreter" (перевірено живцем). Реальний URL читаємо З
ВИВОДУ дочірнього процесу, а не хардкодимо :3000 — Reflex сам падає на інший
порт (:3002 і т.д.), якщо 3000/3001/8000 зайняті (типово — залишки попередніх
запусків чи WSL2/Hyper-V NAT-артефакти на Windows), і хардкод порту тоді
змусить нас чекати URL, який ніколи не зʼявиться.

--dev: класичний браузерний dev-режим без вікна, з devtools (те, що main.py
робив раніше). Альтернатива — run_web.ps1, який кличе reflex.exe напряму,
взагалі минаючи цей файл.
Решта аргументів передається далі в `reflex run` (напр. `--env prod`).
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_URL_RE = re.compile(r"App running at: (http://\S+)")
STARTUP_TIMEOUT = 60.0
# :8000 на цій машині тримає "привид" — порт LISTENING, але PID-власник вже не
# існує (WSL2/Hyper-V NAT-артефакт). Не ліземо в WSL/Docker — просто стартуємо
# на іншому, вільному порту за дефолтом. Перекривається власним --backend-port.
DEFAULT_BACKEND_PORT = 8006


def _run_reflex_inplace(extra_args: list[str]) -> None:
    """Класичний dev-режим (--dev): reflex run у ЦЬОМУ процесі (як раніше)."""
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ.setdefault("REFLEX_TELEMETRY_ENABLED", "false")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    from reflex.reflex import cli
    sys.argv = ["reflex", "run", *extra_args]
    cli()


def _start_server(extra_args: list[str]) -> tuple[subprocess.Popen, "queue.Queue[str]"]:
    """Підняти сервер як дочірній процес (python main.py --dev ...). Фоновий
    потік читає його stdout: друкує кожен рядок (логи лишаються видимі в
    консолі) і виловлює рядок "App running at: <url>" із РЕАЛЬНИМ портом."""
    env = {**os.environ, "REFLEX_TELEMETRY_ENABLED": "false", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--dev", *extra_args],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    url_q: "queue.Queue[str]" = queue.Queue(maxsize=1)
    found = False

    def _pump() -> None:
        nonlocal found
        for line in proc.stdout:
            print(line, end="")
            if not found:
                m = APP_URL_RE.search(line)
                if m:
                    found = True
                    url_q.put(m.group(1))
        if not found:
            url_q.put("")   # процес завершився, не дочекавшись рядка з URL

    threading.Thread(target=_pump, daemon=True).start()
    return proc, url_q


def _stop_server(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> None:
    extra = sys.argv[1:]
    if "--dev" in extra:
        _run_reflex_inplace([a for a in extra if a != "--dev"])
        return

    if "--backend-port" not in extra:
        extra = [*extra, "--backend-port", str(DEFAULT_BACKEND_PORT)]
    proc, url_q = _start_server(extra)
    try:
        try:
            url = url_q.get(timeout=STARTUP_TIMEOUT)
        except queue.Empty:
            url = ""
        if not url:
            print(f"Сервер не повідомив URL за {STARTUP_TIMEOUT:.0f}с — дивіться лог вище.")
            return
        import webview
        webview.create_window("local-code-agent", url, width=1280, height=860, min_size=(800, 600))
        webview.start()
    finally:
        _stop_server(proc)


if __name__ == "__main__":
    main()
