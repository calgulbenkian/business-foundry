# Working in Business Foundry

- Keep `.foundry/`, credentials, private business data and real customer documents out of Git. Use synthetic fixtures and screenshots.
- Business truth, assumptions, human reviews and external receipts are separate. Do not collapse prepared, approved, submitted and verified into one completion flag.
- Financial arithmetic belongs in deterministic Decimal models, with independent expected-value tests. AI outputs are advice and cannot grant authority.
- All API and MCP changes must preserve business scopes, agent restrictions, idempotency, optimistic versions and transaction integrity.
- Preserve immutable original evidence, document snapshots and event history. New facts must reopen affected decisions and make prior outputs visibly stale.
- Run `uv run pytest`, `uv run ruff check .`, and `uv run ruff format --check .`. For interface changes also run `npm ci`, `npx playwright install chromium`, and `npm run test:browser`.
- Keep the README, methodology, security boundaries and examples accurate. Report mocked provider checks separately from actual live execution.
- Do not submit filings, send messages, sign agreements, move money or publish private data as part of a software test.
