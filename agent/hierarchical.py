"""ДОСЛІДЖЕННЯ R1: ієрархічне планування проти переповнення контексту. ПРОТОТИП —
НЕ вбудований у GUI; призначений для оцінки на 1-2 важких задачах vs поточний one-pass.

Ідея: не штовхати все в один прохід, а дробити —
  1) декомпозиція задачі на кроки (make_plan);
  2) КОЖЕН крок виконуємо з ВУЗЬКИМ контекстом = компактний зріз стану
     (render_for_planner: план+статуси+стислі результати), а не вся історія;
  3) reduce-фаза: планувальник звіряє, чи кроки узгоджені; якщо ні — повертає
     конкретний крок на переобдумування з уточненням (ліміт раундів).

Так кожен прохід бачить мало контексту, і модель не «захлинається» великим вікном.

Детекція переповнення спирається на client.last_stats.prompt vs num_ctx.
"""
from __future__ import annotations

import json

from . import config
from .agent_loop import run_step
from .llm import OllamaClient
from .memory import TaskState, render_for_planner
from .planner import make_plan
from .toolkit import ToolContext, ToolRegistry, default_registry

OVERFLOW_THRESHOLD = 0.85         # частка num_ctx, від якої вважаємо контекст «на межі»


def context_usage(prompt_tokens: int, num_ctx: int) -> float:
    """Частка зайнятого контекстного вікна (0..1+)."""
    return prompt_tokens / num_ctx if num_ctx else 0.0


def is_overflow(prompt_tokens: int, num_ctx: int | None = None,
                threshold: float = OVERFLOW_THRESHOLD) -> bool:
    """Чи контекст близький до переповнення (сигнал перейти в дробильний режим)."""
    num_ctx = num_ctx or config.EXECUTOR["num_ctx"]
    return context_usage(prompt_tokens, num_ctx) >= threshold


# ── Reduce-фаза: звірка узгодженості ─────────────────────────────────────────
CONSISTENCY_SYSTEM = (
    "You review an executed plan (compact slice: steps, statuses, short results). Decide "
    "if all steps are mutually consistent and the task is complete. Reply ONLY as JSON: "
    "{\"ok\": true} if consistent; otherwise {\"ok\": false, \"step_id\": N, \"note\": "
    "\"what to fix\"} naming ONE step to redo with a short clarification (user's language)."
)

CONSISTENCY_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "step_id": {"type": ["integer", "null"]},
        "note": {"type": ["string", "null"]},
    },
    "required": ["ok"],
}


def check_consistency(state: TaskState, client: OllamaClient) -> dict:
    """Звірити узгодженість виконаного плану. -> {ok, step_id, note}."""
    msg = client.chat(
        [{"role": "system", "content": CONSISTENCY_SYSTEM},
         {"role": "user", "content": render_for_planner(state)}],
        profile=config.EXECUTOR, fmt=CONSISTENCY_SCHEMA,
    )
    try:
        d = json.loads(msg.get("content") or "")
        if isinstance(d, dict):
            return {"ok": bool(d.get("ok")), "step_id": d.get("step_id"),
                    "note": str(d.get("note") or "")}
    except Exception:
        pass
    return {"ok": True, "step_id": None, "note": ""}          # не змогли розпарсити -> не зациклюємось


def _run_one(step, state: TaskState, ctx: ToolContext, client, registry,
             extra: str = "") -> None:
    """Виконати один крок із ВУЗЬКИМ контекстом (зріз стану) і записати результат."""
    if step.kind == "inline":
        state.set_result(step.id, "done", "inline")
        return
    narrow = render_for_planner(state)                        # робоча памʼять — лише зріз
    desc = step.description + (f"\nУточнення: {extra}" if extra else "")
    final, log = run_step(desc, ctx, client, registry, context=narrow)
    state.set_result(step.id, "done" if log or final else "failed", (final or "")[:200])


def solve(task: str, ctx: ToolContext, client: OllamaClient | None = None,
          registry: ToolRegistry | None = None, project_doc: str | None = None,
          max_rounds: int = 2) -> TaskState:
    """ПРОТОТИП ієрархічного розв'язання. Повертає TaskState з результатами кроків."""
    client = client or OllamaClient()
    registry = registry or default_registry()

    state = make_plan(task, client=client, project_doc=project_doc)
    for step in state.steps:                                  # map: вузький прохід по кроках
        _run_one(step, state, ctx, client, registry)

    for _ in range(max_rounds):                               # reduce: звірка + переобдумування
        verdict = check_consistency(state, client)
        if verdict["ok"]:
            break
        step = next((s for s in state.steps if s.id == verdict["step_id"]), None)
        if not step:
            break
        _run_one(step, state, ctx, client, registry, extra=verdict["note"])

    return state
