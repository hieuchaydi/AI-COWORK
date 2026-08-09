# connect-AI

**A desktop AI workspace that runs entirely on your own machine.** Your keys, your files, your
tokens — nothing is proxied through a vendor cloud, and there is no account to sign up for.

connect-AI gives an AI agent real hands: it reads your mail, drives a real browser, edits files
in folders you grant it, and runs on a schedule while you're away. Every action that changes
something asks you first.

```
┌───────────────┐   ┌─────────────────────┐   ┌──────────────────────┐
│  Workspace    │──▶│  agent runtime      │──▶│ connectors · browser │
│  GUI  :1420   │   │  FastAPI     :8765  │   │ files · shell · MCP  │
└───────────────┘   └─────────────────────┘   └──────────────────────┘
        │                     │
        └─────────────────────┴──▶ local helper :8766  (Google sign-in, wizards, outputs)
```

---

## What it does

**Connectors — 25+, all local.** Gmail, Google Calendar, Google Drive, Slack, GitHub, Notion,
Telegram, Outlook, Jira, Linear, HubSpot, Dropbox, Stripe, Asana, Discord, and more. Credentials
are stored on this computer; connecting is a pasted token or a local OAuth flow, never a
third-party broker.

**One Google sign-in covers everything Google.** Sign in once and Gmail, Calendar and Drive all
come up on that account. Signing out revokes the grant at Google and disconnects all three. No
per-service token pasting anywhere in the UI.

**A real browser, not a scraper.** 13 Playwright tools drive a persistent Chromium profile — log
into a site once and the session survives restarts. Plus crawl/scrape tools for bulk reading.

**Automations.** Cron-scheduled agent runs with their own approval rules, so a task can run
unattended and still park anything consequential in your Inbox.

**Model-agnostic, and quota-proof.** Gemini, Claude, GPT, Groq, Bedrock, Vertex, or a local
Ollama. When a model hits its rate/quota limit mid-turn, the run fails over to the next
configured model instead of dying; if every model is walled, it parks and picks the turn back up
when capacity returns.

**Approvals you actually control.** Reads run freely, writes ask. Per-tool toggles, per-session
scoping, standing rules, and an audit trail of everything the agent did.

**MCP.** Bundled stdio bridges (Telegram Bot + MTProto, subagents, skills, slash commands,
artifacts, computer-use) plus any third-party MCP server you register.

---

## Requirements

| | |
|---|---|
| OS | Windows 10/11 (the launcher is `.bat`; the runtime itself is cross-platform) |
| Python | 3.13 — [python.org](https://www.python.org/downloads/) or [uv](https://docs.astral.sh/uv/) |
| Node.js | 20+ (ships `npm`) — [nodejs.org](https://nodejs.org/) |
| A model key | at minimum `GEMINI_API_KEY` ([free tier](https://aistudio.google.com/apikey)) |

## Quick start

```bat
git clone <your-fork-url> connect-AI
cd connect-AI
copy .env.example .env      :: then fill in GEMINI_API_KEY
run-web.bat
```

`run-web.bat` bootstraps everything the first time — creates `.venv`, installs Python and GUI
dependencies, downloads Chromium — then launches. Later runs skip straight to launching (~10s).

Open **<http://localhost:1420>**. Stop everything with `Ctrl+C` in that window.

| URL | What it is |
|---|---|
| <http://localhost:1420> | Workspace GUI — the main interface |
| <http://127.0.0.1:8765/v1/health> | agent runtime health check |
| <http://127.0.0.1:8766/google/wizard> | Google sign-in wizard |
| <http://127.0.0.1:8766/connectors/wizard> | connector wizard (Telegram, token-based) |

`run.bat` starts the terminal UI instead, against the same environment and the same MCP bridges.

## Configuration

Everything secret lives in `.env` (git-ignored). Copy `.env.example` and fill in what you have:

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | ✅ | default model provider ([free tier](https://aistudio.google.com/apikey)) |
| `ANTHROPIC_API_KEY` | — | Claude models (best tool-use); auto-added to the picker when set |
| `OPENAI_API_KEY` | — | GPT models |
| `GROQ_API_KEY` | — | Llama on Groq — a fast, free fallback when Gemini is rate-limited |
| `COWORKER_API_TOKEN` | ✅ | shared secret between the runtime, the GUI and the local helper |

Add a key, restart the launcher, and its models appear in the composer's picker.

### Connecting Google

Click **Sign in with Google** on the *Google* card at the top of Connectors. One consent screen
connects Gmail + Calendar + Drive together.

Google requires the OAuth client to be yours (there is no cloud middleman here), so the very
first sign-in asks you to create one — a 3-minute, one-time step the wizard walks you through,
with a paste box for the downloaded JSON. Prefer not to create a client? The wizard also accepts
a `refresh_token` from the [OAuth Playground](https://developers.google.com/oauthplayground/).

Tokens live in `google-tokens.json` on this machine and refresh themselves every 50 minutes. If a
service ever drops, hit **Reconnect all** on the same card.

## Layout

```
connect-AI/
├── .venv/            Python 3.13 — the one environment (runtime + every bridge dependency)
├── connect-ai/       agent runtime + GUI
│   ├── coworker/       backend package (FastAPI server, engine, connectors, tools)
│   └── surfaces/gui/   Workspace GUI (React + Vite, port 1420)
├── bridge/           MCP stdio servers: telegram, subagents, skills, commands, artifacts,
│                     computer-use
├── skills/           skill packs (*.md) loaded by the skills bridge
├── commands/         slash-command templates
├── logs/             runtime + GUI logs
├── launch.py         the launcher: runtime + GUI + local helper
├── google_auth.py    Google OAuth refresher (Gmail/Calendar/Drive)
├── run-web.bat       main entry point (GUI)
└── run.bat           terminal UI
```

MCP bridges are registered in `%APPDATA%\coworker\mcp.json`; the launcher re-seeds them on every
boot, so a fresh machine needs no manual MCP setup.

## Documentation

| File | Contents |
|---|---|
| [SETUP.md](SETUP.md) | full setup walkthrough — keys, Google, Telegram, automations (Vietnamese) |
| [TOOLS.md](TOOLS.md) | every tool the agent can call, and what each one is allowed to do |
| [AUTOMATIONS.md](AUTOMATIONS.md) | writing and scheduling unattended agent tasks |
| [PATCHES.md](PATCHES.md) | how this repo differs from its upstream base |
| [CLAUDE.md](CLAUDE.md) | architecture notes + conventions for working in this repo |

## Development

```bash
.venv/Scripts/python.exe -m pytest        # backend tests   (run from connect-ai/)
npm test                                  # GUI unit tests  (run from connect-ai/surfaces/gui/)
npx tsc --noEmit                          # GUI typecheck
npx playwright test                       # GUI end-to-end tests
```

The Python package installs in editable mode, so backend edits take effect on the next launcher
restart; the GUI hot-reloads through Vite.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Port 1420 already in use` | a previous run survived — `netstat -ano \| findstr :1420` then `taskkill /F /PID <pid>`. Always stop with `Ctrl+C`, not by closing the window. |
| Google calls return 401 | **Reconnect all** on the Google card, or `python google_auth.py connect-all` |
| Model says "out of quota" | it now fails over automatically; add a second provider key (Groq is free) to give it somewhere to go |
| GUI shows no MCP tools | expected — MCP bridges load in the terminal UI and desktop app, not the web GUI |
| Agent replies in the wrong language | pin your preference in `%APPDATA%\coworker\AGENTS.md` |

## Credits and license

connect-AI is built by drawing on two open-source projects:

- **[OpenWorker](https://github.com/andrewyng/openworker)** by Andrew Ng — the agent runtime,
  connectors, and GUI. Vendored under `connect-ai/` and patched in place; see
  [PATCHES.md](PATCHES.md) for every difference. It remains under its original MIT license
  ([connect-ai/LICENSE](connect-ai/LICENSE)).
- **[browser-use](https://github.com/browser-use/browser-use)** — the reference for giving the
  agent real browser control; the Playwright browser tools and the anti-detection approach draw
  on its design.
