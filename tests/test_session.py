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
    save_session(s, base=base)

    s2 = load_session(s.id, base=base)
    assert s2.title == "Рефакторинг parser"
    assert s2.project_root == r"F:\proj\myproj"
    assert s2.permissions["shell"] == "off"
    assert s2.init_scan is True
    assert len(s2.messages) == 2
    assert s2.messages[0]["content"] == "відрефактори parser.py"

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
