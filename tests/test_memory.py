"""Тест дискового стану задачі (без Ollama).
Запуск:  .venv\\Scripts\\python.exe -m tests.test_memory
"""
import tempfile

from agent.memory import TaskState, load, render_for_planner, save


def main() -> None:
    st = TaskState(task="Рефакторити parser.py")
    s1 = st.add_step("llm", "Переписати get_app_list ітеративно", target="parser.py")
    st.add_step("deterministic", "Повернути літерал BODY_1", target="parser.py")
    st.set_result(s1.id, "done", "рекурсію замінено на while, -8 рядків")
    st.facts["BODY_1"] = "parser.py:25"

    d = tempfile.mkdtemp()
    p = save(st, root=d)
    assert p.exists()

    st2 = load(root=d)
    assert st2.task == st.task
    assert len(st2.steps) == 2
    assert st2.steps[0].status == "done"
    assert st2.steps[0].result.startswith("рекурсію")
    assert st2.facts["BODY_1"] == "parser.py:25"
    assert st2.next_pending().id == 2, "наступний pending має бути крок 2"

    rendered = render_for_planner(st2)
    assert "Задача:" in rendered
    assert "#1" in rendered and "#2" in rendered
    assert "BODY_1" in rendered

    print("OK: стан задачі зберігається/завантажується, рендер компактний\n")
    print(rendered)


if __name__ == "__main__":
    main()
