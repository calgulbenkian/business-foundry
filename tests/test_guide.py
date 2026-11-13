"""Where to start: the guided path, the lines explained and the next action.

Every expected value here is written by hand from the catalog and the rules in docs/WALKTHROUGH.md and
src/foundry/guide.py; none is read back from the guide to check the guide.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from foundry import catalog, models
from foundry.api import create_app
from foundry.demo import seed
from foundry.service import live

ACQUISITION_LINES = [
    "Acquisition",
    "Market",
    "Finance",
    "Setup",
    "Rights",
    "Delivery",
    "Sales to cash",
    "Operate",
]
START_LINES = ["Market", "Finance", "Setup", "Rights", "Delivery", "Sales to cash", "Operate"]


def key():
    return str(uuid4())


def data(cls, **values):
    return cls(**values).model_dump(mode="json")


def laundry(service, actor):
    """The synthetic acquisition of design mockup 01: a workspace for buying a business."""
    return service.create_business(
        actor,
        key(),
        data(
            models.BusinessCreate,
            name="Juniper Street Laundry · example",
            route="acquire",
            concept="Synthetic example: buy a small laundromat in the made-up town of Alder Falls.",
            is_demo=True,
        ),
    )["id"]


def evidence(service, actor, bid, title="Listing text · synthetic"):
    return service.add_evidence(
        actor, bid, key(), data(models.EvidenceCreate, title=title, content="SYNTHETIC listing text.")
    )


def review(service, actor, bid, item):
    return service.review_evidence(
        actor,
        bid,
        item["id"],
        key(),
        data(models.Review, note="Read the synthetic item in full.", expected_version=item["version"]),
    )


def record(service, actor, bid, kind, title):
    return service.add_record(actor, bid, key(), data(models.RecordCreate, kind=kind, title=title))


def deal(service, actor, bid):
    return service.save_deal(
        actor,
        bid,
        key(),
        models.DealInputs(
            target="Juniper Street Laundry",
            thesis="Synthetic thesis: a laundromat with steady local demand.",
            annual_revenue="210000",
            reported_ebitda="62000",
            purchase_price="180000",
        ).model_dump(mode="json", exclude_unset=True),
    )


def move(service, actor, bid, task, state, evidence_ids=()):
    return service.transition(
        actor,
        bid,
        task["id"],
        key(),
        data(
            models.TaskTransition,
            state=state,
            expected_version=task["version"],
            note="Synthetic decision for the guide tests.",
            evidence_ids=list(evidence_ids),
        ),
    )


def task(service, actor, bid, title):
    return next(
        o
        for o in service.snapshot(actor, bid)["objects"]
        if o["kind"] == "task" and o["title"] == title and live(o)
    )


def done(guide):
    return [s["done"] for s in guide["path"]["stops"]]


def step(guide, title):
    return next(s for s in guide["steps"] if s["title"] == title)


def line(guide, category):
    return next(x for x in guide["lines"] if x["category"] == category)


def test_the_catalog_explains_every_line_and_gives_every_entry_path_a_path():
    categories = {t["category"] for t in catalog.BASE + catalog.DEAL_TASKS}
    categories |= {t["category"] for specs in catalog.JURISDICTIONS.values() for t in specs}
    assert set(catalog.LINES) == categories
    base = {t["category"] for t in catalog.BASE}
    assert set(catalog.ON_ACQUISITION) == base
    # every entry path a business can be created with has a guided path of five or six stops
    routes = set(models.BusinessCreate.model_fields["route"].annotation.__args__)
    assert routes == {"start", "acquire"} == set(catalog.PATHS)
    assert [len(catalog.PATHS[r]["stops"]) for r in ("acquire", "start")] == [5, 6]
    assert catalog.LINES["Sales to cash"] == "From a signed agreement to money in the bank."
    assert catalog.ON_ACQUISITION["Delivery"] == (
        "When you buy a business, the work already happens. This line is where you write down how, so it "
        "runs the same way after the handover."
    )
    # plain words: the text the guide shows has no em dash
    words = [*catalog.LINES.values(), *catalog.ON_ACQUISITION.values(), catalog.ACQUISITION_ORDER]
    words += [v for p in catalog.PATHS.values() for s in p["stops"] for v in s.values() if isinstance(v, str)]
    assert not [w for w in words if "—" in w]


def test_the_demo_has_finished_its_path_and_its_next_action_is_the_thesis(setup):
    _, service, owner, _, _ = setup
    bid = seed(service, owner)["id"]
    guide = service.guide(owner, bid)
    assert (guide["route"], guide["role"], guide["pack_version"]) == ("start", "owner", "2026.11.1")
    assert done(guide) == [True] * 6
    assert (guide["path"]["done"], guide["path"]["total"], guide["path"]["complete"]) == (6, 6, True)
    assert guide["path"]["next_stop"] is None
    thesis = task(service, owner, bid, "Approve the acquisition thesis")
    # the path is done and nothing waits on the owner, so the first step ready to start is next
    assert guide["next"] == {
        "from": "ready",
        "stop": None,
        "task_id": thesis["id"],
        "action": {
            "do": "transition",
            "task_id": thesis["id"],
            "to": "in_progress",
            "label": "Start work on Approve the acquisition thesis",
        },
        "label": "Start work on Approve the acquisition thesis",
        "why": None,
    }
    assert [x["category"] for x in guide["lines"]] == [*START_LINES, "Acquisition"]
    assert [x["status"] for x in guide["lines"]] == ["done"] * 7 + ["ready"]
    assert line(guide, "Delivery")["on_acquisition"] is None
    assert line(guide, "Acquisition")["deal_title"] == "Example Mapping Studio · synthetic"
    assert guide["order_note"] is None


def test_an_acquisition_path_ticks_off_stop_by_stop_and_reopens_with_a_new_fact(setup):
    _, service, owner, _, _ = setup
    bid = laundry(service, owner)
    guide = service.guide(owner, bid)
    assert [s["title"] for s in guide["path"]["stops"]] == [
        "Say you are buying, and name the business",
        "Attach the listing and anything the seller sent",
        "Check what you attached",
        "Screen the deal",
        "Decide whether to keep going",
    ]
    assert done(guide) == [True, False, False, False, False]
    assert guide["next"]["from"] == "path" and guide["next"]["stop"] == 2
    assert guide["next"]["action"] == {
        "do": "add_evidence",
        "label": "Add the listing or a document from the seller",
    }
    # The Acquisition line comes first, and before a deal is screened it has no steps yet.
    assert [x["category"] for x in guide["lines"]] == ACQUISITION_LINES
    assert line(guide, "Acquisition")["status"] == "later"
    assert line(guide, "Acquisition")["on_acquisition"] == (
        "This line appears when you save your first acquisition analysis."
    )
    assert line(guide, "Sales to cash")["on_acquisition"] == (
        "When you buy a business, this follows one sale after you take over: the agreement, the delivery, "
        "the invoice and the payment, so you see money arrive while you own it."
    )
    assert guide["order_note"].startswith(
        "You are buying this business, so the Acquisition line comes first."
    )

    listing = evidence(service, owner, bid)
    guide = service.guide(owner, bid)
    assert done(guide) == [True, True, False, False, False]
    assert guide["next"]["action"] == {
        "do": "review_evidence",
        "evidence_id": listing["id"],
        "label": "Review Listing text · synthetic",
    }

    listing = review(service, owner, bid, listing)
    guide = service.guide(owner, bid)
    assert done(guide) == [True, True, True, False, False]
    assert guide["next"]["action"] == {"do": "deal", "label": "Screen the deal in an acquisition analysis"}
    # one more item waiting for review: the check is not done until it is reviewed too
    lease = evidence(service, owner, bid, "Lease summary · synthetic")
    guide = service.guide(owner, bid)
    assert done(guide) == [True, True, False, False, False]
    assert guide["next"]["action"]["evidence_id"] == lease["id"]
    review(service, owner, bid, lease)
    assert done(service.guide(owner, bid)) == [True, True, True, False, False]

    deal(service, owner, bid)
    thesis = task(service, owner, bid, "Approve the acquisition thesis")
    guide = service.guide(owner, bid)
    assert done(guide) == [True, True, True, True, False]
    assert guide["next"]["stop"] == 5
    assert guide["next"]["label"] == "Start work on Approve the acquisition thesis"
    assert guide["path"]["stops"][4]["steps"] == [thesis["id"]]
    # Today's ready list puts the Acquisition line first, as Start here does.
    assert guide["ready"][0] == thesis["id"]
    assert step(guide, "Resolve operating and founder jurisdictions")["id"] in guide["ready"][1:]
    acquisition = line(guide, "Acquisition")
    assert (acquisition["status"], acquisition["stop"], acquisition["steps"]) == ("your_move", 5, 7)

    thesis = move(service, owner, bid, thesis, "in_progress")
    assert service.guide(owner, bid)["next"]["label"] == "Finish preparing Approve the acquisition thesis"
    thesis = move(service, owner, bid, thesis, "prepared", [listing["id"]])
    guide = service.guide(owner, bid)
    # prepared is not decided: the stop waits for the approval
    assert done(guide) == [True, True, True, True, False]
    assert (guide["next"]["from"], guide["next"]["label"]) == (
        "path",
        "Approve: Approve the acquisition thesis",
    )
    thesis = move(service, owner, bid, thesis, "approved", [listing["id"]])
    guide = service.guide(owner, bid)
    assert done(guide) == [True] * 5 and guide["path"]["complete"]
    # the path is done; the approved thesis now waits on the owner to verify it
    assert (guide["next"]["from"], guide["next"]["label"]) == (
        "waiting_on_you",
        "Verify: Approve the acquisition thesis",
    )

    # A new fact: the listing is withdrawn. The thesis that cited it reopens, and the path with it;
    # the lease is still reviewed, so the earlier stops stay done.
    service.review_evidence(
        owner,
        bid,
        listing["id"],
        key(),
        data(
            models.Review, note="The seller withdrew this listing text.", expected_version=listing["version"]
        ),
        revoke=True,
    )
    guide = service.guide(owner, bid)
    assert done(guide) == [True, True, True, True, False]
    assert guide["next"]["stop"] == 5
    reopened = step(guide, "Approve the acquisition thesis")
    assert reopened["state"] == "needs_review"
    assert reopened["why"] == ["It was reopened: Evidence withdrawn: Listing text · synthetic."]
    assert reopened["unlocks"] == ["Prepare it again from the current records."]
    assert reopened["next"]["label"] == "Prepare Approve the acquisition thesis again"


def test_an_analysis_whose_evidence_changed_reopens_the_screen_stop(setup):
    _, service, owner, _, _ = setup
    bid = laundry(service, owner)
    listing = review(service, owner, bid, evidence(service, owner, bid))
    saved = service.save_deal(
        owner,
        bid,
        key(),
        models.DealInputs(
            target="Juniper Street Laundry",
            thesis="Synthetic thesis that cites the listing.",
            annual_revenue="210000",
            reported_ebitda="62000",
            purchase_price="180000",
            evidence_ids=[listing["id"]],
        ).model_dump(mode="json", exclude_unset=True),
    )
    assert done(service.guide(owner, bid)) == [True, True, True, True, False]
    # withdrawing the listing changes its version, so the saved analysis is out of date
    service.review_evidence(
        owner,
        bid,
        listing["id"],
        key(),
        data(
            models.Review, note="The seller withdrew this listing text.", expected_version=listing["version"]
        ),
        revoke=True,
    )
    guide = service.guide(owner, bid)
    assert done(guide) == [True, False, False, False, False]
    stop = guide["path"]["stops"][3]
    assert stop["action"] == {
        "do": "deal",
        "deal_id": saved["id"],
        "label": "Save a fresh acquisition analysis",
    }
    thesis = step(guide, "Approve the acquisition thesis")
    assert thesis["blockers"] == ["Supporting deal evidence changed; create and review a fresh analysis"]
    assert thesis["why"] == [
        "It cannot be approved until there is a fresh acquisition analysis: the evidence behind the saved one "
        "changed."
    ]
    assert thesis["unlocks"] == ["Save a fresh acquisition analysis from the current evidence."]
    assert thesis["next"] == {
        "do": "deal",
        "deal_id": saved["id"],
        "label": "Save a fresh acquisition analysis",
    }


def test_a_start_up_path_ticks_off_stop_by_stop(setup):
    _, service, owner, bid, _ = setup
    guide = service.guide(owner, bid)
    assert [s["title"] for s in guide["path"]["stops"]] == [
        "Say you are starting, and name the business",
        "Describe the buyer and their problem",
        "Talk to buyers and attach the notes",
        "Define the offer",
        "Review cash needs",
        "Decide the structure",
    ]
    assert done(guide) == [True, False, False, False, False, False]
    assert guide["next"]["action"] == {
        "do": "add_record",
        "record_kind": "customer",
        "label": "Add a customer record",
    }
    assert [x["category"] for x in guide["lines"]] == START_LINES
    assert all(x["on_acquisition"] is None for x in guide["lines"])

    record(service, owner, bid, "customer", "Synthetic buyer")
    guide = service.guide(owner, bid)
    assert done(guide) == [True, True, False, False, False, False]
    assert guide["next"]["action"] == {
        "do": "add_evidence",
        "label": "Add your notes from a buyer conversation",
    }
    notes = evidence(service, owner, bid, "Interview notes · synthetic")
    assert service.guide(owner, bid)["next"]["action"]["do"] == "review_evidence"
    review(service, owner, bid, notes)
    guide = service.guide(owner, bid)
    assert done(guide) == [True, True, True, False, False, False]
    assert guide["next"]["action"] == {
        "do": "add_record",
        "record_kind": "offer",
        "label": "Add an offer record",
    }
    record(service, owner, bid, "offer", "Synthetic offer")
    assert service.guide(owner, bid)["next"]["action"] == {"do": "forecast", "label": "Build a forecast"}
    service.save_forecast(
        owner,
        bid,
        key(),
        data(
            models.ForecastCreate, title="Synthetic forecast", inputs=models.ForecastInputs(), evidence_ids=[]
        ),
    )
    guide = service.guide(owner, bid)
    assert done(guide) == [True, True, True, True, True, False]
    # The structure decision needs a decision record before it can be approved: that comes first.
    assert guide["next"]["action"] == {
        "do": "add_record",
        "record_kind": "decision",
        "label": "Add a decision record",
    }
    record(service, owner, bid, "decision", "Synthetic structure decision")
    structure = task(service, owner, bid, "Decide the entity and ownership structure")
    assert service.guide(owner, bid)["next"]["action"] == {
        "do": "transition",
        "task_id": structure["id"],
        "to": "in_progress",
        "label": "Start work on Decide the entity and ownership structure",
    }
    structure = move(service, owner, bid, structure, "prepared", [notes["id"]])
    structure = move(service, owner, bid, structure, "approved", [notes["id"]])
    guide = service.guide(owner, bid)
    assert guide["path"]["complete"] and done(guide) == [True] * 6


def test_why_it_waits_is_the_blockers_logic_for_every_step(setup):
    store, service, owner, start_bid, _ = setup
    demo_bid = seed(service, owner)["id"]
    acquire_bid = laundry(service, owner)
    deal(service, owner, acquire_bid)
    for bid in (start_bid, demo_bid, acquire_bid):
        guide = service.guide(owner, bid)
        snapshot = service.snapshot(owner, bid)
        tasks = [o for o in snapshot["objects"] if o["kind"] == "task" and live(o)]
        assert sorted(s["id"] for s in guide["steps"]) == sorted(t["id"] for t in tasks)
        with store.read() as con:
            for t in tasks:
                explained = (
                    step(guide, t["title"])
                    if t["data"].get("deal_id") is None
                    else next(s for s in guide["steps"] if s["id"] == t["id"])
                )
                assert explained["blockers"] == service.blockers(con, bid, t) == t["blockers"]
                # one reason for each blocker, first and in the same order, then one for the state
                assert len(explained["why"]) >= len(t["blockers"])
                assert explained["ready"] == (t["state"] == "pending" and not t["blockers"])
    # Written out by hand for steps of the new start-up business.
    guide = service.guide(owner, start_bid)
    founders = step(guide, "Record founder and IP arrangements")
    assert founders["blockers"] == [
        "Complete: Decide the entity and ownership structure",
        "Add contract record",
    ]
    assert founders["why"] == [
        "It cannot be approved until Decide the entity and ownership structure is finished. That step is not "
        "started.",
        "It cannot be approved until there is a contract record.",
    ]
    assert founders["unlocks"] == [
        "Finish Decide the entity and ownership structure: verify it, or rule it out as not applicable.",
        "Add a contract record.",
    ]
    assert founders["next"] == {
        "do": "add_record",
        "record_kind": "contract",
        "label": "Add a contract record",
    }
    invoice = step(guide, "Issue and record the invoice")
    assert invoice["why"][-1] == "It cannot be approved until there is an invoice recorded as issued."
    assert invoice["next"] == {"do": "open_source", "source": "sales", "label": "Open Invoices & payments"}
    jurisdiction = step(guide, "Resolve operating and founder jurisdictions")
    assert (jurisdiction["blockers"], jurisdiction["why"], jurisdiction["unlocks"]) == ([], [], [])
    assert jurisdiction["ready"] and jurisdiction["next"]["to"] == "in_progress"


def test_the_next_action_rule_path_then_waiting_on_you_then_ready(setup):
    _, service, owner, _, _ = setup
    bid = seed(service, owner)["id"]
    thesis = task(service, owner, bid, "Approve the acquisition thesis")
    registration = task(service, owner, bid, "Verify applicable registrations")
    # (c) nothing waits on the owner: the first step ready to start
    assert service.guide(owner, bid)["next"]["from"] == "ready"
    # (b) a step being prepared waits on the owner
    registration = move(service, owner, bid, registration, "needs_review")
    move(service, owner, bid, registration, "in_progress")
    guide = service.guide(owner, bid)
    assert guide["path"]["complete"]
    assert (guide["next"]["from"], guide["next"]["task_id"]) == ("waiting_on_you", registration["id"])
    assert guide["next"]["label"] == "Finish preparing Verify applicable registrations"
    # in Today's order, a step that needs review comes first, even on a later line
    thesis = move(service, owner, bid, thesis, "in_progress")
    move(service, owner, bid, thesis, "needs_review")
    guide = service.guide(owner, bid)
    assert (guide["next"]["from"], guide["next"]["task_id"]) == ("waiting_on_you", thesis["id"])
    assert guide["next"]["label"] == "Prepare Approve the acquisition thesis again"
    # each line names its own next action
    assert line(guide, "Setup")["next"]["label"] == "Finish preparing Verify applicable registrations"
    assert line(guide, "Acquisition")["next"]["task_id"] == thesis["id"]
    assert (line(guide, "Market")["status"], line(guide, "Market")["next"]) == ("done", None)
    # (a) a stop of the path not done comes before both: the structure decision reopens
    structure = task(service, owner, bid, "Decide the entity and ownership structure")
    move(service, owner, bid, structure, "needs_review")
    guide = service.guide(owner, bid)
    assert (guide["next"]["from"], guide["next"]["stop"]) == ("path", 6)
    assert guide["next"]["label"] == "Prepare Decide the entity and ownership structure again"
    assert line(guide, "Setup")["stop"] == 6


@pytest.fixture
def keys(setup):
    store, service, owner, _, _ = setup
    bid = laundry(service, owner)
    other = laundry(service, owner)
    evidence(service, owner, bid)
    made = {
        role: service.add_principal(owner, {"name": f"Guide {role}", "role": role, "business_ids": [bid]})[
            "token"
        ]
        for role in ("reviewer", "operator", "agent", "viewer")
    }
    return store, bid, other, made


def guide_as(store, token, bid):
    app = create_app(store.path, allowed_hosts=["testserver"])
    with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
        return client.get(f"/api/businesses/{bid}/guide")


def test_each_role_sees_its_own_next_action_and_no_other_business(setup, keys):
    store, bid, other, made = keys
    _, _, _, _, owner_token = setup
    listing = "Listing text · synthetic"
    answers = {role: guide_as(store, token, bid) for role, token in {"owner": owner_token, **made}.items()}
    assert {role: r.status_code for role, r in answers.items()} == dict.fromkeys(answers, 200)
    guides = {role: r.json() for role, r in answers.items()}
    assert {role: g["role"] for role, g in guides.items()} == {r: r for r in guides}
    # owners and reviewers can review the listing, the stop the path is at
    for role in ("owner", "reviewer"):
        assert guides[role]["next"]["from"] == "path"
        assert guides[role]["next"]["label"] == f"Review {listing}"
    # operators and agents cannot: the stop says who it waits on, and their own next action follows
    for role in ("operator", "agent"):
        stop = guides[role]["path"]["stops"][2]
        assert (stop["action"], stop["waiting_on"]) == (
            None,
            f"It waits on an owner or reviewer to review {listing}.",
        )
        assert guides[role]["next"]["from"] == "ready"
        assert guides[role]["next"]["label"] == "Start work on Resolve operating and founder jurisdictions"
    # a viewer reads it and is offered nothing to do
    viewer = guides["viewer"]
    assert viewer["next"]["action"] is None
    assert viewer["next"]["label"] == "Nothing here is yours to do"
    assert all(s["next"] is None for s in viewer["steps"])
    assert all(s["action"] is None for s in viewer["path"]["stops"])
    # another business's guide is refused to every key scoped to this one
    for role, token in made.items():
        refused = guide_as(store, token, other)
        assert refused.status_code == 403, role
        assert refused.json()["detail"] == "This access key does not cover that business."
    assert guide_as(store, owner_token, "business_unknown").status_code == 404


def test_the_guide_endpoint_reads_and_changes_nothing(setup, client):
    store, service, owner, bid, _ = setup
    with store.read() as con:
        before = con.execute("SELECT count(*) FROM events").fetchone()[0]
    answer = client.get(f"/api/businesses/{bid}/guide")
    assert answer.status_code == 200
    snapshot = client.get(f"/api/businesses/{bid}").json()
    # the page reads the guide with the records, from the same read; both say the same
    assert snapshot["guide"] == answer.json() == service.guide(owner, bid)
    with store.read() as con:
        assert con.execute("SELECT count(*) FROM events").fetchone()[0] == before
    assert client.post(f"/api/businesses/{bid}/guide", headers={"Idempotency-Key": key()}).status_code == 405


def test_a_viewer_is_offered_no_stop_of_the_path(setup):
    store, service, owner, _, _ = setup
    bid = laundry(service, owner)
    viewer = service.add_principal(owner, {"name": "Guide viewer", "role": "viewer", "business_ids": [bid]})
    guide = guide_as(store, viewer["token"], bid).json()
    assert [s["action"] for s in guide["path"]["stops"]] == [None] * 5
    assert [s["waiting_on"] for s in guide["path"]["stops"]] == [None] + [
        "You can read this business. A person who can write to it takes this step."
    ] * 4
