# local-code-agent

*Читати українською — [README.md](README.md).*

A local coding agent built on **gemma4:26b-a4b-it-qat** (via Ollama), designed
around a weak local model: narrow context, task decomposition, an agentic
tool-loop, and deterministic handling of large data.

## Idea

- **Planner** (think=on) — reads the task and splits it into small steps.
- **Per-step tool-loop** (think=off, narrow context) — for each step the model
  calls tools itself via native tool-calling (read/write/edit files, run
  commands); the runtime executes them and feeds results back until the step is
  done. The model never runs code itself — our loop does.
- **Strip/Restore** — large opaque literals (e.g. protobufs embedded in code) are
  deterministically extracted into placeholders *before* the LLM and restored
  byte-for-byte *after*. The model never sees them → can't corrupt them, and the
  context stays small.

## Why not Aider

Aider always puts the whole file in context and regenerates it wholesale — on
token-dense files a weak local model chokes and corrupts data. Here large data
never passes through the model at all.

## Tool registry

All execution is unified under tool-calling. A tool = `{schema, handler}`; adding
a new one = register it, no loop rewrite. Built-in tools:

| Tool | Purpose |
|---|---|
| `list_dir` | project file structure |
| `read_file` | read a file |
| `write_file` | create / overwrite a file |
| `edit_file` | refactor an existing `.py` (with strip/restore) |
| `create_from_source` | create a NEW file as a copy of a source (with strip/restore) |
| `run_shell` | run a program (python / pytest / git) |
| `read_attachment` | read a user-attached file (fuzzy name match) |
| `web_search` | web search (DuckDuckGo via `ddgs`, no key) |

All file-tool paths are confined to the project root (traversal protection against
`..` or absolute paths).

## Permissions

- **edits** (`ask` / `auto`) — `ask` shows the plan and waits for approval;
  `auto` executes immediately.
- **shell** (`smart` / `ask` / `auto` / `allowlist` / `off`):
  - `smart` (default) — safe commands run automatically; potentially destructive
    ones (`rm -rf`, formatting, system dirs, etc. — see `shell_guard.py`) pause for
    confirmation;
  - `ask` — prompts (popup) before each command;
  - `auto` — runs everything without asking;
  - `allowlist` — only safe whitelisted prefixes;
  - `off` — disables shell.

## Layout

```
agent/
  config.py      EXECUTOR / PLANNER profiles
  llm.py         Ollama client (tool-calling, think toggle)
  literals.py    strip / restore of large literals
  tools.py       deterministic file/shell primitives + allow-list
  executor.py    run_edit / create_from_source (strip→LLM→restore→diff)
  toolkit.py     tool registry + ToolContext (root, permissions, confirm)
  agent_loop.py  per-step tool-loop (run_step)
  planner.py     task decomposition into steps
  intent.py      intent classification (edit / plan / answer / shell)
  answerer.py    code Q&A / analysis (think=on)
  memory.py      task state on disk
  session.py     sessions/chats (project, permissions, history, sources)
  project.py     AGENT.md (project knowledge) + structure scan
  convo.py       conversation context-memory (compact summary across turns)
  attachments.py attachments on disk + read_attachment
  shell_guard.py classifier of dangerous shell commands (smart mode)
  websearch.py   web search (DuckDuckGo, pluggable provider)
  settings.py    settings (theme, font sizes) on disk
lca_web/
  lca_web.py     web UI (Reflex, Claude-style); Chat | Code modes
main.py          entry point (convenient from PyCharm)
rxconfig.py      Reflex config
```

## Reliability under a weak model

A few techniques keep the weak local model inside the working loop:

- **DRY sampling** in the `EXECUTOR` profile — penalizes repeating whole
  phrases/sequences (e.g. the same failing command over and over) without
  damaging code the way a blunt `repeat_penalty` would.
- **Tool-call nudge** — if the model narrates an intent instead of calling a tool,
  the loop nudges it once to perform the step as an action.
- **Plan context per step** — the step executor sees the overall task and sibling
  steps, not just its bare step description.
- **Strip/Restore** of large literals (see above) + `think=off` for structured
  output (JSON plans don't get truncated by thinking).

## Project knowledge (AGENT.md)

The working project root may contain an `AGENT.md` — curated knowledge injected
into the planner/executor on every task (not RAG/embeddings — for a small project
and a weak model a single curated file works better). An optional init-scan
auto-drafts `AGENT.md` for a new repo.

## Requirements

- Python 3.12, `requests`, `reflex==0.9.5.post2`, `ddgs` (web search)
- Ollama with the `gemma4:26b-a4b-it-qat` model

## Running

```powershell
# web UI (frontend :3000, backend :8000)
.\run_web.ps1
# or
python main.py
```

From PyCharm: open `main.py` → ▶ Run (interpreter = the project `.venv`).

## Tests

All tests are offline (stub clients, no GPU):

```powershell
.venv\Scripts\python.exe -m tests.test_toolkit
.venv\Scripts\python.exe -m tests.test_agent_loop
# ... and others under tests/
```
