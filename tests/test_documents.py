"""Readable documents: plain words in the body, machine ids only in the reference index.

Expected lines are written by hand from the synthetic business built below, not derived from the renderer.
"""

import copy
import re
from uuid import uuid4

import pytest

from foundry import models
from foundry.demo import seed
from foundry.documents import render_document

ID = re.compile(r"[a-z_]+_[0-9a-f]{20}")
INDEX = "## Reference index"


def dump(cls, **kwargs):
    return cls(**kwargs).model_dump(mode="json")


def key():
    return str(uuid4())


def body_and_index(content):
    assert content.count(INDEX) == 1
    body, index = content.split(INDEX)
    return body, index


def index_rows(content):
    """Reference index rows as lists of cells, split only on unescaped pipes."""
    _, index = body_and_index(content)
    rows = [x for x in index.splitlines() if x.startswith("|")]
    return [[c.strip() for c in re.split(r"(?<!\\)\|", x)[1:-1]] for x in rows[2:]]


def section(content, heading):
    """Lines under a '## ' heading up to the next '## ' heading, blank lines dropped."""
    lines = content.splitlines()
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return [x for x in lines[start:end] if x]


@pytest.fixture
def harbor(setup):
    """A synthetic bookkeeping business with known records, evidence and steps in several states."""
    _, s, a, _, _ = setup
    business = s.create_business(
        a,
        key(),
        dump(
            models.BusinessCreate,
            name="Harbor Books (synthetic)",
            concept="Synthetic month-end bookkeeping for small accounting firms.",
            customer="Small accounting firms (fictional)",
        ),
    )
    bid = business["id"]

    def evidence(title, review=None):
        item = s.add_evidence(
            a, bid, key(), dump(models.EvidenceCreate, title=title, content=title + " body.")
        )
        if review is None:
            return item
        item = s.review_evidence(
            a,
            bid,
            item["id"],
            key(),
            dump(models.Review, note="Checked the synthetic source.", expected_version=1),
        )
        return item

    interview = evidence("Interview notes", review=True)
    quote = evidence("Draft quote")
    price_list = evidence("Old price list", review=True)

    def record(kind, title, certainty, evidence_ids=(), **extra):
        return s.add_record(
            a,
            bid,
            key(),
            dump(
                models.RecordCreate,
                kind=kind,
                title=title,
                certainty=certainty,
                evidence_ids=list(evidence_ids),
                **extra,
            ),
        )

    customer = record(
        "customer",
        "Small accounting firms",
        "verified",
        [interview["id"]],
        details="Firms with two to ten staff.",
        owner="Ana",
    )
    offer = record("offer", "Monthly close package", "reported", [quote["id"], price_list["id"]])
    risk = record("risk", "Clients may not switch", "assumption")
    decision = record("decision", "Trade as a sole trader for the pilot", "reported", [interview["id"]])
    obligation = record("obligation", "Quarterly sales tax filing", "assumption", due_date="2020-01-31")
    s.review_evidence(
        a,
        bid,
        price_list["id"],
        key(),
        dump(models.Review, note="Superseded synthetic price list.", expected_version=price_list["version"]),
        True,
    )

    def tasks():
        return {t["data"]["key"]: t for t in s.snapshot(a, bid)["objects"] if t["kind"] == "task"}

    def move(step, *states, note="Synthetic step.", evidence_ids=None):
        for state in states:
            task = tasks()[step]
            s.transition(
                a,
                bid,
                task["id"],
                key(),
                dump(
                    models.TaskTransition,
                    state=state,
                    expected_version=task["version"],
                    note=note,
                    evidence_ids=[interview["id"]] if evidence_ids is None else evidence_ids,
                ),
            )

    move("structure", "in_progress", evidence_ids=[])
    move("structure", "needs_review", note="Founder may move abroad; recheck.", evidence_ids=[])
    move("demand", "prepared", "approved", "verified")
    move("offer", "prepared", "approved")
    move("rights", "prepared")
    move("bank", "not_applicable")
    return {
        "setup": setup,
        "bid": bid,
        "evidence": {"interview": interview, "quote": quote, "price_list": price_list},
        "records": {
            "customer": customer,
            "offer": offer,
            "risk": risk,
            "decision": decision,
            "obligation": obligation,
        },
    }


def generate(harbor, template, deal_id=None):
    _, s, a, _, _ = harbor["setup"]
    doc = s.generate_document(
        a, harbor["bid"], key(), dump(models.DocumentCreate, template=template, deal_id=deal_id)
    )
    return doc["data"]["content"]


def task_ids(harbor):
    _, s, a, _, _ = harbor["setup"]
    return {o["id"] for o in s.snapshot(a, harbor["bid"])["objects"] if o["kind"] == "task"}


def test_readiness_report_audits_the_workspace_in_order(harbor):
    report = generate(harbor, "readiness_report")
    headings = [x for x in report.splitlines() if x.startswith("## ")]
    assert headings == [
        "## Where things stand",
        "## What needs a person now",
        "## What can be worked on next",
        "## What is blocked",
        "## What is still an assumption",
        "## Money and deadlines",
        "## Every step",
        "## Source and review trail",
        "## Reference index",
    ]
    assert section(report, "## Where things stand") == [
        "17 steps are tracked: 2 finished and 15 still open.",
        "- Verified as done: 1",
        "- Confirmed not applicable: 1",
        "- Sent back for review: 1",
        "- Approved, waiting for the next action: 1",
        "- Prepared, waiting for approval: 1",
        "- Not started: 12",
    ]
    assert section(report, "## What needs a person now") == [
        "### Evidence waiting for review",
        "- Draft quote",
        "### Steps prepared and waiting for approval",
        "- Verify data, software and asset rights",
        "### Steps waiting for the next human action",
        "- Define the offer and acceptance standard — approved; confirm it as verified.",
        "### Steps sent back for review",
        "- Decide the entity and ownership structure — reason: Founder may move abroad; recheck.",
    ]
    assert section(report, "## What can be worked on next") == [
        "- Decide the entity and ownership structure",
        "- Resolve operating and founder jurisdictions",
    ]
    blocked = section(report, "## What is blocked")
    assert blocked[0] == "- Review cash needs and downside — needs first: Add financial plan record."
    assert "- Verify data, software and asset rights — needs first: Add asset record." in blocked
    assert (
        "- Record founder and IP arrangements — needs first: "
        "Decide the entity and ownership structure; Add contract record."
    ) in blocked
    assert len(blocked) == 12
    assert not any("customer demand" in x or "banking" in x or "jurisdictions" in x for x in blocked)
    assert section(report, "## What is still an assumption") == [
        "### Records marked as assumptions",
        "- Obligation: Quarterly sales tax filing",
        "- Risk: Clients may not switch",
        "### Records with no evidence attached",
        "- Obligation: Quarterly sales tax filing",
        "- Risk: Clients may not switch",
    ]
    assert section(report, "## Money and deadlines") == ["- Obligations past their due date: 1."]
    steps = section(report, "## Every step")
    assert steps[:3] == [
        "- Establish customer demand — verified as done",
        "  Record the buyer, problem, alternatives and interview or purchase evidence. "
        "Separate a stated interest from a paid commitment.",
        "- Define the offer and acceptance standard — approved, waiting for the next action",
    ]
    assert "- Establish banking and payment access — confirmed not applicable" in steps
    assert "  Needs first: Add financial plan record" in steps
    assert sum(x.startswith("- ") for x in steps) == 17
    assert steps[-1] == (
        "A completed checklist reflects recorded reviews. It does not establish legal compliance, "
        "commercial viability or an independently audited result."
    )


def test_record_blocks_state_certainty_and_evidence_review_in_plain_words(harbor):
    plan = generate(harbor, "business_plan")
    reviewed_on = harbor["evidence"]["interview"]["data"]["review"]["at"][:10]
    lines = plan.splitlines()
    start = lines.index("### Small accounting firms")
    assert lines[start : start + 6] == [
        "### Small accounting firms",
        "Certainty: Verified — a person confirmed it against reviewed evidence.",
        "Firms with two to ten staff.",
        "Owner: Ana. Due: Not set.",
        "Evidence:",
        f"- Interview notes: reviewed by Local owner on {reviewed_on}.",
    ]
    start = lines.index("### Monthly close package")
    assert lines[start : start + 6] == [
        "### Monthly close package",
        "Certainty: Reported — stated by the business or a source, not yet confirmed against reviewed evidence.",
        "Owner: Founder. Due: Not set.",
        "Evidence:",
        "- Draft quote: not yet reviewed.",
        f"- Old price list: rejected on review by Local owner on {reviewed_on}.",
    ]
    start = lines.index("### Clients may not switch")
    assert lines[start : start + 4] == [
        "### Clients may not switch",
        "Certainty: Assumption — a working belief that nobody has confirmed yet.",
        "Owner: Founder. Due: Not set.",
        "Evidence: None attached.",
    ]
    rows = {row[2]: row for row in index_rows(plan)}
    for name, standing in (
        ("interview", f"Reviewed by Local owner on {reviewed_on}"),
        ("quote", "Not yet reviewed"),
        ("price_list", f"Rejected on review by Local owner on {reviewed_on}"),
    ):
        evidence = harbor["evidence"][name]
        assert rows[evidence["id"]][1] == "Evidence"
        assert rows[evidence["id"]][4] == standing
        assert rows[evidence["id"]][5] == evidence["data"]["content_sha256"][:12]


def test_business_plan_includes_decisions_and_obligations(harbor):
    plan = generate(harbor, "business_plan")
    assert section(plan, "## Decisions made")[:2] == [
        "### Trade as a sole trader for the pilot",
        "Certainty: Reported — stated by the business or a source, not yet confirmed against reviewed evidence.",
    ]
    assert section(plan, "## Commitments and duties")[:3] == [
        "### Quarterly sales tax filing",
        "Certainty: Assumption — a working belief that nobody has confirmed yet.",
        "Owner: Founder. Due: 2020-01-31.",
    ]


@pytest.mark.parametrize("template", ["business_plan", "operating_manual"])
def test_plan_and_manual_summarize_open_work_instead_of_listing_every_step(harbor, template):
    content = generate(harbor, template)
    assert "## Work and unresolved requirements" not in content and "## Every step" not in content
    work = section(content, "## Open work")
    assert work[0] == "17 steps are tracked: 2 finished and 15 still open."
    assert work[7] == "Unfinished steps:"
    assert work[8:10] == [
        "- Define the offer and acceptance standard — approved, waiting for the next action",
        "- Review cash needs and downside — not started",
    ]
    assert len(work) == 7 + 1 + 15 + 2
    assert "- Establish customer demand — verified as done" not in content
    assert work[-2] == (
        "The readiness report lists what needs a person now, what can be worked on next and what is blocked."
    )
    assert "Record the buyer, problem, alternatives" not in content  # step descriptions stay in the report


def test_sources_list_only_what_this_business_cites(harbor):
    report = generate(harbor, "readiness_report")
    trail = section(report, "## Source and review trail")
    assert trail[2:] == [
        "Sources cited by these steps (planning guide 2026.11.1; sources checked on 2026-11-05):",
        "- https://www.sba.gov/counseling/launch-your-business/ — cited by: "
        "Decide the entity and ownership structure; Verify applicable registrations; "
        "Review and establish tax obligations; Establish banking and payment access; "
        "Review permits, insurance and activity rules",
        "- https://commonpaper.com/standards/ — cited by: "
        "Record founder and IP arrangements; Confirm the customer agreement",
    ]
    for content in (report, generate(harbor, "contract_brief")):
        assert "gov.uk" not in content and "verksamt.se" not in content
        assert "plan-your-business" not in content


def test_investment_memo_covers_only_its_own_deal(harbor):
    _, s, a, _, _ = harbor["setup"]
    bid = harbor["bid"]
    first = s.save_deal(
        a, bid, key(), dump(models.DealInputs, target="First shop", thesis="A first synthetic target.")
    )
    s.save_deal(
        a, bid, key(), dump(models.DealInputs, target="Second shop", thesis="A second synthetic target.")
    )
    memo = generate(harbor, "investment_memo", first["id"])
    work = section(memo, "## Open work on this acquisition")
    assert work[:3] == [
        "7 steps are tracked: 0 finished and 7 still open.",
        "- Not started: 7",
        "Unfinished steps:",
    ]
    assert work[3] == "- Approve the acquisition thesis (acquisition: First shop) — not started"
    assert sum(x.startswith("- ") and "(acquisition: First shop)" in x for x in work) == 7
    assert "Second shop" not in memo
    assert "Establish customer demand" not in memo
    trail = section(memo, "## Source and review trail")
    assert trail[3].startswith("- https://www.sba.gov/counseling/plan-your-business/ — cited by: ")
    assert len(trail) == 4
    snapshot = s.snapshot(a, bid)
    own = {
        t["id"] for t in snapshot["objects"] if t["kind"] == "task" and t["data"]["deal_id"] == first["id"]
    }
    indexed = {row[2] for row in index_rows(memo)}
    assert own <= indexed
    assert not (task_ids(harbor) - own) & indexed


@pytest.mark.parametrize(
    "template",
    ["business_plan", "structure_options", "contract_brief", "operating_manual", "readiness_report"],
)
def test_ids_appear_only_in_the_reference_index_and_every_relied_id_is_there(harbor, template):
    _, s, a, _, _ = harbor["setup"]
    s.save_forecast(a, harbor["bid"], key(), dump(models.ForecastCreate, title="Pilot plan"))
    snapshot = s.snapshot(a, harbor["bid"])
    content = generate(harbor, template)
    body, _ = body_and_index(content)
    assert ID.findall(body) == []
    indexed = {row[2] for row in index_rows(content)}
    records = harbor["records"]
    evidence = harbor["evidence"]
    plan_id = next(o["id"] for o in snapshot["objects"] if o["kind"] == "financial_plan")
    expected = {
        "business_plan": {records[k]["id"] for k in records}
        | {e["id"] for e in evidence.values()}
        | {plan_id},
        "structure_options": {
            records["decision"]["id"],
            records["obligation"]["id"],
            evidence["interview"]["id"],
        },
        "contract_brief": {records["customer"]["id"], records["offer"]["id"]}
        | {e["id"] for e in evidence.values()},
        "operating_manual": {records["offer"]["id"], records["obligation"]["id"], records["risk"]["id"]},
        "readiness_report": {records[k]["id"] for k in records} | {evidence["quote"]["id"]},
    }[template] | {harbor["bid"]}
    if template in {"business_plan", "operating_manual", "readiness_report"}:
        expected |= task_ids(harbor)
    assert expected <= indexed
    assert indexed <= {o["id"] for o in snapshot["objects"]} | {harbor["bid"]}


def test_memo_ids_appear_only_in_the_reference_index(harbor):
    _, s, a, _, _ = harbor["setup"]
    interview = harbor["evidence"]["interview"]
    deal = s.save_deal(
        a,
        harbor["bid"],
        key(),
        dump(
            models.DealInputs,
            target="Corner shop",
            thesis="A synthetic target with one supported adjustment.",
            adjustments=[{"label": "Owner salary", "amount": 1000, "evidence_id": interview["id"]}],
        ),
    )
    memo = generate(harbor, "investment_memo", deal["id"])
    body, _ = body_and_index(memo)
    assert ID.findall(body) == []
    assert any(x.startswith("| Add: Owner salary") and "| Interview notes" in x for x in body.splitlines())
    assert {deal["id"], interview["id"], harbor["bid"]} <= {row[2] for row in index_rows(memo)}


def test_rendering_is_pure_and_deterministic(harbor):
    _, s, a, _, _ = harbor["setup"]
    snapshot = s.snapshot(a, harbor["bid"])
    original = copy.deepcopy(snapshot)
    for template in ("business_plan", "readiness_report", "operating_manual"):
        first = render_document(template, snapshot)
        assert render_document(template, copy.deepcopy(snapshot)) == first
        assert render_document(template, snapshot)[1].encode() == first[1].encode()
    assert snapshot == original


def test_empty_business_renders_every_template_with_explicit_empty_sections(setup):
    _, s, a, bid, _ = setup
    snapshot = s.snapshot(a, bid)
    for template in ("business_plan", "structure_options", "contract_brief", "operating_manual"):
        _, content = render_document(template, snapshot)
        assert content.startswith("# Test business — ")
        assert ID.findall(body_and_index(content)[0]) == []
    _, report = render_document("readiness_report", snapshot)
    assert section(report, "## What needs a person now") == [
        "### Evidence waiting for review",
        "No evidence is waiting for review.",
        "### Steps prepared and waiting for approval",
        "No step is waiting for approval.",
        "### Steps waiting for the next human action",
        "No approved or submitted step is waiting.",
        "### Steps sent back for review",
        "No step has been sent back for review.",
    ]
    assert section(report, "## What is still an assumption") == [
        "### Records marked as assumptions",
        "No business records have been added yet.",
        "### Records with no evidence attached",
        "No business records have been added yet.",
    ]
    assert section(report, "## Money and deadlines") == [
        "No outstanding invoices, collected payments or overdue obligations are recorded."
    ]
    bare = {**snapshot, "objects": [], "stats": {}}
    _, report = render_document("readiness_report", bare)
    assert section(report, "## Where things stand") == ["No steps are recorded."]
    assert section(report, "## What can be worked on next") == [
        "No open step is free of prerequisites right now."
    ]
    assert section(report, "## What is blocked") == ["No open step is blocked."]
    assert section(report, "## Every step")[0] == "No steps are recorded."
    assert section(report, "## Source and review trail")[-1] == "These steps cite no external sources."
    assert index_rows(report) == [["Test business", "Business", bid, "1", "—", "—"]]


def test_demonstration_business_renders_with_banner_and_unchanged_trail(setup):
    _, s, a, _, _ = setup
    bid = seed(s, a)["id"]
    snapshot = s.snapshot(a, bid)
    deal = next(o for o in snapshot["objects"] if o["kind"] == "deal")
    for template in ("business_plan", "readiness_report", "operating_manual", "investment_memo"):
        _, content = render_document(template, snapshot, deal)
        lines = content.splitlines()
        assert lines[2] == "Status: DRAFT — human review required."
        assert lines[4].startswith("SYNTHETIC DEMONSTRATION. Names, commercial assumptions and receipts")
        assert (
            f"Business version: 1. Source fingerprint: {snapshot['source_fingerprint']}." in lines
            and "This document is an immutable snapshot. Generate a new version when inputs change; "
            "retain signed external documents as evidence."
            in lines
        )
        assert ID.findall(body_and_index(content)[0]) == []
    _, report = render_document("readiness_report", snapshot)
    assert (
        section(report, "## Where things stand")[0] == "24 steps are tracked: 17 finished and 7 still open."
    )
    assert section(report, "## Money and deadlines") == ["- Cash collected: 4,000.00 USD."]


def bare_snapshot(objects, **business):
    data = {
        "name": "Bare business",
        "concept": "A synthetic business built directly as a snapshot.",
        "route": "start",
        "jurisdiction": "undecided",
        "currency": "USD",
        "customer": "",
        "is_demo": False,
        **business,
    }
    return {
        "business": {"id": "business_" + "0" * 20, "data": data, "version": 1},
        "objects": objects,
        "stats": {},
        "source_fingerprint": "f" * 64,
        "catalog": {"version": "2026.11.1", "source_checked_on": "2026-11-05", "sources": {}},
    }


def obj(kind, n, title, state, **data):
    return {
        "id": f"{kind}_{n:020x}",
        "kind": kind,
        "title": title,
        "state": state,
        "version": 1,
        "data": data,
    }


def step(n, title, state, **data):
    data = {"description": f"{title} description.", "deal_id": None, "history": [], **data}
    return {
        **obj("task", n, title, state, **data),
        "blockers": [],
    }


HOSTILE_HEADINGS = {
    "business_plan": [
        "# \\# Name \\| with pipe — Business plan",
        "## Buyer and problem",
        "### Bad \\| title # Injected heading \\| a \\| b \\| \\|---\\|---\\|",
        "## Offer, price and acceptance",
        "## Resources and rights",
        "## Delivery and accountability",
        "## Decisions made",
        "## Commitments and duties",
        "## Risks and experiments",
        "## Financial scenarios",
        "## Validation milestones",
        "## Open work",
        "## Source and review trail",
        "## Reference index",
    ],
    "readiness_report": [
        "# \\# Name \\| with pipe — Readiness report",
        "## Where things stand",
        "## What needs a person now",
        "### Evidence waiting for review",
        "### Steps prepared and waiting for approval",
        "### Steps waiting for the next human action",
        "### Steps sent back for review",
        "## What can be worked on next",
        "## What is blocked",
        "## What is still an assumption",
        "### Records marked as assumptions",
        "### Records with no evidence attached",
        "## Money and deadlines",
        "## Every step",
        "## Source and review trail",
        "## Reference index",
    ],
}


def test_hostile_text_cannot_break_the_index_table_or_add_a_heading():
    hostile = "Bad | title\n# Injected heading\n| a | b |\n|---|---|"
    record = obj(
        "customer", 1, hostile, "reported", details="# Not a heading\n---\nSecond | line", evidence_ids=[]
    )
    task = step(2, "# Step | title\n## Also not a heading", "pending")
    snapshot = bare_snapshot([record, task], name="# Name | with pipe", concept="# Concept\n# heading")
    for template in ("business_plan", "readiness_report"):
        _, content = render_document(template, snapshot)
        headings = [x for x in content.splitlines() if x.startswith("#")]
        assert headings == HOSTILE_HEADINGS[template]
        for block in content.split("\n\n"):
            rows = [x for x in block.splitlines() if x.startswith("|")]
            assert len({len(re.findall(r"(?<!\\)\|", x)) for x in rows}) <= 1
        rows = index_rows(content)
        assert ["Bad \\| title # Injected heading \\| a \\| b \\| \\|---\\|---\\|", "Customer"] == rows[1][:2]
        assert all(len(row) == 6 for row in rows)
    _, plan = render_document("business_plan", snapshot)
    assert "### Bad \\| title # Injected heading \\| a \\| b \\| \\|---\\|---\\|" in plan.splitlines()
    assert "\\# Not a heading --- Second \\| line" in plan.splitlines()
    assert "Concept: \\# Concept # heading" in plan.splitlines()


def test_dangling_evidence_id_is_reported_unknown_without_raising():
    missing = "evidence_" + "e" * 20
    record = obj("offer", 1, "Pilot offer", "reported", evidence_ids=[missing])
    _, plan = render_document("business_plan", bare_snapshot([record]))
    lines = plan.splitlines()
    start = lines.index("### Pilot offer")
    assert lines[start + 3 : start + 5] == [
        "Evidence:",
        "- Unknown evidence: unknown — this evidence is not in the snapshot.",
    ]
    assert ["Unknown evidence", "Evidence", missing, "—", "Not in this snapshot", "—"] in index_rows(plan)
    assert ID.findall(body_and_index(plan)[0]) == []


def test_superseded_steps_are_listed_as_replaced_and_unknown_states_are_never_finished():
    tasks = [
        step(1, "Done step", "verified"),
        step(2, "Open step", "pending"),
        step(3, "Old UK step", "superseded", source_uri="https://www.gov.uk/limited-company-formation"),
        step(4, "Odd step", "archived"),
    ]
    snapshot = bare_snapshot(tasks)
    _, report = render_document("readiness_report", snapshot)
    assert section(report, "## Where things stand") == [
        "3 steps are tracked: 1 finished and 1 still open, plus 1 in a state this version does not recognise.",
        "- Verified as done: 1",
        "- Not started: 1",
        '- In a state this version does not recognise ("archived"): 1',
        "Replaced steps (no longer part of this plan): Old UK step.",
    ]
    assert section(report, "## What needs a person now")[-2:] == [
        "### Steps in a state this version does not recognise",
        '- Odd step — In a state this version does not recognise ("archived"). Check it by hand.',
    ]
    assert section(report, "## What can be worked on next") == ["- Open step"]
    steps = section(report, "## Every step")
    assert '- Odd step — in a state this version does not recognise ("archived")' in steps
    assert not any("Old UK step" in x for x in steps)
    assert "gov.uk" not in report
    assert "task_" + f"{3:020x}" in {row[2] for row in index_rows(report)}
    _, plan = render_document("business_plan", snapshot)
    assert section(plan, "## Open work")[4:] == [
        "Replaced steps (no longer part of this plan): Old UK step.",
        "Unfinished steps:",
        "- Open step — not started",
        '- Odd step — in a state this version does not recognise ("archived")',
        "The readiness report lists what needs a person now, what can be worked on next and what is blocked.",
        "A completed checklist reflects recorded reviews. It does not establish legal compliance, "
        "commercial viability or an independently audited result.",
    ]


def test_a_single_step_and_a_missing_deal_read_correctly():
    lone = step(1, "Lone step", "pending", deal_id="deal_" + "d" * 20)
    _, report = render_document("readiness_report", bare_snapshot([lone]))
    assert section(report, "## Where things stand") == [
        "1 step is tracked: 0 finished and 1 still open.",
        "- Not started: 1",
    ]
    assert section(report, "## What can be worked on next") == ["- Lone step (acquisition)"]
    assert [
        "Unknown acquisition analysis",
        "Acquisition analysis",
        "deal_" + "d" * 20,
        "—",
        "—",
        "—",
    ] in index_rows(report)
