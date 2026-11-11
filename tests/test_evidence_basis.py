"""Withdrawing evidence reopens completed tasks whose current standing relied on it.

The basis of a completed task is read from its history: within the current cycle (after the most
recent move to needs_review), the most recent preparation and every later step. Expected values in
these tests are written out by hand.
"""

from uuid import uuid4

import pytest
from conftest import reviewed

from foundry import models

WITHDRAWN = "Synthetic evidence deliberately withdrawn."


def dump(cls, **kwargs):
    return cls(**kwargs).model_dump(mode="json")


def key():
    return str(uuid4())


def tasks(setup):
    _, service, actor, bid, _ = setup
    return {
        o["data"]["key"]: o
        for o in service.snapshot(actor, bid)["objects"]
        if o["kind"] == "task" and not o["data"]["deal_id"]
    }


def move(setup, task, state, evidence=()):
    _, service, actor, bid, _ = setup
    return service.transition(
        actor,
        bid,
        task["id"],
        key(),
        dump(
            models.TaskTransition,
            state=state,
            expected_version=task["version"],
            note=f"Synthetic move to {state}.",
            evidence_ids=[e["id"] for e in evidence],
        ),
    )


def withdraw(setup, evidence, idempotency_key=None):
    _, service, actor, bid, _ = setup
    return service.review_evidence(
        actor,
        bid,
        evidence["id"],
        idempotency_key or key(),
        dump(models.Review, note=WITHDRAWN, expected_version=evidence["version"]),
        True,
    )


def source(setup, title, source_type="document"):
    return reviewed(setup, source_type=source_type, title=title)


def finish_structure(setup, evidence):
    """Mark the structure decision not applicable, which unblocks its dependents."""
    return move(setup, tasks(setup)["structure"], "not_applicable", [evidence])


def standing(setup):
    return {k: (t["state"], t["version"]) for k, t in tasks(setup).items()}


def chain(setup):
    """structure: prepared A, approved A, verified B; registration (depends on structure) verified;
    bank (depends on structure) left pending; jurisdiction (independent) verified on its own source."""
    _, service, actor, bid, _ = setup
    service.add_record(actor, bid, key(), dump(models.RecordCreate, kind="decision", title="Entity decision"))
    a, b = source(setup, "Source A"), source(setup, "Source B")
    draft, receipt = (
        source(setup, "Registration draft"),
        source(setup, "Registration receipt", "external_receipt"),
    )
    other = source(setup, "Jurisdiction review")
    t = tasks(setup)
    structure = move(setup, t["structure"], "prepared", [a])
    structure = move(setup, structure, "approved", [a])
    structure = move(setup, structure, "verified", [b])
    registration = move(setup, t["registration"], "prepared", [draft])
    registration = move(setup, registration, "approved", [draft])
    registration = move(setup, registration, "submitted", [receipt])
    registration = move(setup, registration, "verified", [receipt])
    jurisdiction = move(setup, t["jurisdiction"], "prepared", [other])
    jurisdiction = move(setup, jurisdiction, "approved", [other])
    jurisdiction = move(setup, jurisdiction, "verified", [other])
    return a, b, structure, registration, jurisdiction


def test_withdrawing_approval_evidence_reopens_task_verified_on_other_evidence(setup):
    a, _, structure, registration, jurisdiction = chain(setup)
    assert (structure["state"], registration["state"], jurisdiction["state"]) == (
        "verified",
        "verified",
        "verified",
    )
    before = standing(setup)
    withdraw(setup, a)
    after = tasks(setup)
    assert after["structure"]["state"] == "needs_review"
    assert after["structure"]["version"] == structure["version"] + 1
    newest = after["structure"]["data"]["history"][-1]
    assert (newest["from"], newest["to"], newest["note"]) == (
        "verified",
        "needs_review",
        "Evidence withdrawn: Source A",
    )
    assert "evidence_ids" not in newest
    # The completed dependent reopens through depends_on; the pending dependent and an
    # unrelated completed task are untouched.
    assert after["registration"]["state"] == "needs_review"
    assert after["registration"]["version"] == registration["version"] + 1
    assert after["registration"]["data"]["history"][-1]["note"] == "Evidence withdrawn: Source A"
    assert (after["bank"]["state"], after["bank"]["version"]) == before["bank"] == ("pending", 1)
    assert (after["jurisdiction"]["state"], after["jurisdiction"]["version"]) == before["jurisdiction"]
    assert before["jurisdiction"] == ("verified", 4)


def test_withdrawing_receipt_cited_at_submission_reopens_external_task(setup):
    finish_structure(setup, source(setup, "Structure decision"))
    draft = source(setup, "Registration draft")
    filed = source(setup, "Filing receipt", "external_receipt")
    accepted = source(setup, "Acceptance receipt", "external_receipt")
    registration = move(setup, tasks(setup)["registration"], "prepared", [draft])
    registration = move(setup, registration, "approved", [draft])
    registration = move(setup, registration, "submitted", [filed])
    registration = move(setup, registration, "verified", [accepted])
    assert (registration["state"], registration["version"]) == ("verified", 5)
    withdraw(setup, filed)
    after = tasks(setup)["registration"]
    assert (after["state"], after["version"]) == ("needs_review", 6)
    assert after["data"]["history"][-1]["note"] == "Evidence withdrawn: Filing receipt"


@pytest.mark.parametrize("reopen_by", ["manual", "withdrawal"])
def test_evidence_from_an_earlier_cycle_does_not_reopen(setup, reopen_by):
    old, extra, new = source(setup, "Old cycle"), source(setup, "Old extra"), source(setup, "New cycle")
    t = move(setup, tasks(setup)["jurisdiction"], "prepared", [old])
    t = move(setup, t, "approved", [old])
    t = move(setup, t, "verified", [old, extra])
    if reopen_by == "manual":
        t = move(setup, t, "needs_review")
    else:
        # Reopened by invalidation: that history entry carries no evidence_ids.
        withdraw(setup, extra)
        t = tasks(setup)["jurisdiction"]
        assert "evidence_ids" not in t["data"]["history"][-1]
    assert (t["state"], t["version"]) == ("needs_review", 5)
    t = move(setup, t, "prepared", [new])
    t = move(setup, t, "approved", [new])
    t = move(setup, t, "verified", [new])
    assert (t["state"], t["version"]) == ("verified", 8)
    withdraw(setup, old)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("verified", 8)


def test_evidence_at_a_replaced_preparation_does_not_reopen(setup):
    first, second = source(setup, "First draft"), source(setup, "Second draft")
    t = move(setup, tasks(setup)["jurisdiction"], "prepared", [first])
    t = move(setup, t, "in_progress")
    t = move(setup, t, "prepared", [second])
    t = move(setup, t, "approved", [second])
    t = move(setup, t, "verified", [second])
    assert (t["state"], t["version"]) == ("verified", 6)
    withdraw(setup, first)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("verified", 6)


def test_evidence_at_the_surviving_preparation_reopens(setup):
    prepared, approval, check = (
        source(setup, "Preparation source"),
        source(setup, "Approval source"),
        source(setup, "Verification source"),
    )
    t = move(setup, tasks(setup)["jurisdiction"], "prepared", [prepared])
    t = move(setup, t, "approved", [approval])
    t = move(setup, t, "verified", [check])
    assert (t["state"], t["version"]) == ("verified", 4)
    withdraw(setup, prepared)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("needs_review", 5)
    assert after["data"]["history"][-1]["note"] == "Evidence withdrawn: Preparation source"


def test_withdrawing_not_applicable_evidence_reopens(setup):
    basis = source(setup, "Not applicable basis")
    t = move(setup, tasks(setup)["jurisdiction"], "not_applicable", [basis])
    assert (t["state"], t["version"]) == ("not_applicable", 2)
    withdraw(setup, basis)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("needs_review", 3)
    assert after["data"]["history"][-1]["note"] == "Evidence withdrawn: Not applicable basis"


def test_withdrawing_unused_evidence_changes_no_task(setup):
    chain(setup)
    unused = source(setup, "Unused source")
    before = standing(setup)
    assert before["structure"] == ("verified", 4)
    assert before["registration"] == ("verified", 5)
    withdraw(setup, unused)
    assert standing(setup) == before


def test_reopening_withdrawal_keeps_audit_valid_and_replays_idempotently(setup):
    _, service, actor, bid, _ = setup
    a, *_ = chain(setup)
    replay_key = key()
    first = withdraw(setup, a, replay_key)
    assert first["state"] == "rejected"
    audit = service.audit(actor, bid)
    assert audit["integrity"]["valid"]
    snapshot = service.snapshot(actor, bid)
    events = len(audit["events"])
    assert withdraw(setup, a, replay_key) == first
    assert service.snapshot(actor, bid)["objects"] == snapshot["objects"]
    replayed = service.audit(actor, bid)
    assert len(replayed["events"]) == events
    assert replayed["integrity"]["valid"]
    reopened = [
        e
        for e in replayed["events"]
        if (e["payload"].get("data", {}).get("history") or [{}])[-1].get("note")
        == "Evidence withdrawn: Source A"
    ]
    # Exactly one event per reopened task: structure and its completed dependent.
    assert sorted(e["payload"]["data"]["key"] for e in reopened) == ["registration", "structure"]
