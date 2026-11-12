"""Changing the planning jurisdiction: a reviewed, recorded human decision.

The change supersedes the current package's steps (kept, with history, in a terminal state), adds the new
package's steps as new pending steps, and reopens completed work. Expected step lists are written out by
hand below; they are deliberately not derived from the catalog at run time.
"""

import asyncio
import hashlib
import json
import os
import sys
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp import foundry_api

from foundry import catalog, cli, models
from foundry import service as service_module
from foundry.api import create_app
from foundry.demo import seed
from foundry.documents import TITLES
from foundry.service import Service
from foundry.store import DomainError, Store

REASON = "Founders confirmed where they will operate; adviser review is recorded."

# The sixteen base steps every business has, by catalog key.
BASE_KEYS = {
    "demand",
    "offer",
    "economics",
    "structure",
    "founders",
    "rights",
    "registration",
    "tax",
    "bank",
    "permissions",
    "delivery",
    "agreement",
    "acceptance",
    "invoice",
    "collection",
    "close",
}

# Each jurisdiction's own step: (key, title, depends on the base structure decision).
PACKAGE = {
    "undecided": ("jurisdiction", "Resolve operating and founder jurisdictions", False),
    "other": ("jurisdiction", "Obtain a local requirements review", False),
    "US-DE": ("local_review", "Review Delaware and operating-state requirements", True),
    "GB": ("local_review", "Review UK director, registration and reporting duties", True),
    "SE": ("local_review", "Review Swedish company and tax setup", True),
}

# The fields a stored step had before this change. The change adds none.
STEP_FIELDS = {
    "key",
    "category",
    "description",
    "depends_on_keys",
    "required_record_kinds",
    "external",
    "title",
    "source_uri",
    "pack_version",
    "source_checked_on",
    "applicability",
    "owner",
    "evidence_ids",
    "history",
    "depends_on",
    "deal_id",
}


def dump(cls, **kwargs):
    return cls(**kwargs).model_dump(mode="json")


def key():
    return str(uuid4())


def make_business(service, actor, jurisdiction="undecided", route="start"):
    return service.create_business(
        actor,
        key(),
        dump(
            models.BusinessCreate,
            name="Jurisdiction test",
            concept="A synthetic business for jurisdiction change tests.",
            jurisdiction=jurisdiction,
            route=route,
        ),
    )


def source(service, actor, bid, review=True, source_type="document", title="Synthetic source"):
    evidence = service.add_evidence(
        actor,
        bid,
        key(),
        dump(
            models.EvidenceCreate,
            title=title,
            content="Synthetic source text for a jurisdiction change test.",
            source_type=source_type,
        ),
    )
    if not review:
        return evidence
    return service.review_evidence(
        actor,
        bid,
        evidence["id"],
        key(),
        dump(models.Review, note="Checked the synthetic source text.", expected_version=1),
    )


def withdraw(service, actor, bid, evidence):
    return service.review_evidence(
        actor,
        bid,
        evidence["id"],
        key(),
        dump(models.Review, note="Synthetic source withdrawn.", expected_version=evidence["version"]),
        True,
    )


def move(service, actor, bid, task, state, evidence=()):
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


def all_tasks(service, actor, bid):
    return {o["id"]: o for o in service.snapshot(actor, bid)["objects"] if o["kind"] == "task"}


def live_core(service, actor, bid):
    """Live, non-deal steps by catalog key."""
    return {
        t["data"]["key"]: t
        for t in all_tasks(service, actor, bid).values()
        if not t["data"]["deal_id"] and t["state"] != "superseded"
    }


def deal_steps(service, actor, bid):
    return {t["id"]: t for t in all_tasks(service, actor, bid).values() if t["data"]["deal_id"]}


def change(service, actor, bid, to, reason=REASON, evidence=(), version=None, command=None):
    if version is None:
        version = service.snapshot(actor, bid)["business"]["version"]
    return service.change_jurisdiction(
        actor,
        bid,
        command or key(),
        dump(
            models.JurisdictionChange,
            expected_version=version,
            jurisdiction=to,
            reason=reason,
            evidence_ids=[e["id"] for e in evidence],
        ),
    )


def raw(store):
    """Every stored row that a change could touch, exactly as stored."""
    with store.read() as con:
        return {
            table: [tuple(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY {order}")]
            for table, order in (
                ("businesses", "id"),
                ("objects", "id"),
                ("revisions", "object_id, version"),
                ("events", "seq"),
                ("commands", "principal_id, command_key"),
                ("payment_references", "business_id, reference"),
            )
        }


def last_seq(store):
    with store.read() as con:
        return con.execute("SELECT max(seq) FROM events").fetchone()[0]


def without_ids(result):
    return {
        **{k: v for k, v in result.items() if k not in {"business", "business_version"}},
        "added": [{k: v for k, v in a.items() if k != "id"} for a in result["added"]],
    }


# Named changes with their full effect.


def test_undecided_to_gb_supersedes_adds_reopens_and_marks_documents_stale(setup):
    store, service, owner, _, _ = setup
    bid = seed(service, owner)["id"]
    snapshot = service.snapshot(owner, bid)
    receipt = next(o for o in snapshot["objects"] if o["kind"] == "evidence")
    document = next(o for o in snapshot["objects"] if o["kind"] == "document")
    assert not document["stale"]
    core, deals = live_core(service, owner, bid), deal_steps(service, owner, bid)
    assert set(core) == BASE_KEYS | {"jurisdiction"}
    assert {t["state"] for t in core.values()} == {"verified"}
    assert len(deals) == 7 and {t["state"] for t in deals.values()} == {"pending"}
    assert snapshot["stats"]["tasks"] == 24 and snapshot["stats"]["verified"] == 17
    seq = last_seq(store)

    result = change(service, owner, bid, "GB", evidence=[receipt])

    old = core["jurisdiction"]
    assert result["business_version"] == 2
    assert result["business"]["version"] == 2 and result["business"]["data"]["jurisdiction"] == "GB"
    assert (result["from"], result["to"]) == ("undecided", "GB")
    assert result["superseded"] == [
        {
            "id": old["id"],
            "key": "jurisdiction",
            "title": "Resolve operating and founder jurisdictions",
            "state": "verified",
        }
    ]
    [added] = result["added"]
    assert added == {
        "id": added["id"],
        "key": "local_review",
        "title": "Review UK director, registration and reporting duties",
        "depends_on": [core["structure"]["id"]],
    }
    assert sorted(r["key"] for r in result["reopened"]) == sorted(BASE_KEYS)
    assert {r["id"]: r["state"] for r in result["reopened"]} == {core[k]["id"]: "verified" for k in BASE_KEYS}

    after = live_core(service, owner, bid)
    assert set(after) == BASE_KEYS | {"local_review"}
    for k in BASE_KEYS:
        assert after[k]["id"] == core[k]["id"]
        assert after[k]["state"] == "needs_review"
        assert after[k]["version"] == core[k]["version"] + 1
        assert after[k]["data"]["history"][:-1] == core[k]["data"]["history"]
        entry = after[k]["data"]["history"][-1]
        assert {k2: entry[k2] for k2 in ("from", "to", "note", "actor_id")} == {
            "from": "verified",
            "to": "needs_review",
            "note": "Jurisdiction changed from undecided to GB",
            "actor_id": owner["id"],
        }
    new = after["local_review"]
    assert new["id"] == added["id"] and new["state"] == "pending" and new["version"] == 1
    assert new["data"]["depends_on"] == [core["structure"]["id"]]
    assert new["data"]["deal_id"] is None and new["data"]["history"] == []
    assert new["blockers"] == ["Complete: Decide the entity and ownership structure"]
    assert deal_steps(service, owner, bid) == deals

    superseded = all_tasks(service, owner, bid)[old["id"]]
    assert superseded["state"] == "superseded" and superseded["version"] == old["version"] + 1
    assert superseded["data"]["history"][:-1] == old["data"]["history"]
    entry = superseded["data"]["history"][-1]
    assert {k: v for k, v in entry.items() if k != "at"} == {
        "from": "verified",
        "to": "superseded",
        "note": "Jurisdiction changed from undecided to GB. "
        "This step belongs to the earlier planning guide and is kept as history.",
        "jurisdiction_from": "undecided",
        "jurisdiction_to": "GB",
        "actor_id": owner["id"],
    }
    assert superseded["blockers"] == []

    snapshot = service.snapshot(owner, bid)
    assert snapshot["stats"]["tasks"] == 24  # 16 base + 1 new local review + 7 deal steps
    assert snapshot["stats"]["superseded"] == 1
    assert snapshot["stats"]["verified"] == 0 and snapshot["stats"]["needs_review"] == 16
    stale = next(o for o in snapshot["objects"] if o["id"] == document["id"])
    assert stale["stale"] and stale["data"]["content"] == document["data"]["content"]

    events = [e for e in service.audit(owner, bid)["events"] if e["seq"] > seq]
    assert [e["action"] for e in events] == (
        ["business.updated", "task.superseded", "task.created"]
        + ["task.updated"] * 16
        + ["business.jurisdiction_changed"]
    )
    assert {e["actor_id"] for e in events} == {owner["id"]}
    assert events[0]["payload"] == result["business"]
    assert events[1]["payload"] == {k: v for k, v in superseded.items() if k != "blockers"}
    decision = events[-1]["payload"]
    assert decision == {
        "title": "Jurisdiction changed from undecided to GB",
        "reason": REASON,
        "evidence_ids": [receipt["id"]],
        "actor_id": owner["id"],
        "actor_name": "Local owner",
        "business_version": 2,
        "from": "undecided",
        "to": "GB",
        "superseded": result["superseded"],
        "added": result["added"],
        "reopened": result["reopened"],
    }
    integrity = service.audit(owner, bid)["integrity"]
    assert integrity["valid"] and integrity["record_errors"] == []


def gb_with_completed_review(service, owner):
    """A GB business whose structure decision is not applicable and whose local review is verified."""
    bid = make_business(service, owner, "GB")["id"]
    evidence = source(service, owner, bid)
    core = live_core(service, owner, bid)
    move(service, owner, bid, core["structure"], "not_applicable", [evidence])
    local = core["local_review"]
    for state in ("prepared", "approved", "verified"):
        local = move(service, owner, bid, local, state, [evidence])
    return bid, evidence


def test_gb_to_se_supersedes_the_verified_local_review_and_reopens_structure(setup):
    _, service, owner, _, _ = setup
    bid, _ = gb_with_completed_review(service, owner)
    core = live_core(service, owner, bid)
    assert core["local_review"]["state"] == "verified"
    assert service.snapshot(owner, bid)["stats"]["verified"] == 1

    result = change(service, owner, bid, "SE")

    old = core["local_review"]
    assert result["superseded"] == [
        {
            "id": old["id"],
            "key": "local_review",
            "title": "Review UK director, registration and reporting duties",
            "state": "verified",
        }
    ]
    assert result["reopened"] == [
        {
            "id": core["structure"]["id"],
            "key": "structure",
            "title": "Decide the entity and ownership structure",
            "state": "not_applicable",
        }
    ]
    [added] = result["added"]
    assert added == {
        "id": added["id"],
        "key": "local_review",
        "title": "Review Swedish company and tax setup",
        "depends_on": [core["structure"]["id"]],
    }
    after = live_core(service, owner, bid)
    assert set(after) == BASE_KEYS | {"local_review"}
    assert after["local_review"]["id"] == added["id"] != old["id"]
    assert after["local_review"]["data"]["category"] == "Setup"
    assert after["structure"]["state"] == "needs_review"
    assert after["structure"]["data"]["history"][-1]["note"] == "Jurisdiction changed from GB to SE"
    superseded = all_tasks(service, owner, bid)[old["id"]]
    assert superseded["state"] == "superseded"
    assert [h["to"] for h in superseded["data"]["history"]] == [
        "prepared",
        "approved",
        "verified",
        "superseded",
    ]
    assert superseded["data"]["history"][-1]["jurisdiction_from"] == "GB"
    assert superseded["data"]["history"][-1]["jurisdiction_to"] == "SE"
    # Its prerequisite (structure) is open again, but a superseded step is not open work: no blockers.
    assert superseded["data"]["depends_on"] == [core["structure"]["id"]]
    assert superseded["blockers"] == []
    stats = service.snapshot(owner, bid)["stats"]
    assert (stats["tasks"], stats["superseded"], stats["verified"], stats["needs_review"]) == (17, 1, 0, 1)
    assert service.audit(owner, bid)["integrity"]["valid"]


def test_gb_to_undecided_restores_the_resolution_step_as_new_work(setup):
    _, service, owner, _, _ = setup
    bid, _ = gb_with_completed_review(service, owner)
    core = live_core(service, owner, bid)
    result = change(service, owner, bid, "undecided")
    [added] = result["added"]
    assert added == {
        "id": added["id"],
        "key": "jurisdiction",
        "title": "Resolve operating and founder jurisdictions",
        "depends_on": [],
    }
    assert [s["id"] for s in result["superseded"]] == [core["local_review"]["id"]]
    after = live_core(service, owner, bid)
    assert set(after) == BASE_KEYS | {"jurisdiction"}
    assert after["jurisdiction"]["state"] == "pending" and after["jurisdiction"]["blockers"] == []
    assert service.snapshot(owner, bid)["business"]["data"]["jurisdiction"] == "undecided"


def test_undecided_to_other_replaces_one_jurisdiction_step_with_another(setup):
    _, service, owner, bid, _ = setup
    core = live_core(service, owner, bid)
    result = change(service, owner, bid, "other")
    [added] = result["added"]
    assert added == {
        "id": added["id"],
        "key": "jurisdiction",
        "title": "Obtain a local requirements review",
        "depends_on": [],
    }
    assert result["superseded"] == [
        {
            "id": core["jurisdiction"]["id"],
            "key": "jurisdiction",
            "title": "Resolve operating and founder jurisdictions",
            "state": "pending",
        }
    ]
    assert result["reopened"] == []
    after = live_core(service, owner, bid)
    assert after["jurisdiction"]["id"] == added["id"] != core["jurisdiction"]["id"]
    assert all_tasks(service, owner, bid)[core["jurisdiction"]["id"]]["state"] == "superseded"


def test_round_trip_keeps_every_earlier_step_and_adds_fresh_work(setup):
    _, service, owner, bid, _ = setup
    evidence = source(service, owner, bid)
    first = live_core(service, owner, bid)
    original = move(service, owner, bid, first["jurisdiction"], "not_applicable", [evidence])

    there = change(service, owner, bid, "GB")
    gb = live_core(service, owner, bid)["local_review"]
    gb = move(service, owner, bid, gb, "prepared", [evidence])
    back = change(service, owner, bid, "undecided")

    assert there["superseded"][0]["id"] == original["id"]
    assert back["superseded"] == [
        {
            "id": gb["id"],
            "key": "local_review",
            "title": "Review UK director, registration and reporting duties",
            "state": "prepared",
        }
    ]
    [fresh] = back["added"]
    assert fresh["key"] == "jurisdiction" and fresh["id"] not in {original["id"], gb["id"]}
    tasks = all_tasks(service, owner, bid)
    assert len(tasks) == 19
    assert tasks[original["id"]]["state"] == "superseded"
    assert tasks[original["id"]]["version"] == original["version"] + 1  # untouched by the second change
    assert [h["to"] for h in tasks[original["id"]]["data"]["history"]] == ["not_applicable", "superseded"]
    assert tasks[gb["id"]]["state"] == "superseded"
    assert tasks[fresh["id"]]["state"] == "pending"
    after = live_core(service, owner, bid)
    assert set(after) == BASE_KEYS | {"jurisdiction"}
    assert {k: after[k]["id"] for k in BASE_KEYS} == {k: first[k]["id"] for k in BASE_KEYS}
    snapshot = service.snapshot(owner, bid)
    assert snapshot["business"]["version"] == 3
    assert (snapshot["stats"]["tasks"], snapshot["stats"]["superseded"]) == (17, 2)
    assert service.audit(owner, bid)["integrity"]["valid"]


PAIRS = [(a, b) for a in PACKAGE for b in PACKAGE if a != b]


@pytest.mark.parametrize(("start", "to"), PAIRS)
def test_every_change_between_known_jurisdictions_is_allowed(setup, start, to):
    _, service, owner, _, _ = setup
    bid = make_business(service, owner, start)["id"]
    before = live_core(service, owner, bid)
    old_key, old_title, _ = PACKAGE[start]
    new_key, new_title, needs_structure = PACKAGE[to]
    assert set(before) == BASE_KEYS | {old_key} and before[old_key]["title"] == old_title

    result = change(service, owner, bid, to)

    assert (result["from"], result["to"], result["business_version"]) == (start, to, 2)
    assert result["superseded"] == [
        {"id": before[old_key]["id"], "key": old_key, "title": old_title, "state": "pending"}
    ]
    assert result["reopened"] == []
    expected_dependencies = [before["structure"]["id"]] if needs_structure else []
    [added] = result["added"]
    assert added == {
        "id": added["id"],
        "key": new_key,
        "title": new_title,
        "depends_on": expected_dependencies,
    }
    after = live_core(service, owner, bid)
    assert set(after) == BASE_KEYS | {new_key}
    for k in BASE_KEYS:
        assert after[k] == before[k]
    assert after[new_key]["id"] == added["id"] != before[old_key]["id"]
    assert after[new_key]["data"]["depends_on"] == expected_dependencies
    assert (after[new_key]["state"], after[new_key]["version"]) == ("pending", 1)
    assert all_tasks(service, owner, bid)[before[old_key]["id"]]["state"] == "superseded"
    stats = service.snapshot(owner, bid)["stats"]
    assert (stats["tasks"], stats["superseded"]) == (17, 1)


@pytest.mark.parametrize("current", list(PACKAGE))
def test_changing_to_the_current_jurisdiction_is_refused(setup, current):
    store, service, owner, _, _ = setup
    bid = make_business(service, owner, current)["id"]
    before = raw(store)
    with pytest.raises(DomainError, match=f"already {current}") as exc:
        change(service, owner, bid, current)
    assert exc.value.status == 422
    with pytest.raises(DomainError, match=f"already {current}"):
        service.preview_jurisdiction(owner, bid, current)
    assert raw(store) == before


def test_package_keys_never_name_a_base_step():
    """Package steps are recognised by key, so no package may reuse a base step's key; a base step could
    otherwise be superseded. This checks the catalog's shape, not an expected step list."""
    assert {spec["key"] for spec in catalog.BASE} == BASE_KEYS
    assert {spec["key"] for specs in catalog.JURISDICTIONS.values() for spec in specs}.isdisjoint(BASE_KEYS)


# Acquisition steps.


def test_acquisition_business_keeps_its_deal_steps_and_reopens_completed_ones(setup):
    _, service, owner, _, _ = setup
    bid = make_business(service, owner, "GB", route="acquire")["id"]
    evidence = source(service, owner, bid)
    deal = service.save_deal(
        owner,
        bid,
        key(),
        dump(models.DealInputs, target="Synthetic target", thesis="A synthetic deal thesis."),
    )
    deals = deal_steps(service, owner, bid)
    assert len(deals) == 7
    mandate = next(t for t in deals.values() if t["data"]["key"] == "mandate")
    for state in ("prepared", "approved", "verified"):
        mandate = move(service, owner, bid, mandate, state, [evidence])
    core = live_core(service, owner, bid)

    result = change(service, owner, bid, "US-DE")

    assert [s["id"] for s in result["superseded"]] == [core["local_review"]["id"]]
    assert result["reopened"] == [
        {
            "id": mandate["id"],
            "key": "mandate",
            "title": "Approve the acquisition thesis",
            "state": "verified",
        }
    ]
    assert result["added"][0]["title"] == "Review Delaware and operating-state requirements"
    assert result["added"][0]["depends_on"] == [core["structure"]["id"]]
    after = deal_steps(service, owner, bid)
    assert set(after) == set(deals)
    assert {t["data"]["deal_id"] for t in after.values()} == {deal["id"]}
    assert {t["state"] for i, t in after.items() if i != mandate["id"]} == {"pending"}
    assert after[mandate["id"]]["state"] == "needs_review"
    assert after[mandate["id"]]["data"]["history"][-1]["note"] == "Jurisdiction changed from GB to US-DE"
    stats = service.snapshot(owner, bid)["stats"]
    assert (stats["tasks"], stats["superseded"]) == (24, 1)


def test_deal_steps_are_never_superseded_even_with_package_keys(setup, monkeypatch):
    """A deal step sharing a package step's key is kept, and new steps depend only on business steps."""
    _, service, owner, _, _ = setup
    monkeypatch.setattr(
        catalog,
        "DEAL_TASKS",
        [
            catalog.task("local_review", "Deal-specific local review", "Acquisition", "Synthetic step."),
            catalog.task("structure", "Deal structure", "Acquisition", "Synthetic step."),
        ],
    )
    bid = make_business(service, owner, "GB", route="acquire")["id"]
    service.save_deal(
        owner,
        bid,
        key(),
        dump(models.DealInputs, target="Synthetic target", thesis="A synthetic deal thesis."),
    )
    deals = deal_steps(service, owner, bid)
    core = live_core(service, owner, bid)
    result = change(service, owner, bid, "SE")
    assert [s["id"] for s in result["superseded"]] == [core["local_review"]["id"]]
    assert result["added"][0]["depends_on"] == [core["structure"]["id"]]
    assert deal_steps(service, owner, bid) == deals


# Prerequisites, reopening and transitions around superseded steps.


def test_a_superseded_prerequisite_never_blocks_or_reopens_a_live_step(setup, monkeypatch):
    """No catalog step depends on a package step today, so this builds one: a base step that follows the
    undecided package's step, which here also requires a risk record."""
    store, service, owner, _, _ = setup
    monkeypatch.setattr(
        catalog,
        "BASE",
        [catalog.task("after", "Follow the jurisdiction step", "Setup", "Synthetic step.", ["jurisdiction"])],
    )
    monkeypatch.setitem(
        catalog.JURISDICTIONS,
        "undecided",
        [
            catalog.task(
                "jurisdiction",
                "Resolve operating and founder jurisdictions",
                "Setup",
                "Synthetic step.",
                records=["risk"],
            )
        ],
    )
    bid = make_business(service, owner, "undecided")["id"]
    old_basis, own_basis = source(service, owner, bid, title="Old basis"), source(service, owner, bid)
    core = live_core(service, owner, bid)
    assert core["after"]["blockers"] == ["Complete: Resolve operating and founder jurisdictions"]
    move(service, owner, bid, core["jurisdiction"], "prepared", [old_basis])

    change(service, owner, bid, "GB")

    after = live_core(service, owner, bid)["after"]
    assert after["blockers"] == []
    for state in ("prepared", "approved", "verified"):
        after = move(service, owner, bid, after, state, [own_basis])
    assert after["state"] == "verified"
    # The superseded step cited the old basis; withdrawing it reopens nothing that only followed that step.
    withdraw(service, owner, bid, old_basis)
    assert live_core(service, owner, bid)["after"]["state"] == "verified"
    # A new record of a kind only the superseded step required reopens nothing that followed it either.
    service.add_record(owner, bid, key(), dump(models.RecordCreate, kind="risk", title="A new risk"))
    assert live_core(service, owner, bid)["after"]["state"] == "verified"


PRIOR_PATHS = {
    "pending": [],
    "in_progress": ["in_progress"],
    "prepared": ["prepared"],
    "approved": ["prepared", "approved"],
    "verified": ["prepared", "approved", "verified"],
    "needs_review": ["in_progress", "needs_review"],
    "not_applicable": ["not_applicable"],
}
TARGETS = ["in_progress", "prepared", "approved", "submitted", "verified", "needs_review", "not_applicable"]


@pytest.mark.parametrize("prior", list(PRIOR_PATHS))
def test_a_superseded_step_never_changes_again(setup, prior):
    store, service, owner, _, _ = setup
    bid = make_business(service, owner, "GB")["id"]
    evidence = source(service, owner, bid)
    receipt = source(service, owner, bid, source_type="external_receipt", title="Receipt")
    core = live_core(service, owner, bid)
    move(service, owner, bid, core["structure"], "not_applicable", [evidence])
    local = core["local_review"]
    for state in PRIOR_PATHS[prior]:
        local = move(service, owner, bid, local, state, [evidence])
    assert local["state"] == prior

    change(service, owner, bid, "SE")
    old = all_tasks(service, owner, bid)[local["id"]]
    assert old["state"] == "superseded" and old["data"]["history"][-1]["from"] == prior

    for target in TARGETS:
        with pytest.raises(DomainError, match="kept as history"):
            move(service, owner, bid, old, target, [receipt])
    # A general business edit reopens every completed live step, and withdrawing evidence the old step
    # cited reopens what relied on it; neither touches a superseded step.
    business = service.snapshot(owner, bid)["business"]
    service.update_business(
        owner,
        bid,
        key(),
        {
            **business["data"],
            "concept": "A revised synthetic concept.",
            "expected_version": business["version"],
        },
    )
    withdraw(service, owner, bid, evidence)
    assert all_tasks(service, owner, bid)[local["id"]] == old
    assert service.audit(owner, bid)["integrity"]["valid"]


def test_general_edit_still_refuses_a_jurisdiction_change_and_points_to_the_action(setup):
    store, service, owner, bid, _ = setup
    business = service.snapshot(owner, bid)["business"]
    before = raw(store)
    with pytest.raises(DomainError, match="jurisdiction action") as exc:
        service.update_business(
            owner, bid, key(), {**business["data"], "jurisdiction": "GB", "expected_version": 1}
        )
    assert "POST /api/businesses/{id}/jurisdiction" in exc.value.message
    assert raw(store) == before


# Preview.


def preview_fixture(service, owner, start):
    bid = make_business(service, owner, start, route="acquire")["id"]
    evidence = source(service, owner, bid)
    service.save_deal(
        owner,
        bid,
        key(),
        dump(models.DealInputs, target="Synthetic target", thesis="A synthetic deal thesis."),
    )
    core = live_core(service, owner, bid)
    move(service, owner, bid, core["structure"], "not_applicable", [evidence])
    move(service, owner, bid, core["demand"], "prepared", [evidence])
    mandate = next(t for t in deal_steps(service, owner, bid).values() if t["data"]["key"] == "mandate")
    for state in ("prepared", "approved", "verified"):
        mandate = move(service, owner, bid, mandate, state, [evidence])
    return bid


@pytest.mark.parametrize(
    ("start", "to"), [("undecided", "GB"), ("GB", "SE"), ("GB", "undecided"), ("undecided", "other")]
)
def test_preview_is_exactly_what_the_change_does(setup, start, to):
    store, service, owner, _, _ = setup
    bid = preview_fixture(service, owner, start)
    before = raw(store)
    preview = service.preview_jurisdiction(owner, bid, to)
    assert raw(store) == before
    assert preview["business_version"] == 1
    assert {r["key"] for r in preview["reopened"]} == {"structure", "mandate"}

    result = change(service, owner, bid, to)

    assert without_ids(result) == {k: v for k, v in preview.items() if k != "business_version"}
    tasks = all_tasks(service, owner, bid)
    assert {s["id"] for s in result["superseded"]} == {
        i for i, t in tasks.items() if t["state"] == "superseded"
    }
    for r in result["reopened"]:
        assert tasks[r["id"]]["state"] == "needs_review"
    for a in result["added"]:
        t = tasks[a["id"]]
        assert (t["data"]["key"], t["title"], t["data"]["depends_on"], t["state"]) == (
            a["key"],
            a["title"],
            a["depends_on"],
            "pending",
        )


def test_preview_is_readable_by_every_key_that_can_read_the_business(setup, client):
    store, service, owner, bid, _ = setup
    other = make_business(service, owner)["id"]
    roles = ["viewer", "agent", "operator", "reviewer"]
    tokens = {
        role: service.add_principal(owner, {"name": role, "role": role, "business_ids": [bid]})["token"]
        for role in roles
    }
    outsider = service.add_principal(owner, {"name": "Elsewhere", "role": "viewer", "business_ids": [other]})
    expected = service.preview_jurisdiction(owner, bid, "SE")
    url = f"/api/businesses/{bid}/jurisdiction/preview"
    for role in roles + ["owner"]:
        auth = {} if role == "owner" else {"Authorization": "Bearer " + tokens[role]}
        response = client.get(url, params={"to": "SE"}, headers=auth)
        assert response.status_code == 200, role
        assert response.json() == expected
    response = client.get(url, params={"to": "SE"}, headers={"Authorization": "Bearer " + outsider["token"]})
    assert response.status_code == 403
    response = client.get(url, params={"to": "FR"})
    assert response.status_code == 422 and response.json()["detail"].startswith("query.to: ")
    response = client.get(url)
    assert response.status_code == 422 and response.json()["detail"].startswith("query.to: ")
    response = client.get(url, params={"to": "undecided"})
    assert response.status_code == 422 and "already undecided" in response.json()["detail"]


# Authority, versions, idempotency and atomicity through the HTTP API.


def change_body(version=1, to="GB", reason=REASON, evidence_ids=()):
    return {
        "expected_version": version,
        "jurisdiction": to,
        "reason": reason,
        "evidence_ids": list(evidence_ids),
    }


def test_only_an_owner_or_reviewer_can_change_jurisdiction_through_the_api(setup):
    store, service, owner, bid, token = setup
    other = make_business(service, owner)["id"]
    tokens = {
        role: service.add_principal(owner, {"name": role, "role": role, "business_ids": [bid]})["token"]
        for role in ["agent", "operator", "viewer", "reviewer"]
    }
    elsewhere = service.add_principal(
        owner, {"name": "Elsewhere", "role": "reviewer", "business_ids": [other]}
    )
    url = f"/api/businesses/{bid}/jurisdiction"
    with TestClient(create_app(store.path, allowed_hosts=["testserver"])) as c:
        for name, credential in [
            ("agent", tokens["agent"]),
            ("operator", tokens["operator"]),
            ("viewer", tokens["viewer"]),
            ("other business reviewer", elsewhere["token"]),
        ]:
            before = raw(store)
            response = c.post(
                url,
                json=change_body(),
                headers={"Authorization": "Bearer " + credential, "Idempotency-Key": "try-" + name},
            )
            assert response.status_code == 403, name
            assert raw(store) == before, name

        response = c.post(
            url,
            json=change_body(),
            headers={"Authorization": "Bearer " + tokens["reviewer"], "Idempotency-Key": "reviewer-change"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["business_version"] == 2 and body["to"] == "GB"
        assert body["added"][0]["title"] == "Review UK director, registration and reporting duties"

        response = c.post(
            url,
            json=change_body(version=2, to="SE"),
            headers={"Authorization": "Bearer " + token, "Idempotency-Key": "owner-change"},
        )
        assert response.status_code == 200 and response.json()["business_version"] == 3
    events = [
        e for e in service.audit(owner, bid)["events"] if e["action"] == "business.jurisdiction_changed"
    ]
    assert [e["actor_id"] for e in events] == [store.authenticate(tokens["reviewer"])["id"], owner["id"]]


def test_api_refuses_stale_versions_and_invalid_requests_without_changes(setup, client):
    store, service, owner, bid, _ = setup
    url = f"/api/businesses/{bid}/jurisdiction"
    before = raw(store)
    for body, status, detail in [
        (change_body(version=2), 409, "The business changed. Refresh before saving."),
        (change_body(reason="   "), 422, "body.reason: "),
        (change_body(reason="x" * 4001), 422, "body.reason: "),
        (change_body(to="FR"), 422, "body.jurisdiction: "),
        ({k: v for k, v in change_body().items() if k != "reason"}, 422, "body.reason: "),
        ({**change_body(), "force": True}, 422, "body: an unexpected field was sent"),
        (change_body(evidence_ids=["evidence_missing"]), 404, "Record not found."),
    ]:
        response = client.post(url, json=body, headers={"Idempotency-Key": key()})
        assert response.status_code == status, body
        assert response.json()["detail"].startswith(detail), body
    assert client.post(url, json=change_body()).status_code == 400
    assert raw(store) == before


def test_replay_returns_the_original_result_and_changes_nothing_further(setup, client):
    store, service, owner, bid, _ = setup
    url = f"/api/businesses/{bid}/jurisdiction"
    headers = {"Idempotency-Key": "jurisdiction-once"}
    first = client.post(url, json=change_body(), headers=headers)
    assert first.status_code == 200
    settled = raw(store)
    replay = client.post(url, json=change_body(), headers=headers)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert raw(store) == settled
    different = client.post(url, json=change_body(reason="A different stated reason."), headers=headers)
    assert different.status_code == 409 and "different request" in different.json()["detail"]
    assert raw(store) == settled
    # After further changes the replay still returns the original result and does nothing.
    change(service, owner, bid, "SE")
    moved_on = raw(store)
    payload = dump(models.JurisdictionChange, expected_version=1, jurisdiction="GB", reason=REASON)
    assert service.change_jurisdiction(owner, bid, "jurisdiction-once", payload) == first.json()
    assert raw(store) == moved_on
    assert service.snapshot(owner, bid)["business"]["data"]["jurisdiction"] == "SE"


REFUSALS = {
    "unknown evidence": 404,
    "unreviewed evidence": 422,
    "withdrawn evidence": 422,
    "evidence of another business": 404,
    "same jurisdiction": 422,
    "unknown jurisdiction": 422,
    "stale version": 409,
    "blank reason": 422,
    "agent": 403,
    "operator": 403,
    "viewer": 403,
}


@pytest.mark.parametrize("refusal", list(REFUSALS))
def test_every_refusal_leaves_the_workspace_byte_for_byte_unchanged(setup, refusal):
    store, service, owner, _, _ = setup
    bid = seed(service, owner)["id"]
    payload = change_body()
    actor = owner
    if refusal == "unknown evidence":
        payload["evidence_ids"] = ["evidence_00000000000000000000"]
    elif refusal == "unreviewed evidence":
        payload["evidence_ids"] = [source(service, owner, bid, review=False)["id"]]
    elif refusal == "withdrawn evidence":
        payload["evidence_ids"] = [withdraw(service, owner, bid, source(service, owner, bid))["id"]]
    elif refusal == "evidence of another business":
        other = make_business(service, owner)["id"]
        payload["evidence_ids"] = [source(service, owner, other)["id"]]
    elif refusal == "same jurisdiction":
        payload["jurisdiction"] = "undecided"
    elif refusal == "unknown jurisdiction":
        payload["jurisdiction"] = "FR"
    elif refusal == "stale version":
        payload["expected_version"] = 2
    elif refusal == "blank reason":
        payload["reason"] = "  \n "
    else:
        principal = service.add_principal(owner, {"name": refusal, "role": refusal, "business_ids": [bid]})
        actor = store.authenticate(principal["token"])
    before = raw(store)
    with pytest.raises(DomainError) as exc:
        service.change_jurisdiction(actor, bid, "the-only-key", payload)
    assert exc.value.status == REFUSALS[refusal]
    assert raw(store) == before
    if actor is owner:
        # The refused request left no idempotency record: the same key now runs a valid change.
        result = service.change_jurisdiction(owner, bid, "the-only-key", change_body())
        assert result["business_version"] == 2


def test_a_failure_part_way_through_rolls_everything_back(setup, monkeypatch):
    store, service, owner, _, _ = setup
    bid = seed(service, owner)["id"]
    before = raw(store)
    calls = []

    def fail(*args):
        calls.append(args)
        raise DomainError("Simulated failure while reopening work.")

    monkeypatch.setattr(service, "_reopen", fail)
    with pytest.raises(DomainError, match="Simulated failure"):
        change(service, owner, bid, "GB")
    assert len(calls) == 1  # the business row and steps had already been written in this transaction
    assert raw(store) == before


# Documents.


def test_documents_after_a_change_name_superseded_steps_only_as_replaced(setup):
    _, service, owner, bid, _ = setup
    old = live_core(service, owner, bid)["jurisdiction"]
    for template in ("readiness_report", "business_plan", "operating_manual"):
        content = service.generate_document(
            owner, bid, key(), dump(models.DocumentCreate, template=template)
        )["data"]["content"]
        assert "- Resolve operating and founder jurisdictions — not started" in content
    change(service, owner, bid, "other")
    snapshot = service.snapshot(owner, bid)
    assert all(o["stale"] for o in snapshot["objects"] if o["kind"] == "document")
    new = live_core(service, owner, bid)["jurisdiction"]
    for template in ("readiness_report", "business_plan", "operating_manual"):
        content = service.generate_document(
            owner, bid, key(), dump(models.DocumentCreate, template=template)
        )["data"]["content"]
        body, index = content.split("## Reference index")
        # The old step is named once in the body, in the replaced line, and counted nowhere.
        named = [line for line in body.splitlines() if "Resolve operating and founder jurisdictions" in line]
        assert len(named) == 1 and body.count("Resolve operating and founder jurisdictions") == 1
        assert named[0].startswith("Replaced steps (no longer part of this plan): ")
        assert f"{snapshot['stats']['tasks']} steps are tracked:" in body
        # Its id is printed only in its own row of the reference index.
        assert old["id"] not in body and index.count(old["id"]) == 1
        row = next(line for line in index.splitlines() if old["id"] in line)
        assert row.startswith("| Resolve operating and founder jurisdictions ")
        assert "- Obtain a local requirements review — not started" in body and new["id"] in index
        assert "Planning jurisdiction: other." in content


WORK_HEADINGS = (
    "## What needs a person now",
    "## What can be worked on next",
    "## What is blocked",
    "## Every step",
    "Unfinished steps:",
)
STATE_COUNTS = {
    "verified": "Verified as done",
    "not_applicable": "Confirmed not applicable",
    "needs_review": "Sent back for review",
}


def test_replaced_steps_after_real_changes_are_named_once_and_counted_nowhere(setup):
    _, service, owner, _, _ = setup
    bid = seed(service, owner)["id"]
    deal = next(o for o in service.snapshot(owner, bid)["objects"] if o["kind"] == "deal")
    # The first change replaces a verified step and reopens every other completed one.
    change(service, owner, bid, "GB")
    gb = live_core(service, owner, bid)["local_review"]
    move(service, owner, bid, gb, "in_progress")
    move(service, owner, bid, all_tasks(service, owner, bid)[gb["id"]], "prepared")
    # The second replaces a prepared step that cites a source no live step cites.
    for to, replaced_titles in (
        ("GB", ["Resolve operating and founder jurisdictions"]),
        ("SE", ["Resolve operating and founder jurisdictions", gb["title"]]),
    ):
        if to == "SE":
            change(service, owner, bid, "SE")
        snapshot = service.snapshot(owner, bid)
        stats, objects = snapshot["stats"], snapshot["objects"]
        replaced = [o for o in objects if o["kind"] == "task" and o["state"] == "superseded"]
        assert [o["title"] for o in replaced] == replaced_titles and stats["superseded"] == len(replaced)
        reopened = [o for o in objects if o["kind"] == "task" and o["state"] == "needs_review"]
        assert len(reopened) == stats["needs_review"] == 16
        for template in TITLES:
            content = document(service, owner, bid, template, deal if template == "investment_memo" else None)
            body, index = content.split("## Reference index")
            lines = body.splitlines()
            work = template in {"readiness_report", "business_plan", "operating_manual"}
            expected = "Replaced steps (no longer part of this plan): " + "; ".join(replaced_titles) + "."
            assert [line for line in lines if line.startswith("Replaced steps")] == (
                [expected] if work else []
            )
            for step in replaced:
                assert body.count(step["title"]) == (1 if work else 0), (template, step["title"])
                assert step["id"] not in body
                assert index.count(step["id"]) == (1 if work else 0)
            assert gb["data"]["source_uri"] not in content or to == "GB"
            if not work:
                continue
            # Every count the document prints agrees with the snapshot's stats.
            assert f"\n{stats['tasks']} steps are tracked: " in body
            for k, label in STATE_COUNTS.items():
                assert (f"\n- {label}: {stats[k]}\n" in body) == bool(stats[k]), (template, k)
            for heading in WORK_HEADINGS:
                for section in body.split(heading)[1:]:
                    section = section.split("\n## ")[0]
                    assert not any(step["title"] in section for step in replaced), (template, heading)
            if template == "readiness_report":
                sent_back = body.split("### Steps sent back for review\n\n")[1].split("\n\n")[0]
                assert sent_back.splitlines() == [
                    f"- {o['title']} — reason: Jurisdiction changed from undecided to GB." for o in reopened
                ]


# Stored data from earlier releases.


def test_steps_stored_before_this_change_need_no_migration(setup):
    store, service, owner, _, _ = setup
    bid = make_business(service, owner, "GB")["id"]
    tasks = all_tasks(service, owner, bid)
    assert all(set(t["data"]) == STEP_FIELDS for t in tasks.values())
    local = next(t for t in tasks.values() if t["data"]["key"] == "local_review")
    # A step written under an older planning-guide version is still recognised by its key.
    with store.write() as con:
        stored = store.get(con, bid, local["id"])
        store.update(con, stored, {**stored["data"], "pack_version": "2026.10.1"}, "pending", owner, 1)
    with store.read() as con:
        assert con.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "1"

    result = change(service, owner, bid, "SE")

    assert [s["id"] for s in result["superseded"]] == [local["id"]]
    after = all_tasks(service, owner, bid)
    assert all(set(t["data"]) == STEP_FIELDS for t in after.values())
    assert after[local["id"]]["data"]["pack_version"] == "2026.10.1"
    assert service.audit(owner, bid)["integrity"]["valid"]


# Command line, verification, backup and restore.


def test_command_line_change_verify_backup_and_restore(tmp_path, capsys):
    db = tmp_path / "private" / "foundry.db"
    cli.main(["--db", str(db), "init"])
    configuration = tmp_path / "business.json"
    configuration.write_text(
        json.dumps({"name": "CLI business", "concept": "A synthetic command-line business."})
    )
    capsys.readouterr()
    cli.main(["--db", str(db), "create", str(configuration)])
    bid = json.loads(capsys.readouterr().out)["id"]
    store = Store(db)
    owner = store.authenticate((db.parent / "owner.key").read_text().strip())
    service = Service(store)
    evidence = source(service, owner, bid)

    before = raw(store)
    cli.main(["--db", str(db), "jurisdiction", bid, "GB", "--preview"])
    preview = json.loads(capsys.readouterr().out)
    assert raw(store) == before
    with pytest.raises(SystemExit):
        cli.main(["--db", str(db), "jurisdiction", bid, "GB"])
    assert "--reason" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["--db", str(db), "jurisdiction", bid, "GB", "--reason", REASON, "--evidence", "evidence_x"])
    assert "Record not found" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["--db", str(db), "jurisdiction", bid, "GB", "--reason", REASON, "--expected-version", "5"])
    assert "Refresh" in capsys.readouterr().err
    assert raw(store) == before

    cli.main(["--db", str(db), "jurisdiction", bid, "GB", "--reason", REASON, "--evidence", evidence["id"]])
    result = json.loads(capsys.readouterr().out)
    assert without_ids(result) == {k: v for k, v in preview.items() if k != "business_version"}
    assert result["business_version"] == 2
    decision = service.audit(owner, bid)["events"][-1]
    assert decision["action"] == "business.jurisdiction_changed"
    assert decision["actor_id"] == owner["id"]
    assert decision["payload"]["evidence_ids"] == [evidence["id"]]
    assert decision["payload"]["reason"] == REASON

    cli.main(["--db", str(db), "verify", bid])
    assert json.loads(capsys.readouterr().out)["valid"] is True

    backup = tmp_path / "backup" / "foundry.db"
    restored = tmp_path / "restored" / "foundry.db"
    cli.main(["--db", str(db), "backup", str(backup)])
    cli.main(["--db", str(restored), "restore", str(backup)])
    capsys.readouterr()
    restored_store = Store(restored)
    restored_service = Service(restored_store)
    restored_owner = restored_store.authenticate((db.parent / "owner.key").read_text().strip())
    snapshot = restored_service.snapshot(restored_owner, bid)
    assert snapshot == service.snapshot(owner, bid)
    assert snapshot["business"]["data"]["jurisdiction"] == "GB"
    assert snapshot["stats"]["superseded"] == 1
    integrity = restored_service.audit(restored_owner, bid)["integrity"]
    assert integrity["valid"] and integrity["record_errors"] == []
    superseded = next(o for o in snapshot["objects"] if o["kind"] == "task" and o["state"] == "superseded")
    with pytest.raises(DomainError, match="kept as history"):
        move(restored_service, restored_owner, bid, superseded, "prepared")


# The agent, through the real MCP adapter.


def test_agent_through_mcp_has_no_jurisdiction_tool_and_cannot_move_superseded_steps(setup):
    store, service, owner, bid, _ = setup
    agent = service.add_principal(
        owner, {"name": "Jurisdiction agent", "role": "agent", "business_ids": [bid]}
    )
    old = live_core(service, owner, bid)["jurisdiction"]
    change(service, owner, bid, "GB")
    superseded = all_tasks(service, owner, bid)[old["id"]]

    async def check(url):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "foundry.cli", "mcp"],
            env={**os.environ, "FOUNDRY_API_URL": url, "FOUNDRY_API_TOKEN": agent["token"]},
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                initialized = await client.initialize()
                assert "planning jurisdiction is a human decision" in initialized.instructions
                names = {t.name for t in (await client.list_tools()).tools}
                assert names == {
                    "list_businesses",
                    "get_business",
                    "add_record",
                    "update_record",
                    "add_evidence",
                    "prepare_task",
                    "build_forecast",
                    "analyze_acquisition",
                    "generate_document",
                }
                body = json.loads(
                    (await client.call_tool("get_business", {"business_id": bid})).content[0].text
                )
                assert body["business"]["data"]["jurisdiction"] == "GB"
                stored = next(o for o in body["objects"] if o["id"] == old["id"])
                assert stored["state"] == "superseded" and stored["blockers"] == []
                assert (body["stats"]["tasks"], body["stats"]["superseded"]) == (17, 1)
                result = await client.call_tool(
                    "prepare_task",
                    {
                        "business_id": bid,
                        "task_id": old["id"],
                        "command_key": "prepare-superseded",
                        "expected_version": superseded["version"],
                        "note": "Attempt to prepare history.",
                        "evidence_ids": [],
                    },
                )
                assert result.isError and "kept as history" in result.content[0].text
                prompt = await client.get_prompt("founder_intake", {"business_id": bid})
                text = prompt.messages[0].content.text
                assert "The planning jurisdiction is a human decision. No tool changes it." in text
                assert "Operating footprint for the planning jurisdiction" in text

    with foundry_api(store) as url:
        asyncio.run(check(url))
        before = raw(store)
        # The agent key is refused by the API itself, not only by the adapter's tool list.
        response = httpx.post(
            url + f"/api/businesses/{bid}/jurisdiction",
            json=change_body(version=2, to="SE"),
            headers={"Authorization": "Bearer " + agent["token"], "Idempotency-Key": "agent-try"},
        )
        assert response.status_code == 403
        assert "human owner or reviewer" in response.json()["detail"]
        assert raw(store) == before
        preview = httpx.get(
            url + f"/api/businesses/{bid}/jurisdiction/preview",
            params={"to": "SE"},
            headers={"Authorization": "Bearer " + agent["token"]},
        )
        assert preview.status_code == 200 and preview.json()["to"] == "SE"
    assert all_tasks(service, owner, bid)[old["id"]] == superseded


# Earlier deal analyses and forecasts after a jurisdiction change. The mark is worked out when the records
# are read, from the event sequence; nothing stored is rewritten and no step is blocked by it.

EVIDENCE_CHANGED = (
    "Supporting evidence has changed since this deal analysis was saved. "
    "Run a revised analysis before relying on these figures."
)
DEAL_SAVED_UNDER = (
    "This deal analysis was saved while the planning jurisdiction was {saved}. It is now {now}. "
    "Check its assumptions, the tax rate among them, and run a revised analysis before relying on these figures."
)
FORECAST_SAVED_UNDER = (
    "This forecast was saved while the planning jurisdiction was {saved}. It is now {now}. "
    "Check its assumptions, the tax rate among them, and save a revised forecast before relying on these figures."
)
# What a deal and a forecast row held before this round; it adds no stored field.
DEAL_FIELDS = {"inputs", "analysis", "evidence_ids", "evidence_versions"}
FORECAST_FIELDS = {
    "model_version",
    "inputs",
    "scenarios",
    "scenario_definitions",
    "assumptions",
    "evidence_ids",
}


def evidence_reason():
    return {"code": "evidence_changed", "message": EVIDENCE_CHANGED}


def jurisdiction_reason(kind, saved, now):
    template = DEAL_SAVED_UNDER if kind == "deal" else FORECAST_SAVED_UNDER
    return {
        "code": "jurisdiction_changed",
        "saved_jurisdiction": saved,
        "current_jurisdiction": now,
        "message": template.format(saved=saved, now=now),
    }


def save_forecast(service, actor, bid, title="Synthetic forecast"):
    return service.save_forecast(
        actor,
        bid,
        key(),
        dump(models.ForecastCreate, title=title, inputs=dump(models.ForecastInputs, months=12)),
    )


def save_deal(service, actor, bid, target="Synthetic target", evidence=()):
    return service.save_deal(
        actor,
        bid,
        key(),
        dump(
            models.DealInputs,
            target=target,
            thesis="A synthetic deal thesis.",
            evidence_ids=[e["id"] for e in evidence],
        ),
    )


def marks(snapshot):
    """(stale, reasons) of every deal and forecast in a snapshot, by id."""
    return {
        o["id"]: (o["stale"], o["stale_reasons"])
        for o in snapshot["objects"]
        if o["kind"] in {"deal", "financial_plan"}
    }


def mark(service, actor, bid, record):
    return marks(service.snapshot(actor, bid))[record["id"]]


def unmarked():
    return (False, [])


def document(service, actor, bid, template, deal=None):
    payload = dump(models.DocumentCreate, template=template, deal_id=deal["id"] if deal else None)
    return service.generate_document(actor, bid, key(), payload)["data"]["content"]


def test_demo_through_the_api_marks_the_deal_and_forecast_and_documents_warn_above_figures(setup, client):
    _, service, owner, _, _ = setup
    bid = seed(service, owner)["id"]
    before = client.get(f"/api/businesses/{bid}").json()
    deal = next(o for o in before["objects"] if o["kind"] == "deal")
    plan = next(o for o in before["objects"] if o["kind"] == "financial_plan")
    assert (deal["stale"], deal["stale_reasons"]) == unmarked()
    assert (plan["stale"], plan["stale_reasons"]) == unmarked()

    response = client.post(
        f"/api/businesses/{bid}/jurisdiction",
        json=change_body(version=before["business"]["version"]),
        headers={"Idempotency-Key": key()},
    )
    assert response.status_code == 200

    snapshot = client.get(f"/api/businesses/{bid}").json()
    by_id = {o["id"]: o for o in snapshot["objects"]}
    assert next(o for o in snapshot["objects"] if o["kind"] == "document")["stale"] is True
    deal_reason = jurisdiction_reason("deal", "undecided", "GB")
    plan_reason = jurisdiction_reason("financial_plan", "undecided", "GB")
    assert (by_id[deal["id"]]["stale"], by_id[deal["id"]]["stale_reasons"]) == (True, [deal_reason])
    assert (by_id[plan["id"]]["stale"], by_id[plan["id"]]["stale_reasons"]) == (True, [plan_reason])

    memo = client.post(
        f"/api/businesses/{bid}/documents",
        json={"template": "investment_memo", "deal_id": deal["id"]},
        headers={"Idempotency-Key": key()},
    ).json()["data"]["content"]
    assert "Planning jurisdiction: GB." in memo
    assert memo.count("WARNING:") == 1
    assert (
        "These values are input-dependent scenarios. Multiples are user assumptions, not fetched market "
        f"comparables.\n\nWARNING: {deal_reason['message']}\n\nCurrency: USD.\n\n### Earnings normalization"
    ) in memo
    plan_doc = client.post(
        f"/api/businesses/{bid}/documents",
        json={"template": "business_plan"},
        headers={"Idempotency-Key": key()},
    ).json()["data"]["content"]
    assert plan_doc.count("WARNING:") == 1
    assert f"## Financial scenarios\n\nWARNING: {plan_reason['message']}\n\nModel: " in plan_doc


def test_a_deal_with_changed_evidence_and_an_earlier_jurisdiction_lists_and_prints_both(setup):
    _, service, owner, _, _ = setup
    bid = make_business(service, owner, "undecided", route="acquire")["id"]
    unreviewed = source(service, owner, bid, review=False)
    deal = save_deal(service, owner, bid, evidence=[unreviewed])
    assert mark(service, owner, bid, deal) == unmarked()
    source_review = service.review_evidence(
        owner,
        bid,
        unreviewed["id"],
        key(),
        dump(models.Review, note="Checked the synthetic source text.", expected_version=1),
    )
    assert source_review["version"] == 2
    assert mark(service, owner, bid, deal) == (True, [evidence_reason()])

    change(service, owner, bid, "SE")

    reasons = [evidence_reason(), jurisdiction_reason("deal", "undecided", "SE")]
    assert mark(service, owner, bid, deal) == (True, reasons)
    memo = document(service, owner, bid, "investment_memo", deal)
    assert f"WARNING: {reasons[0]['message']}\n\nWARNING: {reasons[1]['message']}\n\nCurrency: USD.\n" in memo
    assert memo.count("WARNING:") == 2


def test_a_forecast_saved_under_an_earlier_jurisdiction_is_marked(setup):
    _, service, owner, bid, _ = setup
    plan = save_forecast(service, owner, bid)
    assert mark(service, owner, bid, plan) == unmarked()
    change(service, owner, bid, "US-DE")
    assert mark(service, owner, bid, plan) == (
        True,
        [jurisdiction_reason("financial_plan", "undecided", "US-DE")],
    )


def test_nothing_changes_a_deal_or_forecast_after_it_is_created(setup):
    """Requirement 5: the figures are calculated once. Withdrawing cited evidence, adding records and
    changing the jurisdiction leave each record at version 1 with exactly the fields it was created with."""
    store, service, owner, _, _ = setup
    bid = make_business(service, owner, "undecided", route="acquire")["id"]
    cited = source(service, owner, bid)
    deal = save_deal(service, owner, bid, evidence=[cited])
    plan = save_forecast(service, owner, bid)
    change(service, owner, bid, "GB")
    withdraw(service, owner, bid, cited)
    service.add_record(owner, bid, key(), dump(models.RecordCreate, kind="risk", title="A new risk"))
    with store.read() as con:
        rows = {
            r["id"]: (r["version"], set(json.loads(r["data"])))
            for r in con.execute("SELECT * FROM objects WHERE id IN (?,?)", (deal["id"], plan["id"]))
        }
        actions = [
            r[0]
            for r in con.execute(
                "SELECT action FROM events WHERE object_id IN (?,?) ORDER BY seq", (deal["id"], plan["id"])
            )
        ]
    assert rows == {deal["id"]: (1, DEAL_FIELDS), plan["id"]: (1, FORECAST_FIELDS)}
    assert actions == ["deal.created", "financial_plan.created"]


@pytest.mark.parametrize("clock", ["same tick", "clock runs backwards"])
def test_order_comes_from_the_event_sequence_not_the_clock(setup, monkeypatch, clock):
    """A record saved just before the change is marked; one saved just after is not, even when both
    saves and the change carry the same timestamp, or the later save carries an earlier one."""
    store, service, owner, _, _ = setup
    bid = make_business(service, owner, "undecided", route="acquire")["id"]
    stamps = iter(["2026-11-11T12:00:00.000000+00:00"] * 1000)
    if clock == "clock runs backwards":
        stamps = iter(f"2026-11-11T12:00:00.{999999 - i:06d}+00:00" for i in range(1000))

    def fixed():
        return next(stamps)

    monkeypatch.setattr("foundry.store.now", fixed)
    monkeypatch.setattr("foundry.service.now", fixed)
    seq = last_seq(store)
    early_deal, early_plan = (
        save_deal(service, owner, bid, "Early"),
        save_forecast(service, owner, bid, "Early"),
    )
    change(service, owner, bid, "GB")
    late_deal, late_plan = save_deal(service, owner, bid, "Late"), save_forecast(service, owner, bid, "Late")
    with store.read() as con:
        stamped = [
            r[0] for r in con.execute("SELECT created_at FROM events WHERE seq>? ORDER BY seq", (seq,))
        ]
    if clock == "same tick":
        assert len(set(stamped)) == 1
    else:
        assert stamped == sorted(stamped, reverse=True)

    found = marks(service.snapshot(owner, bid))
    assert found[early_deal["id"]] == (True, [jurisdiction_reason("deal", "undecided", "GB")])
    assert found[early_plan["id"]] == (True, [jurisdiction_reason("financial_plan", "undecided", "GB")])
    assert found[late_deal["id"]] == unmarked()
    assert found[late_plan["id"]] == unmarked()


def test_records_stored_without_any_new_field_are_marked_without_a_migration(setup):
    """Requirement 7. Saving is unchanged by this round: a deal or forecast row holds exactly the fields
    the earlier code wrote, and the mark is read from events that code already recorded. So a record
    written before the upgrade is the same thing as one written here; it is marked after a later change."""
    store, service, owner, _, _ = setup
    bid = make_business(service, owner, "undecided", route="acquire")["id"]
    deal, plan = save_deal(service, owner, bid), save_forecast(service, owner, bid)
    with store.read() as con:
        stored = {
            r["id"]: set(json.loads(r["data"]))
            for r in con.execute("SELECT id, data FROM objects WHERE kind IN ('deal','financial_plan')")
        }
    assert stored == {deal["id"]: DEAL_FIELDS, plan["id"]: FORECAST_FIELDS}

    change(service, owner, bid, "GB")

    assert mark(service, owner, bid, deal) == (True, [jurisdiction_reason("deal", "undecided", "GB")])
    assert mark(service, owner, bid, plan) == (
        True,
        [jurisdiction_reason("financial_plan", "undecided", "GB")],
    )


def test_away_and_back_clears_the_mark_only_for_records_saved_under_the_original(setup):
    _, service, owner, _, _ = setup
    bid = make_business(service, owner, "undecided", route="acquire")["id"]
    first_deal, first_plan = (
        save_deal(service, owner, bid, "First"),
        save_forecast(service, owner, bid, "First"),
    )
    change(service, owner, bid, "GB")
    gb_deal, gb_plan = (
        save_deal(service, owner, bid, "Under GB"),
        save_forecast(service, owner, bid, "Under GB"),
    )
    change(service, owner, bid, "undecided")

    found = marks(service.snapshot(owner, bid))
    assert found[first_deal["id"]] == unmarked()
    assert found[first_plan["id"]] == unmarked()
    assert found[gb_deal["id"]] == (True, [jurisdiction_reason("deal", "GB", "undecided")])
    assert found[gb_plan["id"]] == (True, [jurisdiction_reason("financial_plan", "GB", "undecided")])


def test_several_changes_name_the_jurisdiction_each_record_was_saved_under(setup):
    _, service, owner, _, _ = setup
    bid = make_business(service, owner, "undecided", route="acquire")["id"]
    first = save_forecast(service, owner, bid, "First")
    change(service, owner, bid, "US-DE")
    delaware_deal, delaware_plan = save_deal(service, owner, bid), save_forecast(service, owner, bid, "US-DE")
    change(service, owner, bid, "GB")

    found = marks(service.snapshot(owner, bid))
    assert found[first["id"]] == (True, [jurisdiction_reason("financial_plan", "undecided", "GB")])
    assert found[delaware_deal["id"]] == (True, [jurisdiction_reason("deal", "US-DE", "GB")])
    assert found[delaware_plan["id"]] == (True, [jurisdiction_reason("financial_plan", "US-DE", "GB")])


@pytest.mark.parametrize("jurisdiction", list(PACKAGE))
def test_a_business_with_no_jurisdiction_change_marks_nothing(setup, jurisdiction):
    _, service, owner, _, _ = setup
    bid = make_business(service, owner, jurisdiction, route="acquire")["id"]
    deal, plan = save_deal(service, owner, bid), save_forecast(service, owner, bid)
    found = marks(service.snapshot(owner, bid))
    assert found == {deal["id"]: unmarked(), plan["id"]: unmarked()}
    assert "WARNING:" not in document(service, owner, bid, "investment_memo", deal)
    assert "WARNING:" not in document(service, owner, bid, "business_plan")


def test_records_saved_after_the_change_have_no_mark_and_documents_no_warning(setup):
    _, service, owner, _, _ = setup
    bid = seed(service, owner)["id"]
    change(service, owner, bid, "GB")
    deal, plan = save_deal(service, owner, bid, "Fresh"), save_forecast(service, owner, bid, "Fresh")
    found = marks(service.snapshot(owner, bid))
    assert found[deal["id"]] == unmarked() and found[plan["id"]] == unmarked()
    # The earlier demo deal and forecast stay marked; documents printing only the fresh ones do not warn.
    assert sum(stale for stale, _ in found.values()) == 2
    assert "WARNING:" not in document(service, owner, bid, "investment_memo", deal)
    business_plan = document(service, owner, bid, "business_plan")
    assert "Model: Fresh. Currency: " in business_plan and "WARNING:" not in business_plan
    objects = service.snapshot(owner, bid)["objects"]
    earlier = next(o for o in objects if o["kind"] == "financial_plan" and o["id"] != plan["id"])
    index = business_plan.split("## Reference index")[1]
    assert plan["id"] in index and earlier["id"] not in index


def test_a_change_leaves_every_stored_deal_forecast_revision_and_earlier_event_byte_for_byte(setup):
    store, service, owner, _, _ = setup
    bid = seed(service, owner)["id"]
    save_deal(service, owner, bid, "Second")
    save_forecast(service, owner, bid, "Second")
    with store.read() as con:
        ids = [r[0] for r in con.execute("SELECT id FROM objects WHERE kind IN ('deal','financial_plan')")]
    assert len(ids) == 4
    seq = last_seq(store)

    def rows():
        stored = raw(store)
        return (
            [r for r in stored["objects"] if r[0] in ids],
            [r for r in stored["revisions"] if r[0] in ids],
            [r for r in stored["events"] if r[0] <= seq],
        )

    before = rows()
    change(service, owner, bid, "GB")
    assert all(stale for stale, _ in marks(service.snapshot(owner, bid)).values())
    assert rows() == before


def test_the_jurisdiction_mark_blocks_no_deal_step_and_changed_evidence_still_does(setup):
    _, service, owner, _, _ = setup
    bid = make_business(service, owner, "undecided", route="acquire")["id"]
    reviewed = source(service, owner, bid)
    deal = save_deal(service, owner, bid, evidence=[reviewed])
    steps = deal_steps(service, owner, bid)
    change(service, owner, bid, "GB")
    assert mark(service, owner, bid, deal)[0] is True
    after = deal_steps(service, owner, bid)
    assert {i: t["blockers"] for i, t in after.items()} == {i: t["blockers"] for i, t in steps.items()}
    mandate = next(t for t in after.values() if t["data"]["key"] == "mandate")
    assert mandate["blockers"] == []
    for state in ("prepared", "approved", "verified"):
        mandate = move(service, owner, bid, mandate, state, [reviewed])
    assert mandate["state"] == "verified"

    unreviewed = source(service, owner, bid, review=False, title="Later source")
    changed_deal = save_deal(service, owner, bid, "Changed basis", evidence=[unreviewed])
    service.review_evidence(
        owner,
        bid,
        unreviewed["id"],
        key(),
        dump(models.Review, note="Checked the synthetic source text.", expected_version=1),
    )
    assert mark(service, owner, bid, changed_deal) == (True, [evidence_reason()])
    blocked = next(
        t
        for t in deal_steps(service, owner, bid).values()
        if t["data"]["deal_id"] == changed_deal["id"] and t["data"]["key"] == "mandate"
    )
    assert blocked["blockers"] == ["Supporting deal evidence changed; create and review a fresh analysis"]
    blocked = move(service, owner, bid, blocked, "prepared", [reviewed])
    with pytest.raises(DomainError, match="Supporting deal evidence changed"):
        move(service, owner, bid, blocked, "approved", [reviewed])


def test_the_snapshot_fingerprint_is_the_same_hash_of_stored_rows_so_no_document_flips(setup):
    """Requirement 12. The fingerprint is a hash of the stored business and non-document rows, worked out
    independently here; the new marks are not part of it, so upgrading alone changes no document's mark."""
    store, service, owner, _, _ = setup
    bid = seed(service, owner)["id"]
    change(service, owner, bid, "GB")
    save_forecast(service, owner, bid, "After the change")
    service.generate_document(owner, bid, key(), dump(models.DocumentCreate, template="business_plan"))

    def unpack(row):
        return {**dict(row), "data": json.loads(row["data"])}

    with store.read() as con:
        business = unpack(con.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone())
        rows = [
            unpack(r)
            for r in con.execute(
                "SELECT * FROM objects WHERE business_id=? AND kind!='document' ORDER BY created_at,id",
                (bid,),
            )
        ]
    expected = hashlib.sha256(
        json.dumps(
            {"business": business, "objects": rows},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    snapshot = service.snapshot(owner, bid)
    assert snapshot["source_fingerprint"] == expected
    documents = [o for o in snapshot["objects"] if o["kind"] == "document"]
    assert [d["stale"] for d in documents] == [True, False]
    assert all(set(d) == set(documents[0]) for d in documents)
    assert [[r["code"] for r in d["stale_reasons"]] for d in documents] == [["source_changed"], []]


def test_every_reader_sees_the_same_mark_http_agent_adapter_and_document(setup, client):
    store, service, owner, bid, _ = setup
    agent = service.add_principal(owner, {"name": "Mark agent", "role": "agent", "business_ids": [bid]})
    service_deal = save_deal(service, owner, bid)
    plan = save_forecast(service, owner, bid)
    change(service, owner, bid, "SE")
    expected = {
        service_deal["id"]: (True, [jurisdiction_reason("deal", "undecided", "SE")]),
        plan["id"]: (True, [jurisdiction_reason("financial_plan", "undecided", "SE")]),
    }
    assert marks(service.snapshot(owner, bid)) == expected
    assert marks(client.get(f"/api/businesses/{bid}").json()) == expected
    viewer = service.add_principal(owner, {"name": "Viewer", "role": "viewer", "business_ids": [bid]})
    assert marks(service.snapshot(store.authenticate(viewer["token"]), bid)) == expected
    memo = document(service, owner, bid, "investment_memo", service_deal)
    assert f"WARNING: {expected[service_deal['id']][1][0]['message']}" in memo
    assert f"WARNING: {expected[plan['id']][1][0]['message']}" in document(
        service, owner, bid, "business_plan"
    )

    async def check(url):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "foundry.cli", "mcp"],
            env={**os.environ, "FOUNDRY_API_URL": url, "FOUNDRY_API_TOKEN": agent["token"]},
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                body = json.loads(
                    (await session.call_tool("get_business", {"business_id": bid})).content[0].text
                )
                assert marks(body) == expected

    with foundry_api(store) as url:
        asyncio.run(check(url))


# Prerequisites of added package steps resolve against the live steps that remain. Today's catalog cannot
# show this (each package step depends only on the base structure step), so these use a synthetic catalog.


def synthetic_packages(monkeypatch, se_prerequisites):
    packages = {
        "undecided": [catalog.task("first_local", "First local step", "Setup", "Synthetic step.")],
        "GB": [catalog.task("gb_local", "GB local step", "Setup", "Synthetic step.")],
        "SE": [catalog.task("se_local", "SE local step", "Setup", "Synthetic step.", se_prerequisites)],
        "US-DE": [],
        "other": [],
    }
    monkeypatch.setattr(catalog, "JURISDICTIONS", packages)
    monkeypatch.setattr(service_module, "PACKAGE_KEYS", {"first_local", "gb_local", "se_local"})


def added_prerequisites(service, owner, bid, to):
    preview = service.preview_jurisdiction(owner, bid, to)
    result = change(service, owner, bid, to)
    [added] = result["added"]
    stored = all_tasks(service, owner, bid)[added["id"]]
    assert preview["added"][0]["depends_on"] == added["depends_on"] == stored["data"]["depends_on"]
    return added["depends_on"]


def test_a_prerequisite_found_only_on_a_superseded_step_is_not_wired(setup, monkeypatch):
    _, service, owner, _, _ = setup
    synthetic_packages(monkeypatch, ["first_local", "gb_local", "structure"])
    bid = make_business(service, owner, "undecided")["id"]
    first = live_core(service, owner, bid)["first_local"]
    change(service, owner, bid, "GB")  # supersedes first_local
    assert all_tasks(service, owner, bid)[first["id"]]["state"] == "superseded"
    structure = live_core(service, owner, bid)["structure"]["id"]
    # first_local was superseded earlier; gb_local is superseded by this change itself.
    assert added_prerequisites(service, owner, bid, "SE") == [structure]


def test_a_prerequisite_found_only_on_a_deal_step_is_not_wired(setup, monkeypatch):
    _, service, owner, _, _ = setup
    synthetic_packages(monkeypatch, ["mandate", "structure"])
    bid = make_business(service, owner, "undecided", route="acquire")["id"]
    save_deal(service, owner, bid)
    assert any(t["data"]["key"] == "mandate" for t in deal_steps(service, owner, bid).values())
    structure = live_core(service, owner, bid)["structure"]["id"]
    assert added_prerequisites(service, owner, bid, "SE") == [structure]


def test_a_prerequisite_on_a_live_base_step_is_wired_to_that_step(setup, monkeypatch):
    _, service, owner, _, _ = setup
    synthetic_packages(monkeypatch, ["founders", "structure"])
    bid = make_business(service, owner, "undecided")["id"]
    core = live_core(service, owner, bid)
    assert added_prerequisites(service, owner, bid, "SE") == [core["founders"]["id"], core["structure"]["id"]]
    assert live_core(service, owner, bid)["se_local"]["blockers"] == [
        "Complete: Record founder and IP arrangements",
        "Complete: Decide the entity and ownership structure",
    ]


def test_package_steps_name_only_base_steps_as_prerequisites():
    """A jurisdiction change wires an added step's prerequisites only to live base steps that remain, and
    never between two steps of the package being added. A package step that names another package step
    would therefore get no dependency after a change, though it would at creation. Wire that in
    jurisdiction_plan before adding such a step."""
    for jurisdiction, specs in catalog.JURISDICTIONS.items():
        for spec in specs:
            named = set(spec["depends_on_keys"]) - BASE_KEYS
            assert not named, (
                f"{jurisdiction} step {spec['key']} names {sorted(named)} as prerequisites. A jurisdiction "
                "change resolves package prerequisites against base steps only, so these would be dropped."
            )
