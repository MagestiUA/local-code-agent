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

## Permissions

- **edits** (`ask` / `auto`) — `ask` shows the plan and waits for approval;
  `auto` executes immediately.
- **shell** (`allowlist` / `ask` / `off`) — `allowlist` permits only safe
  commands; `ask` prompts (popup) before each command; `off` disables shell.

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
lca_web/
  lca_web.py     web UI (Reflex, Claude-style)
main.py          entry point (convenient from PyCharm)
rxconfig.py      Reflex config
```

## Project knowledge (AGENT.md)

The working project root may contain an `AGENT.md` — curated knowledge injected
into the planner/executor on every task (not RAG/embeddings — for a small project
and a weak model a single curated file works better). An optional init-scan
auto-drafts `AGENT.md` for a new repo.

## Requirements

- Python 3.12, `requests`, `reflex==0.9.5.post2`
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
