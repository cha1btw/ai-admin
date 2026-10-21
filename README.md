# AI-admin

AI administrator for Instagram and Telegram DMs of beauty studios. It answers clients from the
studio's own data, offers real free slots, books them, and hands anything sensitive (health,
complaints, discounts, refunds) to the master in Telegram.

- Product rules R1–R13 and architecture: [CLAUDE.md](CLAUDE.md)
- Behaviour, scenarios, studio questionnaire, pilot gate: [docs/PRODUCT.md](docs/PRODUCT.md)
- Studio onboarding: [docs/onboarding.md](docs/onboarding.md) · operations: [docs/runbook.md](docs/runbook.md)
- What founders do outside code: [docs/founders.md](docs/founders.md)
- Platform research: [docs/research/](docs/research/) · hosting decision: [docs/adr/0001-hosting.md](docs/adr/0001-hosting.md)

## How it works

```
Instagram / Telegram ──webhook──► API: check signature, store event, 200
                                   │
                         inbound_event (Postgres queue)
                                   │
Worker ── parse ──► client message ──(debounce)──► reply pipeline
                                                    │  pause / human / handoff state
                                                    │  limits, media rules
                                                    │  model + tools (slots, booking, handoff)
                                                    │  guardrails: amounts, times, phones, links
                                                    │  disclosure added by code
                                                    ▼
                                autopilot: send   ·   draft: Owner Bot with buttons
```

Things the model never decides on its own: the AI disclosure, prices and booking
confirmations (code templates from the database), which slots exist (opaque tokens), and
whether two bookings may overlap (Postgres exclusion constraint).

## Local development

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker.

```bash
cp .env.example .env     # fill OPENAI_API_KEY and LLM_MODEL to talk to the bot
make install
make migrate             # starts Postgres on localhost:5435 and applies migrations
make demo                # loads the demo studio
make chat                # talk to the demo studio in the terminal
make test                # unit + integration tests, no API keys needed
make lint
```

Telegram bots locally (no public URL needed): set `OWNER_BOT_TOKEN` and/or `DEMO_BOT_TOKEN`,
then run in two terminals:

```bash
make poll                # fetches updates and stores them like webhooks do (dev only)
make worker              # processes them, replies, runs periodic jobs
```

Become an admin of your local Owner Bot: put your Telegram id into `ADMIN_TELEGRAM_IDS`, then
`/link demo_studio` in the bot or `uv run python -m app.cli tenant link demo_studio`.

## Evals (real model)

```bash
make eval                         # all cases in tests/evals/cases, hard ones 3 times
make eval ARGS="--case landing_allergy --runs 1"
```

Uses the test database and costs model tokens. Reports go to `tests/evals/reports/`. Compare
the report before changing the prompt or `LLM_MODEL`.

## Command line

| Command | What it does |
|---|---|
| `make tenant-validate FILE=...` | validate a studio config |
| `make tenant-load FILE=...` | create or update a studio (new config version) |
| `make tenant-preview TENANT=...` | what the model knows about the studio |
| `uv run python -m app.cli tenant link <slug> [--role admin]` | one-time Owner Bot link for a master |
| `uv run python -m app.cli tenant stats <slug> --days 14` | pilot report |
| `uv run python -m app.cli instagram link <slug>` | Instagram Business Login link for a studio |
| `make webhooks` | point Telegram bots at `PUBLIC_BASE_URL` (production) |
| `make backup` | database backup (production) |

## Owner Bot (for masters)

`/data` · `/sandbox` · `/mode` · `/pause` · `/resume` · `/stats [days]` · `/today` ·
`/close 25.10-27.10 reason` · `/open 25.10` · `/switch` · `/cancel`. Buttons on notifications:
send/edit/reject a draft, reply to the client, take over, return the bot, cancel a booking,
confirm a prepayment or a request, 👎 on a bot answer. Founders (`ADMIN_TELEGRAM_IDS`):
`/tenants`, `/link`, `/health`, `/pause_all`, `/resume_all`, `/forget`.

## Deployment

One VPS in the EU with `deploy/docker-compose.prod.yml` (Postgres, migrations, API, worker,
Caddy with automatic HTTPS). Step by step: [docs/runbook.md](docs/runbook.md). The landing page
is static (`landing/`, deploy to Vercel with the project root set to `landing`); replace
`YOUR_DEMO_BOT` and `YOUR_CONTACT` first.

## Pilot checklist

Before the first real client of a studio, every item of the pilot gate in
[docs/PRODUCT.md](docs/PRODUCT.md) (section 6) must be done: validated config confirmed by the
master, green studio evals, sandbox tried, channel connected with other auto-replies off,
calendar connected and tested, draft mode with owner hours, alerts and budget on, a fresh
backup with a tested restore, retention and deletion checked, pilot terms signed, privacy
notice published, "before" metrics written down.

## Status

Built and covered by tests (unit and integration on Postgres, fake model and fake Google/Meta
APIs). Not yet verified live: Telegram Business and Instagram with real accounts
([docs/PRODUCT.md](docs/PRODUCT.md), section 8), Google Calendar with a real calendar, and the
eval suite with a real model.
