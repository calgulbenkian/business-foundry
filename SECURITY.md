# Security and deployment

## Supported deployment

v0.1 is a local-first alpha for a founder or small team on a trusted machine. Run one application process with its SQLite database on a local disk. The default bind address is `127.0.0.1`; the server rejects unexpected Host headers. Do not put the database on a network filesystem or scale multiple containers against the same SQLite file.

For remote use, provide a reviewed HTTPS reverse proxy, explicit trusted hosts, an exact public origin, restricted network access and protected backups. Set `FOUNDRY_ALLOWED_HOSTS` to comma-separated hostnames (no ports), and `FOUNDRY_PUBLIC_ORIGIN` to the exact HTTPS origin. Configure forwarded headers only from your trusted proxy. This repository does not deploy a cloud service or provision TLS, SSO, MFA, high availability, tenant encryption keys or managed backup storage.

## Credentials and roles

The first initialization creates one owner token and writes it to `owner.key` beside the database with mode 0600. Database directories are created with mode 0700 where new, and the database file is mode 0600. Existing parent-directory permissions are not changed. Tokens have high-entropy random values and are stored only as SHA-256 hashes. This is token authentication, not password hashing.

| Role | Scope | Authority |
|---|---|---|
| Owner | All businesses | Create businesses and keys, prepare work, review, approve and reconcile |
| Reviewer | Assigned businesses | Prepare and make human review decisions |
| Operator | Assigned businesses | Create ordinary records and prepare work; record an approved external submission with reviewed evidence |
| Agent | Assigned businesses | Prepare records, evidence, models and drafts; no human approvals or execution attestations |
| Viewer | Assigned businesses | Read only |

Owners are global even if scope IDs are supplied; the UI does not offer owner-key creation. Only an owner can create/revoke access keys. An owner cannot revoke the key currently authenticating that request. Revocation removes sessions and blocks bearer authentication. Ordinary keys have no automatic expiry in v0.1; revoke them when access ends. The original owner key is powerful and should remain outside AI clients.

Browser sessions are random, hashed, HttpOnly and SameSite=Strict, with an eight-hour expiry and the Secure flag on HTTPS. Login attempts have an in-memory per-client limit. Cross-origin and browser cross-site writes are rejected. A write authenticated only by the session cookie must also show it came from the application itself: an `Origin` header equal to the expected origin (`FOUNDRY_PUBLIC_ORIGIN`, or the request's own origin when that is unset) or `Sec-Fetch-Site: same-origin`. Without either it is refused with HTTP 403. Signing in creates the session and has no cookie yet, so it works from the browser and the command line. API callers use bearer tokens, which do not need origin headers. Request bodies have a 2 MB limit. Validation failures omit submitted values. They name only locations defined by the application's request models: an unexpected property is reported as "an unexpected field was sent" at its defined parent, and client-chosen dictionary keys are not repeated. Avoid logging request bodies or authorization headers at the proxy. Every response carries a strict content security policy (`script-src 'self'`, `style-src 'self'`) and the page loads no third-party resources. FastAPI's Swagger page is therefore switched off; only the schema at `/api/openapi.json` is served.

## Private data

`.foundry/`, `.env*` (except the template), databases, logs, build outputs and test artifacts are ignored by Git. There are no telemetry calls, remote fonts or automatic document fetches. Source URLs are retained as references and are not fetched by the server. The UI escapes business content and displays documents as text; it does not execute imported HTML or load remote resources embedded in evidence.

Private evidence and backups are not encrypted by the application. Use full-disk encryption, OS access controls and encrypted backup storage appropriate to the data. Anyone with filesystem access to the owner token can act as owner; anyone who can edit the database can rewrite authorization and history. The local event chain does not protect against that administrator. Do not expose this deployment to hostile multi-tenant workloads.

The optional AI review shares summaries only after an explicit user request, but summaries can contain confidential content. The MCP adapter can return full business records, including evidence, to its authorized client. These are distinct sharing surfaces; see [AI setup](docs/AI.md). Provider keys are read only from the deployment environment and never inserted into business records.

Exports contain original evidence, internal decisions and AI review requests/responses. Database backups also contain all businesses and credential hashes. Treat both as private. Public examples and screenshots must use explicitly synthetic data; do not publish actual founder, customer, employee, ownership, financial or contracting information without the relevant authorization.

## Recovery and operational limits

`foundry backup` uses SQLite's backup API to produce a consistent snapshot while the source is running. It refuses to overwrite an existing file. `foundry restore` requires an absent destination and checks the source schema and SQLite integrity before copying. Keep the original owner key separately. Test restored backups before treating them as usable.

The JSON export is not a restore format and cannot import authority from another workspace. Invoice and payment records are an operational register. There is no refund/void/correction ledger in this release; an incorrect financial record must be retained, flagged through evidence withdrawal and reconciled in the authoritative accounting system. Add a proper reversal flow before using Foundry as a financial system of record.

## Reporting issues

Do not post secrets, private evidence or real business exports in public issues. Use the repository's GitHub private vulnerability reporting feature if enabled. For a suspected exposure, revoke affected access keys, stop untrusted access, rotate provider credentials where relevant and preserve a private backup for investigation. This policy does not imply a staffed response SLA or an external security audit.
