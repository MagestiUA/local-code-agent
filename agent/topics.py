"""Теми chat-режиму — спільна (НЕ по-сесійна) памʼять для аналітичних розмов, що
переносяться між чатами (живий кейс: користувач вручну копіює 386KB+ тексту з
попереднього чату, бо хоче продовжити те саме обговорення).

Архітектура — той самий принцип, що й attachments.py (файли на диску, модель/код
читає лише потрібне), але теки СПІЛЬНІ для всього chat-режиму, не на сесію:
  1. Після кожного завершеного ходу розмови — окремий маленький LLM-виклик
     класифікує хід за темою: існуюча (з переданого списку) чи нова.
  2. Нотатка цієї теми (файл на диску) зливається з новим ходом — зберігаючи
     факти, висновки, ланцюжки причин-наслідків (як convo.update_digest), у межах
     бюджету (~30k токенів, із можливістю збільшити).
  3. У наступні ходи розмови підставляється: (а) короткий загальний підсумок
     розмови (convo, зменшений бюджет), (б) перелік тем, що траплялись У ЦІЙ
     сесії, (в) повний вміст ПОТОЧНОЇ релевантної теми (код підставляє напряму,
     без покладання на те, що модель сама викличе тул за нею).

Ім'я з крапкою в назві теки — щоб Reflex dev-watcher не тригерив hot-reload при
записі файлів (той самий гача, що й .attachments — див. attachments.py).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config
from .convo import _strip_echoed_label
from .llm import OllamaClient

TOPICS_ROOT = Path(__file__).resolve().parent.parent / ".chat_topics"
TOPIC_BUDGET_CHARS = 96_000     # ~30k токенів (евристика ~3.2 симв./токен) — можна збільшити

_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _slug(name: str) -> str:
    """Безпечне імʼя файлу з назви теми — лише символи, заборонені у файлових
    іменах (зокрема / і \\, тож traversal неможливий), решта (пробіли, кирилиця
    тощо) лишається як є для читабельності."""
    s = _UNSAFE_RE.sub("_", name.strip()).strip(". ")
    return (s or "тема")[:80]


def list_topics() -> list[str]:
    """Назви всіх існуючих тем (без розширення), відсортовані за останньою зміною
    (найновіші — спочатку, щоб класифікація бачила активні теми першими)."""
    if not TOPICS_ROOT.is_dir():
        return []
    files = sorted(TOPICS_ROOT.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.stem for p in files]


def load_topic(slug: str) -> str:
    p = TOPICS_ROOT / f"{_slug(slug)}.txt"
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def save_topic(slug: str, content: str) -> None:
    TOPICS_ROOT.mkdir(parents=True, exist_ok=True)
    (TOPICS_ROOT / f"{_slug(slug)}.txt").write_text(content, encoding="utf-8")


_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {"topic": {"type": "string"}, "is_new": {"type": "boolean"}},
    "required": ["topic", "is_new"],
}
_CLASSIFY_SYSTEM = (
    "You classify ONE turn of an analytical discussion into a topic. Given a list of "
    "EXISTING topic names and this turn's content, decide: does it belong to one of the "
    "existing topics, or is it a new topic? If it matches an existing topic, reuse that "
    "EXACT name (don't create a near-duplicate). If new, give it a short (2-5 word) "
    "descriptive name, same language as the content. Reply ONLY as JSON: "
    '{"topic": "<name>", "is_new": true|false}.'
)


def classify_topic(user_text: str, outcome: str, existing: list[str],
                   client: OllamaClient | None = None, profile: dict = config.EXECUTOR) -> str:
    """Повертає назву теми (існуючу з existing або нову) для цього ходу розмови.
    profile: chat-режим передає CHAT_EXECUTOR (gemma) — теми зараз лише для chat."""
    client = client or OllamaClient()
    topics_list = "\n".join(f"- {t}" for t in existing) if existing else "(немає існуючих тем)"
    user = (f"Існуючі теми:\n{topics_list}\n\nЦей хід розмови:\n"
            f"Користувач: {user_text}\n\nОбговорено/відповідь: {outcome}")
    msg = client.chat(
        [{"role": "system", "content": _CLASSIFY_SYSTEM}, {"role": "user", "content": user}],
        profile=profile, fmt=_CLASSIFY_SCHEMA,
    )
    try:
        data = json.loads(msg.get("content") or "{}")
        topic = (data.get("topic") or "").strip()
    except Exception:
        topic = ""
    return topic or "загальне"


_MERGE_SYSTEM = (
    "You maintain a running note for ONE topic of an ongoing analytical discussion. You "
    "get the CURRENT note (empty if this is the first turn on this topic) and the NEW turn "
    "(what the user said + what was discussed/concluded). Merge the new turn into the note: "
    "preserve concrete facts, claims, numbers, conclusions, and open questions, and "
    "ESPECIALLY chains of cause-and-effect / events — don't just append a flat bullet list, "
    "keep the connective reasoning that links facts together. Drop only redundancy and "
    "small talk. Output ONLY the updated note, same language as the content."
)


def update_topic_note(prev: str, user_text: str, outcome: str,
                      client: OllamaClient | None = None,
                      budget: int = TOPIC_BUDGET_CHARS, profile: dict = config.EXECUTOR) -> str:
    """Злити новий хід розмови в нотатку теми. Якщо вийшли за бюджет — ще один
    прохід тим самим промптом над самою нотаткою (природно стискає, бо
    зливає-і-стискає за дизайном). profile: chat-режим передає CHAT_EXECUTOR (gemma)."""
    client = client or OllamaClient()
    user = f"Поточна нотатка:\n{prev or '(порожньо)'}\n\nНовий хід:\nКористувач: {user_text}\n\nОбговорено: {outcome}"
    msg = client.chat(
        [{"role": "system", "content": _MERGE_SYSTEM}, {"role": "user", "content": user}],
        profile=profile,
    )
    out = _strip_echoed_label((msg.get("content") or "").strip(), "Поточна нотатка",
                              "Updated note", "Note") or prev
    if len(out) > budget:
        msg2 = client.chat(
            [{"role": "system", "content": _MERGE_SYSTEM},
             {"role": "user", "content": f"Поточна нотатка (СТИСНИ, забагато):\n{out}\n\n"
                                          "Новий хід:\n(немає — лише стисни вище)"}],
            profile=profile,
        )
        out = _strip_echoed_label((msg2.get("content") or "").strip(), "Поточна нотатка",
                                  "Updated note", "Note") or out
    return out


def available_topics_note(names: list[str]) -> str:
    """Блок для контексту чату: які теми (з усіх чатів — спільні) існують зараз.
    Повний вміст НЕ підставляється тут — модель сама читає потрібну через тул
    read_topic(name), якщо поточна розмова її продовжує."""
    if not names:
        return ""
    return ("Існують нотатки попередніх аналітичних розмов за темами (читай через "
            "read_topic(name), якщо ЦЯ розмова продовжує одну з них): "
            + ", ".join(names) + ".\n\n")
