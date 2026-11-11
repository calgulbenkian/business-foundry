"""Withdrawing evidence reopens completed tasks whose current standing relied on it.

The basis of a completed task is read from its history: within the current cycle (after the most
recent move to needs_review), the most recent preparation and every later step. Expected values in
these tests are written out by hand.
"""

from uuid import uuid4

import pytest
from conftest import reviewed

from foundry import models
from foundry.service import evidence_basis

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


def rewrite(setup, task, state, data):
    """Store a task in `state` with hand-written data, bypassing transitions. Ordinary transitions
    never produce these histories; the cases stand in for older or damaged stored workspaces."""
    store, _, actor, _, _ = setup
    with store.write() as con:
        return store.update(con, task, {**task["data"], **data}, state, actor, task["version"])


def incomplete_histories(state, current):
    """Four stored histories from which the history-derived basis cannot recover `current`."""
    return {
        "empty": [],
        "entries_without_evidence_ids": [
            {"from": "pending", "to": "prepared", "note": "Synthetic.", "actor_id": "x", "at": "t"},
            {"from": "prepared", "to": state, "note": "Synthetic.", "actor_id": "x", "at": "t"},
        ],
        "missing_preparation": [
            {"from": "pending", "to": "in_progress", "evidence_ids": [current], "actor_id": "x", "at": "t"},
            {"from": "in_progress", "to": state, "evidence_ids": [], "actor_id": "x", "at": "t"},
        ],
        "only_reopening": [
            {"from": state, "to": "needs_review", "note": "Synthetic reopen.", "actor_id": "x", "at": "t"},
        ],
    }


@pytest.mark.parametrize(
    "shape", ["empty", "entries_without_evidence_ids", "missing_preparation", "only_reopening"]
)
@pytest.mark.parametrize("state", ["approved", "submitted", "verified", "not_applicable"])
def test_currently_attached_evidence_reopens_despite_incomplete_history(setup, state, shape):
    x = source(setup, "Attached source")
    t = tasks(setup)["jurisdiction"]
    assert (t["state"], t["version"]) == ("pending", 1)
    history = incomplete_histories(state, x["id"])[shape]
    t = rewrite(setup, t, state, {"evidence_ids": [x["id"]], "history": history})
    assert (t["state"], t["version"]) == (state, 2)
    withdraw(setup, x)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("needs_review", 3)
    assert after["data"]["history"][-1]["note"] == "Evidence withdrawn: Attached source"


def test_evidence_from_before_a_manual_reopen_still_reopens_through_a_record(setup):
    """The earlier-round exclusion covers only the step's own history. Evidence that still supports
    a record of a kind the step requires reopens it through that record."""
    _, service, actor, bid, _ = setup
    a, b = source(setup, "Source A"), source(setup, "Source B")
    service.add_record(
        actor,
        bid,
        key(),
        dump(models.RecordCreate, kind="decision", title="Entity decision", evidence_ids=[a["id"]]),
    )
    t = move(setup, tasks(setup)["structure"], "prepared", [a])
    t = move(setup, t, "approved", [a])
    t = move(setup, t, "verified", [a])
    t = move(setup, t, "needs_review")
    t = move(setup, t, "prepared", [b])
    t = move(setup, t, "approved", [b])
    t = move(setup, t, "verified", [b])
    assert (t["state"], t["version"]) == ("verified", 8)
    withdraw(setup, a)
    after = tasks(setup)["structure"]
    assert (after["state"], after["version"]) == ("needs_review", 9)
    assert after["data"]["history"][-1]["note"] == "Evidence withdrawn: Source A"


def test_withdrawing_preparation_evidence_reopens_step_resting_in_approved(setup):
    prepared, approval = source(setup, "Preparation source"), source(setup, "Approval source")
    t = move(setup, tasks(setup)["jurisdiction"], "prepared", [prepared])
    t = move(setup, t, "approved", [approval])
    assert (t["state"], t["version"]) == ("approved", 3)
    withdraw(setup, prepared)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("needs_review", 4)
    assert after["data"]["history"][-1]["note"] == "Evidence withdrawn: Preparation source"


def test_withdrawing_approval_evidence_reopens_step_resting_in_submitted(setup):
    finish_structure(setup, source(setup, "Structure decision"))
    draft = source(setup, "Registration draft")
    approval = source(setup, "Registration approval")
    filed = source(setup, "Filing receipt", "external_receipt")
    t = move(setup, tasks(setup)["registration"], "prepared", [draft])
    t = move(setup, t, "approved", [approval])
    t = move(setup, t, "submitted", [filed])
    assert (t["state"], t["version"]) == ("submitted", 4)
    # The approval source is cited only at approval: not at preparation, and not attached now.
    assert t["data"]["evidence_ids"] == [filed["id"]]
    assert [e["evidence_ids"] for e in t["data"]["history"]] == [
        [draft["id"]],
        [approval["id"]],
        [filed["id"]],
    ]
    withdraw(setup, approval)
    after = tasks(setup)["registration"]
    assert (after["state"], after["version"]) == ("needs_review", 5)
    assert after["data"]["history"][-1]["note"] == "Evidence withdrawn: Registration approval"


def test_preparation_citing_no_evidence_keeps_later_evidence_in_the_basis(setup):
    approval, check = source(setup, "Approval source"), source(setup, "Verification source")
    t = move(setup, tasks(setup)["jurisdiction"], "prepared")
    assert t["data"]["history"][-1]["evidence_ids"] == []
    t = move(setup, t, "approved", [approval])
    t = move(setup, t, "verified", [check])
    assert (t["state"], t["version"]) == ("verified", 4)
    withdraw(setup, approval)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("needs_review", 5)
    assert after["data"]["history"][-1]["note"] == "Evidence withdrawn: Approval source"


def test_preparation_citing_no_evidence_replaces_earlier_citations(setup):
    early, check = source(setup, "Early source"), source(setup, "Verification source")
    t = move(setup, tasks(setup)["jurisdiction"], "in_progress", [early])
    t = move(setup, t, "prepared")
    t = move(setup, t, "approved", [check])
    t = move(setup, t, "verified", [check])
    assert (t["state"], t["version"]) == ("verified", 5)
    withdraw(setup, early)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("verified", 5)


# Damaged stored history. Ordinary transitions never write these shapes; they stand in for direct
# database edits or a defect elsewhere. Reading rule: anything unreadable contributes nothing to the
# history-derived basis and is otherwise ignored, so it can never stop a withdrawal.

COMPLETED_STATES = ["approved", "submitted", "verified", "not_applicable"]

NON_LIST_HISTORIES = {
    "history_null": None,
    "history_string": "not a history",
    "history_number": 7,
    "history_object": {"to": "verified", "evidence_ids": ["evidence_x"]},
}


def damaged_entries(state):
    """Single history entries that cannot be read, keyed by shape."""
    return {
        "entry_null": None,
        "entry_string": "verified",
        "entry_number": 3,
        "entry_list": [{"to": state, "evidence_ids": []}],
        "to_missing": {"from": "pending", "evidence_ids": []},
        "to_null": {"from": "pending", "to": None, "evidence_ids": []},
        "to_number": {"from": "pending", "to": 4, "evidence_ids": []},
        "to_list": {"from": "pending", "to": ["needs_review"], "evidence_ids": []},
        "ids_missing": {"from": "pending", "to": state},
        "ids_null": {"from": "pending", "to": state, "evidence_ids": None},
        "ids_string": {"from": "pending", "to": state, "evidence_ids": "evidence_x"},
        "ids_number": {"from": "pending", "to": state, "evidence_ids": 5},
        "ids_object": {"from": "pending", "to": state, "evidence_ids": {"evidence_x": 1}},
        "id_number": {"from": "pending", "to": state, "evidence_ids": [1]},
        "id_null": {"from": "pending", "to": state, "evidence_ids": [None]},
        "id_list": {"from": "pending", "to": state, "evidence_ids": [["evidence_x"]]},
        "id_object": {"from": "pending", "to": state, "evidence_ids": [{"id": "evidence_x"}]},
        "ids_mixed": {"from": "pending", "to": state, "evidence_ids": ["evidence_x", 1, None, [], {}]},
    }


ENTRY_SHAPES = list(damaged_entries("verified"))


def preparation(evidence_id):
    return {"from": "pending", "to": "prepared", "evidence_ids": [evidence_id], "actor_id": "x", "at": "t"}


@pytest.mark.parametrize("placement", ["alone", "after_preparation", "before_preparation"])
@pytest.mark.parametrize("shape", ENTRY_SHAPES)
@pytest.mark.parametrize("state", COMPLETED_STATES)
def test_damaged_entry_does_not_stop_withdrawal_of_attached_evidence(setup, state, shape, placement):
    x = source(setup, "Attached source")
    other = source(setup, "Other source")
    t = tasks(setup)["jurisdiction"]
    assert (t["state"], t["version"]) == ("pending", 1)
    damaged = damaged_entries(state)[shape]
    history = {
        "alone": [damaged],
        "after_preparation": [preparation(other["id"]), damaged],
        "before_preparation": [damaged, preparation(other["id"])],
    }[placement]
    t = rewrite(setup, t, state, {"evidence_ids": [x["id"]], "history": history})
    assert (t["state"], t["version"]) == (state, 2)
    withdraw(setup, x)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("needs_review", 3)
    assert after["data"]["history"][:-1] == history
    newest = after["data"]["history"][-1]
    assert (newest["from"], newest["to"], newest["note"]) == (
        state,
        "needs_review",
        "Evidence withdrawn: Attached source",
    )


def test_mixed_type_ids_in_history_do_not_stop_withdrawal(setup):
    """Finding 2: a basis holding a string id and an integer must not be ordered."""
    x = source(setup, "Attached source")
    t = tasks(setup)["jurisdiction"]
    history = [{"to": "verified", "evidence_ids": [x["id"], 1]}]
    t = rewrite(setup, t, "verified", {"evidence_ids": [x["id"]], "history": history})
    assert (t["state"], t["version"]) == ("verified", 2)
    withdraw(setup, x)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("needs_review", 3)
    assert after["data"]["history"][-1]["note"] == "Evidence withdrawn: Attached source"


def test_string_id_beside_unreadable_ids_in_history_still_counts(setup):
    """Only the string items of an entry's evidence_ids count; the rest are skipped, not fatal."""
    x, y = source(setup, "Attached source"), source(setup, "History-only source")
    t = tasks(setup)["jurisdiction"]
    history = [
        {"from": "pending", "to": "prepared", "evidence_ids": [None, y["id"], 2, ["z"]]},
        {"from": "prepared", "to": "verified", "evidence_ids": [x["id"]]},
    ]
    t = rewrite(setup, t, "verified", {"evidence_ids": [x["id"]], "history": history})
    assert (t["state"], t["version"]) == ("verified", 2)
    withdraw(setup, y)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("needs_review", 3)
    assert after["data"]["history"][-1]["note"] == "Evidence withdrawn: History-only source"


def unrelated_damage(state):
    return {
        **{name: history for name, history in NON_LIST_HISTORIES.items()},
        **{f"{name}_in_list": [entry] for name, entry in damaged_entries(state).items()},
    }


UNRELATED_SHAPES = list(unrelated_damage("verified"))


@pytest.mark.parametrize("shape", UNRELATED_SHAPES)
@pytest.mark.parametrize("state", COMPLETED_STATES)
def test_damaged_unrelated_task_does_not_block_withdrawal(setup, state, shape):
    x = source(setup, "Relied-on source")
    unrelated = source(setup, "Unrelated source")
    t = move(setup, tasks(setup)["jurisdiction"], "prepared", [x])
    t = move(setup, t, "approved", [x])
    t = move(setup, t, "verified", [x])
    assert (t["state"], t["version"]) == ("verified", 4)
    damaged = tasks(setup)["demand"]
    assert (damaged["state"], damaged["version"]) == ("pending", 1)
    history = unrelated_damage(state)[shape]
    damaged = rewrite(setup, damaged, state, {"evidence_ids": [unrelated["id"]], "history": history})
    assert (damaged["state"], damaged["version"]) == (state, 2)
    withdraw(setup, x)
    after = tasks(setup)
    assert (after["jurisdiction"]["state"], after["jurisdiction"]["version"]) == ("needs_review", 5)
    assert after["jurisdiction"]["data"]["history"][-1]["note"] == "Evidence withdrawn: Relied-on source"
    assert (after["demand"]["state"], after["demand"]["version"]) == (state, 2)
    assert after["demand"]["data"]["history"] == history


@pytest.mark.parametrize("shape", ENTRY_SHAPES)
@pytest.mark.parametrize("withdrawn", ["preparation", "approval"])
def test_readable_history_around_a_damaged_entry_still_counts(setup, shape, withdrawn):
    """Evidence cited only in history (not attached now) still reopens the step when a damaged entry
    sits between readable ones."""
    prepared, approval, check = (
        source(setup, "Preparation source"),
        source(setup, "Approval source"),
        source(setup, "Verification source"),
    )
    history = [
        {"from": "pending", "to": "prepared", "evidence_ids": [prepared["id"]], "actor_id": "x", "at": "t"},
        damaged_entries("approved")[shape],
        {"from": "prepared", "to": "approved", "evidence_ids": [approval["id"]], "actor_id": "x", "at": "t"},
        {"from": "approved", "to": "verified", "evidence_ids": [check["id"]], "actor_id": "x", "at": "t"},
    ]
    t = rewrite(
        setup, tasks(setup)["jurisdiction"], "verified", {"evidence_ids": [check["id"]], "history": history}
    )
    assert (t["state"], t["version"]) == ("verified", 2)
    target = prepared if withdrawn == "preparation" else approval
    withdraw(setup, target)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("needs_review", 3)
    assert after["data"]["history"][-1]["note"] == f"Evidence withdrawn: {target['title']}"


def test_damaged_entry_does_not_count_as_a_reopening(setup):
    """Only an object whose "to" is exactly "needs_review" starts a new cycle."""
    prepared, check = source(setup, "Preparation source"), source(setup, "Verification source")
    history = [
        {"from": "pending", "to": "prepared", "evidence_ids": [prepared["id"]]},
        {"from": "prepared", "to": ["needs_review"], "evidence_ids": []},
        "needs_review",
        {"from": "prepared", "to": "verified", "evidence_ids": [check["id"]]},
    ]
    t = rewrite(
        setup, tasks(setup)["jurisdiction"], "verified", {"evidence_ids": [check["id"]], "history": history}
    )
    assert (t["state"], t["version"]) == ("verified", 2)
    withdraw(setup, prepared)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("needs_review", 3)


def test_damaged_entry_does_not_count_as_a_preparation(setup):
    """A damaged entry cannot replace the surviving preparation; evidence cited there still counts."""
    prepared, check = source(setup, "Preparation source"), source(setup, "Verification source")
    history = [
        {"from": "pending", "to": "prepared", "evidence_ids": [prepared["id"]]},
        {"from": "prepared", "to": ["prepared"], "evidence_ids": []},
        "prepared",
        {"from": "prepared", "to": "verified", "evidence_ids": [check["id"]]},
    ]
    t = rewrite(
        setup, tasks(setup)["jurisdiction"], "verified", {"evidence_ids": [check["id"]], "history": history}
    )
    assert (t["state"], t["version"]) == ("verified", 2)
    withdraw(setup, prepared)
    after = tasks(setup)["jurisdiction"]
    assert (after["state"], after["version"]) == ("needs_review", 3)


@pytest.mark.parametrize("shape", list(NON_LIST_HISTORIES))
def test_history_that_is_not_a_list_has_an_empty_basis(shape):
    assert evidence_basis(NON_LIST_HISTORIES[shape]) == set()


def test_basis_reads_only_string_ids_from_readable_entries():
    history = [
        {"to": "verified", "evidence_ids": ["old"]},
        {"to": "needs_review"},
        {"to": ["needs_review"], "evidence_ids": ["a"]},
        None,
        {"to": "prepared", "evidence_ids": ["b", 1, None, ["c"], {"d": 1}]},
        "prepared",
        {"to": "approved", "evidence_ids": "e"},
        {"to": "verified", "evidence_ids": ["f"]},
        {"evidence_ids": ["g"]},
    ]
    assert evidence_basis(history) == {"b", "f", "g"}
    # With no readable preparation in the cycle, only the last entry counts, and here it is unreadable.
    assert evidence_basis([{"to": "approved", "evidence_ids": ["a"]}, None]) == set()
    assert evidence_basis([{"to": "approved", "evidence_ids": ["a"]}, {"to": 1, "evidence_ids": ["h"]}]) == {
        "h"
    }
