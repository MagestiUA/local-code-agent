"""Самодостатній тест strip/restore (без pytest, без реальних даних користувача).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_literals
"""
from agent.literals import strip_code, restore_code
from agent.tools import validate_python

# Синтетичний файл: великий активний літерал + великий коментар-дані.
BIG = "A1B2" * 400          # > 1000 символів
COMMENT = "Z9" * 800        # > 1000 символів
src = (
    "import json\n"
    f'DATA = json.loads("{BIG}")\n'
    "\n"
    "def add(a, b):\n"
    "    return a + b\n"
    f"# {COMMENT}\n"
)


def main() -> None:
    stripped, mapping = strip_code(src)

    assert "__LIT_0__" in stripped, "плейсхолдер не вставлено"
    assert BIG not in stripped, "великий літерал лишився у вході для LLM"
    assert COMMENT not in stripped, "коментар-дані лишився"
    assert len(stripped) < len(src), "обрізаний не менший"

    restored = restore_code(stripped, mapping)
    assert restored == src, "round-trip НЕ байт-у-байт"

    ok, err = validate_python(restored)
    assert ok, f"відновлений код невалідний: {err}"

    print("OK: strip/restore байт-у-байт")
    print(f"  плейсхолдери: {list(mapping.keys())}")
    print(f"  було {len(src)} симв. -> обрізано {len(stripped)} симв.")


if __name__ == "__main__":
    main()
