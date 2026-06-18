# Запуск веб-інтерфейсу local-code-agent (Reflex).
# Відкриє http://localhost:3000. Зупинка — Ctrl+C.
# Альтернатива: python main.py (зручно з PyCharm).
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:REFLEX_TELEMETRY_ENABLED = "false"
& "$PSScriptRoot\.venv\Scripts\reflex.exe" run
