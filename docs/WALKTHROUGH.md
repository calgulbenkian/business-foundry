# From concept to an operating cycle

## 1. Create the workspace

Start with the README setup. Sign in, choose **New business**, and enter the concept, intended buyer, entry path, accountable person, currency and planning jurisdiction. Leave jurisdiction undecided until there is a reviewed basis for selecting it. A workspace creates work and records; it does not register a legal entity.

When the founders later decide where they will operate, a human owner or reviewer changes the planning jurisdiction as its own recorded decision; **Edit business** cannot change it. Preview the effect, then make the change with a reason and, optionally, reviewed evidence:

```sh
uv run foundry jurisdiction BUSINESS_ID GB --preview
uv run foundry jurisdiction BUSINESS_ID GB --reason "Founders confirmed UK operations with an adviser." --evidence EVIDENCE_ID
```

The earlier guide's steps stay in the workspace as superseded history, the new guide's steps are added, and completed work reopens for review. Records, evidence and reviews stay where they are, and earlier documents are marked stale. Earlier deal analyses and forecasts are marked too, with a notice naming the jurisdiction they were saved under and the one in force now. Their figures are not recalculated: check their assumptions, the tax rate among them, and save a revised analysis or forecast. The notice does not block any step. A document generated after the change counts only the current steps and names the replaced ones in a single line. The readiness report lists each reopened step under steps sent back for review, with a reason naming both jurisdictions, such as "Jurisdiction changed from undecided to GB". Each notice is printed as a warning directly above the figures it concerns. The change selects which planning guide applies. It is not legal advice and does not form, move or register an entity. The browser interface has no control for it in this release; use the command line or `POST /api/businesses/{id}/jurisdiction` (preview: `GET /api/businesses/{id}/jurisdiction/preview?to=GB`).

To start from configuration instead:

```sh
uv run foundry init
uv run foundry create examples/business.json
```

The result includes the business ID needed by command-line operations. Examples are synthetic. Replace the concept with your own, and keep private configurations outside the Git repository.

## 2. Describe the business

Use **Business records** for customers, offers, assets/rights, contracts, roles, entities, people, ownership, decisions, risks and obligations. Link related records. Mark uncertain ideas as assumptions and reported claims as reported. A human reviewer can mark a record verified only with reviewed supporting evidence.

Make the offer specific: buyer, problem, deliverable, exclusions, price, timing, acceptance and payment. Record the rights to every data source and software asset needed for delivery. Connect people, entities, ownership and roles with related-record links; those descriptions do not grant access to the application.

A connected MCP client (see [AI.md](AI.md)) can run the `founder_intake`, `acquisition_intake` and `readiness_review` prompt workflows to interview you and draft these records. They record only assumptions or reported claims; human review gates are unchanged.

Use obligations for tax/reporting reviews, renewals, licenses, customer commitments and similar dated work. v0.1 lists overdue obligations by name with their due dates in the readiness report; it does not send reminders or automatically establish recurrence.

## 3. Preserve and review evidence

In **Evidence**, add the original text and an optional source URL and effective date. URLs are references, not fetch instructions. Preserve signed/executed documents in your private document system and attach relevant text and a source reference; this release does not upload binary originals or provide e-signatures.

Choose **external receipt** for evidence of an action outside the app. A reviewer opens the source and records what they checked. “Reviewed” is a human assessment, not a software authenticity guarantee. Corrections are new evidence records; withdrawing a verification retains the original and reopens affected work.

## 4. Model operations or an acquisition

**Financial model** creates linked monthly statements and explicit base/downside/upside cases. Start with believable quantities and unit economics; include real labor and support costs. Examine collection timing and negative cash. Save new versions as assumptions improve.

**Acquisitions** creates an earnings/valuation/financing analysis and seven target-specific diligence steps. Attach independent evidence to add-backs, check replacement management and financing costs, and inspect the cash available after debt service. A target is not approved because its spreadsheet looks attractive.

```sh
uv run foundry forecast BUSINESS_ID examples/forecast.json
uv run foundry acquire BUSINESS_ID examples/acquisition.json
```

## 5. Work through the decisions

Open a step in **Work & decisions**. Prepare it, attach evidence and explain the decision. Approval requires a human owner/reviewer, reviewed evidence, required record types and resolved prerequisites. External execution goes through a separate submitted state and requires an external receipt. Verification means the reviewer checked the recorded result. Review a non-applicability decision explicitly rather than silently omitting a requirement.

Legal, formation, banking, licensing and tax work happens with qualified people and providers. Keep submitted and accepted evidence distinct. Source guidance is a starting point; activity, location, ownership and financing facts determine actual requirements.

## 6. Generate the working pack

Use **Documents** to generate a business plan, structure decision brief, customer contract brief, operating manual, readiness report or investment memo. Read the unknowns. Contract briefs are instructions for preparation and review, not binding agreements. A document records the source state used to create it; later changes flag it for regeneration.

Documents are written for a reader, not a database. Each record shows its certainty in plain words (assumption, reported or verified), its owner and due date, and its evidence by title with its review standing: reviewed by whom and when, not yet reviewed, or rejected on review. Machine identifiers, the version of each record, step, evidence item, plan and deal, and the start of each evidence content hash appear only in the closing **Reference index**, so every statement can still be traced to the workspace; the **Source and review trail** states the business version and the source fingerprint the document was generated from. Besides the business, the index lists exactly the items the body names: a step that is only counted, or a record the readiness report has no reason to mention, has no row. Titles, details, notes and other typed text are shown literally: Markdown characters that would otherwise start a heading, list, quote, code block, link, image or HTML are preceded by a backslash. Every `[`, `` ` `` and `|` gets one. Other characters, such as `*`, `_`, `~`, `<`, `&` and a `#` or `-` at the start of the text, get one only where they could be read as markup, so `Cost * quantity` is printed as typed while `Invoice [draft]` is printed as `Invoice \[draft]`. While the app shows a document's source as plain text, these backslashes are visible. A viewer that strikes through text between single tildes (GitHub does) will strike it through, with the words still present, and a viewer may turn a bare web address into a link on its own. Tabs, line breaks and other whitespace characters become single spaces, and any other control character is printed as U+FFFD (�). The source list names only the guidance that this business's own steps cite, with the guide version and the date the sources were checked.

The **readiness report** is the audit to read first. It is computed from the recorded workspace alone, with no AI, in this order: where things stand (steps counted by state); what needs a person now (evidence waiting for review, steps waiting for approval, approved or submitted steps waiting for the next action, each saying when its next move would be refused for unmet prerequisites and what it needs first, and steps sent back for review with the latest reason); what can be worked on next; what is blocked and by what; what is still an assumption or has no evidence; money and deadlines, naming each obligation past its due date; and every step. An empty section says so. The business plan adds the decisions made and the commitments and duties recorded; it, the operating manual and the investment memo end with a short open-work summary and point to the readiness report rather than repeating every step. An excerpt from a synthetic business:

```text
## What needs a person now

### Evidence waiting for review

- Draft quote

### Steps prepared and waiting for approval

- Verify data, software and asset rights — cannot be approved yet; needs first: Add asset record.

### Steps waiting for the next human action

- Define the offer and acceptance standard — approved; confirm it as verified.

### Steps sent back for review

- Decide the entity and ownership structure — reason: Founder may move abroad; recheck.

## What can be worked on next

- Decide the entity and ownership structure
- Resolve operating and founder jurisdictions
```

```sh
uv run foundry document BUSINESS_ID business_plan
uv run foundry document BUSINESS_ID investment_memo --deal-id DEAL_ID
```

## 7. Record a first sale through payment

Add a customer, agreement evidence and delivery-acceptance evidence. Draft an invoice in **Sales to cash**. Once you actually issue it using your invoicing provider, attach a reviewed external issue receipt and choose **Record issue**. This does not email a customer.

When money is actually received, choose **Record payment**, use the bank/provider reference, attach reviewed receipt evidence and enter the amount/date. Partial payments reduce the balance; duplicate references and overpayments are rejected. The result is an operational invoice/receipt register, not a replacement for the business's accounting ledger. There is no tax calculation, refund, credit note or void flow in v0.1; retain errors in history and reconcile them with the accounting system.

## 8. Review and recover

Use **History** to inspect activity and verify the local chain against current records. Export one business as JSON or back up the entire database:

```sh
uv run foundry verify BUSINESS_ID
uv run foundry export BUSINESS_ID > /private/path/business-export.json
uv run foundry backup /private/path/foundry-backup.db
uv run foundry --db /private/path/recovered/foundry.db restore /private/path/foundry-backup.db
```

Stop the destination server before restoration. Restore refuses to overwrite an existing database. The backup contains all businesses and credential hashes; keep it private and retain the original owner key separately. Use the same key to sign into a restored server. For command-line owner operations, securely place the original `owner.key` beside the restored database. The JSON export is for inspection and portability; it is not a restore or permissions-import feature.
