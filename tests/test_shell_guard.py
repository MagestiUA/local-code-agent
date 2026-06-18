"""Офлайн-тест класифікатора небезпечних shell-команд.
Запуск: .venv\\Scripts\\python.exe -m tests.test_shell_guard
"""
from agent.shell_guard import classify

SAFE = [
    "python main.py",
    "python -m venv .venv",
    "pip install -r requirements.txt",
    ".venv\\Scripts\\pip install aiogram",
    "pytest tests/",
    "git status",
    "git add .",
    "git commit -m 'fix'",
    "mkdir mydir",
    "ls -la",
    "ruff check .",
    "npm install",
    "go build ./...",
]

DANGER = [
    "rm -rf /",
    "rm -rf node_modules",
    "rm -fr .venv",
    "del /s /q C:\\Users",
    "rmdir /s C:\\Windows",
    "rd /s /q C:\\System32",
    "format C:",
    "mkfs.ext4 /dev/sda",
    "diskpart",
    "sc delete MyService",
    "sc stop nginx",
    "net stop wuauserv",
    "taskkill /f /im python.exe",
    "netsh winsock reset",
    "iptables -F",
    "systemctl disable ssh",
    "icacls C:\\Windows\\System32 /grant Everyone:F",
    "dd if=/dev/zero of=/dev/sda",
    "Remove-Item -Recurse -Force C:\\Windows",
]


def main():
    errors = []
    for cmd in SAFE:
        r = classify(cmd)
        if r != "safe":
            errors.append(f"  ПОМИЛКА safe→{r}: {cmd!r}")
    for cmd in DANGER:
        r = classify(cmd)
        if r != "danger":
            errors.append(f"  ПОМИЛКА danger→{r}: {cmd!r}")

    if errors:
        print("FAILED:")
        for e in errors:
            print(e)
        raise SystemExit(1)

    print(f"OK: shell_guard — {len(SAFE)} safe + {len(DANGER)} danger команд класифіковано правильно")


if __name__ == "__main__":
    main()
