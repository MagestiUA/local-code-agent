# Запуск local-code-agent як легкого десктоп-застосунку (pywebview), без браузера.
# Закриття вікна зупиняє все (сервер теж). Для розробки в браузері з devtools —
# run_web.ps1 (кличе reflex.exe напряму) або main.py --dev.
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\main.py"
