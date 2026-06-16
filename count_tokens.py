"""Порахувати реальні токени файлу токенізатором gemma через Ollama.

  count_tokens.py <file>
"""
import sys, requests

path = sys.argv[1]
text = open(path, encoding="utf-8").read()
lines = text.count("\n") + 1

r = requests.post("http://127.0.0.1:11434/api/chat", json={
    "model": "gemma4:26b-a4b-it-qat",
    "messages": [{"role": "user", "content": text}],
    "stream": False,
    "options": {"num_predict": 1, "num_ctx": 131072},
}, timeout=300)
r.raise_for_status()
toks = r.json().get("prompt_eval_count", 0)
print(f"Файл: {path}")
print(f"Рядків: {lines}  |  Символів: {len(text)}  |  Токенів (gemma): {toks}")
