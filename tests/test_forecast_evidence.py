"""Withdrawn evidence and saved analyses.

A forecast that cites evidence a person has since withdrawn is marked stale. A review that accepts its
evidence does not mark it: its figures never depended on that review. A deal keeps its own rule, any
change to a cited item's version since it was saved. A new deal or forecast cannot cite withdrawn
evidence at all, while a record may. Expected reasons and messages are written out by hand below.
"""

import json
from uuid import uuid4

import pytest

from foundry import cli, models
from foundry.service import Service
from foundry.store import DomainError

FORECAST_WITHDRAWN = "Evidence this forecast cites has been withdrawn. Save a revised forecast before relying on these figures."
DEAL_CHANGED = (
    "Supporting evidence has changed since this deal analysis was saved. "
    "Run a revised analysis before relying on these figures."
)
FORECAST_GB = (
    "This forecast was saved while the planning jurisdiction was undecided. It is now GB. Check its "
    "assumptions, the tax rate among them, and save a revised forecast before relying on these figures."
)
# What a forecast row holds. Nothing new is stored for the mark.
FORECAST_FIELDS = {
    "model_version",
    "inputs",
    "scenarios",
    "scenario_definitions",
    "assumptions",
    "evidence_ids",
}


def key():
    return str(uuid4())


def dump(cls, **kwargs):
    return cls(**kwargs).model_dump(mode="json")


def add_evidence(service, actor, bid, title):
    return service.add_evidence(
        actor,
        bid,
        key(),
        dump(models.EvidenceCreate, title=title, content=f"Synthetic source body for {title}."),
    )


def current(service, actor, bid, item):
    return next(o for o in service.snapshot(actor, bid)["objects"] if o["id"] == item["id"])


def review(service, actor, bid, evidence, withdraw=False):
    version = current(service, actor, bid, evidence)["version"]
    payload = dump(models.Review, note="Synthetic review note.", expected_version=version)
    return service.review_evidence(actor, bid, evidence["id"], key(), payload, revoke=withdraw)


def withdraw(service, actor, bid, evidence):
    return review(service, actor, bid, evidence, withdraw=True)


def forecast_payload(evidence=(), title="Synthetic forecast"):
    return dump(
        models.ForecastCreate,
        title=title,
        inputs=dump(models.ForecastInputs, months=12),
        evidence_ids=[e["id"] for e in evidence],
    )


def save_forecast(service, actor, bid, evidence=(), title="Synthetic forecast"):
    return service.save_forecast(actor, bid, key(), forecast_payload(evidence, title))


def deal_payload(evidence=(), adjustments=()):
    return dump(
        models.DealInputs,
        target="Synthetic target",
        thesis="A synthetic deal thesis.",
        annual_revenue=1000000,
        reported_ebitda=200000,
        purchase_price=800000,
        evidence_ids=[e["id"] for e in evidence],
        adjustments=[
            {"label": "Synthetic owner salary", "amount": 50000, "evidence_id": e["id"]} for e in adjustments
        ],
    )


def save_deal(service, actor, bid, evidence=(), adjustments=()):
    return service.save_deal(actor, bid, key(), deal_payload(evidence, adjustments))


def mark(service, actor, bid, item):
    found = current(service, actor, bid, item)
    return found["stale"], found["stale_reasons"]


def withdrawn_reason():
    return {"code": "evidence_changed", "message": FORECAST_WITHDRAWN}


def deal_reason():
    return {"code": "evidence_changed", "message": DEAL_CHANGED}


def stored(store, bid):
    """Every row a refused request could have written."""
    with store.read() as con:
        return {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("objects", "events", "commands")
        }


def test_withdrawing_cited_evidence_marks_the_forecast_and_the_deal_keeps_its_own_rule(setup):
    _, service, actor, bid, _ = setup
    evidence = add_evidence(service, actor, bid, "Synthetic pilot quote")
    plan = save_forecast(service, actor, bid, [evidence])
    deal = save_deal(service, actor, bid, [evidence])
    assert mark(service, actor, bid, plan) == (False, [])
    assert mark(service, actor, bid, deal) == (False, [])

    withdraw(service, actor, bid, evidence)

    assert mark(service, actor, bid, plan) == (True, [withdrawn_reason()])
    assert mark(service, actor, bid, deal) == (True, [deal_reason()])
    # Nothing new is stored on the forecast for its mark.
    assert set(current(service, actor, bid, plan)["data"]) == FORECAST_FIELDS
    assert current(service, actor, bid, plan)["version"] == plan["version"]


def test_reviewing_cited_evidence_marks_the_deal_but_not_the_forecast(setup):
    _, service, actor, bid, _ = setup
    evidence = add_evidence(service, actor, bid, "Synthetic pilot quote")
    plan = save_forecast(service, actor, bid, [evidence])
    deal = save_deal(service, actor, bid, [evidence])

    review(service, actor, bid, evidence)

    assert current(service, actor, bid, evidence)["state"] == "verified"
    assert mark(service, actor, bid, plan) == (False, [])
    assert mark(service, actor, bid, deal) == (True, [deal_reason()])


def test_a_forecast_saved_before_the_refusal_rule_that_cites_withdrawn_evidence_is_marked(setup, monkeypatch):
    _, service, actor, bid, _ = setup
    evidence = add_evidence(service, actor, bid, "Synthetic withdrawn quote")
    withdraw(service, actor, bid, evidence)
    # Earlier code saved a forecast citing withdrawn evidence without complaint. Save one the same way.
    with monkeypatch.context() as patched:
        patched.setattr(Service, "refuse_withdrawn", lambda *args: None)
        plan = save_forecast(service, actor, bid, [evidence])
    assert set(plan["data"]) == FORECAST_FIELDS
    assert mark(service, actor, bid, plan) == (True, [withdrawn_reason()])


def test_one_withdrawn_item_of_two_marks_the_forecast_and_none_or_no_citation_does_not(setup):
    _, service, actor, bid, _ = setup
    first = add_evidence(service, actor, bid, "Synthetic first quote")
    second = add_evidence(service, actor, bid, "Synthetic second quote")
    other = add_evidence(service, actor, bid, "Synthetic unrelated note")
    both = save_forecast(service, actor, bid, [first, second], title="Cites both")
    only_second = save_forecast(service, actor, bid, [second], title="Cites the second")
    uncited = save_forecast(service, actor, bid, [], title="Cites nothing")

    withdraw(service, actor, bid, other)
    for plan in (both, only_second, uncited):
        assert mark(service, actor, bid, plan) == (False, [])

    withdraw(service, actor, bid, first)
    assert mark(service, actor, bid, both) == (True, [withdrawn_reason()])
    assert mark(service, actor, bid, only_second) == (False, [])
    assert mark(service, actor, bid, uncited) == (False, [])

    withdraw(service, actor, bid, second)
    # Two withdrawn items are still one reason.
    assert mark(service, actor, bid, both) == (True, [withdrawn_reason()])
    assert mark(service, actor, bid, only_second) == (True, [withdrawn_reason()])
    assert mark(service, actor, bid, uncited) == (False, [])


@pytest.mark.parametrize("withdraw_first", [True, False])
def test_a_jurisdiction_change_and_a_withdrawal_give_both_reasons_evidence_first(setup, withdraw_first):
    _, service, actor, bid, _ = setup
    evidence = add_evidence(service, actor, bid, "Synthetic pilot quote")
    plan = save_forecast(service, actor, bid, [evidence])

    def change():
        version = service.snapshot(actor, bid)["business"]["version"]
        payload = dump(
            models.JurisdictionChange, expected_version=version, jurisdiction="GB", reason="Synthetic."
        )
        service.change_jurisdiction(actor, bid, key(), payload)

    steps = [lambda: withdraw(service, actor, bid, evidence), change]
    for step in steps if withdraw_first else reversed(steps):
        step()
    jurisdiction = {
        "code": "jurisdiction_changed",
        "saved_jurisdiction": "undecided",
        "current_jurisdiction": "GB",
        "message": FORECAST_GB,
    }
    assert mark(service, actor, bid, plan) == (True, [withdrawn_reason(), jurisdiction])


def test_a_business_plan_after_the_withdrawal_warns_directly_above_the_forecast_figures(setup):
    _, service, actor, bid, _ = setup
    evidence = add_evidence(service, actor, bid, "Synthetic pilot quote")
    save_forecast(service, actor, bid, [evidence], title="Pilot economics")
    payload = dump(models.DocumentCreate, template="business_plan")
    before = service.generate_document(actor, bid, key(), payload)["data"]["content"]
    assert "## Financial scenarios\n\nModel: Pilot economics. " in before
    assert "WARNING:" not in before

    withdraw(service, actor, bid, evidence)
    after = service.generate_document(actor, bid, key(), payload)["data"]["content"]
    assert f"## Financial scenarios\n\nWARNING: {FORECAST_WITHDRAWN}\n\nModel: Pilot economics. " in after
    assert after.count("WARNING:") == 1


def test_verify_audit_and_export_pass_after_the_withdrawal(setup, client, capsys):
    store, service, actor, bid, token = setup
    evidence = add_evidence(service, actor, bid, "Synthetic pilot quote")
    plan = save_forecast(service, actor, bid, [evidence])
    save_deal(service, actor, bid, [evidence])
    withdraw(service, actor, bid, evidence)
    service.generate_document(actor, bid, key(), dump(models.DocumentCreate, template="business_plan"))

    audit = client.get(f"/api/businesses/{bid}/audit")
    assert audit.status_code == 200 and audit.json()["integrity"]["valid"] is True
    exported = client.get(f"/api/businesses/{bid}/export")
    assert exported.status_code == 200 and exported.json()["integrity"]["valid"] is True
    rows = [o for o in exported.json()["objects"] if o["id"] == plan["id"]]
    assert [set(o["data"]) for o in rows] == [FORECAST_FIELDS]

    (store.path.parent / "owner.key").write_text(token + "\n")
    cli.main(["--db", str(store.path), "verify", bid])
    assert json.loads(capsys.readouterr().out)["valid"] is True


def message(*evidence):
    named = [f"{e['title']} ({e['id']})" for e in evidence]
    if len(named) == 1:
        return (
            f"Evidence {named[0]} was withdrawn and cannot support an analysis. "
            "Leave it out, or cite the evidence that replaced it."
        )
    return (
        f"Evidence {', '.join(named[:-1])} and {named[-1]} were withdrawn and cannot support an analysis. "
        "Leave them out, or cite the evidence that replaced them."
    )


def refused(store, bid, call, expected):
    before = stored(store, bid)
    with pytest.raises(DomainError) as error:
        call()
    assert (error.value.status, error.value.message) == (422, expected)
    assert stored(store, bid) == before


def test_a_new_forecast_citing_withdrawn_evidence_is_refused_and_its_replay_too(setup):
    store, service, actor, bid, _ = setup
    kept = add_evidence(service, actor, bid, "Synthetic kept quote")
    gone = add_evidence(service, actor, bid, "Synthetic withdrawn quote")
    withdraw(service, actor, bid, gone)
    command = key()
    payload = forecast_payload([kept, gone])
    for _ in range(2):  # the same key and request again: still refused, nothing stored
        refused(store, bid, lambda: service.save_forecast(actor, bid, command, payload), message(gone))
    assert [o for o in service.snapshot(actor, bid)["objects"] if o["kind"] == "financial_plan"] == []
    # Without the withdrawn item the same key saves the forecast.
    saved = service.save_forecast(actor, bid, command, forecast_payload([kept]))
    assert saved["data"]["evidence_ids"] == [kept["id"]]


def test_a_new_deal_citing_withdrawn_evidence_or_adjustment_evidence_is_refused(setup):
    store, service, actor, bid, _ = setup
    first = add_evidence(service, actor, bid, "Synthetic first listing")
    second = add_evidence(service, actor, bid, "Synthetic payroll record")
    kept = add_evidence(service, actor, bid, "Synthetic kept listing")
    withdraw(service, actor, bid, first)
    withdraw(service, actor, bid, second)

    command = key()
    in_evidence = deal_payload([kept, first])
    for _ in range(2):
        refused(store, bid, lambda: service.save_deal(actor, bid, command, in_evidence), message(first))
    refused(store, bid, lambda: save_deal(service, actor, bid, [kept], adjustments=[second]), message(second))
    # Each withdrawn item is named once, in the order cited: evidence first, then adjustments.
    refused(
        store,
        bid,
        lambda: save_deal(service, actor, bid, [first, kept], adjustments=[second, first]),
        message(first, second),
    )
    assert [o for o in service.snapshot(actor, bid)["objects"] if o["kind"] == "deal"] == []


def test_the_api_refuses_an_analysis_citing_withdrawn_evidence_with_the_message(setup, client):
    _, service, actor, bid, _ = setup
    gone = add_evidence(service, actor, bid, "Synthetic withdrawn quote")
    withdraw(service, actor, bid, gone)
    for path, body in (("forecasts", forecast_payload([gone])), ("deals", deal_payload([gone]))):
        response = client.post(f"/api/businesses/{bid}/{path}", json=body, headers={"Idempotency-Key": key()})
        assert (response.status_code, response.json()) == (422, {"detail": message(gone)})


def test_a_record_may_still_cite_withdrawn_evidence(setup):
    _, service, actor, bid, _ = setup
    gone = add_evidence(service, actor, bid, "Synthetic withdrawn quote")
    withdraw(service, actor, bid, gone)
    fields = {
        "kind": "risk",
        "title": "The pilot quote was withdrawn",
        "details": "Synthetic: the customer withdrew the quote this risk discusses.",
        "certainty": "reported",
        "evidence_ids": [gone["id"]],
    }
    record = service.add_record(actor, bid, key(), dump(models.RecordCreate, **fields))
    assert record["data"]["evidence_ids"] == [gone["id"]]
    updated = service.add_record(
        actor,
        bid,
        key(),
        dump(
            models.RecordUpdate,
            **{**fields, "details": "Synthetic, revised."},
            expected_version=record["version"],
        ),
        object_id=record["id"],
    )
    assert updated["data"]["evidence_ids"] == [gone["id"]]
    assert updated["data"]["details"] == "Synthetic, revised."


# Evidence a saved analysis cites that the business does not hold. Only a damaged or hand-edited
# database has such a row, so these tests write it straight to the database.
DEAL_MISSING = (
    "Evidence this deal analysis cites is not in this business's records. "
    "Run a revised analysis before relying on these figures."
)
FORECAST_MISSING = (
    "Evidence this forecast cites is not in this business's records. "
    "Save a revised forecast before relying on these figures."
)


def missing_reason(kind):
    return {"code": "evidence_missing", "message": DEAL_MISSING if kind == "deal" else FORECAST_MISSING}


def overwrite(store, item, **fields):
    """Replace fields of a stored row's data, as a damaged or hand-edited database would hold it."""
    with store.write() as con:
        (raw,) = con.execute("SELECT data FROM objects WHERE id=?", (item["id"],)).fetchone()
        con.execute(
            "UPDATE objects SET data=? WHERE id=?", (json.dumps(json.loads(raw) | fields), item["id"])
        )


def test_a_forecast_citing_evidence_the_business_does_not_hold_is_marked_and_the_snapshot_still_reads(
    setup, client
):
    store, service, actor, bid, _ = setup
    plan = save_forecast(service, actor, bid)
    overwrite(store, plan, evidence_ids=["evidence_" + "0" * 20])
    assert mark(service, actor, bid, plan) == (True, [missing_reason("financial_plan")])
    for path in ("", "/guide", "/audit", "/export"):
        assert client.get(f"/api/businesses/{bid}{path}").status_code == 200, path
    content = service.generate_document(
        actor, bid, key(), dump(models.DocumentCreate, template="business_plan")
    )
    assert f"## Financial scenarios\n\nWARNING: {FORECAST_MISSING}\n\nModel: " in content["data"]["content"]


def test_a_deal_citing_a_held_and_a_missing_id_is_marked_and_its_steps_still_read(setup, client):
    store, service, actor, bid, _ = setup
    held = add_evidence(service, actor, bid, "Synthetic listing")
    deal = save_deal(service, actor, bid, [held])
    overwrite(store, deal, evidence_versions={held["id"]: held["version"], "evidence_" + "0" * 20: 1})
    assert mark(service, actor, bid, deal) == (True, [missing_reason("deal")])
    snapshot = client.get(f"/api/businesses/{bid}")
    assert snapshot.status_code == 200
    steps = [
        o for o in snapshot.json()["objects"] if o["kind"] == "task" and o["data"]["deal_id"] == deal["id"]
    ]
    assert steps, "the deal has its steps"
    # A missing id blocks no step; only changed evidence does, as before.
    assert not any("Supporting deal evidence changed" in b for s in steps for b in s["blockers"])
    assert client.get(f"/api/businesses/{bid}/guide").status_code == 200

    review(service, actor, bid, held)  # the held item changes too: both reasons, changed first
    assert mark(service, actor, bid, deal) == (True, [deal_reason(), missing_reason("deal")])
    assert any(
        "Supporting deal evidence changed" in b for b in current(service, actor, bid, steps[0])["blockers"]
    )


def test_ids_of_other_records_and_of_another_business_count_as_missing(setup):
    store, service, actor, bid, _ = setup
    other = service.create_business(
        actor,
        key(),
        dump(models.BusinessCreate, name="Other business", concept="A synthetic second business."),
    )
    foreign = add_evidence(service, actor, other["id"], "Synthetic foreign listing")
    a_step = next(o for o in service.snapshot(actor, bid)["objects"] if o["kind"] == "task")
    for cited in ([foreign["id"]], [a_step["id"]], [{"not": "an id"}], [7]):
        plan = save_forecast(service, actor, bid)
        overwrite(store, plan, evidence_ids=cited)
        assert mark(service, actor, bid, plan) == (True, [missing_reason("financial_plan")]), cited


def test_stored_citations_of_the_wrong_shape_cite_nothing(setup):
    store, service, actor, bid, _ = setup
    for shape in (None, "evidence_x", {"evidence_x": 1}, 7):
        plan = save_forecast(service, actor, bid)
        overwrite(store, plan, evidence_ids=shape)
        assert mark(service, actor, bid, plan) == (False, []), shape
    for shape in (None, ["evidence_x"], "evidence_x", 7):
        deal = save_deal(service, actor, bid)
        overwrite(store, deal, evidence_versions=shape)
        assert mark(service, actor, bid, deal) == (False, []), shape


def test_every_reason_together_comes_in_order(setup):
    store, service, actor, bid, _ = setup
    gone = add_evidence(service, actor, bid, "Synthetic withdrawn quote")
    plan = save_forecast(service, actor, bid, [gone])
    overwrite(store, plan, evidence_ids=[gone["id"], "evidence_" + "0" * 20])
    withdraw(service, actor, bid, gone)
    version = service.snapshot(actor, bid)["business"]["version"]
    payload = dump(
        models.JurisdictionChange, expected_version=version, jurisdiction="GB", reason="Synthetic."
    )
    service.change_jurisdiction(actor, bid, key(), payload)
    stale, reasons = mark(service, actor, bid, plan)
    assert stale and [r["code"] for r in reasons] == [
        "evidence_changed",
        "evidence_missing",
        "jurisdiction_changed",
    ]
    assert reasons[:2] == [withdrawn_reason(), missing_reason("financial_plan")]


# A step cannot cite withdrawn evidence, whatever the move.
def step_refusal(*evidence):
    named = [f"{e['title']} ({e['id']})" for e in evidence]
    if len(named) == 1:
        return f"Evidence {named[0]} was withdrawn and cannot support a step. Leave it out, or cite the evidence that replaced it."
    return (
        f"Evidence {', '.join(named[:-1])} and {named[-1]} were withdrawn and cannot support a step. "
        "Leave them out, or cite the evidence that replaced them."
    )


def free_step(service, actor, bid):
    """A pending step with nothing blocking it."""
    return next(
        o
        for o in service.snapshot(actor, bid)["objects"]
        if o["kind"] == "task" and o["state"] == "pending" and not o["blockers"] and not o["data"]["external"]
    )


def move(service, actor, bid, step, state, evidence=(), command=None):
    payload = dump(
        models.TaskTransition,
        state=state,
        expected_version=current(service, actor, bid, step)["version"],
        note="Synthetic decision note; it may name the withdrawn quote in words.",
        evidence_ids=[e["id"] for e in evidence],
    )
    return service.transition(actor, bid, step["id"], command or key(), payload)


def test_every_step_move_citing_withdrawn_evidence_is_refused_and_stores_nothing(setup):
    store, service, actor, bid, _ = setup
    reviewed = add_evidence(service, actor, bid, "Synthetic reviewed minutes")
    review(service, actor, bid, reviewed)
    reviewed = current(service, actor, bid, reviewed)
    gone = add_evidence(service, actor, bid, "Synthetic withdrawn quote")
    withdraw(service, actor, bid, gone)
    step = free_step(service, actor, bid)

    def refused_moves(states):
        for state in states:
            before = current(service, actor, bid, step)
            command = key()
            for _ in range(2):  # a replay of the refused request is refused again
                refused(
                    store,
                    bid,
                    lambda s=state, c=command: move(service, actor, bid, step, s, [reviewed, gone], c),
                    step_refusal(gone),
                )
            assert current(service, actor, bid, step) == before, state

    refused_moves(["in_progress", "prepared", "not_applicable"])  # from pending
    move(service, actor, bid, step, "prepared", [reviewed])
    refused_moves(["approved", "needs_review"])  # from prepared
    move(service, actor, bid, step, "approved", [reviewed])
    refused_moves(["submitted", "verified"])  # from approved
    assert current(service, actor, bid, step)["state"] == "approved"


def test_an_approval_citing_withdrawn_evidence_is_told_it_was_withdrawn_not_to_review_it(setup):
    store, service, actor, bid, _ = setup
    unreviewed = add_evidence(service, actor, bid, "Synthetic unreviewed note")
    first = add_evidence(service, actor, bid, "Synthetic first quote")
    second = add_evidence(service, actor, bid, "Synthetic second quote")
    withdraw(service, actor, bid, first)
    withdraw(service, actor, bid, second)
    step = free_step(service, actor, bid)
    move(service, actor, bid, step, "prepared", [unreviewed])
    refused(store, bid, lambda: move(service, actor, bid, step, "approved", [first]), step_refusal(first))
    # Withdrawn items are named in the order cited, ahead of any unreviewed one.
    refused(
        store,
        bid,
        lambda: move(service, actor, bid, step, "approved", [unreviewed, second, first, second]),
        step_refusal(second, first),
    )
    # Without withdrawn evidence the review rule is as before.
    refused(
        store,
        bid,
        lambda: move(service, actor, bid, step, "approved", [unreviewed]),
        "Review evidence first: Synthetic unreviewed note",
    )
    assert current(service, actor, bid, step)["state"] == "prepared"


def run_cli(args, capsys):
    try:
        cli.main(args)
        code = 0
    except SystemExit as stopped:
        code = stopped.code
    return code, capsys.readouterr()


def test_the_forecast_and_acquire_commands_refuse_withdrawn_evidence(setup, tmp_path, capsys):
    store, service, actor, bid, token = setup
    gone = add_evidence(service, actor, bid, "Synthetic withdrawn quote")
    withdraw(service, actor, bid, gone)
    (store.path.parent / "owner.key").write_text(token + "\n")
    plan, deal = tmp_path / "plan.json", tmp_path / "deal.json"
    plan.write_text(json.dumps({"title": "CLI forecast", "evidence_ids": [gone["id"]]}))
    deal.write_text(json.dumps({**deal_payload(), "evidence_ids": [gone["id"]]}))
    before = stored(store, bid)
    for command, path in (("forecast", plan), ("acquire", deal)):
        code, out = run_cli(["--db", str(store.path), command, bid, str(path)], capsys)
        assert code == 1, command
        assert out.err.strip() == f"Proprietor: {message(gone)}", command
        assert out.out == ""
    assert stored(store, bid) == before


def test_the_mcp_tools_return_the_refusal_for_withdrawn_evidence(setup):
    from test_mcp import foundry_api, run_client

    store, service, owner, bid, _ = setup
    agent = service.add_principal(owner, {"name": "Synthetic agent", "role": "agent", "business_ids": [bid]})
    gone = add_evidence(service, owner, bid, "Synthetic withdrawn quote")
    withdraw(service, owner, bid, gone)
    step = free_step(service, owner, bid)
    before = stored(store, bid)

    async def check(client):
        calls = [
            (
                "build_forecast",
                {
                    "business_id": bid,
                    "command_key": "mcp-forecast",
                    "title": "Agent forecast",
                    "inputs": {"months": 12},
                    "evidence_ids": [gone["id"]],
                },
                message(gone),
            ),
            (
                "analyze_acquisition",
                {"business_id": bid, "command_key": "mcp-deal", "inputs": deal_payload([gone])},
                message(gone),
            ),
            (
                "prepare_task",
                {
                    "business_id": bid,
                    "task_id": step["id"],
                    "command_key": "mcp-prepare",
                    "expected_version": step["version"],
                    "note": "Synthetic preparation note.",
                    "evidence_ids": [gone["id"]],
                },
                step_refusal(gone),
            ),
        ]
        for tool, arguments, expected in calls:
            result = await client.call_tool(tool, arguments)
            assert result.isError, tool
            assert expected in result.content[0].text, (tool, result.content[0].text)

    with foundry_api(store) as url:
        run_client(url, agent["token"], check)
    assert stored(store, bid) == before


def test_start_here_asks_for_a_revised_forecast_after_its_evidence_is_withdrawn(setup):
    _, service, actor, bid, _ = setup
    quote = add_evidence(service, actor, bid, "Synthetic pilot quote")
    save_forecast(service, actor, bid, [quote])

    def forecast_stop():
        return next(s for s in service.guide(actor, bid)["path"]["stops"] if s["key"] == "forecast")

    assert forecast_stop()["done"] is True
    withdraw(service, actor, bid, quote)
    stop = forecast_stop()
    assert stop["title"] == "Review cash needs"
    assert stop["done"] is False
    assert stop["action"] == {"do": "forecast", "label": "Save a revised forecast"}
