"""Where to start: the guided path for a business's entry path, and every line explained.

Everything here is worked out on each read from the snapshot and the blockers of its steps, inside the
same read, so it can never disagree with them and nothing is stored. The sentences are fixed text from
the catalog or are built here from the records; no model is called. The next action is chosen for the
person who asks, by their role, and offers only what the service would let them do.
"""

from __future__ import annotations

from . import catalog
from .service import FINISHED, REVIEWERS, WRITERS

# A step whose decision has been made: approved, and anything after it. Used for the path's decision stops.
DECIDED = {"approved", "submitted", "verified"}
RECORD_KINDS = {"customer", "offer", "asset", "contract", "obligation", "role", "decision"}
RECORD_NAMES = {
    "customer": "a customer record",
    "offer": "an offer record",
    "asset": "an asset record",
    "contract": "a contract record",
    "obligation": "an obligation record",
    "role": "a role record",
    "decision": "a decision record",
    "financial_plan": "a forecast",
    "deal": "an acquisition analysis",
    "invoice": "an invoice recorded as issued",
    "payment": "a payment recorded against an invoice",
}
STATE_WORDS = {
    "pending": "not started",
    "in_progress": "in progress",
    "prepared": "prepared but not yet approved",
    "approved": "approved but not yet finished",
    "submitted": "submitted and waiting for a receipt",
    "needs_review": "reopened for review",
}


def writer(actor):
    return actor["role"] in WRITERS


def reviewer(actor):
    return actor["role"] in REVIEWERS


def your_move(task, actor):
    """The move waiting on this person, as Today counts it: the page's yourMove rule."""
    if not writer(actor):
        return None
    state, external = task["state"], task["data"]["external"]
    if state == "prepared":
        return "approved" if reviewer(actor) else None
    if state == "approved":
        if external:
            return None if actor["role"] == "agent" else "submitted"
        return "verified" if reviewer(actor) else None
    if state == "needs_review":
        return "prepared"
    if state == "in_progress":
        return "prepared"
    return None


def move_label(task, to):
    title = task["title"]
    if to == "in_progress":
        return f"Start work on {title}"
    if to == "prepared":
        return f"Prepare {title} again" if task["state"] == "needs_review" else f"Finish preparing {title}"
    if to == "approved":
        return f"Approve: {title}"
    if to == "submitted":
        return f"Record that you submitted {title}"
    return f"Add the receipt for {title}" if task["data"]["external"] else f"Verify: {title}"


def record_action(kind):
    if kind in RECORD_KINDS:
        return {"do": "add_record", "record_kind": kind, "label": "Add " + RECORD_NAMES[kind]}
    if kind == "financial_plan":
        return {"do": "forecast", "label": "Build a forecast"}
    if kind == "deal":
        return {"do": "deal", "label": "Screen the deal in an acquisition analysis"}
    return {"do": "open_source", "source": "sales", "label": "Open Invoices & payments"}


def blocker_action(item):
    if item["code"] == "step":
        return {"do": "open_step", "task_id": item["task_id"], "label": f"First finish {item['title']}"}
    if item["code"] == "record":
        return record_action(item["record_kind"])
    return {
        "do": "deal",
        "deal_id": item["deal_id"],
        "label": "Save a fresh acquisition analysis",
    }


def blocker_why(item):
    """A blocker in plain words. Blockers stop approval, submission and verification, not work."""
    if item["code"] == "step":
        state = STATE_WORDS.get(item["state"], item["state"])
        return f"It cannot be approved until {item['title']} is finished. That step is {state}."
    if item["code"] == "record":
        return f"It cannot be approved until there is {RECORD_NAMES.get(item['record_kind'], 'a record')}."
    return (
        "It cannot be approved until there is a fresh acquisition analysis: the evidence behind the "
        "saved one changed."
    )


def blocker_unlock(item):
    if item["code"] == "step":
        return f"Finish {item['title']}: verify it, or rule it out as not applicable."
    if item["code"] == "record":
        return f"Add {RECORD_NAMES.get(item['record_kind'], 'a record')}."
    return "Save a fresh acquisition analysis from the current evidence."


def reopen_note(task):
    history = task["data"].get("history")
    if isinstance(history, list):
        for entry in reversed(history):
            if isinstance(entry, dict) and entry.get("to") == "needs_review":
                note = entry.get("note")
                return note if isinstance(note, str) and note else None
    return None


def state_why(task):
    state, external = task["state"], task["data"]["external"]
    if state == "in_progress":
        return "Work on it has started. It is not prepared yet."
    if state == "prepared":
        return "It is prepared and waits for an owner or reviewer to approve it."
    if state == "approved":
        if external:
            return "It is approved and waits to be submitted to the outside body, which you do yourself."
        return "It is approved and waits for an owner or reviewer to verify the result."
    if state == "submitted":
        return "It was submitted and waits for the outside body's receipt."
    if state == "needs_review":
        note = reopen_note(task)
        return f"It was reopened: {note}." if note else "It was reopened because what it relied on changed."
    return None


def state_unlock(task):
    state, external = task["state"], task["data"]["external"]
    if state == "in_progress":
        return "Finish preparing it, with its evidence attached."
    if state == "prepared":
        return "An owner or reviewer approves it, with reviewed evidence."
    if state == "approved":
        if external:
            return "Submit it yourself, then record that with a reviewed receipt."
        return "An owner or reviewer verifies it, with reviewed evidence."
    if state == "submitted":
        return "Add the outside body's receipt, reviewed, and verify it."
    if state == "needs_review":
        return "Prepare it again from the current records."
    return None


def waiting_on(task, actor):
    """Who the step waits on when nothing on it is this person's to do."""
    if not writer(actor):
        return "You can read this step. A person who can write to this business acts on it."
    state = task["state"]
    if state == "prepared":
        return "It waits on an owner or reviewer to approve it."
    if state == "approved":
        if task["data"]["external"]:
            return "It waits on a person to submit it to the outside body. An assistant cannot."
        return "It waits on an owner or reviewer to verify it."
    if state == "submitted":
        return "It waits on the outside body's receipt, then an owner or reviewer to verify it."
    return None


def step_next(task, items, actor):
    """The one next action on this step for this person, or None when it is not theirs to take."""
    state = task["state"]
    if not writer(actor) or state in FINISHED:
        return None
    if state in {"pending", "in_progress", "needs_review"}:
        # A step can be started and prepared while it waits; a missing record comes first, since
        # preparing the step is writing it down.
        for item in items:
            if item["code"] != "step":
                return blocker_action(item)
        to = "in_progress" if state == "pending" else "prepared"
        return {"do": "transition", "task_id": task["id"], "to": to, "label": move_label(task, to)}
    if state == "prepared":
        to = "approved" if reviewer(actor) else None
    elif state == "approved":
        if task["data"]["external"]:
            to = None if actor["role"] == "agent" else "submitted"
        else:
            to = "verified" if reviewer(actor) else None
    else:  # submitted
        to = "verified" if reviewer(actor) else None
    if to is None:
        return None
    if items:
        return blocker_action(items[0])
    return {"do": "transition", "task_id": task["id"], "to": to, "label": move_label(task, to)}


def receipt_why(task, items):
    """Why a submitted step is the next action: it is with the outside body until their receipt arrives."""
    why = f"{task['title']} is with the outside body: add their receipt when it arrives."
    # A prerequisite reopened after it was submitted: the action is to finish that first, and it says so.
    return " ".join([why, blocker_why(items[0])]) if items else why


def explain_step(task, items, actor):
    why = [blocker_why(i) for i in items]
    unlocks = [blocker_unlock(i) for i in items]
    if task["state"] not in FINISHED:
        if (w := state_why(task)) is not None:
            why.append(w)
        if (u := state_unlock(task)) is not None:
            unlocks.append(u)
    action = step_next(task, items, actor)
    return {
        "id": task["id"],
        "key": task["data"].get("key"),
        "title": task["title"],
        "line": task["data"].get("category"),
        "deal_id": task["data"].get("deal_id"),
        "state": task["state"],
        "blockers": [i["message"] for i in items],
        "ready": task["state"] == "pending" and not items,
        "your_move": your_move(task, actor),
        "why": why,
        "unlocks": unlocks,
        "next": action,
        "waiting_on": None if action or task["state"] in FINISHED else waiting_on(task, actor),
    }


def ordered_lines(tasks):
    """Lines in the order the page draws them: by first appearance, each with its steps in order."""
    lines, by_key = [], {}
    for t in tasks:
        key = (t["data"].get("category") or "Steps", t["data"].get("deal_id"))
        if key not in by_key:
            by_key[key] = {"category": key[0], "deal_id": key[1], "tasks": []}
            lines.append(by_key[key])
        by_key[key]["tasks"].append(t)
    return lines


def build_guide(snapshot, items, actor):
    business = snapshot["business"]
    route = business["data"].get("route", "start")
    objects = snapshot["objects"]
    by_id = {o["id"]: o for o in objects}
    tasks = [o for o in objects if o["kind"] == "task" and o["id"] in items]
    lines = ordered_lines(tasks)
    tasks = [t for line in lines for t in line["tasks"]]
    steps = {t["id"]: explain_step(t, items[t["id"]], actor) for t in tasks}
    deals = [o for o in objects if o["kind"] == "deal"]
    newest_deal = deals[-1] if deals else None

    def step_by_key(key, deal_id=None):
        return next(
            (t for t in tasks if t["data"].get("key") == key and t["data"].get("deal_id") == deal_id),
            None,
        )

    path = build_path(route, objects, steps, step_by_key, newest_deal, actor)
    first_open = next((s for s in path["stops"] if not s["done"]), None)

    # Today's order: what needs review, then the rest waiting on you; steps ready to start come with
    # the lines of the entry path first (on an acquisition, the Acquisition line), then in order.
    mine = [t for t in tasks if steps[t["id"]]["your_move"]]
    mine = [t for t in mine if t["state"] == "needs_review"] + [
        t for t in mine if t["state"] != "needs_review"
    ]
    first_line = "Acquisition" if route == "acquire" else None
    ready = [t for t in tasks if steps[t["id"]]["ready"]]
    ready = [t for t in ready if t["data"].get("category") == first_line] + [
        t for t in ready if t["data"].get("category") != first_line
    ]
    # A submitted step waits on the outside body, so Today does not count it as waiting on you. Once its
    # receipt arrives an owner or reviewer adds it, so it comes last: every other action can be taken now.
    receipts = [t for t in tasks if t["state"] == "submitted" and steps[t["id"]]["next"]]

    def stop_on(line):
        stop = first_open
        if not stop or not stop["action"] or stop["line"] != line["category"]:
            return None
        return stop if line["deal_id"] in {None, newest_deal and newest_deal["id"]} else None

    explained = []
    for line in lines:
        ids = {t["id"] for t in line["tasks"]}
        stop = stop_on(line)
        yours = next((t for t in mine if t["id"] in ids), None)
        start = next((t for t in ready if t["id"] in ids), None)
        receipt = next((t for t in receipts if t["id"] in ids), None)
        open_tasks = [t for t in line["tasks"] if t["state"] not in FINISHED]
        if stop:
            status, action, focus = "your_move", stop["action"], None
        elif yours:
            status, action, focus = "your_move", steps[yours["id"]]["next"], yours
        elif start:
            status, action, focus = "ready", steps[start["id"]]["next"], start
        elif receipt:
            status, action, focus = "waiting", steps[receipt["id"]]["next"], receipt
        else:
            status = "waiting" if open_tasks else "done"
            action, focus = None, open_tasks[0] if open_tasks else None
        deal = by_id.get(line["deal_id"]) if line["deal_id"] else None
        explained.append(
            {
                "category": line["category"],
                "deal_id": line["deal_id"],
                "name": line["category"],
                "deal_title": deal["title"] if deal else None,
                "about": catalog.LINES.get(line["category"]),
                "on_acquisition": catalog.ON_ACQUISITION.get(line["category"])
                if route == "acquire"
                else None,
                "steps": len(line["tasks"]),
                "verified": sum(t["state"] == "verified" for t in line["tasks"]),
                "finished": len(line["tasks"]) - len(open_tasks),
                "status": status,
                "stop": stop["n"] if stop else None,
                "focus": steps[focus["id"]] if focus else None,
                "next": action,
            }
        )
    if route == "acquire":
        explained.sort(key=lambda x: x["category"] != "Acquisition")
        if not any(x["category"] == "Acquisition" for x in explained):
            explained.insert(
                0,
                {
                    "category": "Acquisition",
                    "deal_id": None,
                    "name": "Acquisition",
                    "deal_title": None,
                    "about": catalog.LINES["Acquisition"],
                    "on_acquisition": catalog.ACQUISITION_LATER,
                    "steps": 0,
                    "verified": 0,
                    "finished": 0,
                    "status": "later",
                    "stop": None,
                    "focus": None,
                    "next": None,
                },
            )

    # The one next action for this person: the first stop of the path not done, if it is theirs to
    # take; then the first step waiting on them, in Today's order; then the first step ready to start;
    # then the first submitted step whose receipt they can add when it arrives.
    if first_open and first_open["action"]:
        nxt = {"from": "path", "stop": first_open["n"], "task_id": None, "action": first_open["action"]}
    elif mine:
        t = mine[0]
        nxt = {"from": "waiting_on_you", "stop": None, "task_id": t["id"], "action": steps[t["id"]]["next"]}
    elif ready and writer(actor):
        t = ready[0]
        nxt = {"from": "ready", "stop": None, "task_id": t["id"], "action": steps[t["id"]]["next"]}
    elif receipts:
        t = receipts[0]
        nxt = {"from": "receipt", "stop": None, "task_id": t["id"], "action": steps[t["id"]]["next"]}
    else:
        nxt = {"from": None, "stop": None, "task_id": None, "action": None}
    if nxt["action"]:
        nxt["label"] = nxt["action"]["label"]
        nxt["why"] = receipt_why(receipts[0], items[receipts[0]["id"]]) if nxt["from"] == "receipt" else None
    elif not writer(actor):
        nxt["label"] = "Nothing here is yours to do"
        nxt["why"] = "You can read this business. A person who can write to it takes each next step."
    elif first_open:
        nxt["label"] = "Nothing is waiting on you"
        nxt["why"] = " ".join(
            filter(
                None,
                [
                    f"Stop {first_open['n']}, {first_open['title']}, is not yours to take.",
                    first_open["waiting_on"],
                ],
            )
        )
    else:
        nxt["label"] = "Nothing is waiting on you"
        nxt["why"] = "Every step left waits on another step, or on someone else."

    return {
        "business_id": business["id"],
        "business_version": business["version"],
        "pack_version": catalog.PACK_VERSION,
        "route": route,
        "role": actor["role"],
        "path": path,
        "next": nxt,
        "order_note": catalog.ACQUISITION_ORDER if route == "acquire" else None,
        "lines": explained,
        "steps": [steps[t["id"]] for t in tasks],
        "ready": [t["id"] for t in ready],
    }


def build_path(route, objects, steps, step_by_key, newest_deal, actor):
    spec = catalog.PATHS[route]
    evidence = [o for o in objects if o["kind"] == "evidence"]
    attached = [e for e in evidence if e["state"] != "rejected"]
    unreviewed = [e for e in evidence if e["state"] == "unverified"]
    reviewed = [e for e in evidence if e["state"] == "verified"]
    plans = [o for o in objects if o["kind"] == "financial_plan"]
    kinds = {o["kind"] for o in objects}
    deal_id = newest_deal["id"] if newest_deal else None

    def review_action(add_label):
        if unreviewed:
            first = unreviewed[0]
            if reviewer(actor):
                return {
                    "do": "review_evidence",
                    "evidence_id": first["id"],
                    "label": f"Review {first['title']}",
                }, None
            return None, f"It waits on an owner or reviewer to review {first['title']}."
        return ({"do": "add_evidence", "label": add_label}, None) if writer(actor) else (None, None)

    def decision(task, missing_label):
        """A stop that is a step's decision: done once the step is approved, verified or later."""
        if task is None:
            action = {"do": "deal", "label": missing_label} if missing_label and writer(actor) else None
            return False, [], action, None
        step = steps[task["id"]]
        return task["state"] in DECIDED, [task["id"]], step["next"], step["waiting_on"]

    stops = []
    for n, stop in enumerate(spec["stops"], start=1):
        key = stop["key"]
        action, waiting = None, None
        if key == "workspace":
            done = True
        elif key == "evidence":
            done = bool(attached)
            if writer(actor):
                action = {"do": "add_evidence", "label": stop["action"]}
        elif key == "reviewed":
            done = bool(reviewed) and not unreviewed
            action, waiting = review_action(stop.get("action", "Add a piece of evidence"))
        elif key == "deal":
            done = newest_deal is not None and not newest_deal.get("stale")
            if writer(actor):
                action = (
                    {"do": "deal", "deal_id": deal_id, "label": "Save a fresh acquisition analysis"}
                    if newest_deal
                    else {"do": "deal", "label": stop["action"]}
                )
        elif key == "decide":
            done, _, action, waiting = decision(
                step_by_key("mandate", deal_id) if deal_id else None, "Screen the deal first"
            )
        elif key in {"customer", "offer"}:
            done = key in kinds
            if writer(actor):
                action = record_action(key) | {"label": stop["action"]}
        elif key == "forecast":
            stale = bool(plans) and bool(plans[-1].get("stale"))
            done = bool(plans) and not stale
            if writer(actor):
                # An out-of-date forecast's reason says to save a revised forecast; the button says the same.
                action = {"do": "forecast", "label": "Save a revised forecast" if stale else stop["action"]}
        elif key == "structure":
            done, _, action, waiting = decision(step_by_key("structure"), None)
        else:  # pragma: no cover - every stop in the catalog has a rule above
            raise ValueError(f"No rule for the stop {key}")
        related = [
            t["id"]
            for k in stop["steps"]
            if (t := step_by_key(k, deal_id if k in DEAL_KEYS else None)) is not None
        ]
        if not writer(actor) and not done:
            waiting = waiting or "You can read this business. A person who can write to it takes this step."
        stops.append(
            {
                "n": n,
                "key": key,
                "title": stop["title"],
                "detail": stop["detail"],
                "why": stop["why"],
                "done_when": stop["done_when"],
                "line": stop["line"],
                "done": done,
                "steps": related,
                "action": None if done else action,
                "waiting_on": None if done or action else waiting,
            }
        )
    done = sum(s["done"] for s in stops)
    first = next((s["n"] for s in stops if not s["done"]), None)
    return {
        "route": route,
        "name": spec["name"],
        "stops": stops,
        "done": done,
        "total": len(stops),
        "complete": first is None,
        "next_stop": first,
    }


DEAL_KEYS = {spec["key"] for spec in catalog.DEAL_TASKS}
