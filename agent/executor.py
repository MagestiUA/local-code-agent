"""Executor — виконує ОДИН крок над ОДНИМ файлом.

Пайплайн (think=off, вузький контекст):
  read -> strip(дані в плейсхолдери) -> LLM(whole) -> ast.parse -> restore -> diff
Опакові дані крізь модель не проходять, тож не псуються й не роздувають контекст.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import config, tools
from .literals import restore_code, strip_code
from .llm import OllamaClient

SYSTEM = (
    "You are a precise code editor. You receive the FULL content of ONE Python file "
    "and an instruction. Return the COMPLETE new content of that file and nothing "
    "else: no explanations, no commentary. You may wrap it in a ```python fence. "
    "Tokens like __LIT_0__ are placeholders for large data that was removed; keep "
    "them EXACTLY as they are, do not alter them. You may delete a placeholder line "
    "only if the instruction explicitly asks to remove that commented/dead code."
)


@dataclass
class EditResult:
    ok: bool
    final_code: str | None
    diff: str
    error: str
    backup: str | None


def _extract_code(text: str) -> str:
    """Дістати код: перший ```-блок, якщо є; інакше весь текст."""
    t = (text or "").strip()
    m = re.search(r"```(?:python)?\s*\n(.*?)```", t, re.DOTALL)
    if m:
        return m.group(1).rstrip("\n")
    return t


def run_edit(path: str | Path, instruction: str,
             client: OllamaClient | None = None, write: bool = False,
             project_doc: str | None = None) -> EditResult:
    client = client or OllamaClient()
    original = tools.read_file(path)

    stripped, mapping = strip_code(original)
    preamble = f"Project context:\n{project_doc}\n\n" if project_doc else ""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",
         "content": f"{preamble}Instruction:\n{instruction}\n\n"
                    f"File `{path}`:\n```python\n{stripped}\n```"},
    ]
    msg = client.chat(messages, profile=config.EXECUTOR)
    new_stripped = _extract_code(msg.get("content") or "")

    ok, err = tools.validate_python(new_stripped)
    if not ok:
        return EditResult(False, None, "", f"вивід LLM невалідний: {err}", None)

    final = restore_code(new_stripped, mapping)
    ok2, err2 = tools.validate_python(final)
    if not ok2:
        return EditResult(False, None, "", f"після restore невалідно: {err2}", None)

    diff = tools.unified_diff(original, final, str(Path(path).name))

    backup = None
    if write:
        backup = tools.backup_file(path)
        tools.write_file(path, final)

    return EditResult(True, final, diff, "", backup)


def create_from_source(target: str | Path, source_path: str | Path, instruction: str,
                       client: OllamaClient | None = None,
                       project_doc: str | None = None) -> EditResult:
    """Створити НОВИЙ файл на основі джерела (read-only). Літерали джерела
    переносяться verbatim через strip/restore (модель їх не бачить)."""
    client = client or OllamaClient()
    original = tools.read_file(source_path)
    stripped, mapping = strip_code(original)
    preamble = f"Project context:\n{project_doc}\n\n" if project_doc else ""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user",
         "content": f"{preamble}Instruction:\n{instruction}\n\n"
                    f"Base the NEW file on this source `{Path(source_path).name}`:\n"
                    f"```python\n{stripped}\n```\nReturn the COMPLETE new file."},
    ]
    msg = client.chat(messages, profile=config.EXECUTOR)
    new_stripped = _extract_code(msg.get("content") or "")

    ok, err = tools.validate_python(new_stripped)
    if not ok:
        return EditResult(False, None, "", f"вивід LLM невалідний: {err}", None)

    final = restore_code(new_stripped, mapping)
    ok2, err2 = tools.validate_python(final)
    if not ok2:
        return EditResult(False, None, "", f"після restore невалідно: {err2}", None)

    diff = tools.unified_diff("", final, str(Path(target).name))
    return EditResult(True, final, diff, "", None)


def apply_edit(path: str | Path, result: EditResult) -> str | None:
    """Записати СХВАЛЕНУ правку з бекапом. Викликати після показу diff і згоди
    користувача (CLI/GUI). Повертає шлях бекапу."""
    if not result.ok or result.final_code is None:
        raise ValueError("немає валідного результату для запису")
    backup = tools.backup_file(path)
    tools.write_file(path, result.final_code)
    return backup
