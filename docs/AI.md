# Bring an AI client or an API account

The core application is deterministic and works without AI. There are two optional paths, with different account and data-sharing boundaries.

## 1. Compatible AI client through MCP

Run the Foundry server. In **Access & AI**, create a key with the **agent** role and explicitly select its businesses. Alternatively:

```sh
uv run foundry key --name "Planning assistant" --role agent --business BUSINESS_ID
```

Save the resulting token in the client's secret configuration. Run the adapter with:

```sh
export FOUNDRY_API_URL=http://127.0.0.1:8765
# Set FOUNDRY_API_TOKEN securely in your environment; do not commit it.
uv run foundry mcp
```

A typical stdio client configuration has this shape; adapt it to the client's documented format and secret mechanism:

```json
{
  "mcpServers": {
    "business-foundry": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/business-foundry", "run", "foundry", "mcp"],
      "env": {
        "FOUNDRY_API_URL": "http://127.0.0.1:8765",
        "FOUNDRY_API_TOKEN": "YOUR_SCOPED_AGENT_KEY"
      }
    }
  }
}
```

The adapter refuses owner/reviewer/operator keys and requires HTTPS for remote URLs. Tools list businesses, read the workspace, create or update assumption and reported records, attach unverified evidence, prepare work, run financial/deal analyses and generate working documents. Human approvals, evidence verification, external-submission attestations and payment reconciliation are not agent tools; the service also rejects those actions for the agent role even if a caller bypasses MCP.

### Updating records

`update_record` replaces an existing record at its current `expected_version`. It is a full replacement, not a patch: every field (`kind`, `title`, `details`, `certainty`, `owner`, `evidence_ids`, `related_ids`, `due_date`, `properties`) is required, so read the record with `get_business` and resend unchanged values. `kind` must match the existing record. A stale version is rejected rather than merged; refresh and retry deliberately. Agents cannot set `certainty="verified"`, and the service rejects any agent or operator change to a record a human has already verified. Updating a record reopens affected completed work for human review. Prefer `update_record` over adding a duplicate record when refining an answer.

### Guided prompts

The adapter registers three MCP prompts, each taking a `business_id`:

- `founder_intake` — interview for starting from a concept: intended buyer, concrete problem, offer/pricing/acceptance, delivery assets and third-party rights, unit economics and cash timing (`build_forecast`), and source evidence (`add_evidence`).
- `acquisition_intake` — target and thesis, reported revenue/EBITDA and replacement management, evidence-backed earnings adjustments, then valuation multiples, uses and debt or seller financing (`analyze_acquisition`).
- `readiness_review` — audit of tasks and blockers, unsupported assumptions, and stale deals or documents. It starts from the latest readiness report when one is current, offers to generate one otherwise, summarizes gaps and asks before changing anything.

Prompts are static instructions. They make no API calls and never embed stored business content; workspace data reaches the client only through tool results, which the prompts tell the model to treat as untrusted. Text an agent writes into records, evidence or step notes is printed literally in generated documents, so it cannot add images, HTML or structure to a document a reviewer reads, or a link that hides its address behind other words. A web or email address it types in full stays visible as typed, and some viewers make it clickable. Every prompt restates the same boundaries: record only assumption or reported certainty, refine with `update_record` instead of duplicating, and use `prepare_task` only to prepare work. A human owner or reviewer still approves tasks, verifies evidence and records, and attests external submissions and payments.

If your client does not surface MCP prompts, follow the same sequence manually: ask the assistant to call `get_business` for the business ID, then interview you one topic at a time in the order above, saving each answer with `add_record` (new) or `update_record` (existing) and attaching sources with `add_evidence`, before running `build_forecast` or `analyze_acquisition` and reviewing the results yourself.

Treat the client as receiving any data returned by its tools, including original evidence text in a business snapshot. Grant access accordingly and review that client's data policy. The MCP path is distinct from the narrower summaries shared by the built-in OpenAI review. Account support and client billing depend on the client. A ChatGPT or other consumer subscription should not be assumed to include separately billed API access.

Use the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) and [MCP Inspector](https://modelcontextprotocol.io/docs/tools) for protocol tooling. The adapter uses the pinned v1 SDK in `uv.lock` and the stdio transport; it is not a public remote OAuth MCP service.

## 2. Built-in OpenAI review

Set `OPENAI_API_KEY` and `FOUNDRY_OPENAI_MODEL` on the server, choosing a model available to that account that supports the Responses API and Structured Outputs. No key is bundled, inferred from an existing subscription or read from another application.

```sh
# Set OPENAI_API_KEY securely, outside source control.
export FOUNDRY_OPENAI_MODEL=YOUR_SUPPORTED_MODEL
export FOUNDRY_AI_DAILY_CALLS=20
uv run foundry serve
```

The `.env.example` file documents settings. The application does not automatically load arbitrary `.env` files; use your shell, process manager or container secret configuration.

From **Access & AI**, request a review and explicitly agree to sharing. Each request sends the business profile, up to 200 record summaries (titles, states, details limited to 2,000 characters, evidence-reference IDs), workspace totals and the question. Original evidence bodies, generated documents, full financial tables, access keys and credentials are omitted. Titles, details and totals may still be confidential. Review the workspace before sharing.

The request uses the [Responses API Structured Outputs format](https://developers.openai.com/api/docs/guides/structured-outputs), `store: false`, a bounded output-token budget and no execution tools. `store: false` is a provider setting, not a claim of zero provider retention; applicable account terms and controls still govern processing. The server-wide request limit counts reservations, including failures and unknown outcomes. It is a call limit, not a currency-denominated spending cap; set provider-side budgets as appropriate. `FOUNDRY_AI_DAILY_CALLS` must be a whole number of 0 or greater and at most 100000 (default 20 when unset; 0 blocks all reviews). After surrounding whitespace is removed, it must be written in plain ASCII digits with no sign, separator, exponent or leading zero: `0`, `20` and `100000` are accepted; `020`, `00`, `+5`, `5_0`, `1e3`, an empty value and anything above 100000 are refused. Any other value, such as a negative, decimal, padded or oversized number, fails closed: review requests are refused with HTTP 503, the same status used when the provider is not configured, and no provider request is made or reserved, and no event is recorded. A review refused this way can be retried with the same idempotency key once the setting is corrected. The automated tests check this with a mocked transport only; they make no live provider call.

Responses must be completed, schema-valid and cite only IDs actually shared. Accepted output contains a summary, recommended actions/reasons/sources and unknowns. The advice never changes business records or approvals. The exact submitted request, response, token usage, source fingerprint and outcome are recorded locally. Credentials are not included in those records.

Retries with the same idempotency key return the same run. Timeouts and transport failures produce `unknown`, because the provider may have completed or billed the request. A process interruption may leave `pending`, also unconfirmed. Neither is automatically retried. Inspect provider usage before deliberately starting a new request.

## Evaluation boundary

Automated tests use an HTTP transport double for valid output, citation rejection, sharing requirements, daily limits, timeout handling and idempotent replay. They incur no provider charges. A live model call must be validated with the deployment's chosen model/account before relying on that integration. Do not infer successful live provider access from passing mocked tests.
