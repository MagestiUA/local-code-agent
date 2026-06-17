"""Офлайн-тест реєстру інструментів (детерміновані тули, без моделі).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_toolkit
"""
import tempfile
from pathlib import Path

from agent.toolkit import ToolContext, default_registry


def main() -> None:
    root = Path(tempfile.mkdtemp())
    (root / "a.py").write_text("X = 1\n", encoding="utf-8")
    reg = default_registry()
    ctx = ToolContext(root=root, permissions={"edits": "auto", "shell": "allowlist"})

    sch = reg.schema()
    assert any(t["function"]["name"] == "run_shell" for t in sch)
    assert any(t["function"]["name"] == "create_from_source" for t in sch)

    assert "X = 1" in reg.dispatch("read_file", {"path": "a.py"}, ctx)
    assert "записано" in reg.dispatch("write_file", {"path": "b.txt", "content": "hi"}, ctx)
    assert (root / "b.txt").read_text(encoding="utf-8") == "hi"

    lst = reg.dispatch("list_dir", {}, ctx)
    assert "a.py" in lst and "b.txt" in lst

    assert "Python" in reg.dispatch("run_shell", {"command": "python --version"}, ctx)

    # allowlist блокує деструктив
    assert "заблоковано" in reg.dispatch("run_shell", {"command": "del b.txt"}, ctx)

    # shell off
    ctx_off = ToolContext(root=root, permissions={"edits": "auto", "shell": "off"})
    assert "вимкнена" in reg.dispatch("run_shell", {"command": "python --version"}, ctx_off)

    # невідомий тул
    assert "невідомий" in reg.dispatch("nope", {}, ctx)

    print("OK: реєстр — схема, read/write/list/run_shell, дозволи, невідомий тул")
    print(f"  інструменти: {reg.names()}")


if __name__ == "__main__":
    main()
