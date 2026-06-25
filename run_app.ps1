# Запуск local-code-agent як легкого десктоп-застосунку (pywebview), без браузера.
# Закриття вікна зупиняє все (сервер теж). Для розробки в браузері з devtools —
# run_web.ps1 / main.py.
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:REFLEX_TELEMETRY_ENABLED = "false"
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\app.py"
