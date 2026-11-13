from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal

from . import catalog, finance
from .documents import OVERDUE_TEMPLATES
from .models import DealInputs, ForecastInputs
from .store import DomainError, Store, canonical, digest, now, uid

REVIEWERS = {"owner", "reviewer"}
WRITERS = {"owner", "operator", "reviewer", "agent"}
FINISHED = {"verified", "not_applicable"}
COMPLETED = {"approved", "submitted", "verified", "not_applicable"}
UNREADABLE = (TypeError, ValueError, RecursionError)
LIMITATION = (
    "The hash chain detects a changed recorded event, but anyone who can edit the database can rewrite "
    "the chain, remove its newest events or append events to it that pass this check. Keep protected "
    "off-machine backups, and store the head hash outside this machine to show later that the events up "
    "to it are unchanged."
)


def read_json(value, encoding="utf-8"):
    """Parse a stored JSON column for verification; None when it cannot be read.

    Bytes are decoded strictly in ``encoding`` first, so a byte-order mark, a lone
    surrogate or another encoding is unreadable rather than auto-detected: the bytes of
    a TEXT value read the same as they would through sqlite3's own str conversion.
    """
    try:
        if isinstance(value, bytes):
            value = value.decode(encoding)
        return json.loads(value)
    except UNREADABLE:
        return None


def stored_literal(storage, value, encoding="utf-8"):
    """Show a stored TEXT, BLOB or NULL value in an error message, as an SQLite literal.

    ``storage`` is the value's typeof() and ``value`` is it as fetched, TEXT as its bytes in
    ``encoding``. NULL is ``NULL`` and a BLOB is ``X'<hex>'``. TEXT that decodes strictly in
    ``encoding`` to printable ASCII is quoted, ``'two'``, quotes doubled, while the quoted form
    is at most 34 characters; any other TEXT is ``CAST(X'<hex>' AS TEXT)`` of its bytes. Hex
    shows at most the first 16 bytes, then ``...``. The result is always printable ASCII of
    at most 52 characters, holds no undecoded byte, and when not cut short selects the same
    value in SQL.
    """
    if value is None:
        return "NULL"
    if storage == "text":
        try:
            text = value.decode(encoding)
        except UNREADABLE:
            text = None
        if text is not None and all(" " <= c <= "~" for c in text):
            quoted = "'" + text.replace("'", "''") + "'"
            if len(quoted) <= 34:
                return quoted
    shown = "X'" + value[:16].hex().upper() + ("..." if len(value) > 16 else "") + "'"
    return f"CAST({shown} AS TEXT)" if storage == "text" else shown


def same_json(stored, recorded):
    """Equal by JSON type and value: false and 0, true and 1, 1 and 1.0 are different.

    Key order and whitespace are gone after parsing. A value canonical() cannot encode,
    such as NaN or Infinity, is never equal to anything.
    """
    try:
        return canonical(stored) == canonical(recorded)
    except UNREADABLE:
        return False


# Terminal state of a step that belonged to a planning guide the business no longer uses. The step and
# its history are kept; it is not open work, never blocks another step and never transitions again.
SUPERSEDED = "superseded"
# Steps that come from a jurisdiction package rather than the shared base guide, identified by catalog key
# so that steps stored before this state existed need no migration.
PACKAGE_KEYS = {spec["key"] for specs in catalog.JURISDICTIONS.values() for spec in specs}


def live(task):
    return task["state"] != SUPERSEDED


def moves_to(entry, state):
    return isinstance(entry, dict) and entry.get("to") == state


def cited(entry):
    ids = entry.get("evidence_ids") if isinstance(entry, dict) else None
    return {eid for eid in ids if isinstance(eid, str)} if isinstance(ids, list) else set()


def evidence_basis(history):
    """Evidence ids that a task's current standing relies on, derived from its history.

    The current cycle is everything after the most recent move to needs_review. Within it, the basis
    is the most recent move to prepared and every later entry; with no such move, the last entry.
    Entries written by invalidation carry no evidence_ids and contribute nothing.

    Stored history that cannot be read contributes nothing and never raises: a history that is not a
    list is empty, an entry moves to a state only if it is an object whose "to" is that string, and
    ids are read only from an object's "evidence_ids" list, keeping its string items.
    """
    if not isinstance(history, list):
        return set()
    starts = [i for i, entry in enumerate(history) if moves_to(entry, "needs_review")]
    cycle = history[starts[-1] + 1 :] if starts else history
    if not cycle:
        return set()
    prepared = [i for i, entry in enumerate(cycle) if moves_to(entry, "prepared")]
    basis = cycle[prepared[-1] :] if prepared else cycle[-1:]
    return set().union(*(cited(entry) for entry in basis))


# A saved deal analysis or forecast keeps the figures it was calculated with. When something it relied on
# has changed since, the snapshot marks it stale and lists why. Each reason's message is the one sentence
# that the snapshot, the interface and generated documents all show.
ANALYSIS_NAMES = {"deal": "deal analysis", "financial_plan": "forecast"}
ANALYSIS_REDO = {"deal": "run a revised analysis", "financial_plan": "save a revised forecast"}
ANALYSIS_EVENTS = ("deal.created", "financial_plan.created")


def evidence_changed_reason():
    return {
        "code": "evidence_changed",
        "message": "Supporting evidence has changed since this deal analysis was saved. "
        "Run a revised analysis before relying on these figures.",
    }


def jurisdiction_changed_reason(kind, saved, current):
    return {
        "code": "jurisdiction_changed",
        "saved_jurisdiction": saved,
        "current_jurisdiction": current,
        "message": f"This {ANALYSIS_NAMES[kind]} was saved while the planning jurisdiction was {saved}. "
        f"It is now {current}. Check its assumptions, the tax rate among them, and {ANALYSIS_REDO[kind]} "
        "before relying on these figures.",
    }


def is_overdue(obligation, on):
    """Whether an obligation is past its due date on the date ``on``, an ISO date string.

    One due on that date is not overdue. The snapshot's flags, its count and the stale rule for documents
    all ask this question here, so they cannot disagree.
    """
    due = obligation["data"].get("due_date")
    return bool(due) and due < on


def readable_date(value):
    """Whether a stored value reads as one calendar date and prints back as the same text.

    Python 3.11 and later also read forms such as 20261112, which must not be compared as text with due
    dates, so only the exact YYYY-MM-DD text a date prints as is accepted.
    """
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


# A generated document keeps the text it was rendered with. The snapshot marks it stale and lists why, in
# the same shape as a deal analysis or a forecast.
def source_changed_reason():
    return {
        "code": "source_changed",
        "message": "The business or its records have changed since this document was generated. "
        "Generate a new version before relying on it.",
    }


def overdue_changed_reason(as_of, today):
    return {
        "code": "overdue_changed",
        "as_of": as_of,
        "today": today,
        "message": f"This document says which obligations were past their due date as of {as_of}. "
        "That has changed since. Generate a new version before relying on it.",
    }


def as_of_unreadable_reason():
    return {
        "code": "as_of_unreadable",
        "message": "The date this document was worked out for cannot be read, so it cannot be shown to be "
        "current. Generate a new version before relying on it.",
    }


def document_stale_reasons(document, fingerprint, obligations, today):
    """Why a generated document is out of date, in order; an empty list when it is current."""
    data = document["data"]
    reasons = []
    if data["source_fingerprint"] != fingerprint:
        reasons.append(source_changed_reason())
    # A document generated before the date was stored has no as_of key; only the fingerprint applies.
    if data.get("template") in OVERDUE_TEMPLATES and "as_of" in data:
        as_of = data["as_of"]
        if not readable_date(as_of):
            reasons.append(as_of_unreadable_reason())
        elif any(is_overdue(o, as_of) != is_overdue(o, today) for o in obligations):
            reasons.append(overdue_changed_reason(as_of, today))
    return reasons


def evaluate(model, *args):
    """Run a financial model; an arithmetic failure is a refusal of these inputs, not a server fault."""
    try:
        return model(*args)
    except ArithmeticError as exc:
        # Raised inside the command transaction, so nothing is stored and the idempotency key stays unused.
        raise DomainError(
            "The financial model cannot evaluate these inputs "
            f"({type(exc).__name__}). Review unusually large or precise values and try again."
        ) from None


class Service:
    def __init__(self, store: Store):
        self.store = store

    def access(self, actor, business_id=None, write=False, review=False):
        if business_id and actor["role"] != "owner" and business_id not in actor["business_ids"]:
            raise DomainError("This access key does not cover that business.", 403)
        if write and actor["role"] not in WRITERS:
            raise DomainError("Read-only access.", 403)
        if review and actor["role"] not in REVIEWERS:
            raise DomainError("A human owner or reviewer must make this decision.", 403)

    def command(self, actor, business_id, key, operation, payload, fn, review=False):
        self.access(actor, business_id, write=True, review=review)

        def run(con):
            self.store.business(con, business_id)
            return fn(con)

        return self.store.command(actor, key, operation, {"business_id": business_id, **payload}, run)

    def list_businesses(self, actor):
        with self.store.read() as con:
            return [
                self.store.unpack(r)
                for r in con.execute("SELECT * FROM businesses ORDER BY created_at")
                if actor["role"] == "owner" or r["id"] in actor["business_ids"]
            ]

    def create_business(self, actor, key, payload):
        if actor["role"] != "owner":
            raise DomainError("Only an owner can create businesses.", 403)

        def run(con):
            stamp, bid = now(), uid("business")
            item = {"id": bid, "data": payload, "version": 1, "created_at": stamp, "updated_at": stamp}
            con.execute(
                "INSERT INTO businesses VALUES(?,?,?,?,?)", (bid, canonical(payload), 1, stamp, stamp)
            )
            self.store.event(con, bid, actor, "business.created", bid, item)
            self._tasks(con, bid, catalog.catalog(payload["jurisdiction"]), actor)
            return item

        return self.store.command(actor, key, "business.create", payload, run)

    def _tasks(self, con, bid, specs, actor, deal_id=None):
        ids = {x["key"]: uid("task") for x in specs}
        result = []
        for spec in specs:
            data = {
                **spec,
                "depends_on": [ids[k] for k in spec["depends_on_keys"] if k in ids],
                "deal_id": deal_id,
            }
            result.append(
                self.store.add(con, bid, "task", spec["title"], data, actor, "pending", ids[spec["key"]])
            )
        return result

    def update_business(self, actor, bid, key, payload):
        def run(con):
            old = self.store.business(con, bid)
            if old["version"] != payload["expected_version"]:
                raise DomainError("The business changed. Refresh before saving.", 409)
            data = {k: v for k, v in payload.items() if k != "expected_version"}
            if data["currency"] != old["data"]["currency"] and any(
                o["kind"] in {"invoice", "payment", "financial_plan", "deal"}
                for o in self.store.objects(con, bid)
            ):
                raise DomainError(
                    "Currency cannot change after financial records exist. Create a separate business."
                )
            # Jurisdiction changes alter which planning guide applies; they are a separate human decision
            # (change_jurisdiction) that records a reason and supersedes the old guide's steps.
            if data["jurisdiction"] != old["data"]["jurisdiction"]:
                raise DomainError(
                    "A general edit cannot change the planning jurisdiction. An owner or reviewer changes it "
                    "with the jurisdiction action (POST /api/businesses/{id}/jurisdiction or "
                    "'foundry jurisdiction'), which records a reason and reopens affected work."
                )
            item = {**old, "data": data, "version": old["version"] + 1, "updated_at": now()}
            con.execute(
                "UPDATE businesses SET data=?,version=?,updated_at=? WHERE id=?",
                (canonical(data), item["version"], item["updated_at"], bid),
            )
            self.store.event(con, bid, actor, "business.updated", bid, item)
            self.invalidate(con, bid, actor, all_tasks=True, reason="Business assumptions changed")
            return item

        return self.command(actor, bid, key, "business.update", payload, run)

    def jurisdiction_plan(self, con, bid, to):
        """Everything a jurisdiction change does, worked out once. The preview and the change both use it.

        The current package's live steps are superseded. The new package's steps are added as pending
        steps whose prerequisites resolve by key against the live steps that remain (for example a new
        local review depends on the existing structure decision). Every remaining completed step, deal
        steps included, is reopened. Base and deal steps keep their identity and are never superseded.
        """
        business = self.store.business(con, bid)
        current = business["data"]["jurisdiction"]
        if to not in catalog.JURISDICTIONS:
            raise DomainError(
                "Choose a known planning jurisdiction: " + ", ".join(catalog.JURISDICTIONS) + "."
            )
        if to == current:
            raise DomainError(f"The planning jurisdiction is already {current}.")
        tasks = [t for t in self.store.objects(con, bid, "task") if live(t)]
        superseded = [
            t for t in tasks if not t["data"].get("deal_id") and t["data"].get("key") in PACKAGE_KEYS
        ]
        gone = {t["id"] for t in superseded}
        remaining = [t for t in tasks if t["id"] not in gone]
        keys = {t["data"]["key"]: t["id"] for t in remaining if not t["data"].get("deal_id")}
        added = [
            {
                **spec,
                "depends_on": [keys[k] for k in spec["depends_on_keys"] if k in keys],
                "deal_id": None,
            }
            for spec in catalog.JURISDICTIONS[to]
        ]
        return {
            "business": business,
            "from": current,
            "to": to,
            "superseded": superseded,
            "added": added,
            "reopened": [t for t in remaining if t["state"] in COMPLETED],
        }

    @staticmethod
    def jurisdiction_summary(plan):
        def step(t):
            return {"id": t["id"], "key": t["data"].get("key"), "title": t["title"], "state": t["state"]}

        return {
            "from": plan["from"],
            "to": plan["to"],
            "superseded": [step(t) for t in plan["superseded"]],
            "added": [
                {"key": s["key"], "title": s["title"], "depends_on": s["depends_on"]} for s in plan["added"]
            ],
            "reopened": [step(t) for t in plan["reopened"]],
        }

    def preview_jurisdiction(self, actor, bid, to):
        """What change_jurisdiction would do now, without changing anything. Any reader may ask."""
        self.access(actor, bid)
        with self.store.read() as con:
            plan = self.jurisdiction_plan(con, bid, to)
        return {"business_version": plan["business"]["version"], **self.jurisdiction_summary(plan)}

    def change_jurisdiction(self, actor, bid, key, payload):
        """A human decision to plan under another jurisdiction. It changes which planning guide applies and
        reopens completed work; it is not legal advice and does not form, move or register an entity."""

        def run(con):
            old = self.store.business(con, bid)
            if old["version"] != payload["expected_version"]:
                raise DomainError("The business changed. Refresh before saving.", 409)
            reason = payload["reason"].strip() if isinstance(payload.get("reason"), str) else ""
            if not reason:
                raise DomainError("Record the reason for this jurisdiction decision.")
            plan = self.jurisdiction_plan(con, bid, payload["jurisdiction"])
            self.references(con, bid, payload["evidence_ids"], "evidence", verified=True)
            summary = self.jurisdiction_summary(plan)
            change = f"Jurisdiction changed from {plan['from']} to {plan['to']}"
            data = {**old["data"], "jurisdiction": plan["to"]}
            item = {**old, "data": data, "version": old["version"] + 1, "updated_at": now()}
            con.execute(
                "UPDATE businesses SET data=?,version=?,updated_at=? WHERE id=?",
                (canonical(data), item["version"], item["updated_at"], bid),
            )
            self.store.event(con, bid, actor, "business.updated", bid, item)
            for t in plan["superseded"]:
                entry = {
                    "from": t["state"],
                    "to": SUPERSEDED,
                    "note": change
                    + ". This step belongs to the earlier planning guide and is kept as history.",
                    "jurisdiction_from": plan["from"],
                    "jurisdiction_to": plan["to"],
                    "actor_id": actor["id"],
                    "at": now(),
                }
                self.store.update(
                    con,
                    t,
                    {**t["data"], "history": t["data"]["history"] + [entry]},
                    SUPERSEDED,
                    actor,
                    t["version"],
                    action="task.superseded",
                )
            added = [
                {"id": self.store.add(con, bid, "task", spec["title"], spec, actor, "pending")["id"], **shown}
                for spec, shown in zip(plan["added"], summary["added"], strict=True)
            ]
            for t in plan["reopened"]:
                self._reopen(con, actor, t, change)
            result = {**summary, "added": added}
            self.store.event(
                con,
                bid,
                actor,
                "business.jurisdiction_changed",
                bid,
                {
                    "title": change,
                    "reason": reason,
                    "evidence_ids": payload["evidence_ids"],
                    "actor_id": actor["id"],
                    "actor_name": actor["name"],
                    "business_version": item["version"],
                    **result,
                },
            )
            return {"business": item, "business_version": item["version"], **result}

        return self.command(actor, bid, key, "business.jurisdiction", payload, run, review=True)

    def references(self, con, bid, ids, kind=None, verified=False, external=False):
        result = []
        for object_id in set(ids):
            obj = self.store.get(con, bid, object_id, kind)
            if verified and obj["state"] != "verified":
                raise DomainError(f"Review evidence first: {obj['title']}")
            if external and obj["data"].get("source_type") != "external_receipt":
                raise DomainError("This step requires an external receipt, reviewed by a human.")
            result.append(obj)
        return result

    def add_record(self, actor, bid, key, payload, object_id=None):
        review = payload["certainty"] == "verified"

        def run(con):
            self.references(con, bid, payload["related_ids"])
            self.references(con, bid, payload["evidence_ids"], "evidence", verified=review)
            if review and not payload["evidence_ids"]:
                raise DomainError("Verified records require reviewed supporting evidence.")
            data = {k: v for k, v in payload.items() if k not in {"expected_version", "title", "kind"}}
            if review:
                data["verified_by"] = actor["id"]
            if object_id:
                old = self.store.get(con, bid, object_id, payload["kind"])
                if old["state"] == "verified" and actor["role"] not in REVIEWERS:
                    raise DomainError("A human owner or reviewer must change a verified record.", 403)
                item = self.store.update(
                    con, old, data, data["certainty"], actor, payload["expected_version"], payload["title"]
                )
                self.invalidate(con, bid, actor, kinds={payload["kind"]}, reason=f"Changed {item['title']}")
            else:
                item = self.store.add(
                    con, bid, payload["kind"], payload["title"], data, actor, data["certainty"]
                )
                self.invalidate(con, bid, actor, kinds={payload["kind"]}, reason=f"Added {item['title']}")
            return item

        return self.command(actor, bid, key, "record.save", {**payload, "id": object_id}, run, review)

    def add_evidence(self, actor, bid, key, payload):
        def run(con):
            data = {**payload, "content_sha256": digest(payload["content"]), "review": None}
            return self.store.add(con, bid, "evidence", payload["title"], data, actor, "unverified")

        return self.command(actor, bid, key, "evidence.create", payload, run)

    def review_evidence(self, actor, bid, eid, key, payload, revoke=False):
        def run(con):
            old = self.store.get(con, bid, eid, "evidence")
            data = {
                **old["data"],
                "review": {
                    "actor_id": actor["id"],
                    "actor_name": actor["name"],
                    "note": payload["note"],
                    "at": now(),
                },
            }
            item = self.store.update(
                con, old, data, "rejected" if revoke else "verified", actor, payload["expected_version"]
            )
            if revoke:
                for obj in self.store.objects(con, bid):
                    if (
                        eid in obj["data"].get("evidence_ids", [])
                        and obj["data"].get("certainty") == "verified"
                    ):
                        self.store.update(
                            con,
                            obj,
                            {**obj["data"], "certainty": "reported"},
                            "reported",
                            actor,
                            obj["version"],
                        )
                self.invalidate(
                    con, bid, actor, evidence_id=eid, reason=f"Evidence withdrawn: {item['title']}"
                )
            return item

        return self.command(
            actor,
            bid,
            key,
            "evidence.revoke" if revoke else "evidence.review",
            {**payload, "id": eid},
            run,
            True,
        )

    def invalidate(
        self,
        con,
        bid,
        actor,
        kinds=None,
        evidence_id=None,
        all_tasks=False,
        reason="Inputs changed",
        task_ids=None,
    ):
        # A superseded step is history: it is never reopened and does not pass a reopening on.
        tasks = [t for t in self.store.objects(con, bid, "task") if live(t)]
        affected = set(task_ids or ())
        related_kinds = set(kinds or ())
        if evidence_id:
            for obj in self.store.objects(con, bid):
                data = obj["data"]
                if obj["kind"] == "task" and not live(obj):
                    continue
                if obj["kind"] == "task" and obj["state"] in COMPLETED:
                    # A completed task relies on its currently attached evidence and on everything
                    # cited since its latest preparation. The current ids always count, so a task
                    # whose stored history is missing or incomplete reopens at least as before, and
                    # history that cannot be read contributes nothing rather than stopping withdrawal.
                    refs = list(data.get("evidence_ids", [])) + list(evidence_basis(data.get("history", [])))
                else:
                    refs = data.get("evidence_ids", [])
                refs = refs + [
                    data.get(k)
                    for k in (
                        "contract_evidence_id",
                        "acceptance_evidence_id",
                        "evidence_id",
                        "issue_evidence_id",
                    )
                ]
                if evidence_id in refs:
                    related_kinds.add(obj["kind"])
                    if obj["kind"] == "task":
                        affected.add(obj["id"])
        for t in tasks:
            if all_tasks or related_kinds.intersection(t["data"]["required_record_kinds"]):
                affected.add(t["id"])
        changed = True
        while changed:
            changed = False
            for t in tasks:
                if t["id"] not in affected and set(t["data"]["depends_on"]) & affected:
                    affected.add(t["id"])
                    changed = True
        for t in tasks:
            if t["id"] in affected and t["state"] in COMPLETED:
                self._reopen(con, actor, t, reason)

    def _reopen(self, con, actor, task, reason):
        history = task["data"]["history"] + [
            {
                "from": task["state"],
                "to": "needs_review",
                "note": reason,
                "actor_id": actor["id"],
                "at": now(),
            }
        ]
        return self.store.update(
            con, task, {**task["data"], "history": history}, "needs_review", actor, task["version"]
        )

    def blockers(self, con, bid, item):
        if not live(item):
            return []
        messages = []
        if item["data"].get("deal_id"):
            deal = self.store.get(con, bid, item["data"]["deal_id"], "deal")
            if any(
                self.store.get(con, bid, eid, "evidence")["version"] != version
                for eid, version in deal["data"].get("evidence_versions", {}).items()
            ):
                messages.append("Supporting deal evidence changed; create and review a fresh analysis")
        for dep_id in item["data"]["depends_on"]:
            dep = self.store.get(con, bid, dep_id, "task")
            # A superseded prerequisite belongs to a planning guide that no longer applies. It can never
            # finish, so it never blocks.
            if live(dep) and dep["state"] not in FINISHED:
                messages.append(f"Complete: {dep['title']}")
        for kind in item["data"]["required_record_kinds"]:
            records = self.store.objects(con, bid, kind)
            if kind == "deal" and item["data"].get("deal_id"):
                records = [r for r in records if r["id"] == item["data"]["deal_id"]]
            if kind == "invoice":
                records = [r for r in records if r["state"] in {"issued", "partially_paid", "paid"}]
            if not records:
                messages.append(
                    f"Add {'an issued invoice' if kind == 'invoice' else kind.replace('_', ' ') + ' record'}"
                )
        return messages

    def transition(self, actor, bid, tid, key, payload):
        state = payload["state"]
        review = state in {"approved", "verified", "not_applicable"}
        if state == "submitted" and actor["role"] == "agent":
            raise DomainError("An agent cannot attest that an external action occurred.", 403)

        def run(con):
            old = self.store.get(con, bid, tid, "task")
            if not live(old):
                raise DomainError(
                    "This step belongs to a planning guide the business no longer uses. "
                    "It is kept as history and cannot change."
                )
            if old["version"] != payload["expected_version"]:
                raise DomainError("Task changed. Refresh before saving.", 409)
            allowed = {
                "pending": {"in_progress", "prepared", "not_applicable"},
                "in_progress": {"prepared", "not_applicable", "needs_review"},
                "prepared": {"approved", "in_progress", "needs_review", "not_applicable"},
                "approved": {"submitted", "verified", "needs_review"},
                "submitted": {"verified", "needs_review"},
                "verified": {"needs_review"},
                "needs_review": {"in_progress", "prepared", "not_applicable"},
                "not_applicable": {"needs_review"},
            }
            if state not in allowed.get(old["state"], set()):
                raise DomainError(f"Cannot move from {old['state']} to {state}.")
            evidence = self.references(
                con,
                bid,
                payload["evidence_ids"],
                "evidence",
                verified=state in {"approved", "submitted", "verified", "not_applicable"},
            )
            if state in {"approved", "submitted", "verified", "not_applicable"} and not evidence:
                raise DomainError("Attach reviewed evidence for this decision.")
            if state in {"approved", "submitted", "verified"}:
                blockers = self.blockers(con, bid, old)
                if blockers:
                    raise DomainError("; ".join(blockers))
            if old["data"]["external"] and state == "verified" and old["state"] != "submitted":
                raise DomainError("Record the external submission before verifying its result.")
            if not old["data"]["external"] and state == "submitted":
                raise DomainError("This is an internal review; verify it after approval.")
            if (
                old["data"]["external"]
                and state in {"submitted", "verified"}
                and not any(e["data"]["source_type"] == "external_receipt" for e in evidence)
            ):
                raise DomainError(
                    "Attach a reviewed external receipt. A draft is not a completed external action."
                )
            data = {
                **old["data"],
                "evidence_ids": payload["evidence_ids"],
                "applicability": "not_applicable" if state == "not_applicable" else "applicable",
                "history": old["data"]["history"]
                + [
                    {
                        "from": old["state"],
                        "to": state,
                        "note": payload["note"],
                        "actor_id": actor["id"],
                        "at": now(),
                        "evidence_ids": payload["evidence_ids"],
                    }
                ],
            }
            result = self.store.update(con, old, data, state, actor, payload["expected_version"])
            if state == "needs_review":
                self.invalidate(con, bid, actor, task_ids={tid}, reason=f"Reopened: {old['title']}")
            return result

        return self.command(actor, bid, key, "task.transition", {**payload, "id": tid}, run, review)

    def save_forecast(self, actor, bid, key, payload):
        def run(con):
            self.references(con, bid, payload["evidence_ids"], "evidence")
            result = evaluate(finance.forecast, ForecastInputs.model_validate(payload["inputs"]))
            item = self.store.add(
                con,
                bid,
                "financial_plan",
                payload["title"],
                {**result, "evidence_ids": payload["evidence_ids"]},
                actor,
                "modeled",
            )
            self.invalidate(
                con, bid, actor, kinds={"financial_plan"}, reason="A new financial scenario requires review"
            )
            return item

        return self.command(actor, bid, key, "finance.forecast", payload, run)

    def save_deal(self, actor, bid, key, payload):
        def run(con):
            ids = payload["evidence_ids"] + [
                a["evidence_id"] for a in payload["adjustments"] if a.get("evidence_id")
            ]
            evidence = self.references(con, bid, ids, "evidence")
            reviewed = {e["id"] for e in evidence if e["state"] == "verified"}
            result = evaluate(finance.acquisition, DealInputs.model_validate(payload), reviewed)
            data = {
                "inputs": payload,
                "analysis": result,
                "evidence_ids": list(set(ids)),
                "evidence_versions": {e["id"]: e["version"] for e in evidence},
            }
            item = self.store.add(con, bid, "deal", payload["target"], data, actor, "screening")
            self._tasks(con, bid, catalog.DEAL_TASKS, actor, item["id"])
            return item

        return self.command(actor, bid, key, "deal.create", payload, run)

    def create_invoice(self, actor, bid, key, payload):
        def run(con):
            self.store.get(con, bid, payload["customer_id"], "customer")
            self.references(
                con, bid, [payload["contract_evidence_id"], payload["acceptance_evidence_id"]], "evidence"
            )
            data = {
                **payload,
                "currency": self.store.business(con, bid)["data"]["currency"],
                "paid": "0.00",
                "balance": payload["amount"],
            }
            return self.store.add(con, bid, "invoice", payload["description"], data, actor)

        return self.command(actor, bid, key, "invoice.create", payload, run)

    def issue_invoice(self, actor, bid, iid, key, payload):
        def run(con):
            old = self.store.get(con, bid, iid, "invoice")
            if old["state"] != "draft":
                raise DomainError("This invoice has already been recorded as issued.", 409)
            self.references(
                con,
                bid,
                [
                    old["data"]["contract_evidence_id"],
                    old["data"]["acceptance_evidence_id"],
                    payload["evidence_id"],
                ],
                "evidence",
                verified=True,
                external=True,
            )
            return self.store.update(
                con,
                old,
                {
                    **old["data"],
                    "issue_evidence_id": payload["evidence_id"],
                    "issue_note": payload["note"],
                    "issued_at": now(),
                },
                "issued",
                actor,
                payload["expected_version"],
            )

        return self.command(actor, bid, key, "invoice.issue", {**payload, "id": iid}, run, True)

    def payment(self, actor, bid, iid, key, payload):
        def run(con):
            invoice = self.store.get(con, bid, iid, "invoice")
            if invoice["state"] not in {"issued", "partially_paid"}:
                raise DomainError("Record issue of an unpaid invoice first.")
            self.references(con, bid, [payload["evidence_id"]], "evidence", verified=True, external=True)
            amount, balance = Decimal(payload["amount"]), Decimal(invoice["data"]["balance"])
            if amount > balance:
                raise DomainError(
                    "Payment exceeds the outstanding balance. Split the receipt before recording it."
                )
            payment_id = uid("payment")
            try:
                con.execute(
                    "INSERT INTO payment_references VALUES(?,?,?)", (bid, payload["reference"], payment_id)
                )
            except sqlite3.IntegrityError:
                raise DomainError(
                    "This payment reference is already recorded for this business.", 409
                ) from None
            item = self.store.add(
                con,
                bid,
                "payment",
                payload["reference"],
                {**payload, "invoice_id": iid, "currency": invoice["data"]["currency"]},
                actor,
                "reconciled",
                payment_id,
            )
            new_balance = balance - amount
            self.store.update(
                con,
                invoice,
                {
                    **invoice["data"],
                    "balance": finance.money(new_balance),
                    "paid": finance.money(Decimal(invoice["data"]["paid"]) + amount),
                },
                "paid" if new_balance == 0 else "partially_paid",
                actor,
                invoice["version"],
            )
            return item

        return self.command(actor, bid, key, "invoice.payment", {**payload, "id": iid}, run, True)

    def snapshot(self, actor, bid):
        self.access(actor, bid)
        with self.store.read() as con:
            return self._snapshot(con, bid)

    def saved_jurisdictions(self, con, bid):
        """The planning jurisdiction in force when each deal analysis and forecast was calculated, by id.

        Read from the event sequence, never from clock time. A deal or forecast is calculated once, when it
        is created, and nothing changes it afterwards. A record created before the first jurisdiction
        change was saved under that change's "from"; a later one under the "to" of the latest change
        before it. With no jurisdiction change in the history the result is empty: nothing to compare.
        Nothing is stored, so records written before this existed are covered without a migration.
        """
        rows = con.execute(
            "SELECT action, object_id, payload FROM events WHERE business_id=? AND action IN (?,?,?) "
            "ORDER BY seq",
            (bid, "business.jurisdiction_changed", *ANALYSIS_EVENTS),
        )
        in_force, before_first, saved = None, [], {}
        for row in rows:
            if row["action"] != "business.jurisdiction_changed":
                if in_force is None:
                    before_first.append(row["object_id"])
                else:
                    saved[row["object_id"]] = in_force
                continue
            change = json.loads(row["payload"])
            if in_force is None:
                saved.update(dict.fromkeys(before_first, change["from"]))
            in_force = change["to"]
        return saved

    def _snapshot(self, con, bid):
        business = self.store.business(con, bid)
        objects = self.store.objects(con, bid)
        by_id = {o["id"]: o for o in objects}
        # The one clock read: every overdue flag, the count, as_of and the document marks use this date.
        today = date.today().isoformat()
        fingerprint = digest(
            canonical({"business": business, "objects": [o for o in objects if o["kind"] != "document"]})
        )
        # Marks below are worked out on every read and added after the fingerprint; nothing is stored.
        saved_under = self.saved_jurisdictions(con, bid)
        jurisdiction = business["data"]["jurisdiction"]
        obligations = [o for o in objects if o["kind"] == "obligation"]
        for obj in objects:
            if obj["kind"] == "task":
                obj["blockers"] = self.blockers(con, bid, obj)
            if obj["kind"] == "document":
                reasons = document_stale_reasons(obj, fingerprint, obligations, today)
                obj["stale"] = bool(reasons)
                obj["stale_reasons"] = reasons
            if obj["kind"] in ANALYSIS_NAMES:
                reasons = []
                if any(by_id[e]["version"] != v for e, v in obj["data"].get("evidence_versions", {}).items()):
                    reasons.append(evidence_changed_reason())
                saved = saved_under.get(obj["id"])
                if saved is not None and saved != jurisdiction:
                    reasons.append(jurisdiction_changed_reason(obj["kind"], saved, jurisdiction))
                obj["stale"] = bool(reasons)
                obj["stale_reasons"] = reasons
            if obj["kind"] in {"invoice", "payment"}:
                refs = [
                    obj["data"].get(k)
                    for k in (
                        "contract_evidence_id",
                        "acceptance_evidence_id",
                        "evidence_id",
                        "issue_evidence_id",
                    )
                ]
                obj["evidence_warning"] = any(by_id[e]["state"] != "verified" for e in refs if e)
            if obj["kind"] == "obligation":
                obj["overdue"] = is_overdue(obj, today)
        # Readiness counts cover live steps only; superseded steps stay in objects as history.
        tasks = [o for o in objects if o["kind"] == "task" and live(o)]
        stats = {
            "verified": sum(t["state"] == "verified" for t in tasks),
            "not_applicable": sum(t["state"] == "not_applicable" for t in tasks),
            "tasks": len(tasks),
            "needs_review": sum(t["state"] == "needs_review" for t in tasks),
            "superseded": sum(o["kind"] == "task" and not live(o) for o in objects),
            "overdue_obligations": sum(o["kind"] == "obligation" and o["overdue"] for o in objects),
            "outstanding_invoices": finance.money(
                sum(
                    (
                        Decimal(o["data"]["balance"])
                        for o in objects
                        if o["kind"] == "invoice" and o["state"] != "draft"
                    ),
                    Decimal(0),
                )
            ),
            "cash_collected": finance.money(
                sum((Decimal(o["data"]["amount"]) for o in objects if o["kind"] == "payment"), Decimal(0))
            ),
        }
        return {
            "business": business,
            "objects": objects,
            "stats": stats,
            "as_of": today,
            "source_fingerprint": fingerprint,
            "catalog": catalog.description(),
        }

    def generate_document(self, actor, bid, key, payload):
        from .documents import render_document

        def run(con):
            snapshot = self._snapshot(con, bid)
            deal = self.store.get(con, bid, payload["deal_id"], "deal") if payload.get("deal_id") else None
            if payload["template"] == "investment_memo" and not deal:
                raise DomainError("Select an acquisition analysis for an investment memo.")
            title, content = render_document(payload["template"], snapshot, deal)
            data = {
                "template": payload["template"],
                "content": content,
                "content_sha256": digest(content),
                "source_fingerprint": snapshot["source_fingerprint"],
                "source_ids": [o["id"] for o in snapshot["objects"] if o["kind"] != "document"],
                "business_version": snapshot["business"]["version"],
                "generated_at": now(),
                # The date the text's overdue standing was worked out for, from the same snapshot.
                "as_of": snapshot["as_of"],
            }
            return self.store.add(con, bid, "document", title, data, actor, "draft")

        return self.command(actor, bid, key, "document.create", payload, run)

    def audit(self, actor, bid, export=False):
        self.access(actor, bid)
        with self.store.read() as con:
            business = self.store.business(con, bid)
            events = [
                dict(r) for r in con.execute("SELECT * FROM events WHERE business_id=? ORDER BY seq", (bid,))
            ]
            for event in events:
                event["payload"] = json.loads(event["payload"])
            integrity = self.store.verify_chain(con, bid)
            # Revision snapshots are selected as CAST(snapshot AS BLOB): sqlite3 raises while
            # fetching TEXT that is not valid UTF-8, before Python sees it. The cast returns
            # TEXT as its bytes in the database encoding, BLOB unchanged, a number as its text
            # and NULL as NULL; read_json decodes those bytes in this encoding, or reports them.
            encoding = con.execute("PRAGMA encoding").fetchone()[0]
            errors, latest, ai_latest = [], {}, {}
            for event in events:
                p = event["payload"]
                if p.get("id") == event["object_id"] and "version" in p and "data" in p:
                    latest[p["id"]] = p
                if p.get("run_id") and p.get("data_sha256"):
                    ai_latest[p["run_id"]] = p
            versions, history_errors = self._recorded_versions(events)
            objects = self.store.objects(con, bid)
            for obj in [business] + objects:
                if not same_json(obj, latest.pop(obj["id"], None)):
                    errors.append(f"Current record differs from history: {obj['id']}")
                if (
                    obj.get("kind") in {"evidence", "document"}
                    and digest(obj["data"]["content"]) != obj["data"]["content_sha256"]
                ):
                    errors.append(f"Content hash mismatch: {obj['id']}")
                if obj.get("kind"):
                    revision = con.execute(
                        "SELECT CAST(snapshot AS BLOB) AS snapshot FROM revisions WHERE object_id=? AND version=?",
                        (obj["id"], obj["version"]),
                    ).fetchone()
                    if not revision or not same_json(read_json(revision["snapshot"], encoding), obj):
                        errors.append(f"Revision mismatch: {obj['id']}")
            errors.extend(f"Historical record missing: {oid}" for oid in latest)
            errors.extend(history_errors)
            errors.extend(self._revision_errors(con, bid, versions, encoding))
            for run in con.execute("SELECT * FROM ai_calls WHERE business_id=?", (bid,)):
                recorded = ai_latest.pop(run["id"], {})
                if recorded.get("state") != run["state"] or recorded.get("data_sha256") != digest(
                    canonical(json.loads(run["data"]))
                ):
                    errors.append(f"AI review differs from history: {run['id']}")
            errors.extend(f"Historical AI review missing: {rid}" for rid in ai_latest)
            integrity = {
                **integrity,
                "valid": integrity["valid"] and not errors,
                "record_errors": errors,
                "limitation": LIMITATION,
            }
            if export:
                archive = {
                    "format": "business-foundry-export",
                    "version": 1,
                    "exported_at": now(),
                    "business": business,
                    "objects": objects,
                    "events": events,
                    "ai_runs": [
                        {
                            "id": r["id"],
                            "state": r["state"],
                            "created_at": r["created_at"],
                            "data": json.loads(r["data"]),
                        }
                        for r in con.execute(
                            "SELECT * FROM ai_calls WHERE business_id=? ORDER BY created_at", (bid,)
                        )
                    ],
                    "integrity": integrity,
                }
                return {**archive, "sha256": digest(canonical(archive))}
            return {"integrity": integrity, "events": events}

    @staticmethod
    def _recorded_versions(events):
        """Map (id, version) to the chained event that records that version, and report history faults.

        An event records a version when its payload's id equals the event's object_id, the payload
        has data, and its version is a JSON integer (not a boolean, a float such as 2.0, text, null,
        a list or an object). Walking events in chain order, the first event that records a given
        id and version stands for it; a later one cannot replace it and is reported once per id and
        version. An event that would record a version but whose version is not a JSON integer is
        reported once per id and never stands for any version. Messages follow chain order of the
        event that first causes each one.
        """
        recorded, repeated, unusable, errors = {}, set(), set(), []
        for event in events:
            p = event["payload"]
            if not (p.get("id") == event["object_id"] and "version" in p and "data" in p):
                continue
            oid, version = p["id"], p["version"]
            if type(version) is not int:
                if oid not in unusable:
                    unusable.add(oid)
                    errors.append(f"History records an unusable version: {oid}")
            elif (oid, version) not in recorded:
                recorded[(oid, version)] = p
            elif (oid, version) not in repeated:
                repeated.add((oid, version))
                errors.append(f"Version recorded more than once in history: {oid} version {version}")
        return recorded, errors

    @staticmethod
    def _revision_errors(con, bid, recorded, encoding):
        """Compare every stored revision with the chained event that stands for that version.

        ``recorded`` comes from _recorded_versions: integer versions only, the first event for each.

        Store.add/update write one revision row and one event per object version. The
        business record itself is versioned through events only and never has revisions.
        Revisions carry no business id, so rows are attributed to this business through
        its current objects and the object ids named by its events, in one query.

        A version stored as INTEGER or REAL is used as fetched. A TEXT version is selected as
        its bytes, since sqlite3 raises while fetching TEXT that is not valid UTF-8; a TEXT,
        BLOB or NULL version matches no chained event and is shown by stored_literal(). The
        object_id column is fetched as is: a row is only selected when its object_id equals
        this business's id or an id in its objects or events, so it holds the same text as
        that id, or a number whose text it is, and fails to decode only if that id does.
        """
        errors = []
        rows = con.execute(
            "SELECT object_id,typeof(version) AS version_type,"
            "CASE typeof(version) WHEN 'text' THEN CAST(version AS BLOB) ELSE version END AS stored_version,"
            "CAST(snapshot AS BLOB) AS snapshot FROM revisions WHERE object_id=? "
            "OR object_id IN (SELECT id FROM objects WHERE business_id=?) "
            "OR object_id IN (SELECT object_id FROM events WHERE business_id=?) "
            "ORDER BY object_id,version",
            (bid, bid, bid),
        ).fetchall()
        stored = {}
        for row in rows:
            oid, version = row["object_id"], row["stored_version"]
            numeric = row["version_type"] in {"integer", "real"}
            if numeric:
                stored[(oid, version)] = True
            else:
                version = stored_literal(row["version_type"], version, encoding)
            if oid == bid:
                errors.append(f"Unexpected revision for business record: {oid} version {version}")
                continue
            event = recorded.get((oid, version)) if numeric else None
            if event is None:
                errors.append(f"Revision has no recorded history: {oid} version {version}")
                continue
            if not same_json(read_json(row["snapshot"], encoding), event):
                errors.append(f"Historical revision differs from history: {oid} version {version}")
        errors.extend(
            f"Historical revision missing: {oid} version {version}"
            for oid, version in recorded
            if oid != bid and (oid, version) not in stored
        )
        return errors

    def principals(self, actor):
        if actor["role"] != "owner":
            raise DomainError("Owner access required.", 403)
        with self.store.read() as con:
            return [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "role": r["role"],
                    "business_ids": json.loads(r["scopes"]),
                    "revoked": bool(r["revoked"]),
                }
                for r in con.execute("SELECT * FROM principals")
            ]

    def add_principal(self, actor, payload):
        if actor["role"] != "owner":
            raise DomainError("Owner access required.", 403)
        if payload["role"] != "owner" and not payload["business_ids"]:
            raise DomainError("Choose at least one business for a scoped access key.")
        with self.store.write() as con:
            for bid in payload["business_ids"]:
                self.store.business(con, bid)
            return self.store.create_principal(con, payload["name"], payload["role"], payload["business_ids"])

    def revoke_principal(self, actor, principal_id):
        if actor["role"] != "owner" or actor["id"] == principal_id:
            raise DomainError("An owner may revoke another key, not their own.", 403)
        with self.store.write() as con:
            if not con.execute("UPDATE principals SET revoked=1 WHERE id=?", (principal_id,)).rowcount:
                raise DomainError("Access key not found.", 404)
            con.execute("DELETE FROM sessions WHERE principal_id=?", (principal_id,))
        return {"revoked": principal_id}
