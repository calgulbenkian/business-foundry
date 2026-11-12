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
```

Preparation is allowed before prerequisites finish, so work can proceed in parallel. Approval, submission and verification require satisfied dependencies and required record types. An external step requires reviewed receipt evidence, not just a generated draft. An authorized reviewer assesses whether the actual content proves the actual outcome; software does not independently authenticate provider receipts. A single reviewer can both prepare and approve in v0.1; configurable separation of duties is future work.

A withdrawn source downgrades verified claims supported by it, flags affected invoices and payments, makes deal analyses stale, and reopens affected completed work and downstream steps. A completed step reopens when its current standing rests on the withdrawn source. That standing is the evidence attached to the step now, which always counts, together with the evidence cited since the step's last reopening: at its latest preparation and at every step after it, or, if it was not prepared in that round, at its last step. Within the step's own history, evidence cited only before the step was last reopened, or only at a preparation that a later preparation replaced, does not count. Such a source can still reopen the step in other ways: through a record it supports whose kind the step requires, or through another step this one depends on. The earlier citations are read from the step's history, so existing workspaces need no migration. A history entry that cannot be read is ignored and cannot stop a withdrawal. New or changed records reopen relevant completed work. Business-profile changes conservatively reopen completed steps. Reopening a step by hand reopens only the completed steps that depend on it, directly or through other steps. Documents retain their original content and expose staleness by comparing source fingerprints. Historical financial outputs are never rewritten.

The task catalog is versioned planning guidance. Its source-check date indicates reference review, not a jurisdiction-specific professional opinion. Existing tasks retain the original package data when application code changes. Jurisdiction changes require a new workspace in this release; silently replacing the legal context would destroy the basis for earlier decisions.

## Transaction and retry rules

Writes use `BEGIN IMMEDIATE` and commit the business change, revision, event and idempotency result together. The same actor/key/request returns the original result. Reusing a key with a changed request yields HTTP 409. Versioned updates reject stale versions. Duplicate payment references and overpayments fail within the same transaction.

Access-key creation is intentionally different: it returns a fresh secret once and does not persist the raw secret in the idempotency cache. Review the key list before repeating a creation after a lost response; revoke unused keys.

AI calls reserve a unique request and a daily-limit slot before the network request. The call is never automatically retried. Unknown transport outcomes remain `unknown`; process interruption can leave `pending`, also an unconfirmed outcome. Neither state grants authority. Successful advice must pass schema and known-record citation validation. Requests and responses remain available for inspection and export.

## Integrity and recovery

Events form a per-business SHA-256 chain. Every object version is written as one revision row and one chained event carrying the same snapshot; the business profile is versioned through events only and has no revision rows. An event records a version when its payload's `id` is the event's object id, the payload has `data`, and its `version` is a JSON integer. Walking the chain in order, the first event that records an object or the business profile at a given version stands for that version, and no later event replaces it. `foundry verify`, the audit endpoint and exports check, for one business: the event chain hashes and links; that each current object and the business profile equal the newest chained snapshot for them, which is the last event whose payload has their `id`, a `version` of any type and `data`; that the revision at each object's current version equals the object; that every stored revision of every version equals the snapshot in the event that stands for the same object and version; that no chained object version lacks a revision and no revision exists for a version, or for the business profile, that no chained event records; that no version of an object or of the business profile is recorded by more than one event, reported once per version as `Version recorded more than once in history`; that no event that would record a version has a `version` that is not a JSON integer, such as `2.0`, `2e0`, `true`, `"2"`, null, a list or an object, reported once per id as `History records an unusable version`, and such an event stands for no version; that no chained object is missing from the current records; evidence/document content hashes; and that AI review records match their chained digests. These messages appear in a fixed order: for each current record in turn, the business profile first, its current-record, content-hash and current-revision messages; then chained objects missing from the current records; then repeated and unusable versions, in the chain order of the event that first causes each; then revision rows, by object id and stored version, followed by recorded versions without a revision, in chain order; then AI reviews. Stored values are compared after parsing, strictly by JSON type and value, so `false` and `0`, `true` and `1`, `1` and `1.0`, or `0.0` and `-0.0` are different. Two spellings of the same value are not told apart: whitespace, key order, string escapes, `1.0` and `1.00`, `-0` and `0`, a key given twice, which is read as its last value, or the same JSON stored as a SQLite BLOB of its text bytes instead of as text. A revision snapshot is read as bytes, whatever its SQLite storage class, and decoded strictly in the database encoding (UTF-8 unless the file was created otherwise); a snapshot that cannot be decoded that way, starts with a byte-order mark, cannot be parsed, is a number or NULL, or holds `NaN` or `Infinity`, is reported as differing. An object revision whose version is stored as TEXT, BLOB or NULL matches no chained event and is reported as having no recorded history, with the stored value shown in SQLite notation, such as `CAST(X'FF7B' AS TEXT)`, and cut short when long; a recorded version that such a row replaced is reported as missing. SQLite stores a `NaN` bound as a number as NULL. A REAL version equal to a whole number counts as that integer. The `actor_id` and `created_at` columns of a revision row are not compared with anything. Revision rows are attributed to a business through its current objects and the object ids named by its events, so a row for an object id unknown to that business is not checked. Stored values stop verification with an error instead of a report in the following cases, and in no others. Below, *unencodable* JSON is JSON that Python parses but the chain's encoding refuses: it holds `NaN`, `Infinity`, `-Infinity` or a number too large for a float, such as `1E400`, or an unpaired surrogate, which is an escape such as `\ud800` in a value or a key, or its raw bytes in JSON stored as a BLOB, or it is nested just deep enough to parse but not to encode. Whatever the state of the chain: any column of an event, the business profile, a current record or an AI review row of this business holds text that is not valid in the database encoding; an event payload, or the data of the business profile or of a current record, is NULL or cannot be parsed, which includes an integer of more than 4,300 digits and nesting of about 10,000 levels; an event payload is JSON but not an object; an event payload has a non-empty list or object as its `run_id` and a `data_sha256` that is present and not `false`, `null`, zero, `""`, `[]` or `{}`; an event has a NULL `object_id` and a payload with `version` and `data` but no `id`; or the data of a current evidence or document record is not an object holding a `content_sha256` and a text `content` without an unpaired surrogate. For an AI review row whose state equals the state in the newest chained event for it, or that has no chained event and a NULL state: its data is NULL, cannot be parsed or is unencodable. For an event the chain check hashes, which is one whose own `prev_hash` and every earlier event's `prev_hash` and `hash` are right, whatever its own `hash`: its payload is unencodable, or its `actor_id`, `action`, `object_id` or `created_at` is a BLOB or an infinite REAL. Otherwise these values are reported: unencodable JSON, a BLOB or an infinite REAL in an event the chain check does not hash, which shows the chain broken at or before that event; unencodable JSON in the data of the business profile or a current record, other than in evidence or document `content`; a BLOB, NULL or infinite REAL in their other columns; any other value in an AI review row; and any value in a revision row. Data that is JSON but not an object is compared like any other value, except in evidence and document records. An export also stops when something it carries cannot be written as JSON: a BLOB or an infinite REAL in any column it includes, unencodable JSON anywhere, or AI review data that cannot be parsed, whatever its state. The audit endpoint, which also returns every event, also stops when any event holds `NaN`, an infinite number or an unpaired surrogate, has a BLOB column that is not valid UTF-8, or has a payload nested about 1,000 levels deep. Changing an event, or removing one that has later events, breaks the chain unless every hash from that point onward is rewritten, the administrator case below; removing only the newest events leaves a shorter chain that still links, which the record checks above may then report as differences. The chain does not show who added an event or that the application wrote it. Anyone who can write to the database can append a correctly chained event without changing any earlier hash, so a head hash anchored before the append is still in the chain afterwards. An appended event that repeats a recorded version, or has an unusable version, is reported as above. An appended event that records a new version, together with a matching current record and revision row, verifies like an application write. An AI review row can be changed and still verify if an event carrying its new state and digest is appended, because each AI review is compared with the newest chained event for it. This detects accidental or simple out-of-band modification. It does not provide nonrepudiation against a database administrator who can rewrite all local records and hashes. Protect off-machine backups or independently anchor exported head hashes for stronger guarantees; an anchored head hash still in the chain shows that the events up to it are unchanged, not that later events came from the application.

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
