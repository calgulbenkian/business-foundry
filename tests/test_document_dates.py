"""A readiness report states which obligations are past their due date, and on which date.

That standing changes when the calendar does, not only when records change, so the snapshot marks such a
document stale with a reason when it would now read differently. Every test goes through the real service
or the HTTP API on a seeded workspace, with the service's calendar date faked.
"""

import asyncio
import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from test_mcp import foundry_api

from foundry import models
from foundry.api import create_app
from foundry.demo import seed
from foundry.documents import OVERDUE_TEMPLATES, TITLES, render_document

SOURCE_CHANGED = {
    "code": "source_changed",
    "message": "The business or its records have changed since this document was generated. "
    "Generate a new version before relying on it.",
}
AS_OF_UNREADABLE = {
    "code": "as_of_unreadable",
    "message": "The date this document was worked out for cannot be read, so it cannot be shown to be "
    "current. Generate a new version before relying on it.",
}


def overdue_changed(as_of, today):
    return {
        "code": "overdue_changed",
        "as_of": as_of,
        "today": today,
        "message": f"This document says which obligations were past their due date as of {as_of}. "
        "That has changed since. Generate a new version before relying on it.",
    }


CURRENT = (False, [])


def dump(cls, **kwargs):
    return cls(**kwargs).model_dump(mode="json")


def key():
    return str(uuid4())


class Clock:
    """The calendar date the service reads. ``step`` days are added after every read."""

    def __init__(self, monkeypatch, day, step=0):
        self.day, self.step, self.reads = date.fromisoformat(day), step, 0
        clock = self

        class FakeDate(date):
            @classmethod
            def today(cls):
                clock.reads += 1
                today = clock.day
                clock.day += timedelta(days=clock.step)
                return cls.fromordinal(today.toordinal())

        monkeypatch.setattr("foundry.service.date", FakeDate)

    def set(self, day):
        self.day = date.fromisoformat(day)


def obligation(s, a, bid, title, due):
    return s.add_record(
        a,
        bid,
        key(),
        dump(models.RecordCreate, kind="obligation", title=title, certainty="reported", due_date=due),
    )


def generate(s, a, bid, template="readiness_report", deal_id=None):
    return s.generate_document(a, bid, key(), dump(models.DocumentCreate, template=template, deal_id=deal_id))


def mark(snapshot, document):
    found = next(o for o in snapshot["objects"] if o["id"] == document["id"])
    return found["stale"], found["stale_reasons"]


def section(content, heading="## Money and deadlines"):
    lines = content.splitlines()
    start = lines.index(heading) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return [x for x in lines[start:end] if x]


def edit_stored(store, document, change):
    """Edit a document's stored data outside the application, as only someone with the database can."""
    with sqlite3.connect(store.path) as con:
        (raw,) = con.execute("SELECT data FROM objects WHERE id=?", (document["id"],)).fetchone()
        data = json.loads(raw)
        change(data)
        con.execute("UPDATE objects SET data=? WHERE id=?", (json.dumps(data), document["id"]))
    con.close()


def test_a_report_made_on_the_due_date_is_current_that_day_and_stale_the_next(setup, monkeypatch):
    _, s, a, bid, _ = setup
    clock = Clock(monkeypatch, "2030-06-01")
    obligation(s, a, bid, "Annual filing", "2030-06-01")
    report = generate(s, a, bid)
    assert report["data"]["as_of"] == "2030-06-01"
    assert section(report["data"]["content"]) == [
        "No outstanding invoices or collected payments are recorded. "
        "No obligation is past its due date as of 2030-06-01."
    ]
    assert mark(s.snapshot(a, bid), report) == CURRENT

    clock.set("2030-06-02")
    snapshot = s.snapshot(a, bid)
    assert snapshot["as_of"] == "2030-06-02" and snapshot["stats"]["overdue_obligations"] == 1
    assert mark(snapshot, report) == (True, [overdue_changed("2030-06-01", "2030-06-02")])
    newer = generate(s, a, bid)
    assert section(newer["data"]["content"]) == [
        "- Obligations past their due date as of 2030-06-02: 1.",
        "  - Annual filing — due 2030-06-01.",
    ]
    snapshot = s.snapshot(a, bid)
    assert mark(snapshot, newer) == CURRENT
    assert mark(snapshot, report) == (True, [overdue_changed("2030-06-01", "2030-06-02")])

    clock.set("2030-07-01")
    snapshot = s.snapshot(a, bid)
    assert mark(snapshot, report) == (True, [overdue_changed("2030-06-01", "2030-07-01")])
    assert mark(snapshot, newer) == CURRENT


def test_a_report_made_once_an_obligation_is_overdue_stays_current_as_days_pass(setup, monkeypatch):
    _, s, a, bid, _ = setup
    clock = Clock(monkeypatch, "2030-06-05")
    obligation(s, a, bid, "Annual filing", "2030-06-01")
    report = generate(s, a, bid)
    assert section(report["data"]["content"]) == [
        "- Obligations past their due date as of 2030-06-05: 1.",
        "  - Annual filing — due 2030-06-01.",
    ]
    for day in ("2030-06-06", "2030-07-05", "2031-06-05"):
        clock.set(day)
        assert mark(s.snapshot(a, bid), report) == CURRENT


def test_no_due_date_and_a_far_due_date_never_mark_a_report(setup, monkeypatch):
    _, s, a, bid, _ = setup
    clock = Clock(monkeypatch, "2030-06-01")
    obligation(s, a, bid, "Annual return", None)
    obligation(s, a, bid, "Licence renewal", "2999-12-31")
    report = generate(s, a, bid)
    for day in ("2000-01-01", "2030-05-31", "2030-06-02", "2031-01-01", "2999-12-31"):
        clock.set(day)
        assert mark(s.snapshot(a, bid), report) == CURRENT


def test_a_clock_set_earlier_than_the_report_across_a_due_date_marks_it(setup, monkeypatch):
    _, s, a, bid, _ = setup
    clock = Clock(monkeypatch, "2030-06-05")
    obligation(s, a, bid, "Annual filing", "2030-06-03")
    report = generate(s, a, bid)
    for day, expected in (
        ("2030-06-02", (True, [overdue_changed("2030-06-05", "2030-06-02")])),
        ("2030-06-03", (True, [overdue_changed("2030-06-05", "2030-06-03")])),
        ("2030-06-04", CURRENT),
    ):
        clock.set(day)
        assert mark(s.snapshot(a, bid), report) == expected


def test_a_changed_record_and_a_passed_due_date_are_both_listed_in_order(setup, monkeypatch):
    _, s, a, bid, _ = setup
    clock = Clock(monkeypatch, "2030-06-01")
    obligation(s, a, bid, "Annual filing", "2030-06-01")
    report = generate(s, a, bid)
    obligation(s, a, bid, "Licence renewal", "2999-12-31")
    assert mark(s.snapshot(a, bid), report) == (True, [SOURCE_CHANGED])
    clock.set("2030-06-02")
    assert mark(s.snapshot(a, bid), report) == (
        True,
        [SOURCE_CHANGED, overdue_changed("2030-06-01", "2030-06-02")],
    )


def test_a_document_without_a_stored_date_is_marked_by_its_fingerprint_only(setup, monkeypatch):
    store, s, a, bid, _ = setup
    clock = Clock(monkeypatch, "2030-06-01")
    obligation(s, a, bid, "Annual filing", "2030-06-01")
    report = generate(s, a, bid)
    edit_stored(store, report, lambda data: data.pop("as_of"))
    clock.set("2030-06-02")
    assert mark(s.snapshot(a, bid), report) == CURRENT
    obligation(s, a, bid, "Licence renewal", "2999-12-31")
    assert mark(s.snapshot(a, bid), report) == (True, [SOURCE_CHANGED])


@pytest.mark.parametrize("stored", ["20261112", 5, None, "", [], ["2030-06-01"]])
def test_an_unreadable_stored_date_marks_the_report_without_repeating_it(setup, monkeypatch, stored):
    store, s, a, bid, _ = setup
    Clock(monkeypatch, "2030-06-01")
    obligation(s, a, bid, "Annual filing", "2030-06-01")
    report = generate(s, a, bid)
    edit_stored(store, report, lambda data: data.update(as_of=stored))
    stale, reasons = mark(s.snapshot(a, bid), report)
    assert (stale, reasons) == (True, [AS_OF_UNREADABLE])
    if stored not in ("", []):
        assert str(stored) not in reasons[0]["message"]
        assert json.dumps(stored) not in reasons[0]["message"]


def test_only_templates_that_state_overdue_standing_change_when_a_due_date_passes(setup, monkeypatch):
    """Every template in TITLES, generated from the same records the day before and the day after a due date."""
    _, s, a, _, _ = setup
    bid = seed(s, a)["id"]
    snapshot = s.snapshot(a, bid)
    deal = next(o for o in snapshot["objects"] if o["kind"] == "deal")
    [due] = {o["data"]["due_date"] for o in snapshot["objects"] if o["kind"] == "obligation"}
    before = (date.fromisoformat(due) - timedelta(days=1)).isoformat()
    after = (date.fromisoformat(due) + timedelta(days=1)).isoformat()
    clock = Clock(monkeypatch, before)
    assert OVERDUE_TEMPLATES and OVERDUE_TEMPLATES <= set(TITLES)

    def generate_all():
        return {t: generate(s, a, bid, t, deal["id"] if t == "investment_memo" else None) for t in TITLES}

    first = generate_all()
    clock.set(after)
    second = generate_all()
    snapshot = s.snapshot(a, bid)
    for template in TITLES:
        same = first[template]["data"]["content"] == second[template]["data"]["content"]
        assert same is (template not in OVERDUE_TEMPLATES), template
        assert first[template]["data"]["source_fingerprint"] == second[template]["data"]["source_fingerprint"]
        expected = [overdue_changed(before, after)] if template in OVERDUE_TEMPLATES else []
        assert mark(snapshot, first[template]) == (bool(expected), expected), template
        assert mark(snapshot, second[template]) == CURRENT, template


def test_one_clock_read_per_snapshot_so_every_date_and_flag_agree(setup, monkeypatch):
    _, s, a, bid, _ = setup
    clock = Clock(monkeypatch, "2030-06-04", step=1)
    dues = [(date(2030, 6, 1) + timedelta(days=n)).isoformat() for n in range(12)]
    for n, due in enumerate(dues):
        obligation(s, a, bid, f"Obligation {n:02d}", due)

    reads = clock.reads
    snapshot = s.snapshot(a, bid)
    assert clock.reads == reads + 1
    as_of = snapshot["as_of"]
    flags = {o["data"]["due_date"]: o["overdue"] for o in snapshot["objects"] if o["kind"] == "obligation"}
    assert flags == {due: due < as_of for due in dues}
    assert snapshot["stats"]["overdue_obligations"] == sum(due < as_of for due in dues) > 0

    reads = clock.reads
    report = generate(s, a, bid)
    assert clock.reads == reads + 1
    stored = report["data"]["as_of"]
    assert stored > as_of
    overdue = sorted(due for due in dues if due < stored)
    assert section(report["data"]["content"]) == [
        f"- Obligations past their due date as of {stored}: {len(overdue)}.",
        *(f"  - Obligation {dues.index(due):02d} — due {due}." for due in overdue),
    ]
    # Rendering the same snapshot twice gives the same text on any day: the renderer reads no clock.
    snapshot = s.snapshot(a, bid)
    assert render_document("readiness_report", snapshot) == render_document("readiness_report", snapshot)


def test_http_and_the_agent_adapter_show_the_same_dates_and_reasons(setup, client, monkeypatch):
    store, s, owner, bid, _ = setup
    clock = Clock(monkeypatch, "2030-06-01")
    agent = s.add_principal(owner, {"name": "Date agent", "role": "agent", "business_ids": [bid]})
    obligation(s, owner, bid, "Annual filing", "2030-06-01")
    response = client.post(
        f"/api/businesses/{bid}/documents",
        json={"template": "readiness_report"},
        headers={"Idempotency-Key": key()},
    )
    assert response.status_code == 201
    report = response.json()
    plan = generate(s, owner, bid, "business_plan")
    assert report["data"]["as_of"] == plan["data"]["as_of"] == "2030-06-01"
    clock.set("2030-06-02")

    def shapes(snapshot):
        return (
            snapshot["as_of"],
            snapshot["stats"]["overdue_obligations"],
            mark(snapshot, report),
            mark(snapshot, plan),
        )

    expected = ("2030-06-02", 1, (True, [overdue_changed("2030-06-01", "2030-06-02")]), CURRENT)
    assert shapes(s.snapshot(owner, bid)) == expected
    assert shapes(client.get(f"/api/businesses/{bid}").json()) == expected
    app = create_app(store.path, allowed_hosts=["testserver"])
    with TestClient(app, headers={"Authorization": "Bearer " + agent["token"]}) as agent_client:
        assert shapes(agent_client.get(f"/api/businesses/{bid}").json()) == expected

    async def check(url):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "foundry.cli", "mcp"],
            env={**os.environ, "FOUNDRY_API_URL": url, "FOUNDRY_API_TOKEN": agent["token"]},
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("get_business", {"business_id": bid})
                assert shapes(json.loads(result.content[0].text)) == expected

    with foundry_api(store) as url:
        asyncio.run(check(url))


def test_the_integrity_check_is_valid_after_documents_store_their_date(setup, monkeypatch):
    _, s, a, _, _ = setup
    clock = Clock(monkeypatch, "2030-06-01")
    bid = seed(s, a)["id"]
    obligation(s, a, bid, "Annual filing", "2030-06-01")
    made = [generate(s, a, bid, template)["id"] for template in ("readiness_report", "business_plan")]
    clock.set("2030-06-02")
    made.append(generate(s, a, bid)["id"])
    integrity = s.audit(a, bid)["integrity"]
    assert integrity["valid"] is True and integrity["record_errors"] == []
    exported = {o["id"]: o for o in s.audit(a, bid, export=True)["objects"]}
    assert [exported[i]["data"]["as_of"] for i in made] == ["2030-06-01", "2030-06-01", "2030-06-02"]
