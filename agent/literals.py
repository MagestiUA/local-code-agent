"""Детерміноване вирізання/відновлення великих опакових літералів (без LLM).

Серце підходу: великі дані (протобафи, payload'и) не повинні проходити крізь
модель — вона їх псує і роздуває контекст. Вирізаємо їх у плейсхолдери ДО LLM
і повертаємо байт-у-байт ПІСЛЯ.

Функції чисті (рядок -> рядок), щоб executor міг працювати в пам'яті.
"""
from __future__ import annotations

import ast
import re

THRESHOLD = 1000  # символів: усе більше вважаємо "опаковими даними"


def strip_code(src: str) -> tuple[str, dict]:
    """Повертає (обрізаний_код, mapping). mapping: {placeholder: {kind, value}}.
    kind='expr' — активна строкова константа; kind='line' — довгий коментар-дані.
    """
    mapping: dict = {}
    idx = 0

    # 1) Активні строкові константи (через AST).
    tree = ast.parse(src)
    segs: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and len(node.value) > THRESHOLD:
            seg = ast.get_source_segment(src, node)
            if seg and seg not in segs:
                segs.append(seg)
    for seg in segs:
        ph = f"__LIT_{idx}__"
        mapping[ph] = {"kind": "expr", "value": seg}
        src = src.replace(seg, repr(ph), 1)
        idx += 1

    # 2) Довгі закоментовані рядки-дані (AST їх не бачить).
    out_lines = []
    for line in src.split("\n"):
        if line.lstrip().startswith("#") and len(line) > THRESHOLD:
            ph = f"__LIT_{idx}__"
            mapping[ph] = {"kind": "line", "value": line}
            indent = line[:len(line) - len(line.lstrip())]
            out_lines.append(f"{indent}#{ph}")
            idx += 1
        else:
            out_lines.append(line)

    return "\n".join(out_lines), mapping


def restore_code(code: str, mapping: dict) -> str:
    """Вставляє оригінали назад. Працює суто текстово (не парсить), тож стійке
    до того, що LLM міг перемістити/перейменувати навколо плейсхолдера."""
    for ph, info in mapping.items():
        if info["kind"] == "expr":
            pattern = r"""['"]""" + re.escape(ph) + r"""['"]"""
        else:  # line: відступ + #__LIT_N__
            pattern = r"""[ \t]*#""" + re.escape(ph)
        code = re.subn(pattern, lambda m: info["value"], code)[0]
    return code
