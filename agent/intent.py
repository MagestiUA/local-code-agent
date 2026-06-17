"""Класифікація наміру задачі: edit | answer | shell.

Дозволяє агенту не лише правити файли, а й відповідати на питання та запускати
команди. Швидкий виклик (EXECUTOR, think off) + format=json для жорсткої структури.
При нерозбірливій відповіді -> 'answer' (безпечний, read-only дефолт).
"""
from __future__ import annotations

import json

from . import config
from .llm import OllamaClient

MODES = {"edit", "answer", "shell", "plan"}

SYSTEM = (
    "Classify the user's request into exactly one mode:\n"
    "- 'edit': modify, refactor, create, or delete code or files right away.\n"
    "- 'plan': the user wants to plan or discuss the approach FIRST, without making "
    "changes yet (e.g. 'let's plan', 'how would you approach', 'don't code yet').\n"
    "- 'answer': explain, analyze, or ask about the code/project; NO changes made.\n"
    "- 'shell': run a command (tests, list files, build, lint).\n"
    'Reply ONLY as JSON: {"mode": "edit|plan|answer|shell"}.'
)

SCHEMA = {
    "type": "object",
    "properties": {"mode": {"type": "string", "enum": ["edit", "plan", "answer", "shell"]}},
    "required": ["mode"],
}


def classify_intent(task: str, client: OllamaClient | None = None) -> str:
    client = client or OllamaClient()
    msg = client.chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task}],
        profile=config.EXECUTOR, fmt=SCHEMA,
    )
    try:
        mode = json.loads(msg.get("content") or "{}").get("mode", "")
    except Exception:
        mode = ""
    return mode if mode in MODES else "answer"


CMD_SYSTEM = (
    "The user wants to run a command. Output ONLY the exact single command line to run, "
    "no markdown, no explanation. Examples: pytest -q | python main.py | git status"
)


def extract_command(task: str, client: OllamaClient | None = None) -> str:
    """Витягти конкретну команду з NL-запиту (для shell-режиму). run_shell усе одно
    перевірить її по allow-list."""
    client = client or OllamaClient()
    msg = client.chat(
        [{"role": "system", "content": CMD_SYSTEM}, {"role": "user", "content": task}],
        profile=config.EXECUTOR,
    )
    lines = (msg.get("content") or "").strip().splitlines()
    return lines[0].strip().strip("`").strip() if lines else ""
