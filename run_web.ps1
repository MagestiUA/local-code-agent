# Запуск веб-інтерфейсу local-code-agent у браузері (Reflex, з devtools).
# Відкриє http://localhost:3000. Зупинка — Ctrl+C.
# Альтернатива: python main.py --dev (той самий режим, зручно з PyCharm).
# Без --dev main.py відкриває нативне вікно (pywebview) замість браузера.
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:REFLEX_TELEMETRY_ENABLED = "false"
& "$PSScriptRoot\.venv\Scripts\reflex.exe" run
