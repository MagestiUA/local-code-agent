# Запуск веб-інтерфейсу local-code-agent.
# Відкриє http://localhost:8080. Зупинка — Ctrl+C.
chcp 65001 > $null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
& "$PSScriptRoot\.venv\Scripts\python.exe" -m agent.gui
