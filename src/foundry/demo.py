"""Reproducible, explicitly fictional examples. Never imports private business data."""

from datetime import date, timedelta

from . import models
from .service import Service


def seed(service: Service, actor):
    def data(cls, **values):
        return cls(**values).model_dump(mode="json")

    business = service.create_business(
        actor,
        "demo:business:v1",
        data(
            models.BusinessCreate,
            name="Polymer Geospatial · demo",
            is_demo=True,
            jurisdiction="undecided",
            concept="Synthetic demonstration: a small geospatial team delivers a licensed site-screening report to an example customer. This is not Polymer's actual business plan.",
            customer="Example infrastructure planning team (fictional)",
            owner="Demo founder",
        ),
    )
    bid = business["id"]
    demo_date = date.fromisoformat(business["created_at"][:10])
    receipt = service.add_evidence(
        actor,
        bid,
        "demo:evidence:v1",
        data(
            models.EvidenceCreate,
            title="Synthetic receipts and review packet",
            source_type="external_receipt",
            content="SYNTHETIC EXAMPLE ONLY. No real entity, contract, filing, account or payment exists. This demo packet represents reviewed customer interviews, licensed example inputs, a fictional signed agreement, accepted report, issued invoice and a 4,000 USD bank receipt. Its purpose is to exercise software state transitions, not certify a business.",
        ),
    )
    reviewed = service.review_evidence(
        actor,
        bid,
        receipt["id"],
        "demo:evidence:review:v1",
        data(
            models.Review,
            note="Reviewed only as synthetic demonstration material; not real-world proof.",
            expected_version=receipt["version"],
        ),
    )
    eid = reviewed["id"]
    records = {}
    for kind, title, details in [
        (
            "customer",
            "Example planning team",
            "Fictional buyer needs a repeatable screening report. Interview and willingness-to-pay claims are demonstration assumptions.",
        ),
        (
            "offer",
            "Example site-screening pilot",
            "Synthetic fixed-price 4,000 USD pilot: 10 sample sites, documented sources, human quality review, accepted PDF report. No safety-critical or legal determination.",
        ),
        (
            "asset",
            "Example licensed geospatial inputs",
            "Use synthetic data. In a real pilot, record licenses, coverage, attribution, redistribution, freshness and transfer rights for each source.",
        ),
        (
            "contract",
            "Example customer and founder arrangements",
            "Synthetic terms: report delivery and acceptance, 30-day payment, defined scope and rights. Real agreements require suitable review and authorized signatures.",
        ),
        (
            "role",
            "Delivery and review owner",
            "Demo founder prepares outputs; an independent domain specialist reviews source fitness, uncertainty and acceptance.",
        ),
        (
            "decision",
            "Keep actual jurisdiction undecided",
            "No real jurisdiction or entity choice has been made. The demo tests workflow states; actual formation requires the founders' operating facts and qualified local review.",
        ),
        (
            "obligation",
            "Review licenses and reporting deadlines",
            "Confirm applicable sources, customer commitments, insurance, tax and reporting obligations. Demonstration deadline only.",
        ),
        (
            "risk",
            "Data permissions and willingness to pay",
            "Unproven customer economics and license constraints can invalidate the offer. Test both before committing capital.",
        ),
    ]:
        records[kind] = service.add_record(
            actor,
            bid,
            "demo:record:" + kind,
            data(
                models.RecordCreate,
                kind=kind,
                title=title,
                details=details,
                owner="Demo founder",
                certainty="reported",
                evidence_ids=[eid],
                due_date=demo_date + timedelta(days=30) if kind == "obligation" else None,
            ),
        )
    service.save_forecast(
        actor,
        bid,
        "demo:forecast:v1",
        data(
            models.ForecastCreate,
            title="Synthetic pilot economics",
            inputs=models.ForecastInputs(
                monthly_units=2,
                unit_price=4000,
                unit_cost=800,
                fixed_costs=7000,
                opening_equity=50000,
                monthly_growth="0.04",
            ),
            evidence_ids=[eid],
        ),
    )
    invoice = service.create_invoice(
        actor,
        bid,
        "demo:invoice:v1",
        data(
            models.InvoiceCreate,
            customer_id=records["customer"]["id"],
            description="Synthetic pilot report",
            amount=4000,
            due_date=demo_date + timedelta(days=30),
            contract_evidence_id=eid,
            acceptance_evidence_id=eid,
        ),
    )
    service.issue_invoice(
        actor,
        bid,
        invoice["id"],
        "demo:invoice:issue:v1",
        data(
            models.InvoiceIssue,
            expected_version=invoice["version"],
            evidence_id=eid,
            note="Synthetic invoice issue receipt; no message or invoice was sent.",
        ),
    )
    service.payment(
        actor,
        bid,
        invoice["id"],
        "demo:payment:v1",
        data(
            models.PaymentCreate,
            amount=4000,
            reference="DEMO-RECEIPT-001",
            evidence_id=eid,
            received_on=demo_date,
        ),
    )
    # Complete the demonstration's startup path in dependency order. No actual external action occurs.
    tasks = [
        o
        for o in service.snapshot(actor, bid)["objects"]
        if o["kind"] == "task" and not o["data"].get("deal_id")
    ]
    for task in tasks:
        if task["state"] in {"verified", "not_applicable"}:
            continue
        steps = ["prepared", "approved"] + (["submitted"] if task["data"]["external"] else []) + ["verified"]
        if task["state"] in steps:
            steps = steps[steps.index(task["state"]) + 1 :]
        for state in steps:
            task = service.transition(
                actor,
                bid,
                task["id"],
                f"demo:task:{task['id']}:{state}",
                data(
                    models.TaskTransition,
                    expected_version=task["version"],
                    state=state,
                    note="Synthetic demonstration step only; no real legal or commercial completion.",
                    evidence_ids=[eid],
                ),
            )
    service.save_deal(
        actor,
        bid,
        "demo:deal:v1",
        data(
            models.DealInputs,
            target="Example Mapping Studio · synthetic",
            thesis="Compare buying a small mapping-services operation with building delivery capacity. All figures are illustrative.",
            adjustments=[
                models.Adjustment(label="Illustrative one-time expense", amount=10000, evidence_id=eid),
                models.Adjustment(label="Unsupported seller add-back", amount=25000),
            ],
            replacement_management=60000,
        ),
    )
    service.generate_document(
        actor, bid, "demo:document:v1", data(models.DocumentCreate, template="business_plan")
    )
    return business
