# Business Foundry

An open operating foundation for **starting, acquiring and running a business**.

Turn a concept into a connected business workspace: customers, offers, assets and rights, roles, decisions, evidence, work, financial scenarios, acquisition diligence, working documents and a first sale through payment. Starting and buying are two entry paths over the same records.

**v0.1 is a working local-first alpha.** It prepares and tracks real work with human review. Government filings, bank onboarding, signatures and payments happen with the relevant providers; Foundry records their receipts. It does not certify legal compliance or replace legal, tax, accounting or investment judgment.

![The synthetic Polymer demo workspace: a source list with Start here, the journey lines and the records, Today's list with a strip of the journey map and the one next action, and a step's inspector saying where it stands](docs/images/workspace.png)

## Run it

**New to the terminal? Follow [your first hour with Foundry](docs/FIRST_HOUR.md).**

You need [uv](https://docs.astral.sh/uv/getting-started/installation/). It also provides Python 3.11 or newer if your computer does not have it.

```sh
git clone https://github.com/calgulbenkian/business-foundry.git
cd business-foundry
uv run foundry start
```

On the first run, uv installs Foundry's dependencies into `.venv` in that folder (downloading them if needed), then Foundry creates a private workspace and owner key in `.foundry/`. When the server is ready, the terminal shows the address to open, **http://127.0.0.1:8765**, your private sign-in key and how to stop (Ctrl+C), and your default browser opens at that address (on Linux, only when a desktop session is running). Paste the key into the sign-in screen. The application works without an AI subscription or API key.

The first run also adds the synthetic example business described below; use `uv run foundry start --empty` for an empty workspace instead. Later runs reuse the same workspace and key and never add the example again. `start` listens on 127.0.0.1 only. `--port` chooses another port and `--no-browser` skips the browser. If the output is not a terminal (for example, piped or captured by a service), the key is not printed. The output names the command that shows it instead, for example `uv run foundry token`.

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

### Desktop window (preview)

Use `foundry start` to work in your web browser, or when an AI client must reach Foundry at a fixed address ([AI setup](docs/AI.md)). Use the desktop window if you would rather have Foundry in a window of its own.

```sh
uv sync --locked --extra desktop
uv run foundry-desktop
```

This opens the same interface in a native window and signs you in automatically: there is no key to paste. Records are kept in your per-user application data folder (`~/Library/Application Support/Business Foundry` on macOS, `%LOCALAPPDATA%\Business Foundry` on Windows, `~/.local/share/Business Foundry` on Linux unless `XDG_DATA_HOME` names another folder), not in the folder you launched from. So the window and `foundry start` keep different databases: a business made in one is not in the other. On its first run the window creates an empty workspace, without the example business. The command-line tools can use that database with `--db`. To back it up on macOS, run `uv run foundry --db "$HOME/Library/Application Support/Business Foundry/foundry.db" backup /private/path/backup.db` from the folder you start the app from, with `/private/path/backup.db` changed to a private place for the backup. The window shows the backup command for the database it has open, including one chosen with `--data-dir`, on its **Access & assistants** page, in **Your business records**, under **Backup and restore**; on Windows it shows the path but no ready command. Windows needs the Microsoft Edge WebView2 Runtime. Only one copy of the app can be open at a time for each data folder. If you choose **Sign out** inside the window, the sign-in screen appears; quit and reopen the app to sign in again. There is no installer, code signing or automatic update yet, and the app has been run on macOS only, not yet on Windows. See [security](SECURITY.md#desktop-app) for how the window signs in.

## What works

- A three-pane browser workspace: each group of steps is a coloured line on a Journey map generated from the step data, every state of a live step has its own glyph, and the panes stack on a phone.
- **Start here**, where a business opens until its first path is done: five or six stops for buying or starting a business, each ticked off by its own records and steps, a **Do this next** button that opens the form or step for the first stop not done, and every line explained in a fixed catalog sentence. Each step says why it is waiting, what unlocks it and the next action for the person signed in, worked out from the same blockers that gate approval, with no AI. `GET /api/businesses/{id}/guide` returns the same guide, and the agent adapter's `get_guide` tool reads it.
- Separate business workspaces, scoped access keys, human review roles and an agent role that cannot approve evidence or attest external execution.
- Dependency-aware startup and acquisition work, with distinct preparation, approval, submission and verification states.
- A human-only, recorded change of planning jurisdiction, with a preview: earlier guide steps stay as superseded history, the new guide's steps are added and completed work is reopened in the same workspace. Earlier documents, deal analyses and forecasts are marked stale; saved figures are never rewritten.
- Immutable evidence bodies, human review notes, versioned records, optimistic concurrency and idempotent write requests.
- Linked monthly income statement, cash flow and balance sheet; collection delays, debt, taxes, capex, three scenarios and DCF.
- Acquisition screening with evidence-supported earnings adjustments, replacement management, valuation ranges, sources and uses, debt service and downside coverage. A building sold with the business is priced and financed separately and is not valued by the multiples; rent the business will pay is deducted from its earnings. A seller note can amortize, pay interest only, or defer every payment to a balloon with interest compounded yearly, and the coverage due in year 1 is shown beside the coverage a lender may test with the note amortizing. Investor equity reduces the buyer's cash at closing, which can be none, or less than the funding brought in. It needs the target's revenue, reported EBITDA and purchase price, and names every other input it filled with an example value. It assumes no debt or investor that was not given, and applies no lender rule.
- Draft business plans, structure decision briefs, contract briefs, operating manuals, readiness reports and investment memos tied to their source versions, written in plain words with machine identifiers kept to a closing reference index. Text typed by a person or an agent is shown literally: it cannot add headings, lists, images or HTML to a document, or a link that hides its address behind other words. A web or email address typed in full stays visible as typed, and some viewers make it clickable. The readiness report audits what needs a person now (saying when a step cannot be approved yet and what it needs first), what is ready, what is blocked, what is still an assumption and which obligations are past their due date, as of a stated date. When a due date passes, an earlier report is marked stale.
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

The command line keeps private state in `.foundry/` (relative to where you run it), which is ignored by Git. The desktop app uses the per-user application data folder instead. No telemetry, remote databases, third-party fonts or analytics are required. Software is MIT licensed; your business data stays yours.

## Scope of this release

This is designed for a founder or small team on a trusted machine. It is not yet a multi-region enterprise platform, full double-entry bookkeeping system, contract-signing service, licensed valuation database or universal compliance engine. Jurisdiction guides are planning prompts requiring a current local review. Changing the planning jurisdiction changes which guide applies and reopens work; it is not legal advice and does not form, move or register an entity. A global “business complete” claim would be misleading; individual steps have explicit evidence and review states.

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

The test starts an isolated local server with a temporary synthetic database, exercises real browser forms, the Journey map and the state glyphs, checks that a new business opens on Start here and one whose path is done on Today, that a decision ruled out as not applicable finishes its stop, which shows **Done · Not applicable** and says so, that **Do this next** opens the right form, that a line's **?** works by keyboard and by tap and is announced as a button that says whether it is open, that Today names the same next action with an acquisition's own line first, that an older analysis whose evidence changed offers to rule its steps out as not applicable in Today's **Do this next**, its line's heading and **?**, the lines explained and the step, each opening the decision form at that move with its evidence field, that the note on ready steps is true or absent, that starting is a step's main move and that the assistant advice is true for a served and a desktop owner, withdraws evidence to check that dependent work reopens, changes the planning jurisdiction twice to check that earlier analyses show the server's reasons and that replaced steps are off the map, out of every count and open read-only, checks desktop, medium and phone layouts and an empty first run, checks keyboard focus and that each line's colour is what the browser actually paints in both themes, holds responses back to check that a late reply never reaches the wrong business, a newer view or a session that has ended (a new access key arriving after sign-out is discarded), that a form opened for one business closes rather than saving into another, and that a result arriving on another business's page names its own business, drops the reload after an accepted change to check that the page says the change was saved and offers to load the records again, drops the answer to key creation and other commands to check that the page promises a safe repeat only where the server provides one, signs in and out in a second tab of the same browser, and ends a session there, to check that the first forgets the previous person and that a new access key reaches no one else, with and without the signal between tabs, holds the answers to who is signed in in that second tab to check that only the latest one acts when a person signs in there before an earlier answer arrives, and that the owner's + New business is not shown and opens no form while that tab does not know an owner is signed in, signs in as a reviewer and as an operator to check that neither is shown + New business, + Create access key, the key list or the owner's section, that none of the owner's forms opens for them however it is reached, and that the record form and step decisions of their role are there, reloads the records under an open document view to check that its warning follows them without moving the reading position or focus, checks the steps each piece of evidence is used by against the server's own rule, opens a step whose history holds entries that are not objects, serves histories that tell each sentence of that rule apart, keeps a new key from a second owner with the same name, checks that a reload that changes nothing leaves an open view's selection and reasons in place and that a viewer's open record view follows a change, serves documents with and without the server's reasons, and saves a record after a document was generated, to check the stale-document reasons from the server: one rule decides whether a document is out of date, every place that names it says so in the server's words, or in plain words when it lists none, and the page states no reason of its own, serves deal analyses and forecasts with reasons the page cannot read among real ones, to check that it shows only the real ones, in the server's order and as text, and one plain sentence when none can be read, adds the desktop window's `desktop` field to `/api/me` to check that the backup panel and the integrity view give that database's backup command, or the path form when none is ready, as text, and the plain command without the field, walks every source at three widths to check that each list holds only list items, and tears it down. Screenshots go to the ignored `test-results/` directory. `FOUNDRY_UPDATE_DOCS_IMAGE=1 npm run test:browser` also regenerates `docs/images/workspace.png` from the synthetic demo workspace.

`npm run check` also runs `tests/palette.cjs`, which reads the stylesheet's custom properties and fails if any two line colours come within 6 L* of each other, if a line's two-letter code falls below 4.5:1 on its disc, if lines collide under simulated protanopia, deuteranopia or tritanopia, if a faint line lacks a casing, or if a main text role falls below 4.5:1 in either theme. It also fails if any declaration of a line's colour property, in any rule that names that line's selector, uses anything but the line's own token. After changing a colour, `npm run palette -- --write` regenerates the measured figures in the stylesheet's header comment; the check fails while they are stale.
