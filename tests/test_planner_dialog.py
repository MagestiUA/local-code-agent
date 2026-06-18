"""Офлайн-тест планувальника-діалогу (deliberate) зі стаб-моделлю.
Запуск:  .venv\\Scripts\\python.exe -m tests.test_planner_dialog
Стаб віддає заскриптовані JSON-рішення; перевіряємо clarify/choose/plan + fallback.
"""
import json

from agent.planner import deliberate


class DecisionStub:
    """Повертає заздалегідь задані відповіді по черзі (як OllamaClient.chat)."""
    def __init__(self, scripted: list[dict]):
        self.scripted = scripted
        self.i = 0

    def chat(self, messages, tools=None, profile=None, fmt=None):
        d = self.scripted[min(self.i, len(self.scripted) - 1)]
        self.i += 1
        return {"content": json.dumps(d, ensure_ascii=False)}


def main() -> None:
    # clarify: який файл міняємо -> клікабельні кандидати
    r = deliberate("онови парсер", client=DecisionStub([{
        "action": "clarify", "reasoning": "не вказано файл",
        "question": "Який файл оновити?",
        "options": [{"label": "a.py", "detail": ""}, {"label": "b.py", "detail": ""}],
    }]))
    assert r["action"] == "clarify"
    assert [o["label"] for o in r["options"]] == ["a.py", "b.py"]
    assert r["state"] is None and r["question"]

    # choose: кілька підходів
    r = deliberate("додай кеш", client=DecisionStub([{
        "action": "choose", "reasoning": "є варіанти",
        "question": "Який підхід?",
        "options": [{"label": "in-memory", "detail": "швидко"},
                    {"label": "redis", "detail": "масштабовано"}],
    }]))
    assert r["action"] == "choose" and len(r["options"]) == 2

    # plan: готові кроки -> TaskState
    r = deliberate("рефактор x.py", client=DecisionStub([{
        "action": "plan", "reasoning": "ясно",
        "steps": [{"kind": "llm", "description": "переписати foo", "target": "x.py"}],
    }]))
    assert r["action"] == "plan" and r["state"] is not None
    assert len(r["state"].steps) == 1 and r["state"].steps[0].target == "x.py"

    # fallback: action=plan без кроків -> deliberate кличе make_plan (другий виклик стабу)
    stub = DecisionStub([
        {"action": "plan", "steps": []},                       # порожньо
        {"steps": [{"kind": "llm", "description": "зробити", "target": "y.py"}]},  # make_plan
    ])
    r = deliberate("щось", client=stub)
    assert r["action"] == "plan" and len(r["state"].steps) == 1
    assert r["state"].steps[0].target == "y.py"

    # невідомий action -> трактуємо як plan
    r = deliberate("?", client=DecisionStub([
        {"action": "nonsense", "steps": [{"kind": "inline", "description": "тривіально"}]},
    ]))
    assert r["action"] == "plan" and r["state"].steps[0].kind == "inline"

    print("OK: deliberate — clarify/choose/plan, нормалізація опцій, fallback, невідомий action")


if __name__ == "__main__":
    main()
