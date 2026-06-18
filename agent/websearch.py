"""Веб-пошук — тонка абстракція над провайдером. Старт: DuckDuckGo (ddgs, без ключа,
безкоштовно). Провайдера можна підмінити (Tavily/Brave) без змін у тулі/циклі.

search() повертає список {title, url, snippet} або None, якщо провайдер недоступний
(не встановлено бібліотеку / помилка мережі). PROVIDER підмінюється в тестах.
"""
from __future__ import annotations

DEFAULT_MAX = 5


def _ddg_search(query: str, max_results: int):
    """Провайдер DuckDuckGo через бібліотеку ddgs (стара назва — duckduckgo_search)."""
    try:
        from ddgs import DDGS
    except Exception:
        try:
            from duckduckgo_search import DDGS
        except Exception:
            return None                       # бібліотека не встановлена
    rows = DDGS().text(query, max_results=max_results) or []
    out = []
    for r in rows:
        out.append({
            "title": (r.get("title") or "").strip(),
            "url": (r.get("href") or r.get("url") or "").strip(),
            "snippet": (r.get("body") or r.get("snippet") or "").strip(),
        })
    return out


PROVIDER = _ddg_search                        # активний провайдер (підмінний)


def search(query: str, max_results: int = DEFAULT_MAX):
    """Пошук. Повертає [{title,url,snippet}] або None, якщо провайдер недоступний."""
    q = (query or "").strip()
    if not q:
        return []
    try:
        return PROVIDER(q, max_results)
    except Exception:
        return None


def format_results(results) -> str:
    """Компактний текст сніпетів для подачі моделі."""
    if results is None:
        return ("веб-пошук недоступний: провайдер не налаштований "
                "(встановіть `ddgs` або перевірте мережу)")
    if not results:
        return "нічого не знайдено"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
    return "\n".join(lines)
