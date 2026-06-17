# Запуск НОВОГО веб-інтерфейсу local-code-agent (Reflex).
# Відкриє http://localhost:3000. Зупинка — Ctrl+C.
# (Старий NiceGUI-інтерфейс — python -m agent.gui на :8080 — більше не використовуємо.)
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:REFLEX_TELEMETRY_ENABLED = "false"
& "$PSScriptRoot\.venv\Scripts\reflex.exe" run
