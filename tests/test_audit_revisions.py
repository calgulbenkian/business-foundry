"""Integrity verification covers every stored revision, not only the current one."""

import json
from uuid import uuid4

import pytest

from foundry import models
from foundry.demo import seed
from foundry.store import canonical


def key():
    return str(uuid4())


def record(**values):
    return models.RecordCreate(**{"kind": "customer", "title": "Example buyer", **values}).model_dump(
        mode="json"
    )


def revise(service, actor, bid, item, title):
    payload = models.RecordUpdate(
        kind=item["kind"], title=title, details=title, expected_version=item["version"]
    ).model_dump(mode="json")
    return service.add_record(actor, bid, key(), payload, item["id"])


def history(setup, versions=3):
    store, service, actor, bid, _ = setup
    item = service.add_record(actor, bid, key(), record())
    for n in range(2, versions + 1):
        item = revise(service, actor, bid, item, f"Example buyer revision {n}")
    assert item["version"] == versions
    return item


def integrity(setup):
    _, service, actor, bid, _ = setup
    return service.audit(actor, bid)["integrity"]


def snapshot(store, object_id, version):
    with store.read() as con:
        row = con.execute(
            "SELECT snapshot FROM revisions WHERE object_id=? AND version=?", (object_id, version)
        ).fetchone()
    return json.loads(row[0])


def test_altered_historical_revision_snapshot_is_detected(setup):
    store = setup[0]
    item = history(setup)
    original = snapshot(store, item["id"], 1)
    with store.write() as con:
        con.execute(
            "UPDATE revisions SET snapshot=? WHERE object_id=? AND version=1",
            (canonical({**original, "title": "Rewritten past"}), item["id"]),
        )
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [f"Historical revision differs from history: {item['id']} version 1"]


def test_unparseable_historical_revision_snapshot_is_detected(setup):
    store = setup[0]
    item = history(setup)
    with store.write() as con:
        con.execute("UPDATE revisions SET snapshot='not json' WHERE object_id=? AND version=2", (item["id"],))
    result = integrity(setup)
    assert not result["valid"]
    assert f"Historical revision differs from history: {item['id']} version 2" in result["record_errors"]


def test_deleted_historical_revision_is_detected(setup):
    store = setup[0]
    item = history(setup)
    with store.write() as con:
        con.execute("DELETE FROM revisions WHERE object_id=? AND version=2", (item["id"],))
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [f"Historical revision missing: {item['id']} version 2"]


def test_revision_for_unrecorded_version_is_detected(setup):
    store = setup[0]
    item = history(setup, versions=2)
    invented = {**item, "version": 7, "title": "Never happened"}
    with store.write() as con:
        con.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?)",
            (item["id"], 7, canonical(invented), "principal_x", invented["updated_at"]),
        )
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [f"Revision has no recorded history: {item['id']} version 7"]


def test_revision_for_business_record_is_detected(setup):
    store, service, actor, bid, _ = setup
    business = service.audit(actor, bid)["events"][0]["payload"]
    with store.write() as con:
        con.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?)",
            (bid, 1, canonical(business), actor["id"], business["created_at"]),
        )
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [f"Unexpected revision for business record: {bid} version 1"]


def test_untouched_multi_version_history_is_valid(setup):
    store = setup[0]
    item = history(setup, versions=4)
    for version in range(1, 5):
        assert snapshot(store, item["id"], version)["version"] == version
    result = integrity(setup)
    assert result["valid"], result["record_errors"]
    assert result["record_errors"] == []


def test_other_business_history_does_not_leak_or_break_verification(setup):
    store, service, actor, bid, _ = setup
    history(setup)
    other = service.create_business(
        actor,
        key(),
        models.BusinessCreate(name="Second business", concept="Another synthetic business.").model_dump(
            mode="json"
        ),
    )
    other_item = service.add_record(actor, other["id"], key(), record(title="Other buyer"))
    with store.write() as con:
        con.execute("DELETE FROM revisions WHERE object_id=? AND version=1", (other_item["id"],))
    assert integrity(setup)["valid"]
    other_result = service.audit(actor, other["id"])["integrity"]
    assert other_result["record_errors"] == [
        f"Revision mismatch: {other_item['id']}",
        f"Historical revision missing: {other_item['id']} version 1",
    ]


def test_seeded_demo_workspace_has_no_false_positives(setup):
    _, service, actor, _, _ = setup
    bid = seed(service, actor)["id"]
    result = service.audit(actor, bid)["integrity"]
    assert result["valid"], result["record_errors"]
    assert result["record_errors"] == []
    assert set(result) == {"valid", "events_checked", "head_hash", "record_errors", "limitation"}


def test_export_integrity_matches_audit(setup):
    store, service, actor, bid, _ = setup
    item = history(setup)
    assert service.audit(actor, bid, True)["integrity"] == integrity(setup)
    assert service.audit(actor, bid, True)["integrity"]["valid"]
    with store.write() as con:
        con.execute("DELETE FROM revisions WHERE object_id=? AND version=1", (item["id"],))
    audit = integrity(setup)
    export = service.audit(actor, bid, True)["integrity"]
    assert export == audit
    assert not export["valid"]
    assert f"Historical revision missing: {item['id']} version 1" in export["record_errors"]


# Unreadable revision rows: the audit always returns a full result and never raises.

OBJECT_WITH_NAN = object()
UNREADABLE_SNAPSHOTS = [
    pytest.param("not json", id="not-json"),
    pytest.param("[]", id="json-array"),
    pytest.param('"x"', id="json-string"),
    pytest.param("5", id="json-number"),
    pytest.param("null", id="json-null"),
    pytest.param("NaN", id="bare-nan"),
    pytest.param("Infinity", id="bare-infinity"),
    pytest.param(OBJECT_WITH_NAN, id="object-with-nan"),
    pytest.param(b"\xff\xfe\x00{", id="invalid-utf8-blob"),
    pytest.param("[" * 100000 + "]" * 100000, id="nested-beyond-recursion-limit"),
]


def unreadable(store, object_id, version, value):
    if value is OBJECT_WITH_NAN:
        # The real snapshot plus one NaN member: Python's json.loads accepts it, canonical() refuses it.
        text = canonical(snapshot(store, object_id, version)).replace('"version":', '"nan":NaN,"version":', 1)
        assert "NaN" in text
        return text
    return value


@pytest.mark.parametrize("value", UNREADABLE_SNAPSHOTS)
def test_unreadable_current_revision_is_reported_without_raising(setup, value):
    store = setup[0]
    item = history(setup)
    with store.write() as con:
        con.execute(
            "UPDATE revisions SET snapshot=? WHERE object_id=? AND version=3",
            (unreadable(store, item["id"], 3, value), item["id"]),
        )
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [
        f"Revision mismatch: {item['id']}",
        f"Historical revision differs from history: {item['id']} version 3",
    ]


@pytest.mark.parametrize("value", UNREADABLE_SNAPSHOTS)
def test_unreadable_historical_revision_is_reported_without_raising(setup, value):
    store = setup[0]
    item = history(setup)
    with store.write() as con:
        con.execute(
            "UPDATE revisions SET snapshot=? WHERE object_id=? AND version=1",
            (unreadable(store, item["id"], 1, value), item["id"]),
        )
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [f"Historical revision differs from history: {item['id']} version 1"]


# JSON type is part of the value: false/0, true/1 and 1/1.0 are different stored values.

TYPED = {"flag": False, "on": True, "count": 1, "zero": 0}
TYPE_ONLY_EDITS = [
    pytest.param('"flag":false', '"flag":0', id="false-to-0"),
    pytest.param('"on":true', '"on":1', id="true-to-1"),
    pytest.param('"count":1', '"count":1.0', id="1-to-1.0"),
    pytest.param('"zero":0', '"zero":0.0', id="0-to-0.0"),
]


def typed_history(setup):
    store, service, actor, bid, _ = setup
    item = service.add_record(actor, bid, key(), record(properties=TYPED))
    payload = models.RecordUpdate(
        kind="customer", title="Example buyer v2", properties=TYPED, expected_version=1
    ).model_dump(mode="json")
    item = service.add_record(actor, bid, key(), payload, item["id"])
    assert item["version"] == 2
    for version in (1, 2):
        stored = snapshot(store, item["id"], version)["data"]["properties"]
        assert [(k, type(v)) for k, v in sorted(stored.items())] == [
            ("count", int),
            ("flag", bool),
            ("on", bool),
            ("zero", int),
        ]
    return item


def retype(text, old, new):
    assert text.count(old) == 1
    return text.replace(old, new)


@pytest.mark.parametrize(("old", "new"), TYPE_ONLY_EDITS)
def test_type_only_edit_to_current_revision_is_detected(setup, old, new):
    store = setup[0]
    item = typed_history(setup)
    with store.write() as con:
        text = con.execute(
            "SELECT snapshot FROM revisions WHERE object_id=? AND version=2", (item["id"],)
        ).fetchone()[0]
        con.execute(
            "UPDATE revisions SET snapshot=? WHERE object_id=? AND version=2",
            (retype(text, old, new), item["id"]),
        )
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [
        f"Revision mismatch: {item['id']}",
        f"Historical revision differs from history: {item['id']} version 2",
    ]


@pytest.mark.parametrize(("old", "new"), TYPE_ONLY_EDITS)
def test_type_only_edit_to_historical_revision_is_detected(setup, old, new):
    store = setup[0]
    item = typed_history(setup)
    with store.write() as con:
        text = con.execute(
            "SELECT snapshot FROM revisions WHERE object_id=? AND version=1", (item["id"],)
        ).fetchone()[0]
        con.execute(
            "UPDATE revisions SET snapshot=? WHERE object_id=? AND version=1",
            (retype(text, old, new), item["id"]),
        )
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [f"Historical revision differs from history: {item['id']} version 1"]


@pytest.mark.parametrize(("old", "new"), TYPE_ONLY_EDITS)
def test_type_only_edit_to_current_record_is_detected(setup, old, new):
    store = setup[0]
    item = typed_history(setup)
    with store.write() as con:
        text = con.execute("SELECT data FROM objects WHERE id=?", (item["id"],)).fetchone()[0]
        con.execute("UPDATE objects SET data=? WHERE id=?", (retype(text, old, new), item["id"]))
    result = integrity(setup)
    assert not result["valid"]
    # The current revision still holds the original value, so it no longer equals the record.
    assert result["record_errors"] == [
        f"Current record differs from history: {item['id']}",
        f"Revision mismatch: {item['id']}",
    ]


def test_untouched_typed_history_is_valid(setup):
    typed_history(setup)
    result = integrity(setup)
    assert result["valid"], result["record_errors"]
    assert result["record_errors"] == []


def test_revision_rewritten_with_other_whitespace_and_key_order_is_valid(setup):
    store = setup[0]
    item = typed_history(setup)

    def reordered(value):
        if isinstance(value, dict):
            return {k: reordered(value[k]) for k in sorted(value, reverse=True)}
        return value

    with store.write() as con:
        for version in (1, 2):
            original = con.execute(
                "SELECT snapshot FROM revisions WHERE object_id=? AND version=?", (item["id"], version)
            ).fetchone()[0]
            rewritten = json.dumps(reordered(json.loads(original)), indent=3, separators=(" ,  ", " : "))
            assert rewritten != original
            con.execute(
                "UPDATE revisions SET snapshot=? WHERE object_id=? AND version=?",
                (rewritten, item["id"], version),
            )
    result = integrity(setup)
    assert result["valid"], result["record_errors"]
    assert result["record_errors"] == []


def test_swapped_revision_versions_are_both_reported(setup):
    store = setup[0]
    item = history(setup)
    with store.write() as con:
        con.execute("UPDATE revisions SET version=-1 WHERE object_id=? AND version=1", (item["id"],))
        con.execute("UPDATE revisions SET version=1 WHERE object_id=? AND version=2", (item["id"],))
        con.execute("UPDATE revisions SET version=2 WHERE object_id=? AND version=-1", (item["id"],))
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [
        f"Historical revision differs from history: {item['id']} version 1",
        f"Historical revision differs from history: {item['id']} version 2",
    ]


@pytest.mark.parametrize("column", ["actor_id", "created_at"])
def test_documented_limit_revision_actor_and_timestamp_columns_are_not_checked(setup, column):
    store = setup[0]
    item = history(setup)
    with store.write() as con:
        con.execute(f"UPDATE revisions SET {column}='edited' WHERE object_id=?", (item["id"],))
    result = integrity(setup)
    assert result["valid"], result["record_errors"]
    assert result["record_errors"] == []


TAMPERS = {
    "unreadable-current-revision": (
        "UPDATE revisions SET snapshot='not json' WHERE object_id=? AND version=2",
        [
            "Revision mismatch: {id}",
            "Historical revision differs from history: {id} version 2",
        ],
    ),
    "invalid-utf8-historical-revision": (
        "UPDATE revisions SET snapshot=X'FFFE007B' WHERE object_id=? AND version=1",
        ["Historical revision differs from history: {id} version 1"],
    ),
    "type-only-historical-revision": (
        "UPDATE revisions SET snapshot=replace(snapshot,'\"flag\":false','\"flag\":0') "
        "WHERE object_id=? AND version=1",
        ["Historical revision differs from history: {id} version 1"],
    ),
    "type-only-current-record": (
        "UPDATE objects SET data=replace(data,'\"on\":true','\"on\":1') WHERE id=?",
        ["Current record differs from history: {id}", "Revision mismatch: {id}"],
    ),
}


@pytest.mark.parametrize("tamper", list(TAMPERS))
def test_tampered_business_does_not_affect_another_business(setup, tamper):
    store, service, actor, bid, _ = setup
    other = service.create_business(
        actor,
        key(),
        models.BusinessCreate(name="Second business", concept="Another synthetic business.").model_dump(
            mode="json"
        ),
    )
    service.add_record(actor, other["id"], key(), record(title="Other buyer", properties=TYPED))
    item = typed_history(setup)
    sql, expected = TAMPERS[tamper]
    with store.write() as con:
        assert con.execute(sql, (item["id"],)).rowcount == 1
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [error.format(id=item["id"]) for error in expected]
    other_result = service.audit(actor, other["id"])["integrity"]
    assert other_result["valid"], other_result["record_errors"]
    assert other_result["record_errors"] == []
