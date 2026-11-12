"""Integrity verification covers every stored revision, not only the current one."""

import hashlib
import json
import sqlite3
import sys
from typing import NamedTuple
from uuid import uuid4

import pytest
from conftest import reviewed

from foundry import models
from foundry.demo import seed
from foundry.service import Service, read_json, same_json, stored_literal
from foundry.store import Store, canonical, digest


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
    pytest.param('{"n":1E400}', id="object-with-overflow"),
    pytest.param('{"n":"\\ud800"}', id="object-with-surrogate-value"),
    pytest.param('{"\\udc00":1}', id="object-with-surrogate-key"),
    pytest.param('{"n":1' + "0" * 4300 + "}", id="integer-over-digit-limit"),
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
    "invalid-utf8-text-current-revision": (
        "UPDATE revisions SET snapshot=CAST(X'FF7B' AS TEXT) WHERE object_id=? AND version=2",
        [
            "Revision mismatch: {id}",
            "Historical revision differs from history: {id} version 2",
        ],
    ),
    "invalid-utf8-text-historical-revision": (
        "UPDATE revisions SET snapshot=CAST(X'FF7B' AS TEXT) WHERE object_id=? AND version=1",
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


# Revision snapshots in every SQLite storage class. sqlite3 raises while fetching TEXT that
# is not valid UTF-8, so these values cannot be bound from Python as str; they are written
# with SQL expressions and their storage class is checked before the audit runs.


REVISION_COLUMNS = {
    "object_id": "TEXT",
    "version": "INTEGER",
    "snapshot": "TEXT",
    "actor_id": "TEXT",
    "created_at": "TEXT",
}


def untyped_column(store, column):
    """Rewrite the revisions schema so one column has no type affinity and accepts NULL.

    With the shipped schema (every column declared with a type and NOT NULL) a TEXT column
    stores an INTEGER or REAL as TEXT, the INTEGER version column stores numeric TEXT as a
    number, and NULL is refused, so those storage classes only exist after a rewrite.
    """
    declared = f"{column} {REVISION_COLUMNS[column]} NOT NULL"
    con = sqlite3.connect(store.path, isolation_level=None)
    try:
        con.execute("PRAGMA writable_schema=ON")
        changed = con.execute(
            "UPDATE sqlite_schema SET sql=replace(sql,?,?) WHERE type='table' AND name='revisions' "
            "AND instr(sql,?)",
            (declared, column, declared),
        ).rowcount
        con.execute("PRAGMA writable_schema=OFF")
    finally:
        con.close()
    assert changed == 1


def untyped_snapshot_column(store):
    untyped_column(store, "snapshot")


def test_shipped_schema_stores_numbers_as_text_and_refuses_null(setup):
    store = setup[0]
    item = history(setup)
    with store.write() as con:
        con.execute("UPDATE revisions SET snapshot=? WHERE object_id=? AND version=1", (5, item["id"]))
        con.execute("UPDATE revisions SET snapshot=? WHERE object_id=? AND version=2", (1.5, item["id"]))
        rows = con.execute(
            "SELECT version,typeof(snapshot),snapshot FROM revisions WHERE object_id=? AND version<3 "
            "ORDER BY version",
            (item["id"],),
        ).fetchall()
        assert [tuple(row) for row in rows] == [(1, "text", "5"), (2, "text", "1.5")]
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("UPDATE revisions SET snapshot=NULL WHERE object_id=? AND version=3", (item["id"],))


STORED_SNAPSHOTS = [
    pytest.param("CAST(X'FF7B' AS TEXT)", "text", False, id="invalid-utf8-text"),
    pytest.param("CAST(X'7B22223AFF7D' AS TEXT)", "text", False, id="invalid-utf8-text-inside-object"),
    pytest.param("CAST(X'22EDA08022' AS TEXT)", "text", False, id="lone-surrogate-utf8-text"),
    pytest.param("CAST(X'' AS TEXT)", "text", False, id="empty-text"),
    pytest.param("X''", "blob", False, id="empty-blob"),
    pytest.param("CAST(X'EFBBBF' AS TEXT) || snapshot", "text", False, id="utf8-bom-text"),
    pytest.param("CAST(X'EFBBBF' || snapshot AS BLOB)", "blob", False, id="utf8-bom-blob"),
    pytest.param("5", "integer", True, id="integer"),
    pytest.param("-9223372036854775808", "integer", True, id="integer-minimum"),
    pytest.param("1.5", "real", True, id="real"),
    pytest.param("1e308 * 10", "real", True, id="real-infinity"),
    pytest.param("NULL", "null", True, id="null"),
]


def store_snapshot(store, object_id, version, expression, storage, untyped):
    if untyped:
        untyped_snapshot_column(store)
    with store.write() as con:
        changed = con.execute(
            f"UPDATE revisions SET snapshot={expression} WHERE object_id=? AND version=?",
            (object_id, version),
        ).rowcount
        stored = con.execute(
            "SELECT typeof(snapshot) FROM revisions WHERE object_id=? AND version=?", (object_id, version)
        ).fetchone()[0]
    assert changed == 1
    assert stored == storage


@pytest.mark.parametrize(("expression", "storage", "untyped"), STORED_SNAPSHOTS)
def test_current_revision_in_any_storage_class_is_reported_without_raising(
    setup, expression, storage, untyped
):
    store = setup[0]
    item = history(setup)
    store_snapshot(store, item["id"], 3, expression, storage, untyped)
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [
        f"Revision mismatch: {item['id']}",
        f"Historical revision differs from history: {item['id']} version 3",
    ]


@pytest.mark.parametrize(("expression", "storage", "untyped"), STORED_SNAPSHOTS)
def test_historical_revision_in_any_storage_class_is_reported_without_raising(
    setup, expression, storage, untyped
):
    store = setup[0]
    item = history(setup)
    store_snapshot(store, item["id"], 1, expression, storage, untyped)
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [f"Historical revision differs from history: {item['id']} version 1"]


def test_invalid_utf8_text_could_not_be_fetched_by_sqlite3(setup):
    # The failure this change avoids: fetching the column itself raises in sqlite3.
    store = setup[0]
    item = history(setup)
    store_snapshot(store, item["id"], 1, "CAST(X'FF7B' AS TEXT)", "text", False)
    with store.read() as con, pytest.raises(sqlite3.OperationalError, match="UTF-8"):
        con.execute(
            "SELECT snapshot FROM revisions WHERE object_id=? AND version=1", (item["id"],)
        ).fetchone()


def test_other_business_verifies_while_revisions_hold_every_storage_class(setup):
    store, service, actor, bid, _ = setup
    other = service.create_business(
        actor,
        key(),
        models.BusinessCreate(name="Second business", concept="Another synthetic business.").model_dump(
            mode="json"
        ),
    )
    other_item = service.add_record(actor, other["id"], key(), record(title="Other buyer", properties=TYPED))
    other_item = revise(service, actor, other["id"], other_item, "Other buyer revision 2")
    item = history(setup, versions=5)
    untyped_snapshot_column(store)
    damage = {1: "CAST(X'FF7B' AS TEXT)", 2: "5", 3: "1.5", 4: "NULL", 5: "CAST(X'FF7B' AS TEXT)"}
    with store.write() as con:
        for version, expression in damage.items():
            con.execute(
                f"UPDATE revisions SET snapshot={expression} WHERE object_id=? AND version=?",
                (item["id"], version),
            )
        stored = con.execute(
            "SELECT typeof(snapshot) FROM revisions WHERE object_id=? ORDER BY version", (item["id"],)
        ).fetchall()
    assert [row[0] for row in stored] == ["text", "integer", "real", "null", "text"]
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [
        f"Revision mismatch: {item['id']}",
        f"Historical revision differs from history: {item['id']} version 1",
        f"Historical revision differs from history: {item['id']} version 2",
        f"Historical revision differs from history: {item['id']} version 3",
        f"Historical revision differs from history: {item['id']} version 4",
        f"Historical revision differs from history: {item['id']} version 5",
    ]
    other_result = service.audit(actor, other["id"])["integrity"]
    assert other_result["valid"], other_result["record_errors"]
    assert other_result["record_errors"] == []


def test_valid_snapshot_stored_as_utf8_blob_is_valid(setup):
    # Documented: the same JSON stored as a BLOB instead of text is not told apart.
    store = setup[0]
    item = history(setup)
    store_snapshot(store, item["id"], 1, "CAST(snapshot AS BLOB)", "blob", False)
    store_snapshot(store, item["id"], 3, "CAST(snapshot AS BLOB)", "blob", False)
    result = integrity(setup)
    assert result["valid"], result["record_errors"]
    assert result["record_errors"] == []


def test_valid_snapshot_in_utf16_blob_is_reported(setup):
    # Bytes are decoded in the database encoding, not guessed: UTF-16 in a UTF-8 database differs.
    store = setup[0]
    item = history(setup)
    text = canonical(snapshot(store, item["id"], 1))
    with store.write() as con:
        con.execute(
            "UPDATE revisions SET snapshot=? WHERE object_id=? AND version=1",
            (text.encode("utf-16"), item["id"]),
        )
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [f"Historical revision differs from history: {item['id']} version 1"]


@pytest.mark.parametrize("encoding", ["UTF-16le", "UTF-16be"])
def test_utf16_database_verifies_text_snapshots_and_reports_damage(tmp_path, encoding):
    path = tmp_path / "private" / "foundry.db"
    path.parent.mkdir()
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA encoding='{encoding}'")
    con.execute("CREATE TABLE encoding_marker(x)")
    con.close()
    store = Store(path)
    with store.read() as con:
        assert con.execute("PRAGMA encoding").fetchone()[0] == encoding
    actor = store.authenticate(store.bootstrap()["token"])
    service = Service(store)
    bid = service.create_business(
        actor,
        key(),
        models.BusinessCreate(name="Encoded business", concept="A synthetic business.").model_dump(
            mode="json"
        ),
    )["id"]
    setup = (store, service, actor, bid, None)
    item = history(setup)
    result = integrity(setup)
    assert result["valid"], result["record_errors"]
    assert result["record_errors"] == []
    with store.write() as con:
        con.execute(
            "UPDATE revisions SET snapshot=replace(snapshot,'Example buyer','Rewritten buyer') "
            "WHERE object_id=? AND version=1",
            (item["id"],),
        )
    result = integrity(setup)
    assert result["record_errors"] == [f"Historical revision differs from history: {item['id']} version 1"]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="none"),
        pytest.param(5, id="int"),
        pytest.param(1.5, id="float"),
        pytest.param(b"\xff{", id="invalid-utf8"),
        pytest.param(b"\xef\xbb\xbf{}", id="utf8-bom"),
        pytest.param("{}".encode("utf-16"), id="utf16-with-bom"),
        pytest.param("{}".encode("utf-32"), id="utf32-with-bom"),
        pytest.param("{}".encode("utf-16-le"), id="utf16le-without-bom"),
        pytest.param(b'"\xed\xa0\x80"', id="utf8-lone-surrogate"),
        pytest.param("﻿{}", id="str-with-bom"),
    ],
)
def test_read_json_returns_none_for_unreadable_values(value):
    assert read_json(value) is None


def test_read_json_reads_utf8_bytes_and_text_alike():
    assert read_json(b'{"a": [1, 2.5, true]}') == {"a": [1, 2.5, True]}
    assert read_json('{"a": [1, 2.5, true]}') == {"a": [1, 2.5, True]}
    assert read_json('{"a":"é"}'.encode()) == {"a": "é"}
    assert read_json('{"a":"é"}'.encode("utf-16-le"), "UTF-16le") == {"a": "é"}


# Revision versions that are not integers. A TEXT version that is not valid UTF-8 could not be
# fetched by sqlite3 at all; a TEXT, BLOB or NULL version matches no chained event and is shown
# as an SQLite literal, so the message stays printable ASCII.

INVALID_UTF8_VERSIONS = [
    pytest.param("CAST(X'FF7B' AS TEXT)", id="invalid-utf8-text"),
    pytest.param("CAST(X'EDA080' AS TEXT)", id="lone-surrogate-utf8-text"),
]


def store_version(store, object_id, version, expression):
    with store.write() as con:
        changed = con.execute(
            f"UPDATE revisions SET version={expression} WHERE object_id=? AND version=?",
            (object_id, version),
        ).rowcount
        stored = con.execute(
            f"SELECT typeof(version) FROM revisions WHERE object_id=? AND version={expression}",
            (object_id,),
        ).fetchone()[0]
    assert changed == 1
    assert stored == "text"


def test_invalid_utf8_text_version_could_not_be_fetched_by_sqlite3(setup):
    # The failure this change avoids: fetching the version column itself raises in sqlite3.
    store = setup[0]
    item = history(setup)
    store_version(store, item["id"], 1, "CAST(X'FF7B' AS TEXT)")
    with store.read() as con, pytest.raises(sqlite3.OperationalError, match="UTF-8"):
        con.execute("SELECT version FROM revisions WHERE object_id=?", (item["id"],)).fetchall()


def test_invalid_utf8_text_current_version_is_reported_without_raising(setup):
    store = setup[0]
    item = history(setup)
    store_version(store, item["id"], 3, "CAST(X'FF7B' AS TEXT)")
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [
        f"Revision mismatch: {item['id']}",
        f"Revision has no recorded history: {item['id']} version CAST(X'FF7B' AS TEXT)",
        f"Historical revision missing: {item['id']} version 3",
    ]


def test_invalid_utf8_text_historical_version_is_reported_without_raising(setup):
    store = setup[0]
    item = history(setup)
    store_version(store, item["id"], 1, "CAST(X'FF7B' AS TEXT)")
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [
        f"Revision has no recorded history: {item['id']} version CAST(X'FF7B' AS TEXT)",
        f"Historical revision missing: {item['id']} version 1",
    ]


def test_lone_surrogate_text_current_version_is_reported_without_raising(setup):
    store = setup[0]
    item = history(setup)
    store_version(store, item["id"], 3, "CAST(X'EDA080' AS TEXT)")
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [
        f"Revision mismatch: {item['id']}",
        f"Revision has no recorded history: {item['id']} version CAST(X'EDA080' AS TEXT)",
        f"Historical revision missing: {item['id']} version 3",
    ]


def test_lone_surrogate_text_historical_version_is_reported_without_raising(setup):
    store = setup[0]
    item = history(setup)
    store_version(store, item["id"], 1, "CAST(X'EDA080' AS TEXT)")
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [
        f"Revision has no recorded history: {item['id']} version CAST(X'EDA080' AS TEXT)",
        f"Historical revision missing: {item['id']} version 1",
    ]


@pytest.mark.parametrize("expression", INVALID_UTF8_VERSIONS)
def test_invalid_utf8_text_version_under_untyped_schema_is_reported_without_raising(setup, expression):
    store = setup[0]
    item = history(setup)
    untyped_column(store, "version")
    store_version(store, item["id"], 2, expression)
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [
        f"Revision has no recorded history: {item['id']} version {expression}",
        f"Historical revision missing: {item['id']} version 2",
    ]


def test_version_text_is_shown_short_and_printable(setup):
    store = setup[0]
    item = history(setup, versions=4)
    long_word = "'" + "a" * 40 + "'"
    long_blob = "X'" + "AB" * 20 + "'"
    for version, expression in [(1, "'it''s'"), (2, long_word), (3, long_blob)]:
        with store.write() as con:
            con.execute(
                f"UPDATE revisions SET version={expression} WHERE object_id=? AND version=?",
                (item["id"], version),
            )
    result = integrity(setup)
    # Rows are read in stored version order: TEXT bytewise, then BLOB.
    assert result["record_errors"] == [
        f"Revision has no recorded history: {item['id']} version "
        "CAST(X'61616161616161616161616161616161...' AS TEXT)",
        f"Revision has no recorded history: {item['id']} version 'it''s'",
        f"Revision has no recorded history: {item['id']} version X'ABABABABABABABABABABABABABABABAB...'",
        f"Historical revision missing: {item['id']} version 1",
        f"Historical revision missing: {item['id']} version 2",
        f"Historical revision missing: {item['id']} version 3",
    ]


# One damaged column of one revision row, in each SQLite storage class. The column's declared
# type and NOT NULL are dropped only when the shipped schema would convert or refuse the value.

STORED_VALUES = [
    # expression, storage class, how the value is shown when stored as a version
    pytest.param("CAST(X'FF7B' AS TEXT)", "text", "CAST(X'FF7B' AS TEXT)", id="invalid-utf8-text"),
    pytest.param("CAST(X'EDA080' AS TEXT)", "text", "CAST(X'EDA080' AS TEXT)", id="lone-surrogate-text"),
    pytest.param("CAST(X'00' AS TEXT)", "text", "CAST(X'00' AS TEXT)", id="nul-text"),
    pytest.param("X'FF7B'", "blob", "X'FF7B'", id="blob"),
    pytest.param("X''", "blob", "X''", id="empty-blob"),
    pytest.param("''", "text", "''", id="empty-text"),
    pytest.param("'word'", "text", "'word'", id="word"),
    pytest.param("5", "integer", "5", id="integer"),
    pytest.param("9223372036854775807", "integer", "9223372036854775807", id="integer-maximum"),
    pytest.param("-1", "integer", "-1", id="negative-integer"),
    pytest.param("2.5", "real", "2.5", id="real"),
    pytest.param("9e999", "real", "inf", id="real-infinity"),
    pytest.param("NULL", "null", "NULL", id="null"),
]


def store_in_storage_class(store, column, object_id, version, expression, storage):
    with store.read() as con:
        rowid = con.execute(
            "SELECT rowid FROM revisions WHERE object_id=? AND version=?", (object_id, version)
        ).fetchone()[0]
    update = f"UPDATE revisions SET {column}={expression} WHERE rowid=?"
    held = f"SELECT typeof({column}) FROM revisions WHERE rowid=?"
    con = sqlite3.connect(store.path, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(update, (rowid,))
            fits = con.execute(held, (rowid,)).fetchone()[0] == storage
        except sqlite3.IntegrityError:
            fits = False
        con.execute("COMMIT" if fits else "ROLLBACK")
    finally:
        con.close()
    if not fits:
        untyped_column(store, column)
        with store.write() as con:
            assert con.execute(update, (rowid,)).rowcount == 1
    with store.read() as con:
        assert con.execute(held, (rowid,)).fetchone()[0] == storage


def second_business(setup):
    store, service, actor, _, _ = setup
    other = service.create_business(
        actor,
        key(),
        models.BusinessCreate(name="Second business", concept="Another synthetic business.").model_dump(
            mode="json"
        ),
    )
    item = service.add_record(actor, other["id"], key(), record(title="Other buyer"))
    revise(service, actor, other["id"], item, "Other buyer revision 2")
    return other["id"]


@pytest.mark.parametrize("version", [pytest.param(3, id="current"), pytest.param(1, id="historical")])
@pytest.mark.parametrize(("expression", "storage", "shown"), STORED_VALUES)
@pytest.mark.parametrize("column", list(REVISION_COLUMNS))
def test_any_value_in_any_revision_column_is_reported_without_raising(
    setup, column, expression, storage, shown, version
):
    store, service, actor, _, _ = setup
    other = second_business(setup)
    item = history(setup)
    store_in_storage_class(store, column, item["id"], version, expression, storage)
    result = integrity(setup)
    oid = item["id"]
    current = [f"Revision mismatch: {oid}"] if version == 3 else []
    expected = {
        "snapshot": current + [f"Historical revision differs from history: {oid} version {version}"],
        "version": current
        + [
            f"Revision has no recorded history: {oid} version {shown}",
            f"Historical revision missing: {oid} version {version}",
        ],
        # The row no longer names this object, so it is not attributed to this business.
        "object_id": current + [f"Historical revision missing: {oid} version {version}"],
        # Documented limit (docs/ARCHITECTURE.md, Integrity and recovery): the actor_id and
        # created_at columns of a revision row are not compared with anything.
        "actor_id": [],
        "created_at": [],
    }[column]
    assert result["record_errors"] == expected
    assert result["valid"] is (column in {"actor_id", "created_at"})
    other_result = service.audit(actor, other)["integrity"]
    assert other_result["valid"], other_result["record_errors"]
    assert other_result["record_errors"] == []


# A version is recorded by the first chained event for it. "Appended" below means one new event row
# written with SQL whose prev_hash is the current head and whose hash is right for the new row; no
# earlier row changes, so the chain itself still verifies.

APPENDED_AT = "2026-01-01T00:00:00+00:00"


def append_event(store, bid, object_id, payload, action="record.updated"):
    """Append one correctly chained event whose stored payload is the text ``payload``."""
    with store.write() as con:
        prev = con.execute(
            "SELECT hash FROM events WHERE business_id=? ORDER BY seq DESC LIMIT 1", (bid,)
        ).fetchone()[0]
        chained = {
            "business_id": bid,
            "actor_id": "principal_appender",
            "action": action,
            "object_id": object_id,
            "payload": json.loads(payload),
            "created_at": APPENDED_AT,
            "prev_hash": prev,
        }
        con.execute(
            "INSERT INTO events(business_id,actor_id,action,object_id,payload,created_at,prev_hash,hash) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                bid,
                chained["actor_id"],
                action,
                object_id,
                payload,
                APPENDED_AT,
                prev,
                digest(canonical(chained)),
            ),
        )


def stored_snapshot_text(store, object_id, version):
    with store.read() as con:
        return con.execute(
            "SELECT snapshot FROM revisions WHERE object_id=? AND version=?", (object_id, version)
        ).fetchone()[0]


def event_hashes(store, bid):
    with store.read() as con:
        return [r[0] for r in con.execute("SELECT hash FROM events WHERE business_id=? ORDER BY seq", (bid,))]


def assert_clean(service, actor, bid):
    result = service.audit(actor, bid)["integrity"]
    assert result["record_errors"] == []
    assert result["valid"]


def test_rewritten_past_revision_is_not_hidden_by_appended_events(setup):
    # The fixture's business is the second business in this database.
    store, service, actor, other, _ = setup
    revise_other = service.add_record(actor, other, key(), record(title="Other buyer"))
    revise(service, actor, other, revise_other, "Other buyer revision 2")
    bid = seed(service, actor)["id"]
    with store.read() as con:
        oid = con.execute(
            "SELECT id FROM objects WHERE business_id=? AND version=2 ORDER BY id LIMIT 1", (bid,)
        ).fetchone()[0]
    clean = service.audit(actor, bid)["integrity"]
    assert clean["record_errors"] == [] and clean["valid"] and clean["events_checked"] == 100
    anchored = event_hashes(store, bid)
    current = stored_snapshot_text(store, oid, 2)
    altered = canonical({**snapshot(store, oid, 1), "title": "Rewritten past"})

    # 1. The past revision row is rewritten.
    with store.write() as con:
        con.execute("UPDATE revisions SET snapshot=? WHERE object_id=? AND version=1", (altered, oid))
    result = service.audit(actor, bid)["integrity"]
    assert result["record_errors"] == [f"Historical revision differs from history: {oid} version 1"]
    assert not result["valid"]

    # 2. An event recording version 1 with the altered snapshot is appended.
    append_event(store, bid, oid, altered)
    result = service.audit(actor, bid)["integrity"]
    assert result["record_errors"] == [
        f"Current record differs from history: {oid}",
        f"Version recorded more than once in history: {oid} version 1",
        f"Historical revision differs from history: {oid} version 1",
    ]
    assert not result["valid"]
    assert result["events_checked"] == 101

    # 3. An event repeating the current version 2 snapshot is appended.
    append_event(store, bid, oid, current)
    result = service.audit(actor, bid)["integrity"]
    assert result["record_errors"] == [
        f"Version recorded more than once in history: {oid} version 1",
        f"Version recorded more than once in history: {oid} version 2",
        f"Historical revision differs from history: {oid} version 1",
    ]
    assert not result["valid"]
    assert result["events_checked"] == 102
    # Documented: appending changed no earlier hash, so the head hash anchored before is still there.
    assert event_hashes(store, bid)[:100] == anchored
    assert event_hashes(store, bid)[99] == clean["head_hash"]
    assert_clean(service, actor, other)


def test_appended_copy_of_current_version_is_reported(setup):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = history(setup, versions=2)
    append_event(store, bid, item["id"], stored_snapshot_text(store, item["id"], 2))
    result = integrity(setup)
    assert result["record_errors"] == [f"Version recorded more than once in history: {item['id']} version 2"]
    assert not result["valid"]
    assert_clean(service, actor, other)


def test_appended_copy_of_past_version_is_reported(setup):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = history(setup, versions=3)
    append_event(store, bid, item["id"], stored_snapshot_text(store, item["id"], 1))
    result = integrity(setup)
    # The newest chained snapshot is now version 1, so the current record no longer equals it.
    assert result["record_errors"] == [
        f"Current record differs from history: {item['id']}",
        f"Version recorded more than once in history: {item['id']} version 1",
    ]
    assert not result["valid"]
    assert_clean(service, actor, other)


def test_version_repeated_many_times_is_reported_once(setup):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = history(setup, versions=2)
    for _ in range(3):
        append_event(store, bid, item["id"], stored_snapshot_text(store, item["id"], 2))
    result = integrity(setup)
    assert result["record_errors"] == [f"Version recorded more than once in history: {item['id']} version 2"]
    assert not result["valid"]
    assert_clean(service, actor, other)


def business_versions(setup):
    store, service, actor, bid, _ = setup
    first = service.audit(actor, bid)["events"][0]["payload"]
    assert first["id"] == bid and first["version"] == 1
    current = service.update_business(
        actor,
        bid,
        key(),
        models.BusinessUpdate(
            name="Test business renamed",
            concept="A synthetic business for independent tests.",
            expected_version=1,
        ).model_dump(mode="json"),
    )
    assert current["version"] == 2
    return first, current


def test_business_profile_repeated_current_version_is_reported(setup):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    _, current = business_versions(setup)
    assert_clean(service, actor, bid)
    append_event(store, bid, bid, canonical(current), "business.updated")
    result = integrity(setup)
    assert result["record_errors"] == [f"Version recorded more than once in history: {bid} version 2"]
    assert not result["valid"]
    assert_clean(service, actor, other)


def test_business_profile_repeated_past_version_is_reported(setup):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    first, _ = business_versions(setup)
    append_event(store, bid, bid, canonical(first), "business.updated")
    append_event(store, bid, bid, canonical(first), "business.updated")
    result = integrity(setup)
    assert result["record_errors"] == [
        f"Current record differs from history: {bid}",
        f"Version recorded more than once in history: {bid} version 1",
    ]
    assert not result["valid"]
    assert_clean(service, actor, other)


# An event whose payload id is not its own object id is not a snapshot of any record: it never
# becomes the newest chained snapshot and records no version, whatever version it carries.

FOREIGN_OBJECT_IDS = [
    pytest.param("business", id="business-id"),
    pytest.param("other", id="another-objects-id"),
    pytest.param("unused", id="id-nothing-else-uses"),
]

FOREIGN_VERSIONS = [pytest.param(2, id="current-version"), pytest.param(99, id="unrecorded-version")]


def foreign_object_id(setup, which):
    _, service, actor, bid, _ = setup
    if which == "business":
        return bid
    if which == "other":
        return service.add_record(actor, bid, key(), record(title="Second buyer"))["id"]
    return "object_nothing_else_uses"


def foreign_snapshot(item, version):
    return {
        **item,
        "title": "Never approved",
        "data": {**item["data"], "details": "Never approved"},
        "version": version,
    }


@pytest.mark.parametrize("version", FOREIGN_VERSIONS)
@pytest.mark.parametrize("which", FOREIGN_OBJECT_IDS)
def test_event_whose_payload_id_is_not_its_object_id_is_no_snapshot(setup, which, version):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = history(setup, versions=2)
    object_id = foreign_object_id(setup, which)
    assert object_id != item["id"]
    append_event(store, bid, object_id, canonical(foreign_snapshot(item, version)))
    assert_clean(service, actor, bid)
    exported = service.audit(actor, bid, True)["integrity"]
    assert exported["record_errors"] == []
    assert exported["valid"]
    assert_clean(service, actor, other)


@pytest.mark.parametrize("version", FOREIGN_VERSIONS)
def test_event_naming_the_business_under_an_objects_id_is_no_snapshot(setup, version):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    _, current = business_versions(setup)
    item = history(setup, versions=2)
    forged = {**current, "data": {**current["data"], "name": "Never approved"}, "version": version}
    append_event(store, bid, item["id"], canonical(forged), "business.updated")
    assert_clean(service, actor, bid)
    exported = service.audit(actor, bid, True)["integrity"]
    assert exported["record_errors"] == []
    assert exported["valid"]
    assert_clean(service, actor, other)


@pytest.mark.parametrize(
    "revision", [pytest.param(False, id="row"), pytest.param(True, id="row-and-revision")]
)
@pytest.mark.parametrize("version", FOREIGN_VERSIONS)
@pytest.mark.parametrize("which", FOREIGN_OBJECT_IDS)
def test_event_whose_payload_id_is_not_its_object_id_hides_no_change(setup, which, version, revision):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = history(setup, versions=2)
    oid = item["id"]
    object_id = foreign_object_id(setup, which)
    forged = foreign_snapshot(item, version)
    with store.write() as con:
        con.execute(
            "UPDATE objects SET title=?,data=?,version=? WHERE id=?",
            (forged["title"], canonical(forged["data"]), version, oid),
        )
        if revision and version == 2:
            con.execute(
                "UPDATE revisions SET snapshot=? WHERE object_id=? AND version=2", (canonical(forged), oid)
            )
        elif revision:
            con.execute(
                "INSERT INTO revisions VALUES(?,?,?,?,?)",
                (oid, 99, canonical(forged), "principal_appender", APPENDED_AT),
            )
    if not revision:
        expected = [f"Current record differs from history: {oid}", f"Revision mismatch: {oid}"]
    elif version == 2:
        expected = [
            f"Current record differs from history: {oid}",
            f"Historical revision differs from history: {oid} version 2",
        ]
    else:
        expected = [
            f"Current record differs from history: {oid}",
            f"Revision has no recorded history: {oid} version 99",
        ]
    before = integrity(setup)
    assert before["record_errors"] == expected
    assert not before["valid"]
    append_event(store, bid, object_id, canonical(forged))
    after = integrity(setup)
    assert after["record_errors"] == expected
    assert not after["valid"]
    assert_clean(service, actor, other)


# An event's object id is text and its payload id is compared with it by value, so a payload id that
# is not text is never a snapshot, under the record's own id or under that value spelled as text.

NON_TEXT_IDS = {
    "null": lambda own: None,
    "false": lambda own: False,
    "true": lambda own: True,
    "zero": lambda own: 0,
    "five": lambda own: 5,
    "two-point-five": lambda own: 2.5,
    "list-of-its-id": lambda own: [own],
    "object-of-its-id": lambda own: {"id": own},
}

NON_TEXT_OBJECT_IDS = {
    "own-id": lambda value, own, bid: own,
    "business-id": lambda value, own, bid: bid,
    "json-text": lambda value, own, bid: json.dumps(value),
    "python-text": lambda value, own, bid: str(value),
}


# For these values the JSON text and the Python text are the same text ("0", "5", "2.5"), so the
# python-text case would repeat the json-text case exactly. It is left out for them.
SAME_JSON_AND_PYTHON_TEXT = {"zero", "five", "two-point-five"}

NON_TEXT_ID_SPELLINGS = [
    pytest.param(payload_id, spelling, id=f"{payload_id}-{spelling}")
    for payload_id in NON_TEXT_IDS
    for spelling in NON_TEXT_OBJECT_IDS
    if not (spelling == "python-text" and payload_id in SAME_JSON_AND_PYTHON_TEXT)
]


def with_id(payload, value):
    return canonical({**payload, "id": value})


@pytest.mark.parametrize("version", FOREIGN_VERSIONS)
@pytest.mark.parametrize(("payload_id", "spelling"), NON_TEXT_ID_SPELLINGS)
def test_event_whose_payload_id_is_not_text_is_no_snapshot(setup, payload_id, spelling, version):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = history(setup, versions=2)
    value = NON_TEXT_IDS[payload_id](item["id"])
    object_id = NON_TEXT_OBJECT_IDS[spelling](value, item["id"], bid)
    append_event(store, bid, object_id, with_id(foreign_snapshot(item, version), value))
    assert_clean(service, actor, bid)
    exported = export_integrity(setup)
    assert exported["record_errors"] == []
    assert exported["valid"]
    assert_clean(service, actor, other)


@pytest.mark.parametrize("version", FOREIGN_VERSIONS)
@pytest.mark.parametrize("payload_id", list(NON_TEXT_IDS))
def test_event_whose_payload_id_is_not_text_hides_no_change(setup, payload_id, version):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = history(setup, versions=2)
    oid = item["id"]
    forged = foreign_snapshot(item, version)
    with store.write() as con:
        con.execute(
            "UPDATE objects SET title=?,data=?,version=? WHERE id=?",
            (forged["title"], canonical(forged["data"]), version, oid),
        )
    expected = [f"Current record differs from history: {oid}", f"Revision mismatch: {oid}"]
    before = integrity(setup)
    assert before["record_errors"] == expected
    assert not before["valid"]
    append_event(store, bid, oid, with_id(forged, NON_TEXT_IDS[payload_id](oid)))
    after = integrity(setup)
    assert after["record_errors"] == expected
    assert not after["valid"]
    exported = export_integrity(setup)
    assert exported["record_errors"] == expected
    assert not exported["valid"]
    assert_clean(service, actor, other)


def renamed_profile(profile, version):
    return {**profile, "data": {**profile["data"], "name": "Never approved"}, "version": version}


@pytest.mark.parametrize(
    "version", [pytest.param(1, id="current-version"), pytest.param(99, id="unrecorded-version")]
)
@pytest.mark.parametrize(
    "which", [pytest.param("business", id="business-id"), pytest.param("record", id="records-id")]
)
@pytest.mark.parametrize("payload_id", list(NON_TEXT_IDS))
def test_business_profile_event_whose_payload_id_is_not_text_is_no_snapshot(
    setup, payload_id, which, version
):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    profile = service.audit(actor, bid)["events"][0]["payload"]
    assert profile["id"] == bid and profile["version"] == 1
    item = history(setup, versions=2)
    object_id = bid if which == "business" else item["id"]
    append_event(
        store,
        bid,
        object_id,
        with_id(renamed_profile(profile, version), NON_TEXT_IDS[payload_id](bid)),
        "business.updated",
    )
    assert_clean(service, actor, bid)
    exported = export_integrity(setup)
    assert exported["record_errors"] == []
    assert exported["valid"]
    assert_clean(service, actor, other)


@pytest.mark.parametrize("payload_id", list(NON_TEXT_IDS))
def test_business_profile_event_whose_payload_id_is_not_text_hides_no_change(setup, payload_id):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    profile = service.audit(actor, bid)["events"][0]["payload"]
    forged = renamed_profile(profile, 2)
    with store.write() as con:
        con.execute("UPDATE businesses SET data=?,version=2 WHERE id=?", (canonical(forged["data"]), bid))
    expected = [f"Current record differs from history: {bid}"]
    before = integrity(setup)
    assert before["record_errors"] == expected
    assert not before["valid"]
    append_event(store, bid, bid, with_id(forged, NON_TEXT_IDS[payload_id](bid)), "business.updated")
    after = integrity(setup)
    assert after["record_errors"] == expected
    assert not after["valid"]
    exported = export_integrity(setup)
    assert exported["record_errors"] == expected
    assert not exported["valid"]
    assert_clean(service, actor, other)


# The empty text is text like any other: a payload id "" is a snapshot only under the object id "".


@pytest.mark.parametrize("version", FOREIGN_VERSIONS)
@pytest.mark.parametrize(
    "which", [pytest.param("record", id="records-id"), pytest.param("business", id="business-id")]
)
def test_event_whose_payload_id_is_empty_text_is_no_snapshot_under_another_id(setup, which, version):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = history(setup, versions=2)
    object_id = item["id"] if which == "record" else bid
    append_event(store, bid, object_id, with_id(foreign_snapshot(item, version), ""))
    assert_clean(service, actor, bid)
    exported = export_integrity(setup)
    assert exported["record_errors"] == []
    assert exported["valid"]
    assert_clean(service, actor, other)


@pytest.mark.parametrize("version", FOREIGN_VERSIONS)
def test_event_whose_payload_id_and_object_id_are_empty_text_is_a_snapshot(setup, version):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = history(setup, versions=2)
    append_event(store, bid, "", with_id(foreign_snapshot(item, version), ""))
    expected = ["Historical record missing: ", f"Historical revision missing:  version {version}"]
    result = integrity(setup)
    assert result["record_errors"] == expected
    assert not result["valid"]
    exported = export_integrity(setup)
    assert exported["record_errors"] == expected
    assert not exported["valid"]
    assert_clean(service, actor, other)


# Revision rows are attributed to a business through the object id of every event it has, whether
# or not that event is a snapshot. A row under an id that nothing of the business names is not
# attributed to it.

UNNAMED_ID = "object_nothing_else_uses"


def unnamed_revision(setup):
    store = setup[0]
    item = history(setup, versions=2)
    with store.write() as con:
        con.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?)",
            (
                UNNAMED_ID,
                1,
                canonical({**item, "id": UNNAMED_ID, "version": 1}),
                "principal_appender",
                APPENDED_AT,
            ),
        )
    return item


def test_documented_limit_revision_under_an_id_no_event_names_is_not_checked(setup):
    _, service, actor, bid, _ = setup
    other = second_business(setup)
    unnamed_revision(setup)
    assert_clean(service, actor, bid)
    exported = export_integrity(setup)
    assert exported["record_errors"] == []
    assert exported["valid"]
    assert_clean(service, actor, other)


@pytest.mark.parametrize(
    "payload_id",
    [
        pytest.param(lambda own: own, id="records-id"),
        pytest.param(lambda own: 5, id="five"),
        pytest.param(lambda own: None, id="null"),
        pytest.param(lambda own: [own], id="list-of-records-id"),
    ],
)
def test_event_that_is_no_snapshot_attributes_revisions_under_its_object_id(setup, payload_id):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = unnamed_revision(setup)
    append_event(store, bid, UNNAMED_ID, with_id(foreign_snapshot(item, 1), payload_id(item["id"])))
    expected = [f"Revision has no recorded history: {UNNAMED_ID} version 1"]
    result = integrity(setup)
    assert result["record_errors"] == expected
    assert not result["valid"]
    exported = export_integrity(setup)
    assert exported["record_errors"] == expected
    assert not exported["valid"]
    assert_clean(service, actor, other)


UNUSABLE_VERSIONS = [
    pytest.param("2.0", id="float-2.0"),
    pytest.param("2e0", id="exponent-2e0"),
    pytest.param("true", id="true"),
    pytest.param('"2"', id="text-2"),
    pytest.param("null", id="null"),
    pytest.param("[2]", id="list"),
    pytest.param('{"n":2}', id="object"),
]


def with_version(text, version):
    assert text.count('"version":2') == 1
    return text.replace('"version":2', '"version":' + version)


@pytest.mark.parametrize("version", UNUSABLE_VERSIONS)
def test_appended_event_with_unusable_version_is_reported_without_raising(setup, version):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = history(setup, versions=2)
    append_event(store, bid, item["id"], with_version(stored_snapshot_text(store, item["id"], 2), version))
    result = integrity(setup)
    assert result["record_errors"] == [
        f"Current record differs from history: {item['id']}",
        f"History records an unusable version: {item['id']}",
    ]
    assert not result["valid"]
    assert_clean(service, actor, other)


@pytest.mark.parametrize("version", ["3.0", "3e0", '"3"', "true", "null", "[3]", '{"n":3}'])
def test_unusable_version_is_reported_once_and_stands_for_no_version(setup, version):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = history(setup, versions=2)
    current = stored_snapshot_text(store, item["id"], 2)
    append_event(store, bid, item["id"], with_version(current, version))
    append_event(store, bid, item["id"], with_version(current, version))
    append_event(store, bid, item["id"], current)
    result = integrity(setup)
    # Not "Historical revision missing ... version 3", and true does not stand for version 1.
    assert result["record_errors"] == [
        f"History records an unusable version: {item['id']}",
        f"Version recorded more than once in history: {item['id']} version 2",
    ]
    assert not result["valid"]
    assert_clean(service, actor, other)


def test_business_profile_unusable_version_is_reported(setup):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    _, current = business_versions(setup)
    append_event(store, bid, bid, canonical({**current, "version": "2"}), "business.updated")
    result = integrity(setup)
    assert result["record_errors"] == [
        f"Current record differs from history: {bid}",
        f"History records an unusable version: {bid}",
    ]
    assert not result["valid"]
    assert_clean(service, actor, other)


def test_integer_spelled_minus_zero_records_version_zero(setup):
    # -0 is a JSON integer: it records version 0, which has no revision row.
    store, service, actor, bid, _ = setup
    item = history(setup, versions=2)
    append_event(store, bid, item["id"], with_version(stored_snapshot_text(store, item["id"], 2), "-0"))
    assert integrity(setup)["record_errors"] == [
        f"Current record differs from history: {item['id']}",
        f"Historical revision missing: {item['id']} version 0",
    ]


def test_history_messages_follow_chain_order(setup):
    store, service, actor, bid, _ = setup
    a = history(setup, versions=2)["id"]
    b = service.add_record(actor, bid, key(), record(title="Second buyer"))["id"]
    b1 = stored_snapshot_text(store, b, 1)
    assert b1.count('"version":1') == 1
    append_event(store, bid, b, b1.replace('"version":1', '"version":"x"'))
    append_event(store, bid, a, stored_snapshot_text(store, a, 1))
    append_event(store, bid, b, b1)
    append_event(store, bid, a, stored_snapshot_text(store, a, 2))
    append_event(store, bid, b, b1.replace('"version":1', '"version":1.0'))
    result = integrity(setup)
    # Current-record messages first, in record order; then history messages in chain order.
    assert result["record_errors"] == [
        f"Current record differs from history: {b}",
        f"History records an unusable version: {b}",
        f"Version recorded more than once in history: {a} version 1",
        f"Version recorded more than once in history: {b} version 1",
        f"Version recorded more than once in history: {a} version 2",
    ]


def test_export_carries_repeated_version_errors(setup):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    item = history(setup, versions=2)
    append_event(store, bid, item["id"], stored_snapshot_text(store, item["id"], 2))
    export = service.audit(actor, bid, True)
    assert export["integrity"] == integrity(setup)
    assert export["integrity"]["record_errors"] == [
        f"Version recorded more than once in history: {item['id']} version 2"
    ]
    assert not export["integrity"]["valid"]
    assert export["events"][-1]["actor_id"] == "principal_appender"
    assert_clean(service, actor, other)


def test_documented_limit_appended_new_version_verifies(setup):
    # An appended event recording a new version, with a matching current record and revision row,
    # is indistinguishable from an application write: the chain shows only that it came last.
    store, service, actor, bid, _ = setup
    item = history(setup, versions=2)
    forged = {**item, "title": "Never approved", "version": 3}
    with store.write() as con:
        con.execute("UPDATE objects SET title=?,version=3 WHERE id=?", (forged["title"], item["id"]))
        con.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?)",
            (item["id"], 3, canonical(forged), "principal_appender", APPENDED_AT),
        )
    append_event(store, bid, item["id"], canonical(forged))
    assert_clean(service, actor, bid)


def test_documented_limit_appended_new_business_profile_version_verifies(setup):
    # The business profile has no revision rows, so a matching profile is all an appended new version needs.
    store, service, actor, bid, _ = setup
    _, current = business_versions(setup)
    forged = {**current, "data": {**current["data"], "name": "Never approved"}, "version": 3}
    with store.write() as con:
        con.execute("UPDATE businesses SET data=?,version=3 WHERE id=?", (canonical(forged["data"]), bid))
    append_event(store, bid, bid, canonical(forged), "business.updated")
    with store.read() as con:
        assert con.execute("SELECT count(*) FROM revisions WHERE object_id=?", (bid,)).fetchone()[0] == 0
    assert_clean(service, actor, bid)


def test_documented_limit_altered_ai_review_with_appended_event_verifies(setup):
    store, service, actor, bid, _ = setup
    run_id = "review_synthetic"
    data = {"note": "Synthetic run written for this test.", "response": None}
    with store.write() as con:
        con.execute(
            "INSERT INTO ai_calls VALUES(?,?,?,?,?,?,?,?)",
            (run_id, bid, actor["id"], key(), "0" * 64, canonical(data), "unknown", APPENDED_AT),
        )
        store.event(
            con,
            bid,
            actor,
            "ai.unknown",
            run_id,
            {"run_id": run_id, "state": "unknown", "data_sha256": digest(canonical(data))},
        )
    assert_clean(service, actor, bid)
    changed = {**data, "advice": {"summary": "Advice that was never returned."}}
    with store.write() as con:
        con.execute("UPDATE ai_calls SET data=?,state='succeeded' WHERE id=?", (canonical(changed), run_id))
    assert integrity(setup)["record_errors"] == [f"AI review differs from history: {run_id}"]
    append_event(
        store,
        bid,
        run_id,
        canonical({"run_id": run_id, "state": "succeeded", "data_sha256": digest(canonical(changed))}),
        "ai.succeeded",
    )
    assert_clean(service, actor, bid)


# Any event counts for an AI review when its payload's run_id and data_sha256 are both truthy,
# whatever else the payload holds and whatever its action, and the last such event for a run id
# stands for that run. An event that does not count leaves the one before it standing.

ABSENT = object()
OTHER_DIGEST = "f" * 64

AI_PAYLOAD_IDS = {
    "text": lambda own: UNNAMED_ID,
    "five": lambda own: 5,
    "null": lambda own: None,
    "list": lambda own: [own],
}

AI_OBJECT_IDS = [pytest.param("record", id="records-id"), pytest.param("run", id="runs-id")]

# An event counts whatever its action: one the AI service writes and one it never writes.
AI_ACTIONS = [
    pytest.param("ai.succeeded", id="action-ai-succeeded"),
    pytest.param("record.updated", id="action-record-updated"),
]


def stored_ai_digest(store, run_id):
    with store.read() as con:
        data = con.execute("SELECT data FROM ai_calls WHERE id=?", (run_id,)).fetchone()[0]
    return digest(canonical(json.loads(data)))


def append_ai_event(setup, item, run, payload_id, which="record", action="ai.succeeded", **fields):
    """Append an event that is no snapshot, carrying ``fields`` and no key whose value is ABSENT."""
    store, _, _, bid, _ = setup
    object_id = item["id"] if which == "record" else run
    payload = {"id": AI_PAYLOAD_IDS[payload_id](item["id"]), "version": 2, "data": item["data"]}
    payload.update({name: value for name, value in fields.items() if value is not ABSENT})
    assert payload["id"] != object_id
    append_event(store, bid, object_id, canonical(payload), action)


def ai_audit(setup, item, payload_id, which, run_id, data_sha256, state, action="ai.succeeded"):
    store, _, actor, bid, _ = setup
    run = add_ai_review(store, actor, bid)
    right = stored_ai_digest(store, run)
    run_id = run if run_id == "run" else run_id
    append_ai_event(
        setup,
        item,
        run,
        payload_id,
        which,
        action,
        run_id=run_id,
        data_sha256=data_sha256(right),
        state=state,
    )
    return integrity(setup)


def assert_export_and_other_business(setup, other, result):
    """The export reports what the audit reported, and the second business audits clean."""
    _, service, actor, _, _ = setup
    exported = export_integrity(setup)
    assert exported["record_errors"] == result["record_errors"]
    assert exported["valid"] is result["valid"]
    assert_clean(service, actor, other)


@pytest.mark.parametrize("action", AI_ACTIONS)
@pytest.mark.parametrize("which", AI_OBJECT_IDS)
@pytest.mark.parametrize("payload_id", list(AI_PAYLOAD_IDS))
def test_event_that_is_no_snapshot_stands_for_its_ai_review(setup, payload_id, which, action):
    other = second_business(setup)
    item = history(setup, versions=2)
    result = ai_audit(setup, item, payload_id, which, "run", lambda right: right, "unknown", action)
    assert result["record_errors"] == []
    assert result["valid"]
    assert_export_and_other_business(setup, other, result)


@pytest.mark.parametrize("action", AI_ACTIONS)
@pytest.mark.parametrize(
    ("data_sha256", "state"),
    [
        pytest.param(lambda right: OTHER_DIGEST, "unknown", id="another-digest"),
        pytest.param(lambda right: right, "succeeded", id="another-state"),
        pytest.param(lambda right: 1, "unknown", id="digest-one"),
        pytest.param(lambda right: True, "unknown", id="digest-true"),
        pytest.param(lambda right: [right], "unknown", id="digest-list-of-the-right-digest"),
    ],
)
@pytest.mark.parametrize("which", AI_OBJECT_IDS)
@pytest.mark.parametrize("payload_id", list(AI_PAYLOAD_IDS))
def test_event_that_is_no_snapshot_and_differs_from_its_ai_review_is_reported(
    setup, payload_id, which, data_sha256, state, action
):
    other = second_business(setup)
    item = history(setup, versions=2)
    result = ai_audit(setup, item, payload_id, which, "run", data_sha256, state, action)
    assert result["record_errors"] == ["AI review differs from history: review_synthetic"]
    assert not result["valid"]
    assert_export_and_other_business(setup, other, result)


@pytest.mark.parametrize(
    ("run_id", "data_sha256"),
    [
        pytest.param("run", lambda right: "", id="digest-empty-text"),
        pytest.param("run", lambda right: 0, id="digest-zero"),
        pytest.param("run", lambda right: False, id="digest-false"),
        pytest.param("run", lambda right: None, id="digest-null"),
        pytest.param("run", lambda right: [], id="digest-empty-list"),
        pytest.param("run", lambda right: {}, id="digest-empty-object"),
        pytest.param("run", lambda right: ABSENT, id="digest-absent"),
        pytest.param("", lambda right: OTHER_DIGEST, id="run-id-empty-text"),
        pytest.param(0, lambda right: OTHER_DIGEST, id="run-id-zero"),
        pytest.param(False, lambda right: OTHER_DIGEST, id="run-id-false"),
        pytest.param(None, lambda right: OTHER_DIGEST, id="run-id-null"),
        pytest.param(ABSENT, lambda right: OTHER_DIGEST, id="run-id-absent"),
    ],
)
@pytest.mark.parametrize("which", AI_OBJECT_IDS)
@pytest.mark.parametrize("payload_id", list(AI_PAYLOAD_IDS))
def test_event_with_a_falsy_run_id_or_digest_does_not_count_for_an_ai_review(
    setup, payload_id, which, run_id, data_sha256
):
    other = second_business(setup)
    item = history(setup, versions=2)
    result = ai_audit(setup, item, payload_id, which, run_id, data_sha256, "succeeded")
    assert result["record_errors"] == []
    assert result["valid"]
    assert_export_and_other_business(setup, other, result)


@pytest.mark.parametrize(
    ("run_id", "shown"),
    [
        pytest.param("review_never_stored", "review_never_stored", id="text"),
        pytest.param(5, "5", id="five"),
        pytest.param(True, "True", id="true"),
    ],
)
@pytest.mark.parametrize("which", AI_OBJECT_IDS)
@pytest.mark.parametrize("payload_id", list(AI_PAYLOAD_IDS))
def test_event_that_is_no_snapshot_for_an_ai_review_never_stored_is_reported(
    setup, payload_id, which, run_id, shown
):
    other = second_business(setup)
    item = history(setup, versions=2)
    result = ai_audit(setup, item, payload_id, which, run_id, lambda right: OTHER_DIGEST, "succeeded")
    assert result["record_errors"] == [f"Historical AI review missing: {shown}"]
    assert not result["valid"]
    assert_export_and_other_business(setup, other, result)


AI_RUNS = [pytest.param(True, id="altered"), pytest.param(False, id="no-history")]


def unrecorded_ai_review(setup, chained):
    """A stored run whose current row no chained event matches, with the state an event would need."""
    store, _, actor, bid, _ = setup
    run = add_ai_review(store, actor, bid, chained=chained)
    if not chained:
        return run, "unknown"
    with store.write() as con:
        con.execute(
            "UPDATE ai_calls SET data=?,state='succeeded' WHERE id=?",
            (canonical({"note": "Advice that was never returned."}), run),
        )
    return run, "succeeded"


@pytest.mark.parametrize("chained", AI_RUNS)
@pytest.mark.parametrize("payload_id", list(AI_PAYLOAD_IDS))
def test_documented_limit_ai_review_verifies_through_an_event_that_is_no_snapshot(setup, payload_id, chained):
    other = second_business(setup)
    item = history(setup, versions=2)
    run, state = unrecorded_ai_review(setup, chained)
    before = integrity(setup)
    assert before["record_errors"] == [f"AI review differs from history: {run}"]
    assert not before["valid"]
    assert_export_and_other_business(setup, other, before)
    right = stored_ai_digest(setup[0], run)
    append_ai_event(setup, item, run, payload_id, run_id=run, data_sha256=right, state=state)
    result = integrity(setup)
    assert result["record_errors"] == []
    assert result["valid"]
    assert_export_and_other_business(setup, other, result)


@pytest.mark.parametrize(
    "data_sha256", [pytest.param("", id="digest-empty-text"), pytest.param(ABSENT, id="digest-absent")]
)
@pytest.mark.parametrize("chained", AI_RUNS)
@pytest.mark.parametrize("payload_id", list(AI_PAYLOAD_IDS))
def test_event_with_an_empty_or_absent_digest_does_not_hide_an_ai_review_change(
    setup, payload_id, chained, data_sha256
):
    other = second_business(setup)
    item = history(setup, versions=2)
    run, state = unrecorded_ai_review(setup, chained)
    expected = [f"AI review differs from history: {run}"]
    before = integrity(setup)
    assert before["record_errors"] == expected
    assert not before["valid"]
    assert_export_and_other_business(setup, other, before)
    append_ai_event(setup, item, run, payload_id, run_id=run, data_sha256=data_sha256, state=state)
    result = integrity(setup)
    assert result["record_errors"] == expected
    assert not result["valid"]
    assert_export_and_other_business(setup, other, result)


# One business's AI reviews are invisible to another's audit and export. The audit reads only this
# business's AI review rows and only this business's events, and the export carries only this
# business's AI review rows.

SECOND_RUN = "review_second_business"
SYNTHETIC_RUN_ENTRY = {
    "id": "review_synthetic",
    "state": "unknown",
    "created_at": "2026-01-01T00:00:00+00:00",
    "data": {"note": "Synthetic run written for this test."},
}
SECOND_RUN_ENTRY = {**SYNTHETIC_RUN_ENTRY, "id": "review_second_business"}


def alter_ai_review(store, run_id):
    """Change a stored run by SQL to a state and data its chained event does not carry."""
    changed = {"note": "Advice that was never returned."}
    with store.write() as con:
        assert (
            con.execute(
                "UPDATE ai_calls SET data=?,state='succeeded' WHERE id=?", (canonical(changed), run_id)
            ).rowcount
            == 1
        )
    return changed


def audited(service, actor, bid, export=False):
    return service.audit(actor, bid, export)["integrity"]


def assert_audit_and_export(service, actor, bid, expected):
    for export in (False, True):
        result = audited(service, actor, bid, export)
        assert result["record_errors"] == expected
        assert result["valid"] is (expected == [])


# Which business holds the only AI review: the first-created one (the fixture's) or the
# second-created one. A version that picked rows by position, not by business id, would pass
# whenever the business under test was the first created and its row the first inserted.


class Holder(NamedTuple):
    first_created: bool
    run_id: str
    differs: list
    missing: list
    entry: dict


HOLDERS = [
    pytest.param(
        Holder(
            True,
            "review_synthetic",
            ["AI review differs from history: review_synthetic"],
            ["Historical AI review missing: review_synthetic"],
            SYNTHETIC_RUN_ENTRY,
        ),
        id="first-created-business-holds-it",
    ),
    pytest.param(
        Holder(
            False,
            "review_second_business",
            ["AI review differs from history: review_second_business"],
            ["Historical AI review missing: review_second_business"],
            SECOND_RUN_ENTRY,
        ),
        id="second-created-business-holds-it",
    ),
]


def holder_and_other(setup, holder):
    """Create the second business; return the business that holds the review, then the other."""
    first = setup[3]
    second = second_business(setup)
    return (first, second) if holder.first_created else (second, first)


def ai_call_ids_in_insertion_order(store):
    with store.read() as con:
        return [row[0] for row in con.execute("SELECT id FROM ai_calls ORDER BY rowid")]


@pytest.mark.parametrize("holder", HOLDERS)
def test_ai_review_of_one_business_is_not_in_another_business_audit(setup, holder):
    store, service, actor, _, _ = setup
    owner, other = holder_and_other(setup, holder)
    add_ai_review(store, actor, owner, run_id=holder.run_id)
    assert_audit_and_export(service, actor, owner, [])
    assert_audit_and_export(service, actor, other, [])


@pytest.mark.parametrize("holder", HOLDERS)
def test_altered_ai_review_of_one_business_is_reported_only_there(setup, holder):
    store, service, actor, _, _ = setup
    owner, other = holder_and_other(setup, holder)
    add_ai_review(store, actor, owner, run_id=holder.run_id)
    alter_ai_review(store, holder.run_id)
    assert_audit_and_export(service, actor, owner, holder.differs)
    assert_audit_and_export(service, actor, other, [])


@pytest.mark.parametrize("holder", HOLDERS)
def test_ai_review_without_history_of_one_business_is_reported_only_there(setup, holder):
    store, service, actor, _, _ = setup
    owner, other = holder_and_other(setup, holder)
    add_ai_review(store, actor, owner, chained=False, run_id=holder.run_id)
    assert_audit_and_export(service, actor, owner, holder.differs)
    assert_audit_and_export(service, actor, other, [])


# Where both businesses hold a review, either business's row may be inserted first.
INSERTION_ORDERS = [
    pytest.param(True, id="first-created-business-row-inserted-first"),
    pytest.param(False, id="second-created-business-row-inserted-first"),
]


def add_both_ai_reviews(setup, first_inserted_first):
    """Create the second business and give each business one AI review, in the order asked."""
    store, _, actor, bid, _ = setup
    other = second_business(setup)
    if first_inserted_first:
        add_ai_review(store, actor, bid)
        add_ai_review(store, actor, other, run_id=SECOND_RUN)
        assert ai_call_ids_in_insertion_order(store) == ["review_synthetic", "review_second_business"]
    else:
        add_ai_review(store, actor, other, run_id=SECOND_RUN)
        add_ai_review(store, actor, bid)
        assert ai_call_ids_in_insertion_order(store) == ["review_second_business", "review_synthetic"]
    return other


@pytest.mark.parametrize("first_inserted_first", INSERTION_ORDERS)
def test_each_business_checks_only_its_own_ai_review(setup, first_inserted_first):
    store, service, actor, bid, _ = setup
    other = add_both_ai_reviews(setup, first_inserted_first)
    assert_audit_and_export(service, actor, bid, [])
    assert_audit_and_export(service, actor, other, [])
    alter_ai_review(store, "review_synthetic")
    assert_audit_and_export(service, actor, bid, ["AI review differs from history: review_synthetic"])
    assert_audit_and_export(service, actor, other, [])
    alter_ai_review(store, "review_second_business")
    assert_audit_and_export(service, actor, bid, ["AI review differs from history: review_synthetic"])
    assert_audit_and_export(service, actor, other, ["AI review differs from history: review_second_business"])


@pytest.mark.parametrize("holder", HOLDERS)
def test_event_in_another_business_chain_stands_for_nothing_here(setup, holder):
    store, service, actor, _, _ = setup
    owner, other = holder_and_other(setup, holder)
    add_ai_review(store, actor, owner, run_id=holder.run_id)
    changed = alter_ai_review(store, holder.run_id)
    # An event appended to the other business's chain carries the holder's run id and the state
    # and digest of its altered row: it would make that row verify if it were in the holder's chain.
    append_event(
        store,
        other,
        holder.run_id,
        canonical({"run_id": holder.run_id, "state": "succeeded", "data_sha256": digest(canonical(changed))}),
        "ai.succeeded",
    )
    # Rule: a business's AI reviews are compared with its own events only. The event in the other
    # chain hides nothing here, so the altered row is still reported.
    assert_audit_and_export(service, actor, owner, holder.differs)
    # Rule: an AI review a business's own history names must be a row of that business. The other
    # business holds no row for this run id, whichever business does.
    assert_audit_and_export(service, actor, other, holder.missing)


# A business's export carries its own AI review rows and no other business's.


def exported_archive(service, actor, bid):
    return service.audit(actor, bid, True)


@pytest.mark.parametrize("holder", HOLDERS)
def test_export_of_a_business_without_ai_reviews_carries_none_of_another_business(setup, holder):
    store, service, actor, _, _ = setup
    owner, other = holder_and_other(setup, holder)
    add_ai_review(store, actor, owner, run_id=holder.run_id)
    assert exported_archive(service, actor, other)["ai_runs"] == []
    assert exported_archive(service, actor, owner)["ai_runs"] == [holder.entry]


@pytest.mark.parametrize("first_inserted_first", INSERTION_ORDERS)
def test_export_of_each_business_carries_only_its_own_ai_review(setup, first_inserted_first):
    _, service, actor, bid, _ = setup
    other = add_both_ai_reviews(setup, first_inserted_first)
    assert exported_archive(service, actor, bid)["ai_runs"] == [SYNTHETIC_RUN_ENTRY]
    assert exported_archive(service, actor, other)["ai_runs"] == [SECOND_RUN_ENTRY]


# One business may hold several AI reviews, and every one of them is checked, exported and
# reported on its own. The two reviews below are alike in state, data and created_at and differ
# only in their ids. The first-stored has the id that sorts last, so storage order and id order
# disagree: a version that read one row, or picked rows by id, would show.

FIRST_STORED_RUN = "review_zulu"
SECOND_STORED_RUN = "review_alpha"


def add_two_ai_reviews(setup, first_chained=True, second_chained=True):
    """Give the fixture's business two AI reviews, stored in this order: zulu, then alpha."""
    store, _, actor, bid, _ = setup
    add_ai_review(store, actor, bid, chained=first_chained, run_id=FIRST_STORED_RUN)
    add_ai_review(store, actor, bid, chained=second_chained, run_id=SECOND_STORED_RUN)
    assert ai_call_ids_in_insertion_order(store) == ["review_zulu", "review_alpha"]


def assert_audit_and_export_in_any_order(service, actor, bid, expected):
    for export in (False, True):
        result = audited(service, actor, bid, export)
        assert sorted(result["record_errors"]) == sorted(expected)
        assert result["valid"] is False


# Rule: every AI review row of a business is compared with its event, not only one of them.
def test_both_ai_reviews_of_one_business_are_checked(setup):
    _, service, actor, bid, _ = setup
    add_two_ai_reviews(setup)
    assert_audit_and_export(service, actor, bid, [])


# Rule: the export carries every AI review row of a business, not only one of them.
def test_both_ai_reviews_of_one_business_are_exported(setup):
    _, service, actor, bid, _ = setup
    add_two_ai_reviews(setup)
    runs = exported_archive(service, actor, bid)["ai_runs"]
    # The two rows have the same created_at and the export orders by created_at only, so SQL does
    # not fix which of them comes first. They are compared sorted by id.
    assert sorted(runs, key=lambda run: run["id"]) == [
        {
            "id": "review_alpha",
            "state": "unknown",
            "created_at": "2026-01-01T00:00:00+00:00",
            "data": {"note": "Synthetic run written for this test."},
        },
        {
            "id": "review_zulu",
            "state": "unknown",
            "created_at": "2026-01-01T00:00:00+00:00",
            "data": {"note": "Synthetic run written for this test."},
        },
    ]


# Rule: a changed AI review is reported, whichever of a business's reviews it is.
@pytest.mark.parametrize(
    ("run_id", "expected"),
    [
        pytest.param(
            "review_zulu", ["AI review differs from history: review_zulu"], id="first-stored-changed"
        ),
        pytest.param(
            "review_alpha", ["AI review differs from history: review_alpha"], id="second-stored-changed"
        ),
    ],
)
def test_changed_ai_review_is_reported_whichever_of_two_it_is(setup, run_id, expected):
    store, service, actor, bid, _ = setup
    add_two_ai_reviews(setup)
    alter_ai_review(store, run_id)
    assert_audit_and_export(service, actor, bid, expected)


# Rule: when both of a business's AI reviews are changed, both are reported and nothing else.
def test_both_changed_ai_reviews_of_one_business_are_reported(setup):
    store, service, actor, bid, _ = setup
    add_two_ai_reviews(setup)
    alter_ai_review(store, FIRST_STORED_RUN)
    alter_ai_review(store, SECOND_STORED_RUN)
    # The audit reads the rows with no ORDER BY, so SQL does not fix the order of these two
    # messages. They are compared without regard to order.
    assert_audit_and_export_in_any_order(
        service,
        actor,
        bid,
        ["AI review differs from history: review_zulu", "AI review differs from history: review_alpha"],
    )


# Rule: an AI review with no event is reported, whichever of a business's reviews it is.
@pytest.mark.parametrize(
    ("first_chained", "expected"),
    [
        pytest.param(
            False, ["AI review differs from history: review_zulu"], id="first-stored-without-history"
        ),
        pytest.param(
            True, ["AI review differs from history: review_alpha"], id="second-stored-without-history"
        ),
    ],
)
def test_ai_review_without_history_is_reported_whichever_of_two_it_is(setup, first_chained, expected):
    _, service, actor, bid, _ = setup
    add_two_ai_reviews(setup, first_chained=first_chained, second_chained=not first_chained)
    assert_audit_and_export(service, actor, bid, expected)


# Rule: every run the history names and no row answers is reported, whichever it is, and in the
# order its event entered the chain. The audit walks the chain to collect them, so that order is
# fixed by the code: here the first-stored review's event comes first.
@pytest.mark.parametrize(
    ("deleted", "expected"),
    [
        pytest.param(
            ["review_zulu"], ["Historical AI review missing: review_zulu"], id="first-stored-missing"
        ),
        pytest.param(
            ["review_alpha"], ["Historical AI review missing: review_alpha"], id="second-stored-missing"
        ),
        pytest.param(
            ["review_zulu", "review_alpha"],
            ["Historical AI review missing: review_zulu", "Historical AI review missing: review_alpha"],
            id="both-missing",
        ),
    ],
)
def test_missing_ai_review_is_reported_whichever_of_two_and_both(setup, deleted, expected):
    store, service, actor, bid, _ = setup
    add_two_ai_reviews(setup)
    with store.write() as con:
        for run_id in deleted:
            assert con.execute("DELETE FROM ai_calls WHERE id=?", (run_id,)).rowcount == 1
    assert_audit_and_export(service, actor, bid, expected)


# Rule: the export lists a business's AI reviews by created_at. The review created later is stored
# first and has the id that sorts first, so neither storage order nor id order gives this list.
# Rows with equal created_at come in an order the query does not fix, and no test pins that order.
def test_export_lists_ai_reviews_of_one_business_by_created_at(setup):
    store, service, actor, bid, _ = setup
    add_ai_review(store, actor, bid, run_id="review_created_later", created_at="2026-03-01T00:00:00+00:00")
    add_ai_review(store, actor, bid, run_id="review_created_sooner", created_at="2026-02-01T00:00:00+00:00")
    assert ai_call_ids_in_insertion_order(store) == ["review_created_later", "review_created_sooner"]
    # The audit does not check created_at.
    assert_audit_and_export(service, actor, bid, [])
    assert exported_archive(service, actor, bid)["ai_runs"] == [
        {
            "id": "review_created_sooner",
            "state": "unknown",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"note": "Synthetic run written for this test."},
        },
        {
            "id": "review_created_later",
            "state": "unknown",
            "created_at": "2026-03-01T00:00:00+00:00",
            "data": {"note": "Synthetic run written for this test."},
        },
    ]


# Three businesses, each holding one AI review. Three give every kind of pair: two created one
# after the other (first and second, second and third) and two that were not (first and third).
# A version that grouped businesses, for example by odd and even rowid, would mix the first and
# third. Known limit: a grouping by rowid modulo 3 or more keeps these three apart, and no fixed
# number of businesses closes that.


def three_businesses_with_ai_reviews(setup):
    store, _, actor, first, _ = setup
    second = second_business(setup)
    third = second_business(setup)
    add_ai_review(store, actor, first, run_id="review_first_business")
    add_ai_review(store, actor, second, run_id="review_second_of_three")
    add_ai_review(store, actor, third, run_id="review_third_business")
    return first, second, third


# Rule: each of three businesses checks and exports only its own AI review.
def test_each_of_three_businesses_checks_and_exports_only_its_own_ai_review(setup):
    _, service, actor, _, _ = setup
    first, second, third = three_businesses_with_ai_reviews(setup)
    for bid in (first, second, third):
        assert_audit_and_export(service, actor, bid, [])
    assert exported_archive(service, actor, first)["ai_runs"] == [
        {
            "id": "review_first_business",
            "state": "unknown",
            "created_at": "2026-01-01T00:00:00+00:00",
            "data": {"note": "Synthetic run written for this test."},
        }
    ]
    assert exported_archive(service, actor, second)["ai_runs"] == [
        {
            "id": "review_second_of_three",
            "state": "unknown",
            "created_at": "2026-01-01T00:00:00+00:00",
            "data": {"note": "Synthetic run written for this test."},
        }
    ]
    assert exported_archive(service, actor, third)["ai_runs"] == [
        {
            "id": "review_third_business",
            "state": "unknown",
            "created_at": "2026-01-01T00:00:00+00:00",
            "data": {"note": "Synthetic run written for this test."},
        }
    ]


# Rule: a changed AI review of one of three businesses is reported there and nowhere else.
def test_changed_ai_reviews_of_three_businesses_are_reported_each_only_in_its_own(setup):
    store, service, actor, _, _ = setup
    first, second, third = three_businesses_with_ai_reviews(setup)
    alter_ai_review(store, "review_third_business")
    assert_audit_and_export(service, actor, first, [])
    assert_audit_and_export(service, actor, second, [])
    assert_audit_and_export(service, actor, third, ["AI review differs from history: review_third_business"])
    alter_ai_review(store, "review_first_business")
    assert_audit_and_export(service, actor, first, ["AI review differs from history: review_first_business"])
    assert_audit_and_export(service, actor, second, [])
    assert_audit_and_export(service, actor, third, ["AI review differs from history: review_third_business"])


# AI reviews that differ from each other in data and state. The reviews above are alike in
# everything but their ids, which catches an export that drops duplicates but not one that gives a
# review another review's data or state. Each review below is chained with the event a review
# ending in its state leaves, so its row matches its own event and no other.


# Rule: each AI review is compared with its own event and exported with its own data and state.
def test_two_ai_reviews_that_differ_are_each_checked_and_exported_with_their_own_fields(setup):
    store, service, actor, bid, _ = setup
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_zulu",
        state="succeeded",
        data={"advice": {"summary": "Price the pilot at cost."}, "http_status": 200},
    )
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_alpha",
        state="failed",
        data={"error": "Provider output failed schema or citation validation.", "http_status": 200},
    )
    assert ai_call_ids_in_insertion_order(store) == ["review_zulu", "review_alpha"]
    assert_audit_and_export(service, actor, bid, [])
    runs = exported_archive(service, actor, bid)["ai_runs"]
    # The two rows have the same created_at and the export orders by created_at only, so SQL does
    # not fix which of them comes first. They are compared sorted by id.
    assert sorted(runs, key=lambda run: run["id"]) == [
        {
            "id": "review_alpha",
            "state": "failed",
            "created_at": "2026-01-01T00:00:00+00:00",
            "data": {"error": "Provider output failed schema or citation validation.", "http_status": 200},
        },
        {
            "id": "review_zulu",
            "state": "succeeded",
            "created_at": "2026-01-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Price the pilot at cost."}, "http_status": 200},
        },
    ]


# Rule: in the created_at order the export fixes, each entry's data and state travel with its own
# row. The review created later is stored first, so a version that paired a row's place in
# created_at order with the row at that place in storage would swap the two reviews' fields.
def test_export_by_created_at_keeps_each_ai_review_with_its_own_data_and_state(setup):
    store, service, actor, bid, _ = setup
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_created_later",
        created_at="2026-03-01T00:00:00+00:00",
        state="failed",
        data={"error": "Provider rejected the request.", "http_status": 403},
    )
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_created_sooner",
        created_at="2026-02-01T00:00:00+00:00",
        state="succeeded",
        data={"advice": {"summary": "Call the three largest buyers first."}, "http_status": 200},
    )
    assert ai_call_ids_in_insertion_order(store) == ["review_created_later", "review_created_sooner"]
    assert_audit_and_export(service, actor, bid, [])
    assert exported_archive(service, actor, bid)["ai_runs"] == [
        {
            "id": "review_created_sooner",
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Call the three largest buyers first."}, "http_status": 200},
        },
        {
            "id": "review_created_later",
            "state": "failed",
            "created_at": "2026-03-01T00:00:00+00:00",
            "data": {"error": "Provider rejected the request.", "http_status": 403},
        },
    ]


# Rule: a business's export carries its own AI review's advice and outcome, never another
# business's, whichever business's row was stored first.
@pytest.mark.parametrize("first_inserted_first", INSERTION_ORDERS)
def test_export_of_each_business_carries_its_own_ai_review_data_and_state(setup, first_inserted_first):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    reviews = [
        lambda: add_ai_review(
            store,
            actor,
            bid,
            state="succeeded",
            data={"advice": {"summary": "Open a second sales channel."}, "http_status": 200},
        ),
        lambda: add_ai_review(
            store,
            actor,
            other,
            run_id=SECOND_RUN,
            state="failed",
            data={"error": "Provider did not complete a usable answer.", "http_status": 200},
        ),
    ]
    for add in reviews if first_inserted_first else reversed(reviews):
        add()
    assert ai_call_ids_in_insertion_order(store) == (
        ["review_synthetic", "review_second_business"]
        if first_inserted_first
        else ["review_second_business", "review_synthetic"]
    )
    assert_audit_and_export(service, actor, bid, [])
    assert_audit_and_export(service, actor, other, [])
    assert exported_archive(service, actor, bid)["ai_runs"] == [
        {
            "id": "review_synthetic",
            "state": "succeeded",
            "created_at": "2026-01-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Open a second sales channel."}, "http_status": 200},
        }
    ]
    assert exported_archive(service, actor, other)["ai_runs"] == [
        {
            "id": "review_second_business",
            "state": "failed",
            "created_at": "2026-01-01T00:00:00+00:00",
            "data": {"error": "Provider did not complete a usable answer.", "http_status": 200},
        }
    ]


# Rule: the export carries a changed AI review as its row stores it, and the same archive reports
# it. It neither shows the state its history recorded nor leaves the review out.
def test_export_carries_a_changed_ai_review_as_stored_and_reports_it(setup):
    store, service, actor, bid, _ = setup
    add_ai_review(store, actor, bid)
    alter_ai_review(store, "review_synthetic")
    archive = exported_archive(service, actor, bid)
    assert archive["ai_runs"] == [
        {
            "id": "review_synthetic",
            "state": "succeeded",
            "created_at": "2026-01-01T00:00:00+00:00",
            "data": {"note": "Advice that was never returned."},
        }
    ]
    assert archive["integrity"]["record_errors"] == ["AI review differs from history: review_synthetic"]
    assert archive["integrity"]["valid"] is False


# Rule: the export carries an AI review that has no history, and the same archive reports it.
def test_export_carries_an_ai_review_without_history_and_reports_it(setup):
    store, service, actor, bid, _ = setup
    add_ai_review(
        store,
        actor,
        bid,
        chained=False,
        state="failed",
        data={"error": "Provider rejected the request.", "http_status": 429},
    )
    archive = exported_archive(service, actor, bid)
    assert archive["ai_runs"] == [
        {
            "id": "review_synthetic",
            "state": "failed",
            "created_at": "2026-01-01T00:00:00+00:00",
            "data": {"error": "Provider rejected the request.", "http_status": 429},
        }
    ]
    assert archive["integrity"]["record_errors"] == ["AI review differs from history: review_synthetic"]
    assert archive["integrity"]["valid"] is False


# Rule: each exported review keeps its own data when another review shares its state, and its own
# state when another review shares its data. The first two reviews share a state and differ in
# data; the first and third share data and differ in state.
def test_export_keeps_each_ai_review_fields_when_reviews_share_a_state_or_data(setup):
    store, service, actor, bid, _ = setup
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_first",
        created_at="2026-02-01T00:00:00+00:00",
        state="succeeded",
        data={"advice": {"summary": "Hire a part-time bookkeeper."}, "http_status": 200},
    )
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_second",
        created_at="2026-03-01T00:00:00+00:00",
        state="succeeded",
        data={"advice": {"summary": "Delay the second location."}, "http_status": 200},
    )
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_third",
        created_at="2026-04-01T00:00:00+00:00",
        state="failed",
        data={"advice": {"summary": "Hire a part-time bookkeeper."}, "http_status": 200},
    )
    assert ai_call_ids_in_insertion_order(store) == ["review_first", "review_second", "review_third"]
    assert_audit_and_export(service, actor, bid, [])
    assert exported_archive(service, actor, bid)["ai_runs"] == [
        {
            "id": "review_first",
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Hire a part-time bookkeeper."}, "http_status": 200},
        },
        {
            "id": "review_second",
            "state": "succeeded",
            "created_at": "2026-03-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Delay the second location."}, "http_status": 200},
        },
        {
            "id": "review_third",
            "state": "failed",
            "created_at": "2026-04-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Hire a part-time bookkeeper."}, "http_status": 200},
        },
    ]


# Rule: a review is compared with its own event, and another review's event does not answer for
# it. A row changed by SQL to the state and data of another review of the same business matches
# that review's event, not its own, and is reported.
def test_ai_review_changed_to_another_review_outcome_is_reported(setup):
    store, service, actor, bid, _ = setup
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_zulu",
        state="succeeded",
        data={"advice": {"summary": "Price the pilot at cost."}, "http_status": 200},
    )
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_alpha",
        state="failed",
        data={"error": "Provider output failed schema or citation validation.", "http_status": 200},
    )
    with store.write() as con:
        assert (
            con.execute(
                "UPDATE ai_calls SET data=?,state='failed' WHERE id='review_zulu'",
                (
                    canonical(
                        {"error": "Provider output failed schema or citation validation.", "http_status": 200}
                    ),
                ),
            ).rowcount
            == 1
        )
    assert_audit_and_export(service, actor, bid, ["AI review differs from history: review_zulu"])


# Two businesses whose AI reviews share some fields and not others. The test above with two
# businesses gives their reviews the same created_at and nothing else in common; the tests with
# three businesses give theirs everything in common but the id. Neither catches an export or an
# audit that takes a field from another business's review that shares another field with this one,
# nor an export that writes another business's review date. Each case below is one way two
# reviews can share fields, and each expected entry is written out beside the values stored.

SHARED_FIELD_CASES = [
    pytest.param(
        {
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Open a second sales channel."}, "http_status": 200},
        },
        {
            "id": "review_synthetic",
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Open a second sales channel."}, "http_status": 200},
        },
        {
            "state": "failed",
            "created_at": "2026-03-01T00:00:00+00:00",
            "data": {"error": "Provider did not complete a usable answer.", "http_status": 200},
        },
        {
            "id": "review_second_business",
            "state": "failed",
            "created_at": "2026-03-01T00:00:00+00:00",
            "data": {"error": "Provider did not complete a usable answer.", "http_status": 200},
        },
        id="nothing-shared",
    ),
    pytest.param(
        {
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Raise the price of the annual plan."}, "http_status": 200},
        },
        {
            "id": "review_synthetic",
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Raise the price of the annual plan."}, "http_status": 200},
        },
        {
            "state": "succeeded",
            "created_at": "2026-03-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Sell through two regional wholesalers."}, "http_status": 200},
        },
        {
            "id": "review_second_business",
            "state": "succeeded",
            "created_at": "2026-03-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Sell through two regional wholesalers."}, "http_status": 200},
        },
        id="state-shared",
    ),
    pytest.param(
        {
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Keep the first site open on Sundays."}, "http_status": 200},
        },
        {
            "id": "review_synthetic",
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Keep the first site open on Sundays."}, "http_status": 200},
        },
        {
            "state": "failed",
            "created_at": "2026-03-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Keep the first site open on Sundays."}, "http_status": 200},
        },
        {
            "id": "review_second_business",
            "state": "failed",
            "created_at": "2026-03-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Keep the first site open on Sundays."}, "http_status": 200},
        },
        id="data-shared",
    ),
    pytest.param(
        {
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Hire a part-time bookkeeper."}, "http_status": 200},
        },
        {
            "id": "review_synthetic",
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Hire a part-time bookkeeper."}, "http_status": 200},
        },
        {
            "state": "failed",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"error": "Provider rejected the request.", "http_status": 429},
        },
        {
            "id": "review_second_business",
            "state": "failed",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"error": "Provider rejected the request.", "http_status": 429},
        },
        id="date-shared",
    ),
    pytest.param(
        {
            "state": "failed",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"error": "Provider output failed schema or citation validation.", "http_status": 200},
        },
        {
            "id": "review_synthetic",
            "state": "failed",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"error": "Provider output failed schema or citation validation.", "http_status": 200},
        },
        {
            "state": "failed",
            "created_at": "2026-03-01T00:00:00+00:00",
            "data": {"error": "Provider output failed schema or citation validation.", "http_status": 200},
        },
        {
            "id": "review_second_business",
            "state": "failed",
            "created_at": "2026-03-01T00:00:00+00:00",
            "data": {"error": "Provider output failed schema or citation validation.", "http_status": 200},
        },
        id="state-and-data-shared",
    ),
    pytest.param(
        {
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Delay the second location."}, "http_status": 200},
        },
        {
            "id": "review_synthetic",
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Delay the second location."}, "http_status": 200},
        },
        {
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Call the three largest buyers first."}, "http_status": 200},
        },
        {
            "id": "review_second_business",
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Call the three largest buyers first."}, "http_status": 200},
        },
        id="state-and-date-shared",
    ),
    pytest.param(
        {
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Price the pilot at cost."}, "http_status": 200},
        },
        {
            "id": "review_synthetic",
            "state": "succeeded",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Price the pilot at cost."}, "http_status": 200},
        },
        {
            "state": "failed",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Price the pilot at cost."}, "http_status": 200},
        },
        {
            "id": "review_second_business",
            "state": "failed",
            "created_at": "2026-02-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Price the pilot at cost."}, "http_status": 200},
        },
        id="data-and-date-shared",
    ),
]


# Rule: each business's review is compared with its own event and exported with its own state,
# data and date, whichever of those it shares with another business's review and whichever
# business's row was stored first.
@pytest.mark.parametrize("first_inserted_first", INSERTION_ORDERS)
@pytest.mark.parametrize(("first_stored", "first_entry", "second_stored", "second_entry"), SHARED_FIELD_CASES)
def test_each_business_ai_review_keeps_its_own_fields_when_another_business_review_shares_some(
    setup, first_stored, first_entry, second_stored, second_entry, first_inserted_first
):
    store, service, actor, bid, _ = setup
    other = second_business(setup)
    reviews = [
        lambda: add_ai_review(store, actor, bid, **first_stored),
        lambda: add_ai_review(store, actor, other, run_id=SECOND_RUN, **second_stored),
    ]
    for add in reviews if first_inserted_first else reversed(reviews):
        add()
    assert ai_call_ids_in_insertion_order(store) == (
        ["review_synthetic", "review_second_business"]
        if first_inserted_first
        else ["review_second_business", "review_synthetic"]
    )
    assert_audit_and_export(service, actor, bid, [])
    assert_audit_and_export(service, actor, other, [])
    assert exported_archive(service, actor, bid)["ai_runs"] == [first_entry]
    assert exported_archive(service, actor, other)["ai_runs"] == [second_entry]


# Rule: the audit compares a review's state with its history exactly, and the export writes the
# state exactly as the row stores it. A state that differs from the recorded one only in letter
# case or in surrounding space is reported, and exported unchanged.
@pytest.mark.parametrize(
    ("stored_state", "expected_entry"),
    [
        pytest.param(
            "Succeeded",
            {
                "id": "review_synthetic",
                "state": "Succeeded",
                "created_at": "2026-01-01T00:00:00+00:00",
                "data": {"advice": {"summary": "Open a second sales channel."}, "http_status": 200},
            },
            id="capital-letter",
        ),
        pytest.param(
            "succeeded ",
            {
                "id": "review_synthetic",
                "state": "succeeded ",
                "created_at": "2026-01-01T00:00:00+00:00",
                "data": {"advice": {"summary": "Open a second sales channel."}, "http_status": 200},
            },
            id="space-after",
        ),
        pytest.param(
            " succeeded",
            {
                "id": "review_synthetic",
                "state": " succeeded",
                "created_at": "2026-01-01T00:00:00+00:00",
                "data": {"advice": {"summary": "Open a second sales channel."}, "http_status": 200},
            },
            id="space-before",
        ),
    ],
)
def test_ai_review_state_changed_only_in_form_is_reported_and_exported_as_stored(
    setup, stored_state, expected_entry
):
    store, service, actor, bid, _ = setup
    add_ai_review(
        store,
        actor,
        bid,
        state="succeeded",
        data={"advice": {"summary": "Open a second sales channel."}, "http_status": 200},
    )
    with store.write() as con:
        assert (
            con.execute("UPDATE ai_calls SET state=? WHERE id='review_synthetic'", (stored_state,)).rowcount
            == 1
        )
    archive = exported_archive(service, actor, bid)
    assert archive["ai_runs"] == [expected_entry]
    assert archive["integrity"]["record_errors"] == ["AI review differs from history: review_synthetic"]
    assert archive["integrity"]["valid"] is False
    assert_audit_and_export(service, actor, bid, ["AI review differs from history: review_synthetic"])


# Rule: the export writes the state of a review its history accepts exactly as stored too. The row
# and its event both carry a state with a capital letter, so the audit is clean and the export
# keeps the capital.
def test_ai_review_state_with_a_capital_that_matches_its_history_is_exported_as_stored(setup):
    store, service, actor, bid, _ = setup
    add_ai_review(
        store,
        actor,
        bid,
        state="Succeeded",
        data={"advice": {"summary": "Open a second sales channel."}, "http_status": 200},
    )
    assert_audit_and_export(service, actor, bid, [])
    assert exported_archive(service, actor, bid)["ai_runs"] == [
        {
            "id": "review_synthetic",
            "state": "Succeeded",
            "created_at": "2026-01-01T00:00:00+00:00",
            "data": {"advice": {"summary": "Open a second sales channel."}, "http_status": 200},
        }
    ]


# Known limit, not wanted behaviour: the audit does not read a review's date. The event a review
# leaves carries its run id, state and data digest and no date, so a created_at changed in the row
# by SQL is not reported, and the export writes the date exactly as the row now stores it.
@pytest.mark.parametrize(
    ("stored_date", "expected_entry"),
    [
        pytest.param(
            "2026-02-01T00:00:00Z",
            {
                "id": "review_synthetic",
                "state": "succeeded",
                "created_at": "2026-02-01T00:00:00Z",
                "data": {"advice": {"summary": "Hire a part-time bookkeeper."}, "http_status": 200},
            },
            id="zulu-suffix",
        ),
        pytest.param(
            "2026-02-01T00:00:00.250000+00:00",
            {
                "id": "review_synthetic",
                "state": "succeeded",
                "created_at": "2026-02-01T00:00:00.250000+00:00",
                "data": {"advice": {"summary": "Hire a part-time bookkeeper."}, "http_status": 200},
            },
            id="fractional-seconds",
        ),
        pytest.param(
            " 2026-02-01T00:00:00+00:00 ",
            {
                "id": "review_synthetic",
                "state": "succeeded",
                "created_at": " 2026-02-01T00:00:00+00:00 ",
                "data": {"advice": {"summary": "Hire a part-time bookkeeper."}, "http_status": 200},
            },
            id="space-before-and-after",
        ),
    ],
)
def test_known_limit_ai_review_date_changed_in_the_row_is_exported_as_changed_and_not_reported(
    setup, stored_date, expected_entry
):
    store, service, actor, bid, _ = setup
    add_ai_review(
        store,
        actor,
        bid,
        state="succeeded",
        data={"advice": {"summary": "Hire a part-time bookkeeper."}, "http_status": 200},
    )
    with store.write() as con:
        assert (
            con.execute(
                "UPDATE ai_calls SET created_at=? WHERE id='review_synthetic'", (stored_date,)
            ).rowcount
            == 1
        )
    assert_audit_and_export(service, actor, bid, [])
    assert exported_archive(service, actor, bid)["ai_runs"] == [expected_entry]


# Known limit of these tests, not of the code: a read capped at N reviews of a business, in the
# audit or the export, passes this file whenever N is at least the most reviews any test stores for
# one business, which is three. A test with more reviews would only move the same gap to a larger
# cap, so no fixed number of reviews closes it, and no test here tries.


# Rule: the export lists reviews by their full created_at, so two reviews created on the same day
# are listed by time of day. The one created later is stored first, so an order by the day alone
# would leave them in storage order.
def test_export_lists_ai_reviews_created_on_the_same_day_by_time(setup):
    store, service, actor, bid, _ = setup
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_afternoon",
        created_at="2026-02-01T14:00:00+00:00",
        state="failed",
        data={"error": "Provider rejected the request.", "http_status": 403},
    )
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_morning",
        created_at="2026-02-01T09:00:00+00:00",
        state="succeeded",
        data={"advice": {"summary": "Call the three largest buyers first."}, "http_status": 200},
    )
    assert ai_call_ids_in_insertion_order(store) == ["review_afternoon", "review_morning"]
    assert_audit_and_export(service, actor, bid, [])
    assert exported_archive(service, actor, bid)["ai_runs"] == [
        {
            "id": "review_morning",
            "state": "succeeded",
            "created_at": "2026-02-01T09:00:00+00:00",
            "data": {"advice": {"summary": "Call the three largest buyers first."}, "http_status": 200},
        },
        {
            "id": "review_afternoon",
            "state": "failed",
            "created_at": "2026-02-01T14:00:00+00:00",
            "data": {"error": "Provider rejected the request.", "http_status": 403},
        },
    ]


# Rule: the export orders reviews by the whole created_at text. The application writes dates with
# microseconds, so two reviews in the same second are ordered by their fractions; and a date's
# length does not decide its place. The reviews are stored in an order that is neither: 09:00:00.75,
# then 10:00:00 (shorter, with no fraction), then 09:00:00.25. An order by the date to the second
# would keep the two 09:00:00 reviews in storage order; an order by length would put 10:00:00 first.
def test_export_lists_ai_reviews_by_the_whole_date_text_to_the_fraction_of_a_second(setup):
    store, service, actor, bid, _ = setup
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_three_quarters",
        created_at="2026-02-01T09:00:00.750000+00:00",
        state="succeeded",
        data={"advice": {"summary": "Hire a part-time bookkeeper."}, "http_status": 200},
    )
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_ten",
        created_at="2026-02-01T10:00:00+00:00",
        state="failed",
        data={"error": "Provider rejected the request.", "http_status": 429},
    )
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_quarter",
        created_at="2026-02-01T09:00:00.250000+00:00",
        state="succeeded",
        data={"advice": {"summary": "Delay the second location."}, "http_status": 200},
    )
    assert ai_call_ids_in_insertion_order(store) == ["review_three_quarters", "review_ten", "review_quarter"]
    assert_audit_and_export(service, actor, bid, [])
    assert exported_archive(service, actor, bid)["ai_runs"] == [
        {
            "id": "review_quarter",
            "state": "succeeded",
            "created_at": "2026-02-01T09:00:00.250000+00:00",
            "data": {"advice": {"summary": "Delay the second location."}, "http_status": 200},
        },
        {
            "id": "review_three_quarters",
            "state": "succeeded",
            "created_at": "2026-02-01T09:00:00.750000+00:00",
            "data": {"advice": {"summary": "Hire a part-time bookkeeper."}, "http_status": 200},
        },
        {
            "id": "review_ten",
            "state": "failed",
            "created_at": "2026-02-01T10:00:00+00:00",
            "data": {"error": "Provider rejected the request.", "http_status": 429},
        },
    ]


# Known limit, not wanted behaviour: the export orders reviews by created_at as text. The
# application writes every date in UTC, where text order is time order; this pins what happens to
# a row that carries another offset. The first review stored is 10:00 at +05:00 (05:00 UTC), the
# second 09:00 at +00:00. As text the second sorts first; as instants the first is earlier; in
# storage order the first comes first. The export lists them in text order.
def test_known_limit_export_orders_ai_review_dates_with_other_offsets_as_text(setup):
    store, service, actor, bid, _ = setup
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_plus_five",
        created_at="2026-02-01T10:00:00+05:00",
        state="succeeded",
        data={"advice": {"summary": "Delay the second location."}, "http_status": 200},
    )
    add_ai_review(
        store,
        actor,
        bid,
        run_id="review_utc",
        created_at="2026-02-01T09:00:00+00:00",
        state="failed",
        data={"error": "Provider did not complete a usable answer.", "http_status": 200},
    )
    assert ai_call_ids_in_insertion_order(store) == ["review_plus_five", "review_utc"]
    assert_audit_and_export(service, actor, bid, [])
    assert exported_archive(service, actor, bid)["ai_runs"] == [
        {
            "id": "review_utc",
            "state": "failed",
            "created_at": "2026-02-01T09:00:00+00:00",
            "data": {"error": "Provider did not complete a usable answer.", "http_status": 200},
        },
        {
            "id": "review_plus_five",
            "state": "succeeded",
            "created_at": "2026-02-01T10:00:00+05:00",
            "data": {"advice": {"summary": "Delay the second location."}, "http_status": 200},
        },
    ]


def test_export_digest_covers_the_archive_with_its_ai_review(setup):
    store, service, actor, bid, _ = setup
    add_ai_review(store, actor, bid)
    archive = exported_archive(service, actor, bid)
    assert archive["ai_runs"] == [SYNTHETIC_RUN_ENTRY]
    body = {name: value for name, value in archive.items() if name != "sha256"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    assert archive["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


# Gaps named in review: literals that run back as SQL, number and key spellings, REAL and NaN versions.

LITERAL_VALUES = [
    "NULL",
    "X''",
    "X'FF7B'",
    "X'" + "AB" * 16 + "'",
    "''",
    "'word'",
    "'it''s'",
    "'" + "a" * 32 + "'",
    "'tab' || char(9)",
    "'é'",
    "CAST(X'00' AS TEXT)",
    "CAST(X'FF7B' AS TEXT)",
    "CAST(X'EDA080' AS TEXT)",
]


@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le", "UTF-16be"])
def test_stored_literal_selects_the_same_value_in_sql(encoding):
    con = sqlite3.connect(":memory:")
    con.execute(f"PRAGMA encoding='{encoding}'")
    con.execute("CREATE TABLE t(v)")
    for expression in LITERAL_VALUES + ["'" + "a" * 33 + "'", "X'" + "AB" * 17 + "'"]:
        rowid = con.execute(f"INSERT INTO t VALUES({expression})").lastrowid
        storage, value = con.execute(
            "SELECT typeof(v), CASE typeof(v) WHEN 'text' THEN CAST(v AS BLOB) ELSE v END FROM t WHERE rowid=?",
            (rowid,),
        ).fetchone()
        shown = stored_literal(storage, value, encoding)
        assert all(" " <= c <= "~" for c in shown) and len(shown) <= 52, shown
        if "..." in shown:
            assert len(value) > 16
            continue
        # The literal selects this row's value; in UTF-16, CAST(X'00' AS TEXT) stores '' (an odd
        # trailing byte is dropped), and the literal shows what is stored.
        assert con.execute(
            f"SELECT v IS {shown} AND typeof(v)=typeof({shown}) FROM t WHERE rowid=?", (rowid,)
        ).fetchone() == (1,), (expression, shown)
    con.close()


def test_snapshot_with_repeated_key_is_read_by_its_last_value(setup):
    # Documented: a repeated key keeps only its last value, so an earlier one is not compared.
    store = setup[0]
    item = history(setup)
    text = stored_snapshot_text(store, item["id"], 1)
    assert text.count('"title":') == 1
    hidden = text.replace('"title":', '"title":"Rewritten past","title":')
    shown = text.replace('"title":"Example buyer"', '"title":"Example buyer","title":"Rewritten past"')
    assert shown != text
    with store.write() as con:
        con.execute("UPDATE revisions SET snapshot=? WHERE object_id=? AND version=1", (hidden, item["id"]))
    assert integrity(setup)["record_errors"] == []
    with store.write() as con:
        con.execute("UPDATE revisions SET snapshot=? WHERE object_id=? AND version=1", (shown, item["id"]))
    assert integrity(setup)["record_errors"] == [
        f"Historical revision differs from history: {item['id']} version 1"
    ]


def test_integer_minus_zero_is_the_same_value_as_zero(setup):
    store = setup[0]
    item = typed_history(setup)
    with store.write() as con:
        text = stored_snapshot_text(store, item["id"], 1)
        con.execute(
            "UPDATE revisions SET snapshot=? WHERE object_id=? AND version=1",
            (retype(text, '"zero":0', '"zero":-0'), item["id"]),
        )
    result = integrity(setup)
    assert result["record_errors"] == []
    assert result["valid"]


def test_float_minus_zero_differs_from_zero():
    assert same_json({"a": -0}, {"a": 0})
    assert not same_json({"a": -0.0}, {"a": 0.0})
    assert not same_json({"a": 0.0}, {"a": 0})


def test_real_whole_number_versions_count_as_integers(setup):
    store = setup[0]
    item = history(setup)
    untyped_column(store, "version")
    with store.write() as con:
        for version in (1, 3):
            con.execute(
                "UPDATE revisions SET version=? WHERE object_id=? AND version=?",
                (float(version), item["id"], version),
            )
        stored = con.execute(
            "SELECT typeof(version) FROM revisions WHERE object_id=? ORDER BY version", (item["id"],)
        ).fetchall()
    assert [row[0] for row in stored] == ["real", "integer", "real"]
    result = integrity(setup)
    assert result["record_errors"] == []
    assert result["valid"]


def test_nan_bound_as_version_is_stored_as_null(setup):
    store = setup[0]
    item = history(setup)
    with store.write() as con, pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "UPDATE revisions SET version=? WHERE object_id=? AND version=1", (float("nan"), item["id"])
        )
    untyped_column(store, "version")
    with store.write() as con:
        con.execute(
            "UPDATE revisions SET version=? WHERE object_id=? AND version=1", (float("nan"), item["id"])
        )
        assert (
            con.execute(
                "SELECT count(*) FROM revisions WHERE object_id=? AND version IS NULL", (item["id"],)
            ).fetchone()[0]
            == 1
        )
    assert integrity(setup)["record_errors"] == [
        f"Revision has no recorded history: {item['id']} version NULL",
        f"Historical revision missing: {item['id']} version 1",
    ]


def test_removing_the_newest_event_keeps_the_chain_valid_but_is_reported(setup):
    # Documented: the chain alone does not show that its newest events were removed.
    store, service, actor, bid, _ = setup
    item = history(setup, versions=2)
    before = integrity(setup)
    with store.write() as con:
        newest = con.execute(
            "SELECT seq,object_id FROM events WHERE business_id=? ORDER BY seq DESC LIMIT 1", (bid,)
        ).fetchone()
        assert newest["object_id"] == item["id"]
        con.execute("DELETE FROM events WHERE seq=?", (newest["seq"],))
    with store.read() as con:
        chain = store.verify_chain(con, bid)
    assert chain["valid"] and chain["events_checked"] == before["events_checked"] - 1
    assert integrity(setup)["record_errors"] == [
        f"Current record differs from history: {item['id']}",
        f"Revision has no recorded history: {item['id']} version 2",
    ]


def test_limitation_text_is_exact(setup):
    assert integrity(setup)["limitation"] == (
        "The hash chain detects a changed recorded event, but anyone who can edit the database can "
        "rewrite the chain, remove its newest events or append events to it that pass this check. Keep "
        "protected off-machine backups, and store the head hash outside this machine to show later that "
        "the events up to it are unchanged."
    )


# Known limits, not wanted behaviour: stored values that stop the audit with an exception instead
# of a report (docs/ARCHITECTURE.md, Integrity and recovery, lists them). Making the audit report
# them is separate work; these tests record exactly where it raises today, so that work has to
# change them on purpose. Each "known_limit" test asserts the exact exception type. The tests
# without "known_limit" in their name pin the neighbouring values that are reported.


class SQL(str):
    """An SQL expression to store as is, where a plain str is bound as TEXT."""


def stored(value):
    return (value, ()) if isinstance(value, SQL) else ("?", (value,))


INVALID_TEXT = SQL("CAST(X'7BFF7D' AS TEXT)")
DEEP = "[" * 100000 + "]" * 100000
LONG_INTEGER = "1" + "0" * 4300  # 4,301 digits; Python's default limit for int() is 4,300.
NOT_THE_HEAD = "f" * 64
NOT_ITS_HASH = "0" * 64


def without_constraint(store, table, declared, column):
    """Rewrite one table's schema so ``declared`` (``<column> <TYPE> NOT NULL``) becomes ``column``."""
    con = sqlite3.connect(store.path, isolation_level=None)
    try:
        con.execute("PRAGMA writable_schema=ON")
        changed = con.execute(
            "UPDATE sqlite_schema SET sql=replace(sql,?,?) WHERE type='table' AND name=? AND instr(sql,?)",
            (declared, column, table, declared),
        ).rowcount
        con.execute("PRAGMA writable_schema=OFF")
    finally:
        con.close()
    assert changed == 1


def append_raw(store, bid, object_id, payload, *, hashed=True, columns=None):
    """Append one event row with SQL and return its seq. Its hash is never right.

    With ``hashed`` its prev_hash is the current head, so the chain check hashes it if every earlier
    event verifies; otherwise its prev_hash is wrong and the check stops before hashing it.
    ``payload`` and the values in ``columns`` are bound as TEXT unless given as SQL.
    """
    with store.write() as con:
        head = con.execute(
            "SELECT hash FROM events WHERE business_id=? ORDER BY seq DESC LIMIT 1", (bid,)
        ).fetchone()[0]
        values = {
            "business_id": bid,
            "actor_id": "principal_appender",
            "action": "record.updated",
            "object_id": object_id,
            "payload": payload,
            "created_at": APPENDED_AT,
            "prev_hash": head if hashed else NOT_THE_HEAD,
            "hash": NOT_ITS_HASH,
            **(columns or {}),
        }
        parts = [stored(v) for v in values.values()]
        con.execute(
            f"INSERT INTO events({','.join(values)}) VALUES({','.join(p[0] for p in parts)})",
            [arg for p in parts for arg in p[1]],
        )
        return con.execute("SELECT max(seq) FROM events").fetchone()[0]


def assert_audit_raises(setup, expected, export=False):
    _, service, actor, bid, _ = setup
    with pytest.raises(Exception) as caught:
        service.audit(actor, bid, export)
    assert caught.type is expected, caught.value


def broken_at(setup, seq, record_errors=()):
    result = integrity(setup)
    assert result["valid"] is False
    assert result["failed_sequence"] == seq
    assert result["record_errors"] == list(record_errors)


POSITIONS = [pytest.param(True, id="hashed"), pytest.param(False, id="not-hashed")]

UNREADABLE_PAYLOADS = [
    pytest.param("{", json.JSONDecodeError, id="not-json"),
    pytest.param("", json.JSONDecodeError, id="empty-text"),
    pytest.param(SQL("X'7BFF7D'"), UnicodeDecodeError, id="blob-not-utf8"),
    pytest.param('{"n":' + LONG_INTEGER + "}", ValueError, id="integer-over-digit-limit"),
    pytest.param(DEEP, RecursionError, id="nested-beyond-recursion-limit"),
    pytest.param("[1]", AttributeError, id="json-array"),
    pytest.param("null", AttributeError, id="json-null"),
    pytest.param("3", AttributeError, id="json-number"),
    pytest.param('"x"', AttributeError, id="json-string"),
    pytest.param('{"run_id":[1],"data_sha256":"a"}', TypeError, id="list-run-id"),
    pytest.param('{"run_id":{"a":1},"data_sha256":true}', TypeError, id="object-run-id-true-digest"),
    pytest.param('{"run_id":[1],"data_sha256":1}', TypeError, id="list-run-id-number-digest"),
    pytest.param('{"run_id":[1],"data_sha256":[0]}', TypeError, id="list-run-id-list-digest"),
]


@pytest.mark.parametrize("hashed", POSITIONS)
@pytest.mark.parametrize(("payload", "expected"), UNREADABLE_PAYLOADS)
def test_known_limit_unreadable_event_payload_raises_at_any_position(setup, payload, expected, hashed):
    store, service, actor, bid, _ = setup
    item = history(setup, versions=1)
    append_raw(store, bid, item["id"], payload, hashed=hashed)
    assert_audit_raises(setup, expected)


def test_known_limit_null_event_payload_raises(setup):
    store, _, _, bid, _ = setup
    without_constraint(store, "events", "payload TEXT NOT NULL", "payload")
    append_raw(store, bid, bid, SQL("NULL"), hashed=False)
    assert_audit_raises(setup, TypeError)


@pytest.mark.parametrize(
    "payload",
    [
        '{"run_id":[],"data_sha256":"a"}',
        '{"run_id":{},"data_sha256":"a"}',
        '{"run_id":[1],"data_sha256":""}',
        '{"run_id":[1],"data_sha256":0}',
        '{"run_id":[1],"data_sha256":false}',
        '{"run_id":{"a":1},"data_sha256":null}',
        '{"run_id":[1],"data_sha256":[]}',
        '{"run_id":[1]}',
    ],
)
def test_list_or_object_run_id_without_a_digest_is_reported(setup, payload):
    store, _, _, bid, _ = setup
    seq = append_raw(store, bid, bid, payload)
    broken_at(setup, seq)


@pytest.mark.parametrize("hashed", POSITIONS)
def test_known_limit_null_object_id_with_a_version_and_no_id_raises(setup, hashed):
    # Without an id, payload.get("id") == object_id is None == None, and the id is then read.
    store, _, _, bid, _ = setup
    without_constraint(store, "events", "object_id TEXT NOT NULL", "object_id")
    append_raw(store, bid, SQL("NULL"), '{"version":1,"data":{}}', hashed=hashed)
    assert_audit_raises(setup, KeyError)


@pytest.mark.parametrize("hashed", POSITIONS)
def test_null_object_id_with_a_null_id_is_reported(setup, hashed):
    store, _, _, bid, _ = setup
    without_constraint(store, "events", "object_id TEXT NOT NULL", "object_id")
    seq = append_raw(store, bid, SQL("NULL"), '{"id":null,"version":1,"data":{}}', hashed=hashed)
    broken_at(setup, seq, ["Historical record missing: None", "Historical revision missing: None version 1"])


def export_integrity(setup):
    _, service, actor, bid, _ = setup
    return service.audit(actor, bid, True)["integrity"]


@pytest.mark.parametrize("hashed", POSITIONS)
@pytest.mark.parametrize(
    ("column", "payload"),
    [
        pytest.param("actor_id", '{"note":1}', id="actor-id"),
        pytest.param("action", '{"note":1}', id="action"),
        pytest.param("created_at", '{"note":1}', id="created-at"),
        pytest.param("prev_hash", '{"note":1}', id="prev-hash"),
        pytest.param("hash", '{"note":1}', id="hash"),
        pytest.param("object_id", '{"data":{}}', id="object-id-payload-without-version"),
    ],
)
def test_null_event_column_is_reported(setup, column, payload, hashed):
    store, _, _, bid, _ = setup
    without_constraint(store, "events", f"{column} TEXT NOT NULL", column)
    seq = append_raw(store, bid, bid, payload, hashed=hashed, columns={column: SQL("NULL")})
    broken_at(setup, seq)
    assert export_integrity(setup)["failed_sequence"] == seq


def test_null_id_of_a_current_record_is_reported(setup):
    # SQLite allows NULL in a TEXT PRIMARY KEY without any change to the table.
    store = setup[0]
    row = record_row(setup, "objects")
    set_column(store, "objects", "id", row, SQL("NULL"))
    expected = [
        "Current record differs from history: None",
        "Revision mismatch: None",
        f"Historical record missing: {row}",
    ]
    assert integrity(setup)["record_errors"] == expected
    assert export_integrity(setup)["record_errors"] == expected


def add_ai_review(
    store,
    actor,
    bid,
    chained=True,
    run_id="review_synthetic",
    created_at=APPENDED_AT,
    data=None,
    state="unknown",
):
    """Store an AI review row and, when chained, the event a review ending in that state leaves."""
    if data is None:
        data = {"note": "Synthetic run written for this test."}
    with store.write() as con:
        con.execute(
            "INSERT INTO ai_calls VALUES(?,?,?,?,?,?,?,?)",
            (run_id, bid, actor["id"], key(), "0" * 64, canonical(data), state, created_at),
        )
        if chained:
            store.event(
                con,
                bid,
                actor,
                "ai." + state,
                run_id,
                {"run_id": run_id, "state": state, "data_sha256": digest(canonical(data))},
            )
    return run_id


TABLE_COLUMNS = [
    *[
        ("events", c)
        for c in ("actor_id", "action", "object_id", "payload", "created_at", "prev_hash", "hash")
    ],
    *[("businesses", c) for c in ("data", "version", "created_at", "updated_at")],
    *[
        ("objects", c)
        for c in ("id", "kind", "title", "data", "state", "version", "created_at", "updated_at")
    ],
    *[
        ("ai_calls", c)
        for c in ("id", "principal_id", "command_key", "request_hash", "data", "state", "created_at")
    ],
]


@pytest.mark.parametrize("hashed", POSITIONS)
@pytest.mark.parametrize(("table", "column"), TABLE_COLUMNS, ids=[f"{t}.{c}" for t, c in TABLE_COLUMNS])
def test_known_limit_text_not_valid_in_the_database_encoding_raises(setup, table, column, hashed):
    # sqlite3 raises while fetching the row; only revision rows are read in a way that avoids it.
    store, _, actor, bid, _ = setup
    item = history(setup, versions=1)
    if table == "events":
        append_raw(store, bid, item["id"], '{"note":1}', hashed=hashed, columns={column: INVALID_TEXT})
    else:
        if table == "ai_calls":
            add_ai_review(store, actor, bid)
        row = {"businesses": bid, "objects": item["id"], "ai_calls": "review_synthetic"}[table]
        with store.write() as con:
            assert con.execute(f"UPDATE {table} SET {column}={INVALID_TEXT} WHERE id=?", (row,)).rowcount == 1
    assert_audit_raises(setup, sqlite3.OperationalError)


@pytest.mark.parametrize(("encoding", "unpaired"), [("UTF-16le", "X'00D8'"), ("UTF-16be", "X'D800'")])
def test_known_limit_text_not_valid_in_a_utf16_database_raises(tmp_path, encoding, unpaired):
    path = tmp_path / "private" / "foundry.db"
    path.parent.mkdir()
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA encoding='{encoding}'")
    con.execute("CREATE TABLE encoding_marker(x)")
    con.close()
    store = Store(path)
    actor = store.authenticate(store.bootstrap()["token"])
    service = Service(store)
    bid = service.create_business(
        actor,
        key(),
        models.BusinessCreate(name="Encoded business", concept="A synthetic business.").model_dump(
            mode="json"
        ),
    )["id"]
    append_raw(
        store, bid, bid, '{"note":1}', hashed=False, columns={"actor_id": SQL(f"CAST({unpaired} AS TEXT)")}
    )
    assert_audit_raises((store, service, actor, bid, None), sqlite3.OperationalError)


UNREADABLE_DATA = [
    pytest.param("{", json.JSONDecodeError, id="not-json"),
    pytest.param("", json.JSONDecodeError, id="empty-text"),
    pytest.param(SQL("X'FF'"), UnicodeDecodeError, id="blob-not-utf8"),
    pytest.param('{"n":' + LONG_INTEGER + "}", ValueError, id="integer-over-digit-limit"),
    pytest.param(DEEP, RecursionError, id="nested-beyond-recursion-limit"),
]


def record_row(setup, table):
    store, _, _, bid, _ = setup
    return bid if table == "businesses" else history(setup, versions=1)["id"]


def set_column(store, table, column, row, value):
    expression, args = stored(value)
    with store.write() as con:
        assert con.execute(f"UPDATE {table} SET {column}={expression} WHERE id=?", (*args, row)).rowcount == 1


@pytest.mark.parametrize("table", ["businesses", "objects"])
@pytest.mark.parametrize(("data", "expected"), UNREADABLE_DATA)
def test_known_limit_unreadable_record_data_raises(setup, table, data, expected):
    store = setup[0]
    set_column(store, table, "data", record_row(setup, table), data)
    assert_audit_raises(setup, expected)


@pytest.mark.parametrize("table", ["businesses", "objects"])
def test_known_limit_null_record_data_raises(setup, table):
    store = setup[0]
    row = record_row(setup, table)
    without_constraint(store, table, "data TEXT NOT NULL", "data")
    set_column(store, table, "data", row, SQL("NULL"))
    assert_audit_raises(setup, TypeError)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param("[1]", id="json-array"),
        pytest.param('"x"', id="json-string"),
        pytest.param("3", id="json-number"),
        pytest.param("null", id="json-null"),
        pytest.param('{"n":NaN}', id="nan"),
        pytest.param('{"n":1E400}', id="overflow"),
        pytest.param('{"n":-Infinity}', id="minus-infinity"),
        pytest.param('{"n":"\\ud800"}', id="surrogate-value"),
        pytest.param('{"\\udc00":1}', id="surrogate-key"),
        pytest.param(SQL("CAST('{\"n\":1}' AS BLOB)"), id="utf8-blob"),
        pytest.param(SQL("X'7B226E223A22EDA080227D'"), id="blob-with-raw-surrogate"),
    ],
)
@pytest.mark.parametrize("table", ["businesses", "objects"])
def test_record_data_that_parses_is_reported(setup, table, data):
    store = setup[0]
    row = record_row(setup, table)
    set_column(store, table, "data", row, data)
    expected = [f"Current record differs from history: {row}"]
    assert integrity(setup)["record_errors"] == expected + (
        [f"Revision mismatch: {row}"] if table == "objects" else []
    )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(SQL("X'41'"), id="blob"),
        pytest.param(SQL("1e308 * 10"), id="real-infinity"),
        pytest.param("abc", id="text"),
        pytest.param(SQL("NULL"), id="null"),
    ],
)
@pytest.mark.parametrize(
    ("table", "column", "declared"),
    [
        ("businesses", "version", "version INTEGER NOT NULL"),
        ("businesses", "updated_at", "updated_at TEXT NOT NULL"),
        ("objects", "title", "title TEXT NOT NULL"),
        ("objects", "version", "version INTEGER NOT NULL"),
    ],
)
def test_record_columns_in_any_storage_class_are_reported(setup, table, column, declared, value):
    store = setup[0]
    row = record_row(setup, table)
    if value == "NULL":
        without_constraint(store, table, declared, column)
    set_column(store, table, column, row, value)
    expected = [f"Current record differs from history: {row}"]
    assert integrity(setup)["record_errors"] == expected + (
        [f"Revision mismatch: {row}"] if table == "objects" else []
    )


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param("[1]", TypeError, id="json-array"),
        pytest.param('"abc"', TypeError, id="json-string"),
        pytest.param("3", TypeError, id="json-number"),
        pytest.param("null", TypeError, id="json-null"),
        pytest.param('{"content_sha256":"x"}', KeyError, id="no-content"),
        pytest.param('{"content":"abc"}', KeyError, id="no-content-hash"),
        pytest.param('{"content":1,"content_sha256":"x"}', AttributeError, id="number-content"),
        pytest.param('{"content":[1],"content_sha256":"x"}', AttributeError, id="list-content"),
        pytest.param('{"content":null,"content_sha256":"x"}', AttributeError, id="null-content"),
        pytest.param(
            '{"content":"\\ud800","content_sha256":"x"}', UnicodeEncodeError, id="surrogate-content"
        ),
    ],
)
@pytest.mark.parametrize("kind", ["evidence", "document"])
def test_known_limit_malformed_evidence_or_document_data_raises(setup, kind, data, expected):
    store = setup[0]
    row = record_row(setup, "objects")
    with store.write() as con:
        con.execute("UPDATE objects SET kind=?,data=? WHERE id=?", (kind, data, row))
    assert_audit_raises(setup, expected)


@pytest.mark.parametrize("content_hash", ['"x"', "[1]", "NaN", "null"])
@pytest.mark.parametrize("kind", ["evidence", "document"])
def test_evidence_or_document_content_hash_of_any_type_is_reported(setup, kind, content_hash):
    store = setup[0]
    row = record_row(setup, "objects")
    with store.write() as con:
        con.execute(
            "UPDATE objects SET kind=?,data=? WHERE id=?",
            (kind, '{"content":"abc","content_sha256":' + content_hash + "}", row),
        )
    assert integrity(setup)["record_errors"] == [
        f"Current record differs from history: {row}",
        f"Content hash mismatch: {row}",
        f"Revision mismatch: {row}",
    ]


AI_DATA = [
    pytest.param("{", json.JSONDecodeError, id="not-json"),
    pytest.param(SQL("X'FF'"), UnicodeDecodeError, id="blob-not-utf8"),
    pytest.param('{"n":' + LONG_INTEGER + "}", ValueError, id="integer-over-digit-limit"),
    pytest.param(DEEP, RecursionError, id="nested-beyond-recursion-limit"),
    pytest.param('{"n":NaN}', ValueError, id="nan"),
    pytest.param('{"n":1E400}', ValueError, id="overflow"),
    pytest.param('{"n":"\\ud800"}', UnicodeEncodeError, id="surrogate-value"),
    pytest.param('{"\\udc00":1}', UnicodeEncodeError, id="surrogate-key"),
]


@pytest.mark.parametrize(("data", "expected"), AI_DATA)
def test_known_limit_ai_review_data_raises_when_its_state_matches_history(setup, data, expected):
    store, _, actor, bid, _ = setup
    run_id = add_ai_review(store, actor, bid)
    set_column(store, "ai_calls", "data", run_id, data)
    assert_audit_raises(setup, expected)


def test_known_limit_null_ai_review_data_raises_when_its_state_matches_history(setup):
    store, _, actor, bid, _ = setup
    run_id = add_ai_review(store, actor, bid)
    without_constraint(store, "ai_calls", "data TEXT NOT NULL", "data")
    set_column(store, "ai_calls", "data", run_id, SQL("NULL"))
    assert_audit_raises(setup, TypeError)


def test_known_limit_ai_review_with_null_state_and_no_history_reads_its_data(setup):
    store, _, actor, bid, _ = setup
    run_id = add_ai_review(store, actor, bid, chained=False)
    without_constraint(store, "ai_calls", "state TEXT NOT NULL", "state")
    with store.write() as con:
        con.execute("UPDATE ai_calls SET state=NULL,data='{' WHERE id=?", (run_id,))
    assert_audit_raises(setup, json.JSONDecodeError)


@pytest.mark.parametrize(
    "chained", [pytest.param(True, id="state-changed"), pytest.param(False, id="no-history")]
)
@pytest.mark.parametrize(("data", "expected"), AI_DATA)
def test_ai_review_data_is_not_read_when_its_state_differs_from_history(setup, data, expected, chained):
    store, _, actor, bid, _ = setup
    run_id = add_ai_review(store, actor, bid, chained)
    set_column(store, "ai_calls", "data", run_id, data)
    with store.write() as con:
        con.execute("UPDATE ai_calls SET state='succeeded' WHERE id=?", (run_id,))
    assert integrity(setup)["record_errors"] == [f"AI review differs from history: {run_id}"]


@pytest.mark.parametrize("data", ["[1]", '"x"', "3", "null", SQL("CAST('{\"n\":1}' AS BLOB)")])
def test_ai_review_data_that_parses_is_reported(setup, data):
    store, _, actor, bid, _ = setup
    run_id = add_ai_review(store, actor, bid)
    set_column(store, "ai_calls", "data", run_id, data)
    assert integrity(setup)["record_errors"] == [f"AI review differs from history: {run_id}"]


@pytest.mark.parametrize("chained", [pytest.param(True, id="chained"), pytest.param(False, id="no-history")])
def test_null_id_of_an_ai_review_row_is_reported(setup, chained):
    # SQLite allows NULL in a TEXT PRIMARY KEY without any change to the table.
    store, _, actor, bid, _ = setup
    run_id = add_ai_review(store, actor, bid, chained)
    set_column(store, "ai_calls", "id", run_id, SQL("NULL"))
    expected = ["AI review differs from history: None"]
    if chained:
        expected.append(f"Historical AI review missing: {run_id}")
    assert integrity(setup)["record_errors"] == expected
    assert export_integrity(setup)["record_errors"] == expected


UNENCODABLE_EVENTS = [
    pytest.param('{"note":NaN}', {}, ValueError, id="nan"),
    pytest.param('{"note":Infinity}', {}, ValueError, id="infinity"),
    pytest.param('{"note":-Infinity}', {}, ValueError, id="minus-infinity"),
    pytest.param('{"note":1E400}', {}, ValueError, id="overflow"),
    pytest.param('{"note":-1E400}', {}, ValueError, id="negative-overflow"),
    pytest.param('{"run_id":NaN,"data_sha256":"a"}', {}, ValueError, id="nan-run-id"),
    pytest.param('{"note":"\\ud800"}', {}, UnicodeEncodeError, id="surrogate-value"),
    pytest.param('{"\\udc00":1}', {}, UnicodeEncodeError, id="surrogate-key"),
    pytest.param(SQL("X'7B226E223A22EDA080227D'"), {}, UnicodeEncodeError, id="blob-with-raw-surrogate"),
    *[
        pytest.param('{"note":1}', {column: SQL("X'41'")}, TypeError, id=f"blob-{column}")
        for column in ("actor_id", "action", "object_id", "created_at")
    ],
]


@pytest.mark.parametrize(("payload", "columns", "expected"), UNENCODABLE_EVENTS)
def test_known_limit_unencodable_event_raises_when_the_chain_check_hashes_it(
    setup, payload, columns, expected
):
    store, _, _, bid, _ = setup
    item = history(setup, versions=1)
    append_raw(store, bid, item["id"], payload, columns=columns)
    assert_audit_raises(setup, expected)


@pytest.mark.parametrize("column", ["actor_id", "action", "object_id", "created_at"])
def test_known_limit_infinite_real_event_column_raises_when_the_chain_check_hashes_it(setup, column):
    # Only after a schema change: a TEXT column stores an infinite REAL as the text 'Inf'.
    store, _, _, bid, _ = setup
    without_constraint(store, "events", f"{column} TEXT NOT NULL", column)
    append_raw(store, bid, bid, '{"note":1}', columns={column: SQL("1e308 * 10")})
    assert_audit_raises(setup, ValueError)


@pytest.mark.parametrize(
    "after_break", [pytest.param(False, id="own-link-wrong"), pytest.param(True, id="after-broken-event")]
)
@pytest.mark.parametrize(("payload", "columns", "expected"), UNENCODABLE_EVENTS)
def test_unencodable_event_is_reported_when_the_chain_check_does_not_hash_it(
    setup, payload, columns, expected, after_break
):
    store, _, _, bid, _ = setup
    item = history(setup, versions=1)
    if after_break:
        # A well-formed event with a wrong hash; the next one links to that hash but is not hashed.
        broken = append_raw(store, bid, item["id"], '{"note":1}')
        append_raw(store, bid, item["id"], payload, columns=columns)
    else:
        broken = append_raw(store, bid, item["id"], payload, hashed=False, columns=columns)
    missing = ["Historical AI review missing: nan"] if "NaN," in payload else []
    broken_at(setup, broken, missing)


@pytest.mark.parametrize(
    ("column", "value", "declared"),
    [
        pytest.param("prev_hash", SQL("X'41'"), None, id="blob-prev-hash"),
        pytest.param("hash", SQL("X'41'"), None, id="blob-hash"),
        pytest.param("prev_hash", SQL("NULL"), "prev_hash TEXT NOT NULL", id="null-prev-hash"),
        pytest.param("hash", SQL("NULL"), "hash TEXT NOT NULL", id="null-hash"),
        pytest.param("actor_id", SQL("NULL"), "actor_id TEXT NOT NULL", id="null-actor-id"),
        pytest.param("created_at", SQL("7"), "created_at TEXT NOT NULL", id="integer-created-at"),
        pytest.param("action", SQL("2.5"), "action TEXT NOT NULL", id="real-action"),
    ],
)
def test_hashed_event_columns_in_other_storage_classes_are_reported(setup, column, value, declared):
    store, _, _, bid, _ = setup
    if declared:
        without_constraint(store, "events", declared, column)
    seq = append_raw(store, bid, bid, '{"note":1}', columns={column: value})
    broken_at(setup, seq)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param(("payload", '{"note":NaN}'), ValueError, id="unhashed-event-nan"),
        pytest.param(("payload", '{"note":"\\ud800"}'), UnicodeEncodeError, id="unhashed-event-surrogate"),
        pytest.param(("actor_id", SQL("X'41'")), TypeError, id="unhashed-event-blob-column"),
        pytest.param(("hash", SQL("X'41'")), TypeError, id="blob-hash"),
        pytest.param(("businesses", "data", '{"n":NaN}'), ValueError, id="business-data-nan"),
        pytest.param(("businesses", "version", SQL("X'41'")), TypeError, id="business-version-blob"),
        pytest.param(
            ("businesses", "version", SQL("1e308 * 10")), ValueError, id="business-version-infinite-real"
        ),
        pytest.param(("objects", "data", '{"n":"\\ud800"}'), UnicodeEncodeError, id="record-data-surrogate"),
        pytest.param(("objects", "title", SQL("X'41'")), TypeError, id="record-title-blob"),
        pytest.param(("ai_calls", "data", "{"), json.JSONDecodeError, id="unread-ai-data-not-json"),
        pytest.param(("ai_calls", "data", '{"n":1E400}'), ValueError, id="unread-ai-data-overflow"),
        pytest.param(("ai_calls", "created_at", SQL("X'41'")), TypeError, id="ai-created-at-blob"),
        pytest.param(("ai_calls", "state", SQL("X'41'")), TypeError, id="ai-state-blob"),
    ],
)
def test_known_limit_export_raises_on_values_it_cannot_write(setup, change, expected):
    store, service, actor, bid, _ = setup
    item = history(setup, versions=1)
    # The AI review has no chained event, so verification reports it and does not read its data.
    run_id = add_ai_review(store, actor, bid, chained=False)
    if len(change) == 2:
        append_raw(
            store,
            bid,
            item["id"],
            change[1] if change[0] == "payload" else '{"note":1}',
            hashed=False,
            columns={} if change[0] == "payload" else {change[0]: change[1]},
        )
    else:
        table, column, value = change
        set_column(
            store, table, column, {"businesses": bid, "objects": item["id"], "ai_calls": run_id}[table], value
        )
    assert not integrity(setup)["valid"]
    assert_audit_raises(setup, expected, export=True)


@pytest.mark.parametrize("column", ["principal_id", "command_key", "request_hash"])
def test_export_leaves_out_ai_review_columns_it_does_not_carry(setup, column):
    store, service, actor, bid, _ = setup
    run_id = add_ai_review(store, actor, bid)
    set_column(store, "ai_calls", column, run_id, SQL("X'41'"))
    export = service.audit(actor, bid, True)
    assert export["integrity"]["valid"], export["integrity"]["record_errors"]
    assert set(export["ai_runs"][0]) == {"id", "state", "created_at", "data"}


@pytest.mark.parametrize(
    ("payload", "columns", "expected"),
    [
        pytest.param('{"note":NaN}', {}, ValueError, id="nan"),
        pytest.param('{"note":1E400}', {}, ValueError, id="overflow"),
        pytest.param('{"note":"\\ud800"}', {}, UnicodeEncodeError, id="surrogate"),
        pytest.param('{"note":1}', {"actor_id": SQL("X'FF'")}, UnicodeDecodeError, id="blob-not-utf8"),
    ],
)
def test_known_limit_audit_endpoint_raises_on_events_it_cannot_send(
    setup, client, payload, columns, expected
):
    # The service returns a report for these unhashed events; sending the events as JSON fails.
    store, service, actor, bid, _ = setup
    append_raw(store, bid, bid, payload, hashed=False, columns=columns)
    assert service.audit(actor, bid)["integrity"]["valid"] is False
    with pytest.raises(Exception) as caught:
        client.get(f"/api/businesses/{bid}/audit")
    assert caught.type is expected


# Python 3.11 counts JSON parsing against the recursion limit of 1,000, so there the service cannot read 2,000 levels;
# 3.12 and later give the parser its own limit of about 10,000, so the service reports and only sending fails.
READS_2000_LEVELS = sys.version_info >= (3, 12)


@pytest.mark.parametrize(
    "service_reads",
    [
        pytest.param(
            READS_2000_LEVELS, id="py3.12+-service-reports" if READS_2000_LEVELS else "py3.11-service-raises"
        )
    ],
)
def test_known_limit_audit_endpoint_raises_on_an_event_nested_2000_levels(setup, client, service_reads):
    store, service, actor, bid, _ = setup
    append_raw(store, bid, bid, '{"n":' + "[" * 2000 + "]" * 2000 + "}", hashed=False)
    if service_reads:
        assert service.audit(actor, bid)["integrity"]["valid"] is False
    else:
        assert_audit_raises(setup, RecursionError)
    with pytest.raises(Exception) as caught:
        client.get(f"/api/businesses/{bid}/audit")
    assert caught.type is RecursionError


def test_audit_endpoint_sends_an_event_column_stored_as_a_utf8_blob(setup, client):
    store, _, _, bid, _ = setup
    seq = append_raw(store, bid, bid, '{"note":1}', hashed=False, columns={"actor_id": SQL("X'41'")})
    response = client.get(f"/api/businesses/{bid}/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["integrity"]["failed_sequence"] == seq
    assert body["events"][-1]["actor_id"] == "A"


# Jurisdiction changes. One change writes a business.updated event, a task.superseded update for each
# step of the earlier package, the new package's steps, a reopening of each remaining completed step, and
# then a business.jurisdiction_changed event. That last event names the business as its object but has no
# id, version or data, so it records no version. Steps are moved through the service, as a founder would.

JURISDICTION_REASON = "Founders confirmed where they will operate."


def change_jurisdiction(setup, to):
    _, service, actor, bid, _ = setup
    version = service.snapshot(actor, bid)["business"]["version"]
    payload = models.JurisdictionChange(
        expected_version=version, jurisdiction=to, reason=JURISDICTION_REASON
    ).model_dump(mode="json")
    return service.change_jurisdiction(actor, bid, key(), payload)


def move_step(setup, step_key, states, evidence):
    _, service, actor, bid, _ = setup
    [task] = [
        o
        for o in service.snapshot(actor, bid)["objects"]
        if o["kind"] == "task" and o["state"] != "superseded" and o["data"]["key"] == step_key
    ]
    for state in states:
        payload = models.TaskTransition(
            state=state,
            expected_version=task["version"],
            note=f"Synthetic move to {state}.",
            evidence_ids=[evidence["id"]],
        ).model_dump(mode="json")
        task = service.transition(actor, bid, task["id"], key(), payload)
    return task


def jurisdiction_history(setup):
    """undecided to GB to SE to undecided. Returns each change's result, in order."""
    evidence = reviewed(setup, source_type="document")
    move_step(setup, "jurisdiction", ["not_applicable"], evidence)
    move_step(setup, "structure", ["not_applicable"], evidence)
    changes = [change_jurisdiction(setup, "GB")]
    move_step(setup, "local_review", ["in_progress"], evidence)
    move_step(setup, "structure", ["not_applicable"], evidence)
    changes.append(change_jurisdiction(setup, "SE"))
    move_step(setup, "structure", ["not_applicable"], evidence)
    move_step(setup, "local_review", ["prepared", "approved", "verified"], evidence)
    changes.append(change_jurisdiction(setup, "undecided"))
    return changes


def test_several_jurisdiction_changes_verify_in_audit_and_export(setup):
    _, service, actor, bid, _ = setup
    changes = jurisdiction_history(setup)
    # Superseded from three different states; each change adds one step and reopens the structure step.
    assert [[s["state"] for s in c["superseded"]] for c in changes] == [
        ["not_applicable"],
        ["in_progress"],
        ["verified"],
    ]
    assert [(len(c["added"]), [s["key"] for s in c["reopened"]]) for c in changes] == [(1, ["structure"])] * 3
    assert service.snapshot(actor, bid)["business"]["version"] == 4
    assert_clean(service, actor, bid)
    assert service.audit(actor, bid, True)["integrity"] == integrity(setup)


def superseded_from_verified(setup):
    _, service, actor, bid, _ = setup
    step = jurisdiction_history(setup)[-1]["superseded"][0]
    [task] = [o for o in service.snapshot(actor, bid)["objects"] if o["id"] == step["id"]]
    assert task["state"] == "superseded" and task["version"] == 5
    return task


def live_step(setup):
    _, service, actor, bid, _ = setup
    [task] = [
        o
        for o in service.snapshot(actor, bid)["objects"]
        if o["kind"] == "task" and o["state"] != "superseded" and o["data"]["key"] == "structure"
    ]
    return task


SUPERSEDED_DAMAGE = [
    pytest.param(
        "UPDATE revisions SET snapshot=json_set(snapshot,'$.title','Rewritten') WHERE object_id=? AND version=?",
        ["Revision mismatch: {id}", "Historical revision differs from history: {id} version {version}"],
        id="current-revision-changed",
    ),
    pytest.param(
        "UPDATE objects SET title='Rewritten' WHERE id=? AND version=?",
        ["Current record differs from history: {id}", "Revision mismatch: {id}"],
        id="current-row-changed",
    ),
    pytest.param(
        "DELETE FROM revisions WHERE object_id=? AND version=?",
        ["Revision mismatch: {id}", "Historical revision missing: {id} version {version}"],
        id="revision-removed",
    ),
]


@pytest.mark.parametrize("step", [superseded_from_verified, live_step], ids=["superseded", "live"])
@pytest.mark.parametrize(("sql", "expected"), SUPERSEDED_DAMAGE)
def test_superseded_step_is_checked_like_any_other_step(setup, step, sql, expected):
    # For the superseded step the damaged version is the one its task.superseded event recorded.
    store = setup[0]
    if step is live_step:
        jurisdiction_history(setup)
    task = step(setup)
    with store.write() as con:
        assert con.execute(sql, (task["id"], task["version"])).rowcount == 1
    result = integrity(setup)
    assert not result["valid"]
    assert result["record_errors"] == [e.format(**task) for e in expected]


def test_jurisdiction_change_event_records_no_version(setup):
    store, service, actor, bid, _ = setup
    change_jurisdiction(setup, "GB")
    events = service.audit(actor, bid)["events"]
    [decision] = [e for e in events if e["action"] == "business.jurisdiction_changed"]
    assert decision["object_id"] == bid and decision["payload"]["business_version"] == 2
    assert not {"id", "version", "data"} & set(decision["payload"])
    recorded, errors = Service._recorded_versions(events)
    business = service.snapshot(actor, bid)["business"]
    assert {v: p for (o, v), p in recorded.items() if o == bid} == {1: events[0]["payload"], 2: business}
    assert errors == []
    # A correctly chained copy of the decision repeats nothing and is not an unusable version.
    with store.read() as con:
        text = con.execute("SELECT payload FROM events WHERE seq=?", (decision["seq"],)).fetchone()[0]
    append_event(store, bid, bid, text, action="business.jurisdiction_changed")
    assert_clean(service, actor, bid)


def damage_first_jurisdiction_event(setup, payload):
    store, service, actor, bid, _ = setup
    jurisdiction_history(setup)
    with store.write() as con:
        seq = con.execute(
            "SELECT min(seq) FROM events WHERE business_id=? AND action='business.jurisdiction_changed'",
            (bid,),
        ).fetchone()[0]
        con.execute("UPDATE events SET payload=? WHERE seq=?", (payload, seq))
    return seq


@pytest.mark.parametrize("payload", ['{"to":"GB"}', '{"from":"undecided"}'])
def test_changed_jurisdiction_event_payload_breaks_the_chain_in_audit_export_and_endpoint(
    setup, client, payload
):
    # Verification never reads the jurisdiction a payload names. Reading the snapshot does, and raises.
    _, service, actor, bid, _ = setup
    seq = damage_first_jurisdiction_event(setup, payload)
    broken_at(setup, seq)
    assert service.audit(actor, bid, True)["integrity"] == integrity(setup)
    response = client.get(f"/api/businesses/{bid}/audit")
    assert response.status_code == 200
    assert response.json()["integrity"] == integrity(setup)


def test_known_limit_jurisdiction_event_payload_that_is_not_json_raises(setup, client):
    _, _, _, bid, _ = setup
    damage_first_jurisdiction_event(setup, "not json")
    assert_audit_raises(setup, json.JSONDecodeError)
    assert_audit_raises(setup, json.JSONDecodeError, export=True)
    with pytest.raises(Exception) as caught:
        client.get(f"/api/businesses/{bid}/audit")
    assert caught.type is json.JSONDecodeError
