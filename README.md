# Business Foundry

An open operating foundation for **starting, acquiring and running a business**.

Turn a concept into a connected business workspace: customers, offers, assets and rights, roles, decisions, evidence, work, financial scenarios, acquisition diligence, working documents and a first sale through payment. Starting and buying are two entry paths over the same records.

**v0.1 is a working local-first alpha.** It prepares and tracks real work with human review. Government filings, bank onboarding, signatures and payments happen with the relevant providers; Foundry records their receipts. It does not certify legal compliance or replace legal, tax, accounting or investment judgment.

![Synthetic Polymer workspace](docs/images/workspace.png)

## Run it

**New to the terminal? Follow [your first hour with Foundry](docs/FIRST_HOUR.md).**

You need [uv](https://docs.astral.sh/uv/getting-started/installation/). It also provides Python 3.11 or newer if your computer does not have it.

```sh
git clone https://github.com/calgulbenkian/business-foundry.git
cd business-foundry
uv run foundry start
```

On the first run, uv installs Foundry's dependencies into `.venv` in that folder (downloading them if needed), then Foundry creates a private workspace and owner key in `.foundry/`. When the server is ready, the terminal shows the address to open, **http://127.0.0.1:8765**, your private sign-in key and how to stop (Ctrl+C), and your default browser opens at that address (on Linux, only when a desktop session is running). Paste the key into the sign-in screen. The application works without an AI subscription or API key.

The first run also adds the synthetic example business described below; use `uv run foundry start --empty` for an empty workspace instead. Later runs reuse the same workspace and key and never add the example again. `start` listens on 127.0.0.1 only. `--port` chooses another port and `--no-browser` skips the browser. If the output is not a terminal (for example, piped or captured by a service), the key is not printed; run `uv run foundry token` to see it.

The Polymer Geospatial example is **explicitly synthetic**. Its concept, customers, figures, receipts and completed steps are demonstration fixtures. They do not describe or certify Polymer's actual business.

The same setup is also available as separate commands:

```sh
uv sync --locked
uv run foundry init    # create the private database and owner key
uv run foundry demo    # optional: add the synthetic example
uv run foundry serve   # serve on 127.0.0.1:8765; --host and --port change the address
uv run foundry token   # print the sign-in key to paste into the sign-in screen
```

Alternative: `python -m venv .venv`, activate it, `pip install .`, then use `foundry` directly.

## What works

- Separate business workspaces, scoped access keys, human review roles and an agent role that cannot approve evidence or attest external execution.
- Dependency-aware startup and acquisition work, with distinct preparation, approval, submission and verification states.
- Immutable evidence bodies, human review notes, versioned records, optimistic concurrency and idempotent write requests.
- Linked monthly income statement, cash flow and balance sheet; collection delays, debt, taxes, capex, three scenarios and DCF.
- Acquisition screening with evidence-supported earnings adjustments, replacement management, valuation ranges, sources and uses, amortizing debt service and downside coverage.
- Draft business plans, structure decision briefs, contract briefs, operating manuals, readiness reports and investment memos tied to their source versions.
- Invoice drafts, recorded issue, partial/full payment reconciliation and duplicate-payment protection.
- A reviewable event history, local integrity checks, portable JSON exports and consistent database backups/restores.
- Optional OpenAI reviews with explicit sharing, recorded prompts/results, limits and validated citations; an MCP adapter for compatible AI clients.

See [your first hour](docs/FIRST_HOUR.md), [the walkthrough](docs/WALKTHROUGH.md), [architecture](docs/ARCHITECTURE.md), [financial methodology](docs/FINANCE.md), [AI setup](docs/AI.md), [security and deployment](SECURITY.md), and [the research and roadmap](docs/RESEARCH.md).

## Development

```sh
uv sync --locked --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The OpenAPI schema is served at `/api/openapi.json`; load it into any OpenAPI viewer you run yourself. FastAPI's built-in documentation pages (Swagger UI at `/docs` and ReDoc at `/redoc`) are switched off, because they load scripts from a third-party host and run inline scripts, which the content security policy forbids. Authenticated requests use `Authorization: Bearer <access-key>`; such requests need no `Origin` header. Business writes require an `Idempotency-Key`; reuse it only when retrying the identical request. Access-key creation returns a new key once and is intentionally not cached.

Private state lives in `.foundry/` and is ignored by Git. No telemetry, remote databases, third-party fonts or analytics are required. Software is MIT licensed; your business data stays yours.

## Scope of this release

This is designed for a founder or small team on a trusted machine. It is not yet a multi-region enterprise platform, full double-entry bookkeeping system, contract-signing service, licensed valuation database or universal compliance engine. Jurisdiction guides are planning prompts requiring a current local review. A global “business complete” claim would be misleading; individual steps have explicit evidence and review states.

The next proving ground is a real private Polymer pilot: replace synthetic assumptions with the founders' actual operating facts, review rights and responsibilities, deliver an agreed customer result, reconcile payment, then publish only reusable improvements. See [the pilot acceptance plan](docs/PILOT.md).

## Container option

```sh
docker compose up --build -d
docker compose exec foundry foundry token
# Optional synthetic example:
docker compose exec foundry foundry demo
```

The default port is published only on localhost, and private state lives in a named volume. The container runs as an unprivileged user. Read the security guide before changing network exposure. Container packaging is separate from a hosted deployment.

## Browser verification

Requires Node.js 20+ in addition to the Python development setup:

```sh
npm ci
npx playwright install chromium
npm run test:browser
```

The test starts an isolated local server with a temporary synthetic database, exercises real browser forms, checks desktop/mobile layout and tears it down. Screenshots go to the ignored `test-results/` directory.
