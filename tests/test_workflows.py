import copy
import json
import re
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from conftest import reviewed

from foundry import models
from foundry.cli import backup_database
from foundry.demo import seed
from foundry.documents import render_document
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


def test_verified_record_can_only_be_changed_by_a_human_reviewer(setup):
    store, service, owner, bid, _ = setup
    evidence = reviewed(setup, source_type="document")
    record = service.add_record(
        owner,
        bid,
        key(),
        dump(
            models.RecordCreate,
            kind="customer",
            title="Verified buyer",
            certainty="verified",
            evidence_ids=[evidence["id"]],
        ),
    )
    assert record["state"] == "verified"
    for role in ["agent", "operator"]:
        p = service.add_principal(owner, {"name": role, "role": role, "business_ids": [bid]})
        actor = store.authenticate(p["token"])
        with pytest.raises(DomainError, match="verified record") as exc:
            service.add_record(
                actor,
                bid,
                key(),
                dump(
                    models.RecordUpdate,
                    kind="customer",
                    title="Downgraded buyer",
                    certainty="assumption",
                    expected_version=1,
                ),
                record["id"],
            )
        assert exc.value.status == 403
    updated = service.add_record(
        owner,
        bid,
        key(),
        dump(
            models.RecordUpdate,
            kind="customer",
            title="Buyer under re-review",
            certainty="reported",
            expected_version=1,
        ),
        record["id"],
    )
    assert updated["version"] == 2 and updated["state"] == "reported"


def test_updating_an_asset_reopens_rights_and_delivery_reviews(setup):
    _, service, actor, _, _ = setup
    bid = seed(service, actor)["id"]
    asset = next(o for o in service.snapshot(actor, bid)["objects"] if o["kind"] == "asset")
    service.add_record(
        actor,
        bid,
        key(),
        {
            **{k: v for k, v in asset["data"].items() if k in models.RecordCreate.model_fields},
            "kind": "asset",
            "title": asset["title"],
            "details": "Licence scope changed and needs a fresh review.",
            "certainty": "reported",
            "expected_version": asset["version"],
        },
        asset["id"],
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


def table_rows(content, marker):
    line = next(x for x in content.splitlines() if marker in x)
    return [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]


def test_business_plan_and_memo_render_deterministic_tables(setup):
    _, s, a, bid, _ = setup
    s.save_forecast(
        a,
        bid,
        key(),
        dump(
            models.ForecastCreate,
            title="Six | month\nplan",
            inputs=dict(
                months=6,
                opening_equity=1000,
                monthly_units=10,
                unit_price=100,
                unit_cost=20,
                monthly_growth=0,
                fixed_costs=100,
                receivable_days=0,
                tax_rate=0,
            ),
        ),
    )
    evidence = reviewed(setup, source_type="document", title="Lease statement")
    deal = s.save_deal(
        a,
        bid,
        key(),
        dump(
            models.DealInputs,
            target="Loss-making | shop",
            thesis="Test the screening tables on a loss-making target.",
            annual_revenue=100000,
            reported_ebitda=-5000,
            replacement_management=10000,
            adjustments=[
                {"label": "Owner | car\nlease", "amount": 3000, "evidence_id": evidence["id"]},
                {"label": "Unsupported | item", "amount": 1000},
            ],
            purchase_price=50000,
            fees=0,
            working_capital=0,
            senior_debt=0,
        ),
    )

    def generate(template, deal_id=None):
        doc = s.generate_document(
            a, bid, key(), dump(models.DocumentCreate, template=template, deal_id=deal_id)
        )
        return doc["data"]["content"]

    plan = generate("business_plan")
    memo = generate("investment_memo", deal["id"])
    for content in (plan, memo):
        assert "```json" not in content and '"months":' not in content
    assert "Model: Six \\| month plan (financial_plan_" in plan
    assert "Currency: USD. Horizon: 6 months." in plan
    assert table_rows(plan, "| Total revenue ") == ["Total revenue", "6,000.00", "4,500.00", "7,500.00"]
    assert table_rows(plan, "| First shortfall month ")[1:] == ["None modeled"] * 3
    assert table_rows(plan, "| Statements balance ")[1:] == ["Yes"] * 3
    assert table_rows(plan, "| Line item ") == ["Line item", "Months 1–6"]
    assert table_rows(plan, "| Revenue ") == ["Revenue", "6,000.00"]
    assert table_rows(plan, "| Cash  ") == ["Cash", "5,200.00"]
    assert table_rows(plan, "| Monthly growth ") == ["Monthly growth", "0.00%"]
    assert table_rows(plan, "| Monthly unit retention ") == ["Monthly unit retention", "0.00%"]
    assert table_rows(plan, "| Fixed-cost step per month ") == ["Fixed-cost step per month", "0.00"]
    assert table_rows(plan, "| Fixed-cost step start month ") == ["Fixed-cost step start month", "None"]
    assert "| ---------------------- | " in plan and "-------: |" in plan
    assert table_rows(plan, "| Base     |")[1].startswith("Not available — needs at least 12 months")
    assert "DCF basis:" not in plan
    assert "Full monthly statements are stored in financial plan financial_plan_" in plan

    assert "## Target: Loss-making \\| shop" in memo
    assert "WARNING: Supporting evidence" not in memo
    assert table_rows(memo, "| Reported EBITDA ") == ["Reported EBITDA", "-5,000.00", ""]
    assert table_rows(memo, "| Add: Owner") == ["Add: Owner \\| car lease", "3,000.00", evidence["id"]]
    assert table_rows(memo, "| Normalized EBITDA ") == ["Normalized EBITDA", "-12,000.00", ""]
    assert table_rows(memo, "| Unsupported") == [
        "Unsupported \\| item",
        "1,000.00",
        "None",
        "A reviewed evidence record is required.",
    ]
    assert table_rows(memo, "| Multiple range ") == ["Multiple range", "3.00x – 5.00x"]
    assert table_rows(memo, "| Assumed enterprise value range ")[1].startswith("Not meaningful")
    assert table_rows(memo, "| Assumed equity value range ")[1].startswith("Not meaningful")
    assert table_rows(memo, "| Buyer equity required ") == ["Sources", "Buyer equity required", "50,000.00"]
    assert "Annual debt service: 0.00 USD." in memo
    assert table_rows(memo, "| Base     |") == [
        "Base",
        "-12,000.00",
        "-27,000.00",
        "-27,000.00",
        "No debt service",
        "Yes",
    ]
    assert "## Conditions before commitment" in memo
    for content in (plan, memo):
        for block in content.split("\n\n"):
            rows = [x for x in block.splitlines() if x.startswith("|")]
            assert len({len(re.findall(r"(?<!\\)\|", x)) for x in rows}) <= 1

    snapshot = s.snapshot(a, bid)
    stored_deal = next(o for o in snapshot["objects"] if o["id"] == deal["id"])
    assert render_document("investment_memo", snapshot, stored_deal) == render_document(
        "investment_memo", snapshot, stored_deal
    )
    legacy = copy.deepcopy(snapshot)
    plan_obj = next(o for o in legacy["objects"] if o["kind"] == "financial_plan")
    del plan_obj["data"]["inputs"]["excess_cash"]
    _, legacy_plan = render_document("business_plan", legacy)
    assert "| Excess cash" not in legacy_plan and "| Discount rate" in legacy_plan
    v1_plan = copy.deepcopy(snapshot)
    v1_obj = next(o for o in v1_plan["objects"] if o["kind"] == "financial_plan")
    v1_obj["data"]["model_version"] = "1.0.0"
    for field in ("monthly_retention", "fixed_cost_step", "fixed_cost_step_month"):
        del v1_obj["data"]["inputs"][field]
    _, v1_content = render_document("business_plan", v1_plan)
    assert "| Monthly unit retention" not in v1_content
    assert "| Fixed-cost step" not in v1_content
    assert table_rows(v1_content, "| Monthly growth ") == ["Monthly growth", "0.00%"]

    s.review_evidence(
        a,
        bid,
        evidence["id"],
        key(),
        dump(models.Review, note="Lease statement withdrawn.", expected_version=evidence["version"]),
        True,
    )
    stale_memo = generate("investment_memo", deal["id"])
    assert "WARNING: Supporting evidence has changed since this deal analysis was saved." in stale_memo


def test_business_plan_renders_retention_and_fixed_cost_step_inputs(setup):
    _, s, a, bid, _ = setup
    inputs = dict(
        months=3,
        monthly_units=10,
        unit_price=100,
        monthly_growth=0,
        monthly_retention="0.5",
        fixed_costs=100,
        fixed_cost_step=50,
        fixed_cost_step_month=2,
    )
    s.save_forecast(a, bid, key(), dump(models.ForecastCreate, title="Step plan", inputs=inputs))
    doc = s.generate_document(a, bid, key(), dump(models.DocumentCreate, template="business_plan"))
    plan = doc["data"]["content"]
    assert table_rows(plan, "| Monthly unit retention ") == ["Monthly unit retention", "50.00%"]
    assert table_rows(plan, "| Fixed-cost step per month ") == ["Fixed-cost step per month", "50.00"]
    assert table_rows(plan, "| Fixed-cost step start month ") == ["Fixed-cost step start month", "Month 2"]
    assert any("Monthly retention carries" in line for line in plan.splitlines())


def test_maximum_bound_forecast_generates_business_plan(setup):
    _, s, a, bid, _ = setup
    inputs = dict(months=60, monthly_units=100000000, unit_price=1000000000000, monthly_growth="0.5")
    saved = s.save_forecast(a, bid, key(), dump(models.ForecastCreate, title="Max plan", inputs=inputs))
    month60 = saved["data"]["scenarios"]["base"]["months"][-1]
    doc = s.generate_document(a, bid, key(), dump(models.DocumentCreate, template="business_plan"))
    plan = doc["data"]["content"]
    assert table_rows(plan, "| Line item ") == ["Line item"] + [f"Year {n}" for n in range(1, 6)]
    revenue = table_rows(plan, "| Revenue ")
    assert len(revenue) == 6 and all(re.fullmatch(r"\d{1,3}(,\d{3})*\.\d{2}", v) for v in revenue[1:])
    assert table_rows(plan, "| Cash  ")[-1] == f"{Decimal(month60['cash']):,.2f}"


def test_long_forecast_renders_yearly_dcf_columns(setup):
    _, s, a, bid, _ = setup
    s.save_forecast(a, bid, key(), dump(models.ForecastCreate, title="Default plan"))
    s.save_deal(a, bid, key(), dump(models.DealInputs, target="Debt case", thesis="A financed target."))
    snapshot = s.snapshot(a, bid)
    _, plan = render_document("business_plan", snapshot)
    assert table_rows(plan, "| Line item ") == ["Line item", "Year 1", "Year 2", "Year 3"]
    assert "DCF basis: Projected unlevered cash flow" in plan
    assert table_rows(plan, "| Base     |")[5].endswith("%")
    deal = next(o for o in snapshot["objects"] if o["kind"] == "deal")
    _, memo = render_document("investment_memo", snapshot, deal)
    assert table_rows(memo, "| Base     |")[4].endswith("x")
    assert "None — no unsupported adjustments were submitted." in memo
