"""Офлайн-тест проектів chat-режиму (chat_projects). Запуск:
.venv\\Scripts\\python.exe -m tests.test_chat_projects

ІЗОЛЯЦІЯ: підміняємо CP.PROJECTS_DIR на тимчасову теку на час тесту. Раніше тест
видаляв (shutil.rmtree) РЕАЛЬНУ ~/.local-code-agent/chat_projects/ — кожен
прогін тестів стирав справжні проекти користувача без жодного попередження
(живий кейс: користувацький проект зник після кількох прогонів цього тесту
в одній сесії). НІКОЛИ не чіпати продакшен-шлях у тесті."""
import shutil
import tempfile
from pathlib import Path

from agent import chat_projects as CP


def main() -> None:
    real_dir = CP.PROJECTS_DIR
    CP.PROJECTS_DIR = Path(tempfile.mkdtemp()) / "chat_projects"
    try:
        assert CP.list_projects() == []

        p = CP.new_project("Аналітика", "Будь стислим і конкретним.")
        CP.save_project(p)
        listed = CP.list_projects()
        assert len(listed) == 1 and listed[0]["name"] == "Аналітика"
        loaded = CP.load_project(p.id)
        assert loaded.prompt == "Будь стислим і конкретним."

        # редагування промпту -> перезберегти, перевірити що оновилось
        loaded.prompt = "Новий промпт."
        CP.save_project(loaded)
        assert CP.load_project(p.id).prompt == "Новий промпт."

        # ліміт 5000 символів — обрізається і при new_project, і при save_project
        big = "x" * 6000
        p2 = CP.new_project("Великий", big)
        assert len(p2.prompt) == CP.PROMPT_MAX_CHARS
        p2.prompt = big
        CP.save_project(p2)
        assert len(CP.load_project(p2.id).prompt) == CP.PROMPT_MAX_CHARS

        # сортування за іменем (без регістру)
        CP.save_project(CP.new_project("яблуко"))
        CP.save_project(CP.new_project("Банан"))
        names = [p["name"] for p in CP.list_projects()]
        assert names == sorted(names, key=str.lower), names

        # видалення
        CP.delete_project(p.id)
        assert CP.load_project(p.id) is None
        assert all(x["id"] != p.id for x in CP.list_projects())

        # неіснуючий/порожній id -> None, без винятку
        assert CP.load_project("") is None
        assert CP.load_project("nope") is None

        # as_system_block
        assert CP.as_system_block("") == ""
        assert CP.as_system_block("  ") == ""
        assert "Будь" in CP.as_system_block("Будь стислим.")

        print("OK: chat_projects — створення/завантаження/редагування, ліміт 5000 символів, "
              "сортування, видалення, as_system_block")
    finally:
        shutil.rmtree(CP.PROJECTS_DIR, ignore_errors=True)
        CP.PROJECTS_DIR = real_dir


if __name__ == "__main__":
    main()
