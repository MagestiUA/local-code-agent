"""Офлайн-тест проектної довідки (AGENT.md). Стаб замість Ollama.
Запуск:  .venv\\Scripts\\python.exe -m tests.test_project
"""
import tempfile
from pathlib import Path

from agent.planner import make_plan
from agent.project import ensure_project_doc, load_project_doc, scan_structure


class CapStub:
    """Стаб LLM, що ЗАПАМ'ЯТОВУЄ отримані повідомлення (для перевірки вшивання)."""
    def __init__(self, reply: str):
        self.reply = reply
        self.captured = None

    def chat(self, messages, tools=None, profile=None, fmt=None):
        self.captured = messages
        return {"content": self.reply}


def main() -> None:
    root = Path(tempfile.mkdtemp())
    (root / "README.md").write_text("# Demo project\nDoes things.", encoding="utf-8")
    (root / "agent").mkdir()
    (root / "agent" / "x.py").write_text("def f(): return 1\n", encoding="utf-8")
    # службова тека має відсіюватись
    (root / ".venv").mkdir()
    (root / ".venv" / "junk.py").write_text("# huge junk\n", encoding="utf-8")

    # 1) scan: бачить код, не бачить .venv
    scan = scan_structure(root)
    assert "README.md" in scan and "agent/x.py" in scan, scan
    assert "junk.py" not in scan and ".venv" not in scan, "службова тека не відсіялась"

    # 2) AGENT.md ще нема
    assert load_project_doc(root) is None

    # 3) ensure без init_scan -> None (нічого не генеруємо)
    assert ensure_project_doc(root, client=CapStub("x"), init_scan=False) is None

    # 4) ensure з init_scan -> генерує (стаб), зберігає, повертає
    draft = ensure_project_doc(root, client=CapStub("# AGENT\nЦе демо."), init_scan=True)
    assert draft and "демо" in draft
    assert load_project_doc(root) == draft, "AGENT.md не збережено"

    # 5) повторний ensure -> повертає наявний (без генерації)
    again = ensure_project_doc(root, client=CapStub("ІНШЕ"), init_scan=True)
    assert again == draft, "має повертати наявний файл, а не генерувати знову"

    # 6) вшивання проектного контексту в планувальник
    cap = CapStub('{"steps":[]}')
    make_plan("зроби щось", client=cap, project_doc="МАРКЕР_ДОКУ_123")
    joined = " ".join(m["content"] for m in cap.captured)
    assert "МАРКЕР_ДОКУ_123" in joined, "проектний контекст не потрапив у промпт планувальника"

    print("OK: scan відсіює службове, AGENT.md ensure/load/draft, вшивання в planner")
    print(f"  структура:\n    " + scan.replace(chr(10), chr(10) + "    "))


if __name__ == "__main__":
    main()
