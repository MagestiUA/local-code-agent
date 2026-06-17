"""Офлайн-тест сесій (без Ollama).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_session
"""
import tempfile

from agent.session import list_sessions, load_session, new_session, save_session


def main() -> None:
    base = tempfile.mkdtemp()

    s = new_session("Рефакторинг parser", r"F:\proj\myproj",
                    permissions={"edits": "ask", "shell": "off"}, init_scan=True)
    s.add_message("user", "відрефактори parser.py")
    s.add_message("assistant", "склав план з 4 кроків", kind="plan")
    s.add_reference(r"F:\other\src.py")
    s.add_reference(r"F:\other\src.py")   # дублікат не додається
    save_session(s, base=base)

    s2 = load_session(s.id, base=base)
    assert s2.title == "Рефакторинг parser"
    assert s2.project_root == r"F:\proj\myproj"
    assert s2.permissions["shell"] == "off"
    assert s2.init_scan is True
    assert len(s2.messages) == 2
    assert s2.messages[0]["content"] == "відрефактори parser.py"
    assert s2.reference_files == [r"F:\other\src.py"], "файл-джерело не зберігся"
    s2.remove_reference(r"F:\other\src.py")
    assert s2.reference_files == [], "видалення джерела не спрацювало"

    # pending-план + plan_first round-trip
    from agent.memory import TaskState
    plan = TaskState(task="p")
    plan.add_step("llm", "крок А", target="a.py")
    s.plan_first = True
    s.set_pending_plan(plan)
    save_session(s, base=base)
    s3 = load_session(s.id, base=base)
    assert s3.plan_first is True
    pp = s3.get_pending_plan()
    assert pp is not None and pp.steps[0].description == "крок А"
    s3.clear_pending_plan()
    assert s3.get_pending_plan() is None

    # другий чат -> у списку обидва, нові згори
    s3 = new_session("Інший проект", r"F:\proj\other")
    save_session(s3, base=base)
    lst = list_sessions(base=base)
    assert len(lst) == 2
    assert {x["title"] for x in lst} == {"Рефакторинг parser", "Інший проект"}

    print("OK: сесії створюються/зберігаються/завантажуються, список працює")
    for x in lst:
        print(f"  {x['id']}  {x['title']}  ({x['project_root']})")


if __name__ == "__main__":
    main()
