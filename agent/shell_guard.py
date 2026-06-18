"""Класифікатор shell-команд: "safe" | "danger".

Використовується режимом shell="smart" — виконує безпечні команди автоматично,
зупиняє на підтвердження тільки справді небезпечні (rm -rf, format, системні теки).
"""
from __future__ import annotations

import re

# Шаблони, що вважаються небезпечними (перевіряються у нижньому регістрі).
# Кожен елемент — рядок-підрядок або re.Pattern.
DANGER_PATTERNS: list[str | re.Pattern] = [
    # Рекурсивне/форсоване видалення
    "rm -rf", "rm -fr", "rm --force",
    "del /s", "del /f",
    "rmdir /s", "rd /s",
    # Форматування носіїв
    "format ", "mkfs", "diskpart",
    # Системні директорії Windows
    r"c:\windows", r"c:\system32", r"c:\program files",
    "%systemroot%", "%windir%",
    # Системні директорії Linux/Mac
    "/etc/", "/usr/", "/bin/", "/sbin/", "/boot/", "/lib/",
    # Зміна прав/власника на системне
    re.compile(r"icacls\s+.*\\(system32|windows)\b", re.IGNORECASE),
    re.compile(r"chmod\s+.*\s+/etc/", re.IGNORECASE),
    re.compile(r"chown\s+.*\s+/etc/", re.IGNORECASE),
    # Мережеві деструктивні операції
    "netsh winsock reset", "netsh int ip reset",
    "iptables -f", "iptables --flush",
    # Зупинка/видалення сервісів
    "sc delete", "sc stop",
    "systemctl disable", "systemctl stop",
    "net stop", "taskkill /f",
    # Знищення дисків/розділів
    re.compile(r"dd\s+if=.*of=/dev/(sd|hd|nvme)", re.IGNORECASE),
    # PowerShell небезпечне
    re.compile(r"remove-item\s+.*-recurse\s+-force", re.IGNORECASE),
    re.compile(r"format-volume\b", re.IGNORECASE),
]


def classify(cmd: str) -> str:
    """Повернути "danger" якщо команда потенційно деструктивна, інакше "safe"."""
    low = cmd.lower().strip()
    for pattern in DANGER_PATTERNS:
        if isinstance(pattern, re.Pattern):
            if pattern.search(cmd):
                return "danger"
        else:
            if pattern.lower() in low:
                return "danger"
    return "safe"
