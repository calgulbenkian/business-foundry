# Contributing

Start with a reproducible business problem and the result a user needs. Prefer a small change with a clear evidence or workflow invariant over a new agent persona or a confident generated document.

1. Install with `uv sync --locked --extra dev`.
2. Use a separate synthetic database and run the relevant flow.
3. Add a meaningful regression test for changes to authority, money, evidence, persistence or external execution.
4. Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and the browser checks for interface changes.
5. Describe the before/after behavior, validation and material limits in the pull request.

Financial changes need independent expected values and a methodology update. Jurisdiction changes need current primary sources, source/effective dates and explicit applicability limits. Provider adapters need retry, unknown-outcome, scope and receipt tests before claiming successful execution. Schema changes need a migration and restore test; do not silently mutate old evidence or decision history.

Use synthetic examples only. Do not add private business records, owner keys, database files, provider credentials or screenshots containing them. Link external document standards unless their licenses and attribution requirements have been checked. Do not call AI-generated estimates audited facts or imply an agent can create legal authority.

The initial contribution scope is the shared foundation and its demonstrated workflows. Enterprise hosting, full accounting, real provider execution and maintained legal-rule packages require additional work and review; see the roadmap.
