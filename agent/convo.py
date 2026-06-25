"""Контекст-памʼять розмови — компактний підсумок зробленого, що подається в
наступні запити (передусім планувальнику). НЕ повний транскрипт: після кожного
завершеного запиту дописуємо стислий «епізод» (LLM), а коли підсумок переростає
бюджет — стискаємо його (LLM). Так модель пов'язує кроки розмови, а контекст не
роздуває 65k вікна.

Бюджет ~10k токенів ≈ ~32k символів (евристика ~3.2 символи/токен для UA+коду).

Два РІЗНІ профілі стиснення:
  code/answer/shell-режими — `summarize_turn`/`compress_summary` (kind="code"):
    орієнтовані на файли/функції/рішення — нерелевантно для аналітичних розмов.
  chat-режим — ті самі функції з kind="chat": фокус на тезах, фактах, ланцюжках
    подій/причин-наслідків розмови, без коду-специфічних рамок.

Окремо — `update_digest`: для ВЕЛИКИХ вкладень, що читаються частинами
(read_attachment-пагінація). Замість накопичення сирих шматків у контексті,
кожен прочитаний шматок одразу зливається в один файл-памʼять (~30k токенів),
зберігаючи деталі й ланцюжки подій, а не просто список фактів.
"""
from __future__ import annotations

import re

from . import config
from .llm import OllamaClient

BUDGET_CHARS = 32000          # ~10k токенів
COMPRESS_TO = BUDGET_CHARS // 2

_EPISODE_SYSTEM = {
    "code": (
        "You compress ONE turn of a coding session into 1-3 short factual bullet lines for "
        "future context. State concretely: which files were created/changed, which "
        "functions/classes were added or modified, key decisions, and what the user asked. "
        "Be terse — no fluff, no restating the whole code. Same language as the content."
    ),
    "chat": (
        "You compress ONE turn of an analytical discussion into factual bullet lines for "
        "future context. State concretely: what topic/claim was discussed, key facts and "
        "numbers mentioned, conclusions or open questions raised, and what the user asked. "
        "Preserve cause-and-effect / event chains, not just isolated facts — don't reduce a "
        "complex turn to 1-3 lines if it covered several distinct topics; use as many bullet "
        "lines as the content actually needs. Be terse but DON'T drop concrete details. Same "
        "language as the content."
    ),
}

_COMPRESS_SYSTEM = {
    "code": (
        "You condense a running summary of a coding session. KEEP every concrete fact: files "
        "changed/created, functions/classes added or modified, decisions made, and any open "
        "threads. Drop redundancy and small talk. Make it about half as long. Same language."
    ),
    "chat": (
        "You condense a running summary of an analytical discussion. KEEP every concrete fact, "
        "number, claim, conclusion, and open question — and KEEP cause-and-effect/event chains "
        "intact (don't flatten them into disconnected bullet points). Drop only redundancy and "
        "small talk. Make it about half as long. Same language."
    ),
}


# kind="chat" -> gemma (CHAT_EXECUTOR), kind="code" -> Qwen (EXECUTOR). Інакше
# chat-режим випадково обробляв би аналітичний текст кодинг-моделлю.
def _profile_for(kind: str) -> dict:
    return config.CHAT_EXECUTOR if kind == "chat" else config.EXECUTOR


def summarize_turn(user_text: str, outcome: str, client: OllamaClient | None = None,
                   kind: str = "code") -> str:
    """Стислий епізод одного запиту (що просили + що зроблено). kind="code"|"chat"."""
    client = client or OllamaClient()
    user = f"Запит користувача:\n{user_text}\n\nЩо зроблено:\n{outcome}"
    msg = client.chat(
        [{"role": "system", "content": _EPISODE_SYSTEM[kind]}, {"role": "user", "content": user}],
        profile=_profile_for(kind),
    )
    return (msg.get("content") or "").strip()


def compress_summary(summary: str, client: OllamaClient | None = None, kind: str = "code") -> str:
    """Стиснути переповнений підсумок, зберігши конкретні факти. kind="code"|"chat"."""
    client = client or OllamaClient()
    msg = client.chat(
        [{"role": "system", "content": _COMPRESS_SYSTEM[kind]}, {"role": "user", "content": summary}],
        profile=_profile_for(kind),
    )
    return (msg.get("content") or "").strip()


def update_summary(summary: str, user_text: str, outcome: str,
                   client: OllamaClient | None = None, budget: int = BUDGET_CHARS,
                   kind: str = "code") -> str:
    """Дописати епізод поточного запиту; стиснути, якщо вийшли за бюджет."""
    ep = summarize_turn(user_text, outcome, client, kind=kind)
    if not ep:
        return summary
    new = (summary + "\n- " + ep).strip() if summary else "- " + ep
    if len(new) > budget:
        new = compress_summary(new, client, kind=kind)
    return new


def as_context(summary: str) -> str:
    """Блок підсумку для підстановки в запит (порожній рядок, якщо нема)."""
    return f"Контекст попередньої розмови (підсумок зробленого):\n{summary}\n\n" if summary else ""


# ── Дайджест великого вкладення (читання частинами) ─────────────────────────
DIGEST_BUDGET_CHARS = 96_000   # ~30k токенів (евристика ~3.2 симв./токен)

_DIGEST_SYSTEM = (
    "You maintain a running digest of a long document being read in sequential chunks. "
    "You get the CURRENT digest (empty on the first chunk) and the NEXT raw chunk of the "
    "document. Merge the chunk's content into the digest. Preserve concrete facts, names, "
    "numbers, claims, and ESPECIALLY chains of events / cause-and-effect / topic threads — "
    "don't just append a flat bullet list, keep the connective reasoning that links facts "
    "together. Drop only redundancy and filler. Output ONLY the updated digest (no preamble, "
    "no commentary about the task), same language as the document."
)


# Слабка модель часто ЛУНАЄ заголовок нашого ж промпту ("Поточний дайджест:",
# "Поточна нотатка:") на початку відповіді замість чистого тексту — бачено живцем,
# іноді обгорнутий у markdown ("**Поточна нотатка:**"). Прибираємо програмно
# (regex толерує провідні *#_> та крапку з пробілами), бо словами в промпті
# ("Output ONLY the digest") це не завжди стримує.
def _strip_echoed_label(text: str, *labels: str) -> str:
    s = text.lstrip()
    for label in labels:
        m = re.match(r'^[\s*#_>-]*' + re.escape(label) + r'[\s*:：]*', s, re.IGNORECASE)
        if m:
            return s[m.end():].lstrip()
    return text


# Якщо модель не мала чим заповнити дайджест/нотатку (напр. перший хід занадто
# куций) — вона іноді ЛУНАЄ наш власний плейсхолдер ("(порожньо)",
# "(порожньо — це перший шматок)") замість того, щоб лишити поле незмінним.
# Живий кейс: створився файл теми буквально з вмістом "(порожньо)" — сміття на
# диску. Розпізнаємо й трактуємо як "нічого корисного не повернула".
def _is_empty_echo(text: str) -> bool:
    t = text.strip().strip("()").strip()
    return not t or t.lower().startswith("порожньо")


def update_digest(digest: str, chunk: str, client: OllamaClient | None = None,
                  budget: int = DIGEST_BUDGET_CHARS, profile: dict = config.EXECUTOR) -> str:
    """Злити новий шматок великого документа в дайджест (файл-памʼять). Якщо
    результат вийшов за бюджет — стиснути ще раз тим самим промптом (він і так
    зливає/стискає, тож повторний прохід над самим дайджестом природно коротшає).
    profile: за дефолтом EXECUTOR (Qwen, код); chat-режим передає CHAT_EXECUTOR
    (gemma) — інакше аналітичний текст обробляла б кодинг-модель."""
    client = client or OllamaClient()
    user = (f"Поточний дайджест:\n{digest or '(порожньо — це перший шматок)'}\n\n"
            f"Новий шматок документа:\n{chunk}")
    msg = client.chat(
        [{"role": "system", "content": _DIGEST_SYSTEM}, {"role": "user", "content": user}],
        profile=profile,
    )
    out = _strip_echoed_label((msg.get("content") or "").strip(), "Поточний дайджест",
                              "Updated digest", "Digest")
    if _is_empty_echo(out):
        out = digest
    if len(out) > budget:
        msg2 = client.chat(
            [{"role": "system", "content": _DIGEST_SYSTEM},
             {"role": "user", "content": f"Поточний дайджест (СТИСНИ, забагато):\n{out}\n\n"
                                          "Новий шматок документа:\n(немає — лише стисни вище)"}],
            profile=profile,
        )
        out2 = _strip_echoed_label((msg2.get("content") or "").strip(), "Поточний дайджест",
                                   "Updated digest", "Digest")
        out = out if _is_empty_echo(out2) else out2
    return out
