"""Readable documents: plain words in the body, machine ids only in the reference index.

Expected lines are written by hand from the synthetic business built below, not derived from the renderer.
"""

import copy
import re
from datetime import date
from uuid import uuid4

import pytest

from foundry import models
from foundry.demo import seed
from foundry.documents import STALE_WITHOUT_MESSAGE, render_document, stale_warnings
from foundry.store import DomainError, canonical, digest

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
        "- Verify data, software and asset rights — cannot be approved yet; needs first: Add asset record.",
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
    assert section(report, "## Money and deadlines") == [
        "- Obligations past their due date: 1.",
        "  - Quarterly sales tax filing — due 2020-01-31.",
    ]
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


def test_a_step_sent_back_twice_shows_only_the_latest_reason(harbor):
    _, s, a, _, _ = harbor["setup"]
    bid = harbor["bid"]
    first = "Founder may move abroad; recheck."
    second = "Second founder joins from another country; review the structure again."

    def structure():
        return next(
            o
            for o in s.snapshot(a, bid)["objects"]
            if o["kind"] == "task" and o["data"]["key"] == "structure"
        )

    # The fixture already sent the structure decision back once, with the first note.
    for state, note in (("in_progress", "Synthetic rework."), ("needs_review", second)):
        step = structure()
        s.transition(
            a,
            bid,
            step["id"],
            key(),
            dump(models.TaskTransition, state=state, expected_version=step["version"], note=note),
        )
    history = structure()["data"]["history"]
    assert [e["note"] for e in history if e.get("to") == "needs_review"] == [first, second]
    report = generate(harbor, "readiness_report")
    sent_back = report.split("### Steps sent back for review\n\n")[1].split("\n\n")[0]
    # Sending it back again also reopens the banking step, which depends on it.
    assert sent_back.splitlines() == [
        f"- Decide the entity and ownership structure — reason: {second}",
        "- Establish banking and payment access — reason: Reopened: Decide the entity and ownership structure.",
    ]
    assert first not in report


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
        "The readiness report lists every step, what needs a person now, what can be worked on next "
        "and what is blocked."
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
        # The report names only records that are assumptions or have no evidence.
        "readiness_report": {records["risk"]["id"], records["obligation"]["id"], evidence["quote"]["id"]},
    }[template] | {harbor["bid"]}
    tasks = [o for o in snapshot["objects"] if o["kind"] == "task"]
    if template == "readiness_report":
        expected |= task_ids(harbor)
    elif template in {"business_plan", "operating_manual"}:
        # The two finished steps are counted, not named; every unfinished one is named.
        expected |= {t["id"] for t in tasks if t["state"] not in {"verified", "not_applicable"}}
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
        "The readiness report lists every step, what needs a person now, what can be worked on next "
        "and what is blocked.",
        "A completed checklist reflects recorded reviews. It does not establish legal compliance, "
        "commercial viability or an independently audited result.",
    ]


def test_a_stale_reason_without_a_message_still_warns_and_a_missing_list_warns_nothing(setup):
    _, s, a, bid, _ = setup
    s.save_forecast(
        a, bid, key(), dump(models.ForecastCreate, title="Pilot", inputs=models.ForecastInputs(months=12))
    )
    version = s.snapshot(a, bid)["business"]["version"]
    s.change_jurisdiction(
        a,
        bid,
        key(),
        dump(models.JurisdictionChange, expected_version=version, jurisdiction="GB", reason="Synthetic."),
    )
    snapshot = s.snapshot(a, bid)
    plan = next(o for o in snapshot["objects"] if o["kind"] == "financial_plan")
    [reason] = plan["stale_reasons"]
    fallback = "WARNING: " + STALE_WITHOUT_MESSAGE
    for missing in ({k: v for k, v in reason.items() if k != "message"}, {**reason, "message": None}, {}):
        plan["stale_reasons"] = [missing, reason]
        _, content = render_document("business_plan", snapshot)
        assert f"## Financial scenarios\n\n{fallback}\n\nWARNING: {reason['message']}\n\nModel: " in content
        assert "WARNING: None" not in content and content.count("WARNING:") == 2
    # A snapshot built by hand, with no stale_reasons at all, prints the figures with no warning.
    for absent in ({}, {"stale_reasons": None}):
        plan.pop("stale_reasons", None)
        plan.update(absent)
        _, content = render_document("business_plan", snapshot)
        assert "WARNING:" not in content and "## Financial scenarios\n\nModel: Pilot. " in content
    assert stale_warnings(None) == []


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


def test_a_prepared_step_with_open_prerequisites_says_it_cannot_be_approved_yet(harbor):
    _, s, a, _, _ = harbor["setup"]
    bid = harbor["bid"]
    founders = next(
        o for o in s.snapshot(a, bid)["objects"] if o["kind"] == "task" and o["data"]["key"] == "founders"
    )
    founders = s.transition(
        a,
        bid,
        founders["id"],
        key(),
        dump(models.TaskTransition, state="prepared", expected_version=founders["version"], note="Draft."),
    )
    report = generate(harbor, "readiness_report")
    needs = "Decide the entity and ownership structure; Add contract record"
    assert section(report, "## What needs a person now")[2:5] == [
        "### Steps prepared and waiting for approval",
        "- Record founder and IP arrangements — cannot be approved yet; needs first: " + needs + ".",
        "- Verify data, software and asset rights — cannot be approved yet; needs first: Add asset record.",
    ]
    assert f"- Record founder and IP arrangements — needs first: {needs}." in section(
        report, "## What is blocked"
    )
    # The sentence and the service agree: approval really is refused, for the same reasons.
    interview = harbor["evidence"]["interview"]["id"]
    with pytest.raises(DomainError) as refused:
        s.transition(
            a,
            bid,
            founders["id"],
            key(),
            dump(
                models.TaskTransition,
                state="approved",
                expected_version=founders["version"],
                note="Synthetic approval attempt.",
                evidence_ids=[interview],
            ),
        )
    assert refused.value.message == "Complete: Decide the entity and ownership structure; Add contract record"
    stored = next(o for o in s.snapshot(a, bid)["objects"] if o["id"] == founders["id"])
    assert stored["state"] == "prepared" and stored["version"] == founders["version"]


def test_an_approved_or_submitted_step_with_open_prerequisites_names_the_refused_move():
    # The service reopens a completed step when a prerequisite reopens, so this state is hand-built.
    blocked = ["Complete: Earlier step", "Add contract record"]
    tasks = [
        step(1, "Internal step", "approved", external=False),
        step(2, "Filing step", "approved", external=True),
        step(3, "Sent step", "submitted", external=True),
        step(4, "Open internal step", "approved", external=False),
    ]
    for t in tasks[:3]:
        t["blockers"] = blocked
    _, report = render_document("readiness_report", bare_snapshot(tasks))
    assert section(report, "## What needs a person now")[4:9] == [
        "### Steps waiting for the next human action",
        "- Internal step — approved, but cannot be verified yet; needs first: Earlier step; Add contract record.",
        "- Filing step — approved, but cannot be submitted yet; needs first: Earlier step; Add contract record.",
        "- Sent step — submitted, but cannot be verified yet; needs first: Earlier step; Add contract record.",
        "- Open internal step — approved; confirm it as verified.",
    ]


def test_overdue_obligations_are_named_with_their_due_dates(harbor):
    _, s, a, _, _ = harbor["setup"]
    bid = harbor["bid"]
    for title, due in (
        ("Licence renewal", "2999-12-31"),
        ("Annual return", None),
        ("Old payroll filing", "2001-06-30"),
    ):
        s.add_record(
            a,
            bid,
            key(),
            dump(models.RecordCreate, kind="obligation", title=title, certainty="reported", due_date=due),
        )
    snapshot = s.snapshot(a, bid)
    flags = {o["title"]: o["overdue"] for o in snapshot["objects"] if o["kind"] == "obligation"}
    assert flags == {
        "Quarterly sales tax filing": True,
        "Licence renewal": False,
        "Annual return": False,
        "Old payroll filing": True,
    }
    assert snapshot["stats"]["overdue_obligations"] == 2
    report = generate(harbor, "readiness_report")
    assert section(report, "## Money and deadlines") == [
        "- Obligations past their due date: 2.",
        "  - Old payroll filing — due 2001-06-30.",
        "  - Quarterly sales tax filing — due 2020-01-31.",
    ]
    # The flag is derived: nothing is stored and the source fingerprint is computed without it.
    store = harbor["setup"][0]
    with store.read() as con:
        business, objects = store.business(con, bid), store.objects(con, bid)
    assert not any("overdue" in o for o in objects)
    unflagged = [o for o in objects if o["kind"] != "document"]
    assert snapshot["source_fingerprint"] == digest(canonical({"business": business, "objects": unflagged}))


def test_an_obligation_due_today_is_not_overdue(setup, monkeypatch):
    _, s, a, bid, _ = setup

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2030, 6, 1)

    monkeypatch.setattr("foundry.service.date", FixedDate)
    for title, due in (("Yesterday", "2030-05-31"), ("Today", "2030-06-01"), ("Tomorrow", "2030-06-02")):
        s.add_record(
            a,
            bid,
            key(),
            dump(models.RecordCreate, kind="obligation", title=title, certainty="reported", due_date=due),
        )
    snapshot = s.snapshot(a, bid)
    assert {o["title"]: o["overdue"] for o in snapshot["objects"] if o["kind"] == "obligation"} == {
        "Yesterday": True,
        "Today": False,
        "Tomorrow": False,
    }
    _, report = render_document("readiness_report", snapshot)
    assert section(report, "## Money and deadlines") == [
        "- Obligations past their due date: 1.",
        "  - Yesterday — due 2030-05-31.",
    ]


def test_a_snapshot_without_obligation_flags_falls_back_to_the_count():
    snapshot = bare_snapshot(
        [obj("obligation", 1, "Filing", "reported", due_date="2001-01-01", evidence_ids=[])]
    )
    snapshot["stats"] = {"overdue_obligations": 1}
    _, report = render_document("readiness_report", snapshot)
    assert section(report, "## Money and deadlines") == ["- Obligations past their due date: 1."]


NAMED_KINDS = {
    "customer",
    "offer",
    "asset",
    "contract",
    "obligation",
    "role",
    "entity",
    "person",
    "ownership",
    "decision",
    "risk",
    "task",
    "evidence",
    "financial_plan",
    "deal",
}


@pytest.fixture
def renamed(harbor):
    """The harbor snapshot with a plan and a deal, every title replaced by one printed nowhere else."""
    _, s, a, _, _ = harbor["setup"]
    bid = harbor["bid"]
    interview = harbor["evidence"]["interview"]["id"]
    s.save_forecast(a, bid, key(), dump(models.ForecastCreate, title="Pilot plan"))
    deal_id = s.save_deal(
        a,
        bid,
        key(),
        dump(
            models.DealInputs,
            target="Corner shop",
            thesis="A synthetic target.",
            adjustments=[
                {"label": "Owner salary", "amount": 1000, "evidence_id": interview},
                {"label": "Unsupported add-back", "amount": 500},
            ],
        ),
    )["id"]
    snapshot = copy.deepcopy(s.snapshot(a, bid))
    named = [o for o in snapshot["objects"] if o["kind"] in NAMED_KINDS]
    old = {o["id"]: o["title"] for o in named}
    steps = [old[o["id"]] for o in named if o["kind"] == "task"]
    assert len(set(steps)) == len(steps)
    new = {o["id"]: f"Item{n:03d}x" for n, o in enumerate(named)}
    by_title = {old[i]: new[i] for i in new}
    for o in named:
        o["title"] = new[o["id"]]
        if o["kind"] == "task":
            o["blockers"] = [
                "Complete: " + by_title[b.removeprefix("Complete: ")] if b.startswith("Complete: ") else b
                for b in o["blockers"]
            ]
    deal = next(o for o in snapshot["objects"] if o["id"] == deal_id)
    return snapshot, deal, {o["id"]: o for o in named}


@pytest.mark.parametrize(
    "template",
    [
        "business_plan",
        "structure_options",
        "contract_brief",
        "operating_manual",
        "readiness_report",
        "investment_memo",
    ],
)
def test_the_reference_index_lists_exactly_the_items_the_body_names(renamed, template):
    snapshot, deal, named = renamed
    content = render_document(template, snapshot, deal)[1]
    body, _ = body_and_index(content)
    rows = index_rows(content)
    assert rows[0][1] == "Business"
    indexed = {row[2] for row in rows[1:]}
    # Every row but the business row names an item whose title the body prints.
    assert indexed <= set(named)
    assert [row[0] for row in rows[1:] if row[0] not in body] == []
    # Every item whose title the body prints has a row.
    assert {i for i, o in named.items() if o["title"] in body} == indexed


def test_the_report_and_the_plan_leave_out_what_they_do_not_name(harbor, renamed):
    snapshot, _, named = renamed
    customer = named[harbor["records"]["customer"]["id"]]
    interview = named[harbor["evidence"]["interview"]["id"]]
    demand = next(o for o in named.values() if o["kind"] == "task" and o["data"]["key"] == "demand")
    # A verified record with reviewed evidence needs nobody now: the report names neither.
    assert customer["state"] == "verified" and interview["state"] == "verified"
    report = render_document("readiness_report", snapshot)[1]
    report_ids = {row[2] for row in index_rows(report)}
    assert customer["title"] not in report and customer["id"] not in report_ids
    assert interview["title"] not in report and interview["id"] not in report_ids
    # A finished step is counted in the plan, not named, so it has no row there; the report names it.
    assert demand["state"] == "verified"
    plan = render_document("business_plan", snapshot)[1]
    # 17 business steps and the deal's 7: demand is verified and bank is not applicable.
    assert section(plan, "## Open work")[0] == "24 steps are tracked: 2 finished and 22 still open."
    assert demand["title"] not in plan and demand["id"] not in {row[2] for row in index_rows(plan)}
    assert demand["title"] in report and demand["id"] in report_ids
