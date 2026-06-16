"""
Детерміновано (без LLM) вирізає великі літерали-дані з .py у плейсхолдери:
  - активні строкові константи (через AST)
  - довгі закоментовані рядки-дані (через пройму по рядках)
Оригінали — в сайдкар-JSON для точного відновлення байт-у-байт.

  strip_literals.py strip   <in.py> <out.py> <map.json>
  strip_literals.py restore <refactored.py> <map.json> <final.py>

(У M1 переїде в agent/literals.py як функції.)
"""
from __future__ import annotations
import ast, json, re, sys

THRESHOLD = 1000  # символів


def strip(src_path: str, out_path: str, map_path: str) -> None:
    src = open(src_path, encoding="utf-8").read()
    mapping = {}
    idx = 0

    tree = ast.parse(src)
    segs = []
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
    src = "\n".join(out_lines)

    open(out_path, "w", encoding="utf-8").write(src)
    json.dump(mapping, open(map_path, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"Вирізано літералів: {len(mapping)}")
    for ph, info in mapping.items():
        print(f"  {ph} [{info['kind']}]: {len(info['value'])} символів")


def restore(refactored_path: str, map_path: str, final_path: str) -> None:
    out = open(refactored_path, encoding="utf-8").read()
    mapping = json.load(open(map_path, encoding="utf-8"))
    restored = 0
    for ph, info in mapping.items():
        if info["kind"] == "expr":
            pattern = r"""['"]""" + re.escape(ph) + r"""['"]"""
        else:
            pattern = r"""[ \t]*#""" + re.escape(ph)
        out, n = re.subn(pattern, lambda m: info["value"], out)
        restored += n
    open(final_path, "w", encoding="utf-8").write(out)
    print(f"Відновлено вставок: {restored} з {len(mapping)} літералів")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "strip":
        strip(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "restore":
        restore(sys.argv[2], sys.argv[3], sys.argv[4])
