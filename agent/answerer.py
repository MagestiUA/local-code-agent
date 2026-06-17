"""Відповіді на питання й аналіз коду — read-only, без правок.

Збирає контекст (AGENT.md + структура проекту + згадані у питанні файли +
read-only джерела) і відповідає через PLANNER-профіль (think on — аналіз).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import config
from .llm import OllamaClient
from .project import load_project_doc, scan_structure
from .tools import read_file

SYSTEM = (
    "You are a code analysis assistant for a local project. Use the provided context "
    "(project structure, file contents, AGENT.md) to answer accurately and concisely. "
    "Cite the relevant file/function when it helps. Do NOT propose code edits unless "
    "explicitly asked. Answer in the same language as the question."
)

FILE_RE = re.compile(r"[\w./\\-]+\.[A-Za-z]{1,6}")


def build_context(root: str | Path, reference_files=(), question: str = "",
                  per_file: int = 6000) -> str:
    """Контекст для відповіді: довідка + структура + згадані файли + джерела."""
    root = Path(root)
    parts: list[str] = []

    doc = load_project_doc(root)
    if doc:
        parts.append(f"AGENT.md:\n{doc}")

    parts.append(f"Структура проекту:\n{scan_structure(root)}")

    seen: set[str] = set()
    for name in FILE_RE.findall(question):
        p = root / name
        if p.is_file() and str(p) not in seen:
            seen.add(str(p))
            try:
                parts.append(f"{name}:\n{read_file(p)[:per_file]}")
            except Exception:
                pass

    for rf in reference_files:
        try:
            parts.append(f"Reference {Path(rf).name} (read-only):\n{read_file(rf)[:per_file]}")
        except Exception:
            pass

    return "\n\n".join(parts)


def answer(question: str, context: str = "", client: OllamaClient | None = None) -> str:
    client = client or OllamaClient()
    user = (f"Контекст:\n{context}\n\n" if context else "") + f"Питання:\n{question}"
    msg = client.chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        profile=config.PLANNER,
    )
    return (msg.get("content") or "").strip()
