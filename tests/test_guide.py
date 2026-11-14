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
from foundry.store import DomainError

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


def test_the_screen_stop_says_what_the_deal_model_requires_and_lists():
    """The sentence under Screen the deal is checked against DealInputs, not read from it."""
    stop = next(s for s in catalog.PATHS["acquire"]["stops"] if s["key"] == "deal")
    assert stop["detail"] == (
        "An acquisition analysis needs the revenue, the earnings and the price of the business itself; a "
        "building sold with it has a price of its own, which can be left empty. Any other figure you leave "
        "out, and the seller note's schedule, takes an example value, and the analysis lists each one it "
        "used."
    )
    # The three figures it names are the model's only required figures; the building's price is not one.
    fields = models.DealInputs.model_fields
    required = {name for name, field in fields.items() if field.is_required()}
    assert required == {"target", "thesis", "annual_revenue", "reported_ebitda", "purchase_price"}
    assert not fields["real_estate_price"].is_required()
    given = {
        "target": "Screen stop target · synthetic",
        "thesis": "A synthetic deal to check the sentence.",
        "annual_revenue": "900000",
        "reported_ebitda": "200000",
        "purchase_price": "600000",
    }
    # With no seller note, its rate, term and schedule are not used, and not listed.
    unused = {"seller_rate", "seller_years", "seller_schedule"}
    assert not unused & set(models.DealInputs(**given).assumed_inputs())
    # With one, the schedule left out takes the example and is listed with its rate and term.
    with_note = models.DealInputs(**given, seller_note="100000").assumed_inputs()
    assert unused <= set(with_note) and "real_estate_price" in with_note
    assert models.DealInputs(**given, seller_note="100000").seller_schedule == "amortizing"


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
    # The decide stop says the checks cannot be approved until the thesis is finished, not just approved.
    assert guide["path"]["stops"][4]["why"] == (
        "The checks on the books and the contracts cannot be approved until this step is finished: "
        "verified, or ruled out as not applicable. Decide here before you pay anyone for those checks."
    )
    for title in ("Reconcile earnings and working capital", "Review liabilities and transfer rights"):
        assert step(guide, title)["blockers"] == ["Complete: Approve the acquisition thesis"]
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


def test_a_forecast_saved_under_another_jurisdiction_reopens_the_forecast_stop(setup):
    _, service, owner, bid, _ = setup
    record(service, owner, bid, "customer", "Synthetic buyer")
    review(service, owner, bid, evidence(service, owner, bid, "Interview notes · synthetic"))
    record(service, owner, bid, "offer", "Synthetic offer")
    service.save_forecast(
        owner,
        bid,
        key(),
        data(
            models.ForecastCreate, title="Synthetic forecast", inputs=models.ForecastInputs(), evidence_ids=[]
        ),
    )
    assert done(service.guide(owner, bid)) == [True, True, True, True, True, False]
    # A forecast stores no evidence versions: a jurisdiction change is what puts it out of date.
    service.change_jurisdiction(
        owner,
        bid,
        key(),
        data(
            models.JurisdictionChange,
            expected_version=service.snapshot(owner, bid)["business"]["version"],
            jurisdiction="GB",
            reason="Synthetic decision: plan under England and Wales.",
        ),
    )
    plans = [o for o in service.snapshot(owner, bid)["objects"] if o["kind"] == "financial_plan"]
    assert [p["stale"] for p in plans] == [True]
    assert [[r["code"] for r in p["stale_reasons"]] for p in plans] == [["jurisdiction_changed"]]
    guide = service.guide(owner, bid)
    stop = guide["path"]["stops"][4]
    assert stop["title"] == "Review cash needs"
    assert stop["done"] is False
    assert stop["action"] == {"do": "forecast", "label": "Save a revised forecast"}


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


def test_a_second_analysis_moves_the_path_and_the_lines_to_the_newest_deal(setup):
    _, service, owner, _, _ = setup
    bid = laundry(service, owner)
    review(service, owner, bid, evidence(service, owner, bid))
    first = deal(service, owner, bid)
    second = service.save_deal(
        owner,
        bid,
        key(),
        models.DealInputs(
            target="Juniper Street Laundry · second offer",
            thesis="Synthetic thesis: the same laundromat at a lower price.",
            annual_revenue="210000",
            reported_ebitda="62000",
            purchase_price="150000",
        ).model_dump(mode="json", exclude_unset=True),
    )
    theses = [
        o
        for o in service.snapshot(owner, bid)["objects"]
        if o["kind"] == "task" and o["data"].get("key") == "mandate" and live(o)
    ]
    assert [t["data"]["deal_id"] for t in theses] == [first["id"], second["id"]]
    old, new = theses
    guide = service.guide(owner, bid)
    assert done(guide) == [True, True, True, True, False]
    # stop 4 offers nothing more and stop 5 is the newest analysis's thesis, not the first one's
    assert guide["path"]["stops"][3]["action"] is None
    assert guide["path"]["stops"][4]["steps"] == [new["id"]]
    assert guide["next"] == {
        "from": "path",
        "stop": 5,
        "task_id": None,
        "action": {
            "do": "transition",
            "task_id": new["id"],
            "to": "in_progress",
            "label": "Start work on Approve the acquisition thesis",
        },
        "label": "Start work on Approve the acquisition thesis",
        "why": None,
    }
    # each analysis has its own Acquisition line; the path's stop is on the newest one's
    rows = [x for x in guide["lines"] if x["category"] == "Acquisition"]
    assert [(x["deal_id"], x["deal_title"], x["stop"], x["status"]) for x in rows] == [
        (first["id"], "Juniper Street Laundry", None, "ready"),
        (second["id"], "Juniper Street Laundry · second offer", 5, "your_move"),
    ]
    assert rows[0]["next"]["task_id"] == old["id"]
    assert rows[1]["next"]["task_id"] == new["id"]

    # Approving the newest thesis finishes the path, though the first analysis's thesis is not started.
    reviewed = review(service, owner, bid, evidence(service, owner, bid, "Lease summary · synthetic"))
    new = move(service, owner, bid, new, "prepared", [reviewed["id"]])
    move(service, owner, bid, new, "approved", [reviewed["id"]])
    guide = service.guide(owner, bid)
    assert done(guide) == [True] * 5
    assert step(guide, "Approve the acquisition thesis")["state"] == "pending"


def submitted_registration(service, owner, bid, keep=()):
    """The demo with one external step submitted and every other unfinished step, but those titled in
    `keep`, ruled out."""
    (registration,), receipt = submitted_steps(service, owner, bid, ["Verify applicable registrations"], keep)
    return registration, receipt


def submitted_steps(service, owner, bid, titles, keep=()):
    """The demo with the external steps titled in `titles` submitted, in that order, and every other
    unfinished step, but those titled in `keep`, ruled out."""
    receipt = service.add_evidence(
        owner,
        bid,
        key(),
        data(
            models.EvidenceCreate,
            title="Registration receipt · synthetic",
            content="SYNTHETIC confirmation that a registration was received.",
            source_type="external_receipt",
        ),
    )
    receipt = review(service, owner, bid, receipt)
    submitted = []
    for title in titles:
        item = task(service, owner, bid, title)
        for state in ("needs_review", "prepared", "approved", "submitted"):
            item = move(service, owner, bid, item, state, [receipt["id"]])
        submitted.append(item)
    ids = {t["id"] for t in submitted}
    # Reopening a submitted step reopens the steps that relied on it; rule out every step left.
    for _ in range(3):
        for t in service.snapshot(owner, bid)["objects"]:
            if t["kind"] != "task" or not live(t) or t["id"] in ids:
                continue
            if t["state"] in {"verified", "not_applicable"} or t["title"] in keep:
                continue
            if t["state"] in {"approved", "submitted"}:
                t = move(service, owner, bid, t, "needs_review")
            move(service, owner, bid, t, "not_applicable", [receipt["id"]])
    left = [
        t["id"]
        for t in service.snapshot(owner, bid)["objects"]
        if t["kind"] == "task" and live(t) and t["state"] not in {"verified", "not_applicable"}
    ]
    assert sorted(left) == sorted([*ids, *(task(service, owner, bid, k)["id"] for k in keep)])
    assert [t["state"] for t in submitted] == ["submitted"] * len(titles)
    return submitted, receipt


def test_a_submitted_step_is_the_next_action_for_whoever_can_add_its_receipt(setup):
    store, service, owner, _, owner_token = setup
    bid = seed(service, owner)["id"]
    registration, receipt = submitted_registration(service, owner, bid)
    made = {
        role: service.add_principal(owner, {"name": f"Receipt {role}", "role": role, "business_ids": [bid]})[
            "token"
        ]
        for role in ("reviewer", "operator", "agent", "viewer")
    }
    guides = {
        role: guide_as(store, token, bid).json() for role, token in {"owner": owner_token, **made}.items()
    }
    rid = registration["id"]
    verify = {
        "do": "transition",
        "task_id": rid,
        "to": "verified",
        "label": "Add the receipt for Verify applicable registrations",
    }
    for role, guide in guides.items():
        assert guide["path"]["complete"], role
        explained = step(guide, "Verify applicable registrations")
        # Today's rule is unchanged: a submitted step waits on the outside body, never on you
        assert explained["your_move"] is None, role
        assert explained["why"] == ["It was submitted and waits for the outside body's receipt."], role
        setup_line = line(guide, "Setup")
        assert (setup_line["status"], setup_line["stop"], setup_line["focus"]["id"]) == (
            "waiting",
            None,
            rid,
        ), role
        assert [x["status"] for x in guide["lines"] if x["category"] != "Setup"] == ["done"] * 7, role
    # An owner and a reviewer can add the receipt: it is their next action, last of all kinds.
    for role in ("owner", "reviewer"):
        guide = guides[role]
        assert guide["next"] == {
            "from": "receipt",
            "stop": None,
            "task_id": rid,
            "action": verify,
            "label": "Add the receipt for Verify applicable registrations",
            "why": "Verify applicable registrations is with the outside body: add their receipt when it "
            "arrives.",
        }, role
        explained = step(guide, "Verify applicable registrations")
        assert (explained["next"], explained["waiting_on"]) == (verify, None), role
        assert line(guide, "Setup")["next"] == verify, role
    # An operator and an agent cannot verify it: nothing is offered, and what they are told is true.
    for role in ("operator", "agent"):
        guide = guides[role]
        assert guide["next"] == {
            "from": None,
            "stop": None,
            "task_id": None,
            "action": None,
            "label": "Nothing is waiting on you",
            "why": "Every step left waits on another step, or on someone else.",
        }, role
        explained = step(guide, "Verify applicable registrations")
        assert (explained["next"], explained["waiting_on"]) == (
            None,
            "It waits on the outside body's receipt, then an owner or reviewer to verify it.",
        ), role
        assert line(guide, "Setup")["next"] is None, role
    assert guides["viewer"]["next"]["label"] == "Nothing here is yours to do"
    assert line(guides["viewer"], "Setup")["next"] is None
    # The move the guide offers is one the service accepts; then nothing is left to do.
    move(service, owner, bid, registration, "verified", [receipt["id"]])
    guide = service.guide(owner, bid)
    assert (guide["next"]["from"], guide["next"]["label"]) == (None, "Nothing is waiting on you")
    assert line(guide, "Setup")["status"] == "done"


def test_a_step_ready_to_start_comes_before_a_receipt_that_waits_on_the_outside_body(setup):
    _, service, owner, _, _ = setup
    bid = seed(service, owner)["id"]
    registration, _ = submitted_registration(service, owner, bid, keep={"Approve the acquisition thesis"})
    thesis = task(service, owner, bid, "Approve the acquisition thesis")
    assert thesis["state"] == "pending"
    guide = service.guide(owner, bid)
    assert guide["path"]["complete"]
    assert guide["ready"] == [thesis["id"]]
    start = {
        "do": "transition",
        "task_id": thesis["id"],
        "to": "in_progress",
        "label": "Start work on Approve the acquisition thesis",
    }
    verify = {
        "do": "transition",
        "task_id": registration["id"],
        "to": "verified",
        "label": "Add the receipt for Verify applicable registrations",
    }
    # The owner may add the receipt, but it waits on the outside body: the step they can start now is next.
    assert guide["next"] == {
        "from": "ready",
        "stop": None,
        "task_id": thesis["id"],
        "action": start,
        "label": "Start work on Approve the acquisition thesis",
        "why": None,
    }
    assert step(guide, "Verify applicable registrations")["next"] == verify
    # The receipt is still offered on its own line, which has no step ready to start.
    lines = {
        x["category"]: (x["status"], x["focus"]["id"] if x["focus"] else None, x["next"])
        for x in guide["lines"]
    }
    assert lines == {
        "Market": ("done", None, None),
        "Finance": ("done", None, None),
        "Setup": ("waiting", registration["id"], verify),
        "Rights": ("done", None, None),
        "Delivery": ("done", None, None),
        "Sales to cash": ("done", None, None),
        "Operate": ("done", None, None),
        "Acquisition": ("ready", thesis["id"], start),
    }


def test_each_line_gives_its_own_receipt_and_the_first_in_line_order_is_next(setup):
    _, service, owner, _, _ = setup
    bid = seed(service, owner)["id"]
    # Submitted in this order; Finance comes before Setup on the page, so its receipt is first.
    (registration, banking), _ = submitted_steps(
        service, owner, bid, ["Verify applicable registrations", "Establish banking and payment access"]
    )
    guide = service.guide(owner, bid)
    assert guide["path"]["complete"]
    bank = {
        "do": "transition",
        "task_id": banking["id"],
        "to": "verified",
        "label": "Add the receipt for Establish banking and payment access",
    }
    register = {
        "do": "transition",
        "task_id": registration["id"],
        "to": "verified",
        "label": "Add the receipt for Verify applicable registrations",
    }
    assert guide["next"] == {
        "from": "receipt",
        "stop": None,
        "task_id": banking["id"],
        "action": bank,
        "label": "Add the receipt for Establish banking and payment access",
        "why": "Establish banking and payment access is with the outside body: add their receipt when it "
        "arrives.",
    }
    lines = {
        x["category"]: (x["status"], x["focus"]["id"] if x["focus"] else None, x["next"])
        for x in guide["lines"]
    }
    assert lines["Finance"] == ("waiting", banking["id"], bank)
    assert lines["Setup"] == ("waiting", registration["id"], register)


def test_a_receipt_whose_analysis_evidence_changed_says_what_holds_it(setup):
    """A submitted step can have a blocker: an analysis saved citing evidence nobody had reviewed yet,
    its closing submitted, then that evidence reviewed. Reviewing changes the evidence's version, so the
    analysis is out of date and verifying the closing is blocked. On a start-up business no stop of the
    path asks for an analysis, so the closing's receipt is the next action, and it says what holds it."""
    _, service, owner, bid, _ = setup
    notes = review(service, owner, bid, evidence(service, owner, bid, "Interview notes · synthetic"))
    receipt = review(service, owner, bid, external_receipt(service, owner, bid))
    for kind, title in (
        ("customer", "Synthetic buyer"),
        ("offer", "Synthetic offer"),
        ("decision", "Synthetic structure decision"),
    ):
        record(service, owner, bid, kind, title)
    service.save_forecast(
        owner,
        bid,
        key(),
        data(
            models.ForecastCreate, title="Synthetic forecast", inputs=models.ForecastInputs(), evidence_ids=[]
        ),
    )
    structure = task(service, owner, bid, "Decide the entity and ownership structure")
    for state in ("prepared", "approved", "verified"):
        structure = move(service, owner, bid, structure, state, [notes["id"]])
    rule_out_base(service, owner, bid, notes)
    listing = evidence(service, owner, bid, "Seller's listing · synthetic")
    analysis = deal_citing(service, owner, bid, listing, "Corner bakery")
    walk(service, owner, bid, analysis["id"], receipt, upto="closing")
    closing = task(service, owner, bid, "Verify agreement, funds and closing conditions")
    assert (closing["state"], closing["blockers"]) == ("submitted", [])
    review(service, owner, bid, listing)
    closing = task(service, owner, bid, "Verify agreement, funds and closing conditions")
    assert (closing["state"], closing["blockers"]) == ("submitted", [DEAL_BLOCKER])
    guide = service.guide(owner, bid)
    assert guide["path"]["complete"]
    fresh = {"do": "deal", "deal_id": analysis["id"], "label": "Save a fresh acquisition analysis"}
    assert guide["next"] == {
        "from": "receipt",
        "stop": None,
        "task_id": closing["id"],
        "action": fresh,
        "label": "Save a fresh acquisition analysis",
        "why": "Verify agreement, funds and closing conditions is with the outside body: add their receipt "
        "when it arrives. It cannot be approved until there is a fresh acquisition analysis: the evidence "
        "behind the saved one changed.",
    }


DEAL_BLOCKER = "Supporting deal evidence changed; create and review a fresh analysis"
DEAL_STEPS = [
    "Approve the acquisition thesis",
    "Reconcile earnings and working capital",
    "Review liabilities and transfer rights",
    "Review valuation, financing and downside",
    "Verify agreement, funds and closing conditions",
    "Verify the transition into operations",
    "Review results against the acquisition thesis",
]
OLDER_WHY = (
    "This step belongs to an acquisition analysis whose evidence changed. A newer analysis was saved after "
    "it. Saving another analysis does not clear this step."
)
RULE_OUT_UNLOCKS = [
    "An owner or reviewer rules it out as not applicable, with reviewed evidence.",
    "If you still want this deal, a fresh analysis of it is a new analysis with its own steps; these still "
    "need ruling out.",
]


def external_receipt(service, owner, bid):
    return service.add_evidence(
        owner,
        bid,
        key(),
        data(
            models.EvidenceCreate,
            title="Closing receipt · synthetic",
            content="SYNTHETIC confirmation from an outside body.",
            source_type="external_receipt",
        ),
    )


def deal_citing(service, owner, bid, item, target):
    return service.save_deal(
        owner,
        bid,
        key(),
        models.DealInputs(
            target=target,
            thesis="Synthetic thesis that cites one piece of evidence.",
            annual_revenue="210000",
            reported_ebitda="62000",
            purchase_price="180000",
            evidence_ids=[item["id"]],
        ).model_dump(mode="json", exclude_unset=True),
    )


def deal_steps(service, owner, bid, deal_id):
    """An analysis's steps in the catalog's order, as they are now."""
    found = [
        o
        for o in service.snapshot(owner, bid)["objects"]
        if o["kind"] == "task" and o["data"].get("deal_id") == deal_id
    ]
    assert [t["title"] for t in found] == DEAL_STEPS
    return found


def rule_out_base(service, owner, bid, item):
    """Rule out every unfinished step that belongs to no analysis, so the analyses' steps are what is left."""
    for t in service.snapshot(owner, bid)["objects"]:
        if t["kind"] == "task" and live(t) and not t["data"].get("deal_id"):
            if t["state"] not in {"verified", "not_applicable"}:
                move(service, owner, bid, t, "not_applicable", [item["id"]])


def walk(service, owner, bid, deal_id, receipt, upto=None):
    """Take an analysis's steps to verified in order, or up to the step keyed `upto`, left submitted."""
    order = ["pending", "prepared", "approved", "submitted", "verified"]
    for t in deal_steps(service, owner, bid, deal_id):
        last = t["data"]["key"] == upto
        states = ["prepared", "approved"] + (["submitted"] if t["data"]["external"] else [])
        for state in states + ([] if last else ["verified"]):
            if order.index(t["state"]) < order.index(state):
                t = move(service, owner, bid, t, state, [receipt["id"]])
        if last:
            return


def by_id(guide, tid):
    return next(s for s in guide["steps"] if s["id"] == tid)


def acquisition_lines(guide):
    return [
        (x["deal_id"], x["status"], x["focus"]["id"] if x["focus"] else None, x["next"])
        for x in guide["lines"]
        if x["category"] == "Acquisition"
    ]


def rule_out(t):
    return {
        "do": "transition",
        "task_id": t["id"],
        "to": "not_applicable",
        "label": f"Rule out {t['title']}: its analysis is out of date",
    }


def withdraw(service, owner, bid, item, note):
    return service.review_evidence(
        owner,
        bid,
        item["id"],
        key(),
        data(models.Review, note=note, expected_version=item["version"]),
        revoke=True,
    )


def test_an_older_analysis_whose_evidence_changed_is_ruled_out_not_saved_again(setup):
    _, service, owner, _, _ = setup
    bid = laundry(service, owner)
    notes = review(service, owner, bid, evidence(service, owner, bid, "Decision notes · synthetic"))
    receipt = review(service, owner, bid, external_receipt(service, owner, bid))
    rule_out_base(service, owner, bid, notes)

    # 1. The first analysis cites the first listing; its thesis is approved.
    first = review(service, owner, bid, evidence(service, owner, bid, "First listing · synthetic"))
    d1 = deal_citing(service, owner, bid, first, "Juniper Street Laundry")
    thesis1 = deal_steps(service, owner, bid, d1["id"])[0]
    for state in ("prepared", "approved"):
        thesis1 = move(service, owner, bid, thesis1, state, [notes["id"]])

    # 2. Withdrawing that listing reopens the thesis; every step of the first analysis is blocked by it.
    withdraw(service, owner, bid, first, "The seller withdrew the first listing.")
    old = deal_steps(service, owner, bid, d1["id"])
    assert [t["state"] for t in old] == ["needs_review"] + ["pending"] * 6
    assert all(DEAL_BLOCKER in t["blockers"] for t in old)

    # 3. A second analysis from a second listing; its thesis is approved and the path is complete.
    second = review(service, owner, bid, evidence(service, owner, bid, "Second listing · synthetic"))
    d2 = deal_citing(service, owner, bid, second, "Juniper Street Laundry · second listing")
    thesis2 = deal_steps(service, owner, bid, d2["id"])[0]
    for state in ("prepared", "approved"):
        thesis2 = move(service, owner, bid, thesis2, state, [notes["id"]])

    # 4. The first thesis is not offered "Save a fresh acquisition analysis" again: it is ruled out.
    guide = service.guide(owner, bid)
    assert guide["path"]["complete"]
    explained = by_id(guide, thesis1["id"])
    assert explained["next"] == rule_out(thesis1)
    assert explained["waiting_on"] is None
    assert explained["why"] == [OLDER_WHY, "It was reopened: Evidence withdrawn: First listing · synthetic."]
    assert explained["unlocks"] == RULE_OUT_UNLOCKS
    # Today's rule is unchanged: it is still listed as needing review, with this next action.
    assert explained["your_move"] == "prepared"
    # Current work comes first: the second thesis waits on the owner to verify it.
    verify2 = {
        "do": "transition",
        "task_id": thesis2["id"],
        "to": "verified",
        "label": "Verify: Approve the acquisition thesis",
    }
    assert guide["next"] == {
        "from": "waiting_on_you",
        "stop": None,
        "task_id": thesis2["id"],
        "action": verify2,
        "label": "Verify: Approve the acquisition thesis",
        "why": None,
    }
    assert acquisition_lines(guide) == [
        (d1["id"], "rule_out", thesis1["id"], rule_out(thesis1)),
        (d2["id"], "your_move", thesis2["id"], verify2),
    ]

    # 5. A third analysis, its thesis approved. The first thesis is still to be ruled out, not redone.
    d3 = deal_citing(service, owner, bid, second, "Juniper Street Laundry · third offer")
    thesis3 = deal_steps(service, owner, bid, d3["id"])[0]
    for state in ("prepared", "approved"):
        thesis3 = move(service, owner, bid, thesis3, state, [notes["id"]])
    guide = service.guide(owner, bid)
    assert by_id(guide, thesis1["id"])["next"] == rule_out(thesis1)
    assert (guide["next"]["from"], guide["next"]["task_id"]) == ("waiting_on_you", thesis2["id"])

    # The current analyses' steps come first while any waits on you or is ready to start.
    walk(service, owner, bid, d2["id"], receipt)
    guide = service.guide(owner, bid)
    assert (guide["next"]["from"], guide["next"]["task_id"]) == ("waiting_on_you", thesis3["id"])
    move(service, owner, bid, thesis3, "verified", [notes["id"]])
    guide = service.guide(owner, bid)
    diligence = deal_steps(service, owner, bid, d3["id"])[1]
    assert guide["next"] == {
        "from": "ready",
        "stop": None,
        "task_id": diligence["id"],
        "action": {
            "do": "transition",
            "task_id": diligence["id"],
            "to": "in_progress",
            "label": "Start work on Reconcile earnings and working capital",
        },
        "label": "Start work on Reconcile earnings and working capital",
        "why": None,
    }
    walk(service, owner, bid, d3["id"], receipt, upto="closing")
    closing3 = deal_steps(service, owner, bid, d3["id"])[4]
    assert closing3["state"] == "submitted"

    # Nothing current waits on the owner or is ready: ruling out comes next, before the third closing's
    # receipt, which waits on the outside body. One by one, each step of the first analysis.
    for n in range(7):
        t = deal_steps(service, owner, bid, d1["id"])[n]
        guide = service.guide(owner, bid)
        assert guide["next"] == {
            "from": "rule_out",
            "stop": None,
            "task_id": t["id"],
            "action": rule_out(t),
            "label": f"Rule out {DEAL_STEPS[n]}: its analysis is out of date",
            "why": OLDER_WHY,
        }, n
        assert acquisition_lines(guide)[0] == (d1["id"], "rule_out", t["id"], rule_out(t)), n
        move(service, owner, bid, t, "not_applicable", [notes["id"]])

    # The loop is over; the next action is current work again: the third closing's receipt.
    guide = service.guide(owner, bid)
    assert [t["state"] for t in deal_steps(service, owner, bid, d1["id"])] == ["not_applicable"] * 7
    assert guide["next"] == {
        "from": "receipt",
        "stop": None,
        "task_id": closing3["id"],
        "action": {
            "do": "transition",
            "task_id": closing3["id"],
            "to": "verified",
            "label": "Add the receipt for Verify agreement, funds and closing conditions",
        },
        "label": "Add the receipt for Verify agreement, funds and closing conditions",
        "why": "Verify agreement, funds and closing conditions is with the outside body: add their receipt "
        "when it arrives.",
    }
    assert acquisition_lines(guide)[0] == (d1["id"], "done", None, None)


def test_only_an_owner_or_reviewer_is_offered_the_rule_out(setup):
    store, service, owner, _, owner_token = setup
    bid = laundry(service, owner)
    notes = review(service, owner, bid, evidence(service, owner, bid, "Decision notes · synthetic"))
    receipt = review(service, owner, bid, external_receipt(service, owner, bid))
    rule_out_base(service, owner, bid, notes)
    first = review(service, owner, bid, evidence(service, owner, bid, "First listing · synthetic"))
    d1 = deal_citing(service, owner, bid, first, "Juniper Street Laundry")
    withdraw(service, owner, bid, first, "The seller withdrew the first listing.")
    second = review(service, owner, bid, evidence(service, owner, bid, "Second listing · synthetic"))
    d2 = deal_citing(service, owner, bid, second, "Juniper Street Laundry · second listing")
    walk(service, owner, bid, d2["id"], receipt, upto="closing")
    thesis1 = deal_steps(service, owner, bid, d1["id"])[0]
    assert (thesis1["state"], thesis1["blockers"]) == ("pending", [DEAL_BLOCKER])
    made = {
        role: service.add_principal(owner, {"name": f"Rule-out {role}", "role": role, "business_ids": [bid]})
        for role in ("reviewer", "operator", "agent", "viewer")
    }
    guides = {
        role: guide_as(store, token, bid).json()
        for role, token in {"owner": owner_token, **{r: p["token"] for r, p in made.items()}}.items()
    }
    for role in ("owner", "reviewer"):
        guide = guides[role]
        assert guide["next"] == {
            "from": "rule_out",
            "stop": None,
            "task_id": thesis1["id"],
            "action": rule_out(thesis1),
            "label": "Rule out Approve the acquisition thesis: its analysis is out of date",
            "why": OLDER_WHY,
        }, role
        explained = by_id(guide, thesis1["id"])
        assert (explained["next"], explained["waiting_on"]) == (rule_out(thesis1), None), role
        assert explained["why"] == [OLDER_WHY], role
        assert explained["unlocks"] == RULE_OUT_UNLOCKS, role
        assert acquisition_lines(guide)[0] == (d1["id"], "rule_out", thesis1["id"], rule_out(thesis1)), role
    for role in ("operator", "agent"):
        guide = guides[role]
        assert guide["next"] == {
            "from": None,
            "stop": None,
            "task_id": None,
            "action": None,
            "label": "Nothing is waiting on you",
            "why": "Every step left waits on another step, or on someone else.",
        }, role
        explained = by_id(guide, thesis1["id"])
        assert (explained["next"], explained["waiting_on"]) == (
            None,
            "It waits on an owner or reviewer to rule it out as not applicable: its analysis is out of date, "
            "and a newer analysis was saved after it.",
        ), role
        assert acquisition_lines(guide)[0] == (d1["id"], "rule_out", thesis1["id"], None), role
        # the server agrees: an operator or agent cannot rule a step out
        actor = store.authenticate(made[role]["token"])
        with pytest.raises(DomainError):
            move(service, actor, bid, thesis1, "not_applicable", [notes["id"]])
    viewer = guides["viewer"]
    assert viewer["next"]["action"] is None
    assert viewer["next"]["label"] == "Nothing here is yours to do"
    explained = by_id(viewer, thesis1["id"])
    assert (explained["next"], explained["waiting_on"]) == (
        None,
        "You can read this step. A person who can write to this business acts on it.",
    )
    assert acquisition_lines(viewer)[0] == (d1["id"], "rule_out", thesis1["id"], None)
    # The reviewer's rule-out is the move the service accepts.
    reviewer = store.authenticate(made["reviewer"]["token"])
    assert move(service, reviewer, bid, thesis1, "not_applicable", [notes["id"]])["state"] == "not_applicable"


def test_an_approved_step_of_an_older_analysis_is_reopened_then_ruled_out(setup):
    _, service, owner, _, _ = setup
    bid = laundry(service, owner)
    notes = review(service, owner, bid, evidence(service, owner, bid, "Decision notes · synthetic"))
    rule_out_base(service, owner, bid, notes)
    # Saved citing a listing nobody had reviewed; the thesis is approved; then the listing is reviewed,
    # which changes its version. Nothing reopens the thesis: it stays approved, with the blocker.
    listing = evidence(service, owner, bid, "Listing text · synthetic")
    d1 = deal_citing(service, owner, bid, listing, "Juniper Street Laundry")
    thesis1 = deal_steps(service, owner, bid, d1["id"])[0]
    for state in ("prepared", "approved"):
        thesis1 = move(service, owner, bid, thesis1, state, [notes["id"]])
    listing = review(service, owner, bid, listing)
    deal_citing(service, owner, bid, listing, "Juniper Street Laundry · fresh")
    thesis1 = deal_steps(service, owner, bid, d1["id"])[0]
    assert (thesis1["state"], thesis1["blockers"]) == ("approved", [DEAL_BLOCKER])
    explained = by_id(service.guide(owner, bid), thesis1["id"])
    # An approved step cannot be ruled out directly: it is reopened first.
    assert explained["next"] == {
        "do": "transition",
        "task_id": thesis1["id"],
        "to": "needs_review",
        "label": "Reopen Approve the acquisition thesis to rule it out: its analysis is out of date",
    }
    assert explained["why"] == [OLDER_WHY]
    assert explained["unlocks"] == [
        "An owner or reviewer reopens it, then rules it out as not applicable, with reviewed evidence.",
        RULE_OUT_UNLOCKS[1],
    ]
    thesis1 = move(service, owner, bid, thesis1, "needs_review")
    assert by_id(service.guide(owner, bid), thesis1["id"])["next"] == rule_out(thesis1)
    move(service, owner, bid, thesis1, "not_applicable", [notes["id"]])


STRUCTURE_STOP = {
    "n": 6,
    "key": "structure",
    "title": "Decide the structure",
    "detail": "Decide the entity and ownership structure, with a decision record.",
    "why": "Registrations, tax, banking, permits and the founders' arrangements all wait on this decision.",
    "done_when": "Done when Decide the entity and ownership structure is approved or further along, or ruled "
    "out as not applicable.",
    "line": "Setup",
}
DECIDE_STOP = {
    "n": 5,
    "key": "decide",
    "title": "Decide whether to keep going",
    "detail": "Approve the acquisition thesis. It is the first real decision.",
    "why": "The checks on the books and the contracts cannot be approved until this step is finished: "
    "verified, or ruled out as not applicable. Decide here before you pay anyone for those checks.",
    "done_when": "Done when Approve the acquisition thesis, for the newest analysis, is approved or further "
    "along, or ruled out as not applicable.",
    "line": "Acquisition",
}


def test_a_structure_decision_ruled_out_finishes_the_start_up_path(setup, client):
    """The founder already has an entity: the structure decision is ruled out as not applicable, with
    reviewed evidence. That is a reviewed decision the server counts as finished, so the stop is done,
    the path is complete and the business opens on Today, and the stop says it was ruled out."""
    _, service, owner, bid, _ = setup
    record(service, owner, bid, "customer", "Synthetic buyer")
    notes = review(service, owner, bid, evidence(service, owner, bid, "Interview notes · synthetic"))
    record(service, owner, bid, "offer", "Synthetic offer")
    service.save_forecast(
        owner,
        bid,
        key(),
        data(
            models.ForecastCreate, title="Synthetic forecast", inputs=models.ForecastInputs(), evidence_ids=[]
        ),
    )
    structure = task(service, owner, bid, "Decide the entity and ownership structure")
    guide = service.guide(owner, bid)
    assert done(guide) == [True, True, True, True, True, False]
    assert guide["path"]["stops"][5] == STRUCTURE_STOP | {
        "done": False,
        "ruled_out": None,
        "steps": [structure["id"]],
        "action": {"do": "add_record", "record_kind": "decision", "label": "Add a decision record"},
        "waiting_on": None,
    }

    move(service, owner, bid, structure, "not_applicable", [notes["id"]])
    guide = service.guide(owner, bid)
    assert done(guide) == [True] * 6
    assert (guide["path"]["done"], guide["path"]["complete"], guide["path"]["next_stop"]) == (6, True, None)
    assert guide["path"]["stops"][5] == STRUCTURE_STOP | {
        "done": True,
        "ruled_out": "Decide the entity and ownership structure was reviewed and ruled out as not applicable.",
        "steps": [structure["id"]],
        "action": None,
        "waiting_on": None,
    }
    # only that stop was decided by a ruling out; the others are done by their records
    assert [s["ruled_out"] for s in guide["path"]["stops"][:5]] == [None] * 5
    # the next action is no longer the path's: the stop is not left open
    assert guide["next"]["from"] != "path" and guide["next"]["stop"] is None
    # The page opens a business on Start here until its path is complete, and on Today after that: the
    # business's own read says complete.
    read = client.get(f"/api/businesses/{bid}").json()["guide"]["path"]
    assert (read["complete"], read["stops"][5]["done"], read["stops"][5]["ruled_out"]) == (
        True,
        True,
        "Decide the entity and ownership structure was reviewed and ruled out as not applicable.",
    )


def test_an_approved_structure_decision_is_done_and_not_ruled_out(setup):
    _, service, owner, bid, _ = setup
    record(service, owner, bid, "decision", "Synthetic structure decision")
    notes = review(service, owner, bid, evidence(service, owner, bid, "Interview notes · synthetic"))
    structure = task(service, owner, bid, "Decide the entity and ownership structure")
    for state in ("prepared", "approved"):
        structure = move(service, owner, bid, structure, state, [notes["id"]])
    stop = service.guide(owner, bid)["path"]["stops"][5]
    assert (stop["done"], stop["ruled_out"]) == (True, None)


def test_a_thesis_ruled_out_finishes_the_acquisition_path_and_an_older_one_does_not(setup, client):
    """The founder decided not to go ahead: the newest analysis's thesis is ruled out as not applicable.
    A thesis of an older analysis ruled out does not count: the stop follows the newest analysis."""
    _, service, owner, _, _ = setup
    bid = laundry(service, owner)
    listing = review(service, owner, bid, evidence(service, owner, bid))
    first = deal(service, owner, bid)
    second = deal(service, owner, bid)
    old = deal_steps(service, owner, bid, first["id"])[0]
    new = deal_steps(service, owner, bid, second["id"])[0]

    # the older analysis's thesis ruled out: the stop still waits on the newest one's
    move(service, owner, bid, old, "not_applicable", [listing["id"]])
    guide = service.guide(owner, bid)
    assert done(guide) == [True, True, True, True, False]
    assert guide["path"]["stops"][4] == DECIDE_STOP | {
        "done": False,
        "ruled_out": None,
        "steps": [new["id"]],
        "action": {
            "do": "transition",
            "task_id": new["id"],
            "to": "in_progress",
            "label": "Start work on Approve the acquisition thesis",
        },
        "waiting_on": None,
    }
    assert (guide["next"]["from"], guide["next"]["stop"]) == ("path", 5)

    # the newest analysis's thesis ruled out: the stop is done and says how
    move(service, owner, bid, new, "not_applicable", [listing["id"]])
    guide = service.guide(owner, bid)
    assert done(guide) == [True] * 5
    assert (guide["path"]["done"], guide["path"]["complete"], guide["path"]["next_stop"]) == (5, True, None)
    assert guide["path"]["stops"][4] == DECIDE_STOP | {
        "done": True,
        "ruled_out": "Approve the acquisition thesis was reviewed and ruled out as not applicable.",
        "steps": [new["id"]],
        "action": None,
        "waiting_on": None,
    }
    assert [s["ruled_out"] for s in guide["path"]["stops"][:4]] == [None] * 4
    assert guide["next"]["from"] != "path" and guide["next"]["stop"] is None
    read = client.get(f"/api/businesses/{bid}").json()["guide"]["path"]
    assert (read["complete"], read["stops"][4]["done"], read["stops"][4]["ruled_out"]) == (
        True,
        True,
        "Approve the acquisition thesis was reviewed and ruled out as not applicable.",
    )

    # Reopening the ruled-out thesis takes the stop back: it is not done, and not ruled out.
    new = deal_steps(service, owner, bid, second["id"])[0]
    move(service, owner, bid, new, "needs_review")
    stop = service.guide(owner, bid)["path"]["stops"][4]
    assert (stop["done"], stop["ruled_out"]) == (False, None)


def test_a_submitted_step_of_an_older_analysis_is_reopened_then_ruled_out(setup):
    """The server refuses submitted to not applicable, so a submitted step of an older analysis whose
    evidence changed is offered the reopen, and once reopened, the rule-out, which the service accepts."""
    _, service, owner, _, _ = setup
    bid = laundry(service, owner)
    notes = review(service, owner, bid, evidence(service, owner, bid, "Decision notes · synthetic"))
    receipt = review(service, owner, bid, external_receipt(service, owner, bid))
    rule_out_base(service, owner, bid, notes)
    # Saved citing a listing nobody had reviewed, its closing submitted; then the listing is reviewed,
    # which changes its version, and a newer analysis is saved. Nothing reopens the closing.
    listing = evidence(service, owner, bid, "Listing text · synthetic")
    d1 = deal_citing(service, owner, bid, listing, "Juniper Street Laundry")
    walk(service, owner, bid, d1["id"], receipt, upto="closing")
    listing = review(service, owner, bid, listing)
    deal_citing(service, owner, bid, listing, "Juniper Street Laundry · fresh")
    closing = deal_steps(service, owner, bid, d1["id"])[4]
    assert (closing["title"], closing["state"], closing["blockers"]) == (
        "Verify agreement, funds and closing conditions",
        "submitted",
        [DEAL_BLOCKER],
    )
    explained = by_id(service.guide(owner, bid), closing["id"])
    assert explained["next"] == {
        "do": "transition",
        "task_id": closing["id"],
        "to": "needs_review",
        "label": "Reopen Verify agreement, funds and closing conditions to rule it out: its analysis is out "
        "of date",
    }
    assert explained["why"] == [OLDER_WHY]
    assert explained["unlocks"] == [
        "An owner or reviewer reopens it, then rules it out as not applicable, with reviewed evidence.",
        RULE_OUT_UNLOCKS[1],
    ]
    # the server agrees: a submitted step cannot be ruled out directly
    with pytest.raises(DomainError):
        move(service, owner, bid, closing, "not_applicable", [notes["id"]])
    # The offered action, taken through the service, leads to the rule-out, which the service accepts.
    closing = move(service, owner, bid, closing, explained["next"]["to"])
    assert closing["state"] == "needs_review"
    explained = by_id(service.guide(owner, bid), closing["id"])
    assert explained["next"] == rule_out(closing)
    closing = move(service, owner, bid, closing, explained["next"]["to"], [notes["id"]])
    assert closing["state"] == "not_applicable"


def test_an_older_analysis_whose_evidence_is_current_keeps_its_normal_work(setup):
    """Two analyses, neither's evidence changed. The older one's second step waits only on its thesis,
    another step: it is not offered a rule-out, but its normal work, as on any analysis."""
    _, service, owner, _, _ = setup
    bid = laundry(service, owner)
    review(service, owner, bid, evidence(service, owner, bid))
    first = deal(service, owner, bid)
    deal(service, owner, bid)
    diligence = deal_steps(service, owner, bid, first["id"])[1]
    assert (diligence["title"], diligence["state"], diligence["blockers"]) == (
        "Reconcile earnings and working capital",
        "pending",
        ["Complete: Approve the acquisition thesis"],
    )
    guide = service.guide(owner, bid)
    explained = by_id(guide, diligence["id"])
    assert explained["next"] == {
        "do": "transition",
        "task_id": diligence["id"],
        "to": "in_progress",
        "label": "Start work on Reconcile earnings and working capital",
    }
    assert explained["why"] == [
        "It cannot be approved until Approve the acquisition thesis is finished. That step is not started."
    ]
    assert explained["unlocks"] == [
        "Finish Approve the acquisition thesis: verify it, or rule it out as not applicable."
    ]
    assert explained["waiting_on"] is None
    # the older analysis's line is ordinary work too, not a rule-out
    assert acquisition_lines(guide)[0][1] == "ready"
