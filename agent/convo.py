"""Контекст-памʼять розмови — компактний підсумок зробленого, що подається в
наступні запити (передусім планувальнику). НЕ повний транскрипт: після кожного
завершеного запиту дописуємо стислий «епізод» (LLM), а коли підсумок переростає
бюджет — стискаємо його (LLM). Так модель пов'язує кроки розмови, а контекст не
роздуває 65k вікна.

Бюджет ~10k токенів ≈ ~32k символів (евристика ~3.2 символи/токен для UA+коду).
"""
from __future__ import annotations

from . import config
from .llm import OllamaClient

BUDGET_CHARS = 32000          # ~10k токенів
COMPRESS_TO = BUDGET_CHARS // 2

_EPISODE_SYSTEM = (
    "You compress ONE turn of a coding session into 1-3 short factual bullet lines for "
    "future context. State concretely: which files were created/changed, which "
    "functions/classes were added or modified, key decisions, and what the user asked. "
    "Be terse — no fluff, no restating the whole code. Same language as the content."
)

_COMPRESS_SYSTEM = (
    "You condense a running summary of a coding session. KEEP every concrete fact: files "
    "changed/created, functions/classes added or modified, decisions made, and any open "
    "threads. Drop redundancy and small talk. Make it about half as long. Same language."
)


def summarize_turn(user_text: str, outcome: str, client: OllamaClient | None = None) -> str:
    """Стислий епізод одного запиту (що просили + що зроблено)."""
    client = client or OllamaClient()
    user = f"Запит користувача:\n{user_text}\n\nЩо зроблено:\n{outcome}"
    msg = client.chat(
        [{"role": "system", "content": _EPISODE_SYSTEM}, {"role": "user", "content": user}],
        profile=config.EXECUTOR,
    )
    return (msg.get("content") or "").strip()


def compress_summary(summary: str, client: OllamaClient | None = None) -> str:
    """Стиснути переповнений підсумок, зберігши конкретні факти."""
    client = client or OllamaClient()
    msg = client.chat(
        [{"role": "system", "content": _COMPRESS_SYSTEM}, {"role": "user", "content": summary}],
        profile=config.EXECUTOR,
    )
    return (msg.get("content") or "").strip()


def update_summary(summary: str, user_text: str, outcome: str,
                   client: OllamaClient | None = None, budget: int = BUDGET_CHARS) -> str:
    """Дописати епізод поточного запиту; стиснути, якщо вийшли за бюджет."""
    ep = summarize_turn(user_text, outcome, client)
    if not ep:
        return summary
    new = (summary + "\n- " + ep).strip() if summary else "- " + ep
    if len(new) > budget:
        new = compress_summary(new, client)
    return new


def as_context(summary: str) -> str:
    """Блок підсумку для підстановки в запит (порожній рядок, якщо нема)."""
    return f"Контекст попередньої розмови (підсумок зробленого):\n{summary}\n\n" if summary else ""
