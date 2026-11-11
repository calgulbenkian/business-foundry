"""Integrity verification covers every stored revision, not only the current one."""

import json
import sqlite3
from uuid import uuid4

import pytest

from foundry import models
from foundry.demo import seed
from foundry.service import Service, read_json
from foundry.store import Store, canonical


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
