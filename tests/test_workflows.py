import json
from datetime import date
from uuid import uuid4

import pytest
from conftest import reviewed

from foundry import models
from foundry.cli import backup_database
from foundry.demo import seed
from foundry.service import Service
from foundry.store import DomainError, Store


def dump(cls, **kwargs):
    return cls(**kwargs).model_dump(mode="json")


def key():
    return str(uuid4())


def test_demo_is_end_to_end_idempotent_and_all_records_survive_restart(setup):
    store, service, actor, _, token = setup
    business = seed(service, actor)
    bid = business["id"]
    snapshot = service.snapshot(actor, bid)
    assert snapshot["stats"]["cash_collected"] == "4000.00"
    assert snapshot["stats"]["outstanding_invoices"] == "0.00"
    assert snapshot["stats"]["verified"] == 17
    invoice = next(o for o in snapshot["objects"] if o["kind"] == "invoice")
    assert invoice["state"] == "paid"
    assert len([o for o in snapshot["objects"] if o["kind"] == "task" and o["data"]["deal_id"]]) == 7
    assert seed(service, actor)["id"] == bid
    new_store = Store(store.path)
    assert Service(new_store).snapshot(new_store.authenticate(token), bid) == snapshot
    assert service.audit(actor, bid)["integrity"]["valid"]


def test_idempotency_and_revision_conflicts(setup):
    _, service, actor, bid, _ = setup
    payload = dump(models.RecordCreate, kind="customer", title="One buyer")
    item = service.add_record(actor, bid, "one-command", payload)
    assert service.add_record(actor, bid, "one-command", payload) == item
    with pytest.raises(DomainError, match="different request"):
        service.add_record(actor, bid, "one-command", payload | {"title": "Different"})
    change = payload | {"expected_version": 1, "details": "A revised finding"}
    updated = service.add_record(actor, bid, key(), change, item["id"])
    assert updated["version"] == 2
    with pytest.raises(DomainError, match="changed"):
        service.add_record(actor, bid, key(), change, item["id"])


def test_scoped_roles_fail_closed(setup):
    store, service, owner, bid, _ = setup
    other = service.create_business(
        owner, key(), dump(models.BusinessCreate, name="Other", concept="Separate private business.")
    )
    for role in ["agent", "viewer", "operator"]:
        p = service.add_principal(owner, {"name": role, "role": role, "business_ids": [bid]})
        actor = store.authenticate(p["token"])
        assert len(service.list_businesses(actor)) == 1
        with pytest.raises(DomainError) as exc:
            service.snapshot(actor, other["id"])
        assert exc.value.status == 403
        with pytest.raises(DomainError):
            service.add_record(
                actor,
                bid,
                key(),
                dump(
                    models.RecordCreate, kind="customer", title="Unjustified certainty", certainty="verified"
                ),
            )
        e = reviewed(setup)
        with pytest.raises(DomainError):
            service.review_evidence(
                actor,
                bid,
                e["id"],
                key(),
                dump(models.Review, expected_version=2, note="An unauthorized approval."),
            )
        if role == "viewer":
            with pytest.raises(DomainError):
                service.add_record(
                    actor, bid, key(), dump(models.RecordCreate, kind="customer", title="No writing")
                )
        service.revoke_principal(owner, p["id"])
        with pytest.raises(DomainError):
            store.authenticate(p["token"])


def test_cross_business_references_rejected_even_for_owner(setup):
    _, service, actor, bid, _ = setup
    other = service.create_business(
        actor, key(), dump(models.BusinessCreate, name="Other", concept="Separate private business.")
    )
    e = reviewed(setup)
    with pytest.raises(DomainError) as exc:
        service.add_record(
            actor,
            other["id"],
            key(),
            dump(models.RecordCreate, kind="asset", title="Bad reference", evidence_ids=[e["id"]]),
        )
    assert exc.value.status == 404


def test_task_dependencies_receipts_and_human_review(setup):
    _, s, a, bid, _ = setup
    e = reviewed(setup, source_type="document")
    tasks = s.snapshot(a, bid)["objects"]
    registration = next(t for t in tasks if t["data"].get("key") == "registration")
    registration = s.transition(
        a,
        bid,
        registration["id"],
        key(),
        dump(
            models.TaskTransition,
            state="prepared",
            expected_version=1,
            note="Prepared registration details.",
            evidence_ids=[e["id"]],
        ),
    )
    with pytest.raises(DomainError, match="Complete: Decide"):
        s.transition(
            a,
            bid,
            registration["id"],
            key(),
            dump(
                models.TaskTransition,
                state="approved",
                expected_version=2,
                note="Reviewed registration details.",
                evidence_ids=[e["id"]],
            ),
        )
    structure = next(t for t in tasks if t["data"].get("key") == "structure")
    s.add_record(a, bid, key(), dump(models.RecordCreate, kind="decision", title="Review structure options"))
    for status in ["prepared", "approved", "verified"]:
        structure = s.transition(
            a,
            bid,
            structure["id"],
            key(),
            dump(
                models.TaskTransition,
                state=status,
                expected_version=structure["version"],
                note="Documented human decision.",
                evidence_ids=[e["id"]],
            ),
        )
    registration = s.transition(
        a,
        bid,
        registration["id"],
        key(),
        dump(
            models.TaskTransition,
            state="approved",
            expected_version=registration["version"],
            note="Review is now complete.",
            evidence_ids=[e["id"]],
        ),
    )
    with pytest.raises(DomainError, match="external submission"):
        s.transition(
            a,
            bid,
            registration["id"],
            key(),
            dump(
                models.TaskTransition,
                state="verified",
                expected_version=registration["version"],
                note="Attempted to skip submission.",
                evidence_ids=[e["id"]],
            ),
        )
    with pytest.raises(DomainError, match="external receipt"):
        s.transition(
            a,
            bid,
            registration["id"],
            key(),
            dump(
                models.TaskTransition,
                state="submitted",
                expected_version=registration["version"],
                note="Only a document exists.",
                evidence_ids=[e["id"]],
            ),
        )
    receipt = reviewed(setup)
    for status in ["submitted", "verified"]:
        registration = s.transition(
            a,
            bid,
            registration["id"],
            key(),
            dump(
                models.TaskTransition,
                state=status,
                expected_version=registration["version"],
                note="Checked the actual provider receipt.",
                evidence_ids=[receipt["id"]],
            ),
        )
    assert registration["state"] == "verified"


def test_evidence_revocation_reopens_downstream_and_marks_document_stale(setup):
    _, s, a, _, _ = setup
    bid = seed(s, a)["id"]
    snapshot = s.snapshot(a, bid)
    evidence = next(o for o in snapshot["objects"] if o["kind"] == "evidence")
    doc = next(o for o in snapshot["objects"] if o["kind"] == "document")
    assert not doc["stale"]
    s.review_evidence(
        a,
        bid,
        evidence["id"],
        key(),
        dump(
            models.Review,
            note="Synthetic evidence deliberately withdrawn.",
            expected_version=evidence["version"],
        ),
        True,
    )
    snapshot = s.snapshot(a, bid)
    assert snapshot["stats"]["verified"] == 0
    assert next(o for o in snapshot["objects"] if o["id"] == doc["id"])["stale"]
    assert next(o for o in snapshot["objects"] if o["kind"] == "invoice")["evidence_warning"]
    assert next(o for o in snapshot["objects"] if o["kind"] == "deal")["stale"]
    assert (
        next(o for o in snapshot["objects"] if o["id"] == doc["id"])["data"]["content"]
        == doc["data"]["content"]
    )
    assert s.audit(a, bid)["integrity"]["valid"]


def invoice(setup):
    _, s, a, bid, _ = setup
    e = reviewed(setup)
    c = s.add_record(a, bid, key(), dump(models.RecordCreate, kind="customer", title="Example customer"))
    inv = s.create_invoice(
        a,
        bid,
        key(),
        dump(
            models.InvoiceCreate,
            customer_id=c["id"],
            description="Accepted service",
            amount=100,
            due_date=date.today(),
            contract_evidence_id=e["id"],
            acceptance_evidence_id=e["id"],
        ),
    )
    inv = s.issue_invoice(
        a,
        bid,
        inv["id"],
        key(),
        dump(
            models.InvoiceIssue,
            note="Verified issued invoice in source.",
            expected_version=1,
            evidence_id=e["id"],
        ),
    )
    return inv, e


def test_partial_payments_duplicate_protection_and_transaction_rollback(setup):
    store, s, a, bid, _ = setup
    inv, e = invoice(setup)
    p = dump(
        models.PaymentCreate,
        amount=40,
        reference="bank-unique",
        evidence_id=e["id"],
        received_on=date.today(),
    )
    first = s.payment(a, bid, inv["id"], "pay-1", p)
    assert s.payment(a, bid, inv["id"], "pay-1", p) == first
    with pytest.raises(DomainError, match="already recorded"):
        s.payment(a, bid, inv["id"], key(), p)
    with pytest.raises(DomainError, match="exceeds"):
        s.payment(a, bid, inv["id"], key(), p | {"amount": "61", "reference": "too-large"})
    with store.read() as con:
        current = store.get(con, bid, inv["id"])
        assert current["state"] == "partially_paid"
        assert current["data"]["balance"] == "60.00"
        assert (
            con.execute("SELECT count(*) FROM payment_references WHERE business_id=?", (bid,)).fetchone()[0]
            == 1
        )
    s.payment(a, bid, inv["id"], key(), p | {"amount": "60", "reference": "second-transfer"})
    with store.read() as con:
        assert store.get(con, bid, inv["id"])["state"] == "paid"


def test_backup_restore_and_export_integrity(setup, tmp_path):
    store, s, a, _, token = setup
    bid = seed(s, a)["id"]
    backup = tmp_path / "backup.db"
    backup_database(store.path, backup)
    restored = Store(backup)
    restored_service = Service(restored)
    assert restored_service.snapshot(restored.authenticate(token), bid) == s.snapshot(a, bid)
    export = s.audit(a, bid, True)
    assert export["format"] == "business-foundry-export"
    assert export["integrity"]["valid"]
    serialized = json.dumps(export)
    assert token not in serialized and "key_hash" not in serialized
    with pytest.raises(FileExistsError):
        backup_database(store.path, backup)


def test_integrity_detects_out_of_band_record_and_event_changes(setup):
    store, s, a, bid, _ = setup
    obj = s.add_record(a, bid, key(), dump(models.RecordCreate, kind="offer", title="Original"))
    with store.write() as con:
        con.execute("UPDATE objects SET title=? WHERE id=?", ("Tampered", obj["id"]))
    result = s.audit(a, bid)["integrity"]
    assert not result["valid"] and result["record_errors"]
    with store.write() as con:
        con.execute(
            "UPDATE events SET action='tampered' WHERE business_id=? AND seq=(SELECT min(seq) FROM events WHERE business_id=?)",
            (bid, bid),
        )
    assert not s.audit(a, bid)["integrity"]["valid"]


def test_business_change_reopens_reviews_and_fixed_currency_prevents_relabeling(setup):
    _, s, a, _, _ = setup
    business = seed(s, a)
    bid = business["id"]
    payload = business["data"] | {"expected_version": 1, "currency": "EUR"}
    with pytest.raises(DomainError, match="Currency"):
        s.update_business(a, bid, key(), payload)
    payload = business["data"] | {
        "expected_version": 1,
        "concept": "A changed concept that requires a new review.",
    }
    s.update_business(a, bid, key(), payload)
    assert s.snapshot(a, bid)["stats"]["needs_review"] == 17


def test_new_asset_reopens_previously_reviewed_rights_and_delivery(setup):
    _, service, actor, _, _ = setup
    bid = seed(service, actor)["id"]
    service.add_record(
        actor,
        bid,
        key(),
        dump(models.RecordCreate, kind="asset", title="New source with unreviewed licensing"),
    )
    tasks = {
        o["data"]["key"]: o
        for o in service.snapshot(actor, bid)["objects"]
        if o["kind"] == "task" and not o["data"]["deal_id"]
    }
    assert tasks["rights"]["state"] == "needs_review"
    assert tasks["delivery"]["state"] == "needs_review"
    assert tasks["agreement"]["state"] == "needs_review"
    assert tasks["structure"]["state"] == "verified"


def test_concurrent_receipts_cannot_overpay_an_invoice(setup):
    from concurrent.futures import ThreadPoolExecutor

    _, service, actor, bid, _ = setup
    inv, evidence = invoice(setup)

    def pay(reference):
        try:
            return service.payment(
                actor,
                bid,
                inv["id"],
                key(),
                dump(
                    models.PaymentCreate,
                    amount=60,
                    reference=reference,
                    evidence_id=evidence["id"],
                    received_on=date.today(),
                ),
            )["kind"]
        except DomainError as error:
            return error.message

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(pay, ["parallel-A", "parallel-B"]))
    assert results.count("payment") == 1
    assert any("exceeds" in result for result in results)
    snapshot = service.snapshot(actor, bid)
    assert snapshot["stats"]["cash_collected"] == "60.00"
    assert snapshot["stats"]["outstanding_invoices"] == "40.00"
