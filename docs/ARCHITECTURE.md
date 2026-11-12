# Architecture and invariants

Business Foundry starts with the durable parts of a business: people and entities, customers, offers, resources and rights, responsibilities, obligations, decisions, evidence, work and money. Starting and acquiring both produce these records. Acquisition adds a target, an investment thesis, financial reconciliation, a transfer and an integration plan.

## Runtime

```mermaid
flowchart LR
  UI[Browser workspace] --> API[Authenticated FastAPI service]
  CLI[Local command line] --> Domain[Business rules]
  AIClient[AI client] --> MCP[Scoped MCP adapter]
  MCP --> API
  API --> Domain
  Domain --> DB[(Private SQLite database)]
  Domain --> Models[Decimal financial models]
  Domain --> Docs[Versioned working documents]
  API --> Advice[Optional bounded AI review]
  Advice --> Provider[Configured OpenAI API account]
```

One Python process, SQLite with WAL, and static HTML/CSS/JavaScript are sufficient. No build service, vector database, message broker, cloud account or AI key is required. The wheel includes the interface. Monetary outputs use decimal strings. Provider model output never supplies financial arithmetic.

## Records and scope

A business workspace is a container, not proof that a legal entity exists. Entity, person, ownership and role records can describe an organization and connect through `related_ids`; they do not create entities, issue equity, enforce cap-table totals or grant application permissions. Application authority comes from access keys and their roles.

All objects have a business ID, type, title, state, version and timestamps. Related records and evidence must belong to the same business. Cross-business references are rejected even for an owner. Multiple legal entities can be described within a workspace; consolidated accounting and cross-workspace transaction accounting are not implemented.

| Layer | Contents |
|---|---|
| Facts and assumptions | Customers, offers, assets, contracts, obligations, people, entities, ownership, roles, decisions, risks |
| Evidence | Immutable text, source reference, effective date, SHA-256, human assessment |
| Work | Versioned planning guide, prerequisites, required record types, decisions and receipts |
| Models | Explicit inputs, deterministic results, calculation version, evidence links |
| Documents | Draft content, hash, source fingerprint and source IDs |
| Sales | Invoice draft, external issue attestation, payment receipts and remaining balance |
| History | Actor-attributed events, object revisions, local hash chain |

## State transitions

```mermaid
stateDiagram-v2
  pending --> in_progress
  pending --> prepared
  in_progress --> prepared
  prepared --> approved: human decision + reviewed evidence
  approved --> verified: internal step
  approved --> submitted: external step + reviewed receipt
  submitted --> verified: human checks outcome receipt
  verified --> needs_review: inputs or evidence change
  needs_review --> prepared
  pending --> not_applicable: human decision + reviewed evidence
  not_applicable --> needs_review
  state "any state of a jurisdiction-package step" as package
  package --> superseded: human changes the planning jurisdiction
  superseded --> [*]
```

Preparation is allowed before prerequisites finish, so work can proceed in parallel. Approval, submission and verification require satisfied dependencies and required record types. An external step requires reviewed receipt evidence, not just a generated draft. An authorized reviewer assesses whether the actual content proves the actual outcome; software does not independently authenticate provider receipts. A single reviewer can both prepare and approve in v0.1; configurable separation of duties is future work.

A withdrawn source downgrades verified claims supported by it, flags affected invoices and payments, makes deal analyses stale, and reopens affected completed work and downstream steps. A completed step reopens when its current standing rests on the withdrawn source. That standing is the evidence attached to the step now, which always counts, together with the evidence cited since the step's last reopening: at its latest preparation and at every step after it, or, if it was not prepared in that round, at its last step. Within the step's own history, evidence cited only before the step was last reopened, or only at a preparation that a later preparation replaced, does not count. Such a source can still reopen the step in other ways: through a record it supports whose kind the step requires, or through another step this one depends on. The earlier citations are read from the step's history, so existing workspaces need no migration. A history entry that cannot be read is ignored and cannot stop a withdrawal. New or changed records reopen relevant completed work. Business-profile changes conservatively reopen completed steps. Reopening a step by hand reopens only the completed steps that depend on it, directly or through other steps. Documents retain their original content and expose staleness by comparing source fingerprints. Historical financial outputs are never rewritten.

The task catalog is versioned planning guidance. Its source-check date indicates reference review, not a jurisdiction-specific professional opinion. Existing tasks retain the original package data when application code changes.

The planning jurisdiction changes only through its own human decision, never through a general business edit. An owner or reviewer records a reason, and optionally reviewed evidence of the same business, against the current business version (`POST /api/businesses/{id}/jurisdiction`, or `foundry jurisdiction`). Agent, operator and viewer keys are refused, and the agent adapter has no tool for it. In one transaction the change moves the current package's steps, in whatever state they are, to the terminal `superseded` state with a history entry naming both jurisdictions and the actor; adds the new package's steps as new pending steps whose prerequisites resolve by catalog key against the steps that remain (a new local review depends on the existing structure decision); and reopens every remaining completed step, deal steps included, with the note "Jurisdiction changed from X to Y". Base and deal steps keep their identity and are never superseded. Package steps are recognised by catalog key, so existing workspaces need no migration. Any of the known jurisdictions can follow any other, including a return to `undecided`; each change adds fresh steps rather than reviving superseded ones. Nothing is deleted or rewritten: superseded steps keep their full history and stay in the snapshot and exports. They never transition again, never block another step, are never reopened, and are excluded from readiness counts (`stats.tasks` and the state counts; `stats.superseded` counts them separately) and from the work lists of generated documents. The readiness report, the business plan and the operating manual name them in one line, as replaced steps, and list them nowhere else; the reference index of each gives the id of each one it names. The other documents never name them: the structure decision brief and the customer contract brief count no steps, and the investment memo counts only its acquisition's steps, which are never superseded. Earlier documents become stale. Earlier deal analyses and forecasts are marked as well. In the snapshot every deal and forecast carries `stale` and `stale_reasons`, a list in which each reason has a `code` and a `message`, one plain sentence: `evidence_changed` when a deal's cited evidence has changed since it was saved, and `jurisdiction_changed`, which also gives `saved_jurisdiction` and `current_jurisdiction`, when the planning jurisdiction in force now differs from the one in force when the record was saved. A deal can carry both. Generated documents print each reason's sentence as a warning directly above the figures it concerns, and the browser interface shows the same sentences. The jurisdiction a record was saved under is worked out from the event log each time the snapshot is read: it is the `to` of the latest `business.jurisdiction_changed` event before the record's `deal.created` or `financial_plan.created` event, or the `from` of the first change if the record came before any change. Order is the event sequence, never clock time. A deal or forecast is calculated once, when it is saved, and nothing changes it afterwards. Nothing is stored or rewritten, so records saved before this mark existed are covered without a migration, and a business with no jurisdiction change in its history has no record marked for this reason. A newly saved analysis or forecast has no mark; an earlier one stays marked until the planning jurisdiction returns to the one it was saved under. The jurisdiction mark blocks no step: the change has already reopened every completed step, so each decision is taken again with the mark in view, and the calculations take no jurisdiction input, so requiring a founder to re-enter an unchanged analysis first would cost effort for no gain. Changed evidence still blocks a deal's steps. A save that is committed after the change counts as saved under the new jurisdiction, even if whoever sent it had not yet seen the change. The event log records the decision (`business.jurisdiction_changed`, with from, to, reason, evidence and the affected steps) alongside each superseded, added and reopened step. `GET /api/businesses/{id}/jurisdiction/preview?to=...`, open to any key that can read the business, returns what the change would supersede, add and reopen; it uses the same planning function as the change. A jurisdiction change selects which planning guide applies. It is not legal advice and does not form, move or register an entity.

## Transaction and retry rules

Writes use `BEGIN IMMEDIATE` and commit the business change, revision, event and idempotency result together. The same actor/key/request returns the original result. Reusing a key with a changed request yields HTTP 409. Versioned updates reject stale versions. Duplicate payment references and overpayments fail within the same transaction.

Access-key creation is intentionally different: it returns a fresh secret once and does not persist the raw secret in the idempotency cache. Review the key list before repeating a creation after a lost response; revoke unused keys.

AI calls reserve a unique request and a daily-limit slot before the network request. The call is never automatically retried. Unknown transport outcomes remain `unknown`; process interruption can leave `pending`, also an unconfirmed outcome. Neither state grants authority. Successful advice must pass schema and known-record citation validation. Requests and responses remain available for inspection and export.

## Integrity and recovery

Events form a per-business SHA-256 chain. Verification also compares current objects with the last recorded snapshot, checks current revision snapshots, detects missing historical objects and checks evidence/document content hashes. This detects accidental or simple out-of-band modification. It does not provide nonrepudiation against a database administrator who can rewrite all local records and hashes. Protect off-machine backups or independently anchor exported head hashes for stronger guarantees.

JSON export is a portable inspection artifact containing records, events, documents and AI runs. It is not an authority-bearing import format. Restore uses a consistent SQLite backup, preserving IDs, history, keys and command receipts. Restoration refuses to overwrite an existing destination. Protect the owner key separately; its plaintext is never in the database.

## Repository map

- `src/foundry/models.py`: input contracts and numerical bounds.
- `src/foundry/store.py`: persistence, transactions, credentials, revisions and events.
- `src/foundry/service.py`: scopes, human decisions, dependencies and business operations.
- `src/foundry/finance.py`: linked statements, DCF and acquisition analysis.
- `src/foundry/documents.py`: grounded working documents.
- `src/foundry/catalog.py`: versioned work and reference sources.
- `src/foundry/api.py`, `cli.py`, `mcp_server.py`: three interfaces over the same rules.
- `src/foundry/ai.py`: optional provider review and request accounting.
- `src/foundry/static/`: responsive, dependency-free browser interface.
- `tests/`: independent financial expectations, authority and workflow failures, persistence and provider tests.
- `tests/browser.cjs`: actual browser creation, review, modeling, document and payment flows.
