"""Text typed by a person or an agent is printed literally: it cannot add, remove or restyle Markdown.

A CommonMark parser (with GFM tables and strikethrough, as most viewers render) is the judge. Every string
in the snapshot's business and objects is replaced, one at a time, by each hostile string, and the parsed
document is compared with the same field holding plain words.
"""

import copy
import functools
from collections import Counter
from uuid import uuid4

import pytest
from markdown_it import MarkdownIt

from foundry import models
from foundry.documents import TITLES, inline, render_document

MD = MarkdownIt("commonmark").enable(["table", "strikethrough"])
PLAIN = "Plain words"
MARKUP = {
    "link_open",
    "image",
    "html_inline",
    "html_block",
    "em_open",
    "strong_open",
    "s_open",
    "code_inline",
}
BACKTICKS = "`" * 3

HOSTILE = [
    "a | b",
    "a \\| b",
    "ends with backslash \\",
    "# heading",
    "  # heading after spaces",
    "first\n# second line",
    "first\n===",
    "---",
    "===",
    "-",
    "=",
    "***",
    "___",
    "- - -",
    "-- -",
    BACKTICKS,
    BACKTICKS + " python",
    "~~~",
    "> quote",
    "- item",
    "* item",
    "+ item",
    "1. item",
    "1) item",
    "<script>alert(1)</script>",
    "<div>",
    "<!-- comment",
    "<details><summary>x</summary>",
    "<https://example.invalid>",
    "<sam@example.invalid>",
    "a\r\nb",
    "a\tb",
    "| a | b |\n|---|---|\n| c | d |",
    "[link](http://example.invalid)",
    "![image](http://example.invalid/x.png)",
    "[x]: http://example.invalid",
    "[ref][x]",
    "`code | pipe`",
    "*em* and **strong**",
    "_em_ and __strong__",
    "__init__ and a__b__c",
    "~~struck~~",
    "&amp; &#35; &copy;",
    "Title ends with hash #",
    "###",
    "|",
    "\\",
    "two trailing spaces  ",
    "\\*not em\\* and \\_not em\\_",
]

ORDINARY = [
    "Harbor Books - spring plan (2026)",
    "Owner's note: rent is $1,200.50/month; 50% due on 1 March.",
    "R&D budget > marketing budget, but < payroll",
    "Use the file named customer_list_2026.csv",
    "See https://example.com/a_b/c-d?x=1&y=2#frag and https://example.com/~team/notes",
    "Is it ready? Yes! Email sam@example.com (cc: finance+ap@example.com)",
    "Step 2 of 3: sign, scan & file = done",
    "C# and F# developers",
]


def dump(cls, **kwargs):
    return cls(**kwargs).model_dump(mode="json")


def key():
    return str(uuid4())


KEPT_STEPS = {"offer", "rights", "structure", "founders"}


@pytest.fixture(scope="module")
def compact(tmp_path_factory):
    """A small synthetic business that reaches every printing path, as two (snapshot, deal) halves."""
    from foundry.service import Service
    from foundry.store import Store

    store = Store(tmp_path_factory.mktemp("text") / "private" / "foundry.db")
    a = store.authenticate(store.bootstrap()["token"])
    s = Service(store)
    bid = s.create_business(
        a,
        key(),
        dump(
            models.BusinessCreate,
            name="Harbor Books",
            concept="Month-end bookkeeping.",
            customer="Small firms",
        ),
    )["id"]

    def evidence(title, review):
        item = s.add_evidence(a, bid, key(), dump(models.EvidenceCreate, title=title, content=title + "."))
        if review:
            item = s.review_evidence(
                a, bid, item["id"], key(), dump(models.Review, note="Checked.", expected_version=1)
            )
        return item

    interview, quote = evidence("Interview notes", True), evidence("Draft quote", False)

    def record(kind, title, certainty, evidence_ids=(), **extra):
        payload = dump(
            models.RecordCreate,
            kind=kind,
            title=title,
            certainty=certainty,
            evidence_ids=list(evidence_ids),
            **extra,
        )
        return s.add_record(a, bid, key(), payload)

    record("customer", "Small firms", "verified", [interview["id"]], details="Two to ten staff.", owner="Ana")
    record("obligation", "Sales tax filing", "assumption", details="Quarterly.", due_date="2001-01-31")
    record("offer", "Monthly close", "reported", [quote["id"]])
    record("risk", "Clients may not switch", "assumption")
    s.save_forecast(
        a,
        bid,
        key(),
        dump(models.ForecastCreate, title="Pilot plan", inputs=models.ForecastInputs(months=12)),
    )
    deal = s.save_deal(
        a,
        bid,
        key(),
        dump(
            models.DealInputs,
            target="Corner shop",
            thesis="A synthetic target.",
            adjustments=[
                {"label": "Owner salary", "amount": 1000, "evidence_id": interview["id"]},
                {"label": "Unsupported add-back", "amount": 500},
            ],
        ),
    )

    def move(step, *states, note="Synthetic step.", evidence_ids=None):
        for state in states:
            snap = s.snapshot(a, bid)
            task = next(t for t in snap["objects"] if t["kind"] == "task" and t["data"]["key"] == step)
            s.transition(
                a,
                bid,
                task["id"],
                key(),
                dump(
                    models.TaskTransition,
                    state=state,
                    expected_version=task["version"],
                    note=note,
                    evidence_ids=[interview["id"]] if evidence_ids is None else evidence_ids,
                ),
            )

    move("structure", "in_progress", evidence_ids=[])
    move("structure", "needs_review", note="Founder may move abroad.", evidence_ids=[])
    move("offer", "prepared", "approved")
    move("rights", "prepared")
    snapshot = s.snapshot(a, bid)
    objects = snapshot["objects"]
    deal_step = next(t for t in objects if t["kind"] == "task" and t["data"]["deal_id"])
    # Two halves keep each parsed document small: the financial tables and the deal tables are only in
    # the second, so the many record and step fields are checked against shorter documents.
    work = [o for o in objects if o["kind"] in {"evidence", "customer", "offer", "obligation", "risk"}]
    work += [o for o in objects if o["kind"] == "task" and o["data"]["key"] in KEPT_STEPS]
    money = [o for o in objects if o["kind"] in {"evidence", "financial_plan", "deal"}] + [deal_step]
    halves = {}
    for name, kept in (("work", work), ("money", money)):
        half = copy.deepcopy({**snapshot, "objects": kept})
        halves[name] = half, next((o for o in half["objects"] if o["id"] == deal["id"]), None)
    return halves


def leaves(value, path=()):
    """Every string in a nested structure, as (container, key, path)."""
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for k, v in items:
        if isinstance(v, str):
            yield value, k, (*path, k)
        else:
            yield from leaves(v, (*path, k))


def text_of(token) -> str:
    return "".join(
        c.content if c.type in {"text", "code_inline"} else " " if c.type.endswith("break") else ""
        for c in token.children or []
    )


@functools.lru_cache(maxsize=4096)
def shape(content):
    tokens = MD.parse(content)
    blocks = [(t.type, t.tag) for t in tokens]
    markup = Counter(c.type for t in tokens for c in [t, *(t.children or [])] if c.type in MARKUP)
    return blocks, tuple(sorted(markup.items())), [text_of(t) for t in tokens if t.type == "inline"]


def render(template, snapshot, deal):
    return render_document(template, snapshot, deal)[1]


def substituted(compact, container, k, value, templates=TITLES):
    """Render templates with one field replaced; None marks a template that cannot render it."""
    snapshot, deal = compact
    original = container[k]
    container[k] = value
    try:
        out = {}
        for template in templates:
            try:
                out[template] = render(template, snapshot, deal)
            except (ArithmeticError, ValueError, TypeError, KeyError):
                out[template] = None
        return out
    finally:
        container[k] = original


@pytest.mark.parametrize("half", ["work", "money"])
def test_no_string_in_the_snapshot_can_change_document_structure_or_add_markup(compact, half):
    compact = compact[half]
    snapshot, deal = compact
    baseline = {t: render(t, snapshot, deal) for t in TITLES if t != "investment_memo" or deal}
    fields = [*leaves(snapshot["business"], ("business",)), *leaves(snapshot["objects"], ("objects",))]
    checked, failures = set(), []
    for container, k, path in fields:
        plain = substituted(compact, container, k, PLAIN)
        # Templates that do not read this field, or that cannot render it as words (a number or a date
        # parsed for arithmetic), are not printing free text from it.
        templates = [t for t in baseline if plain[t] is not None and plain[t] != baseline[t]]
        if not templates:
            continue
        checked.add(path)
        expected = {t: shape(plain[t])[:2] for t in templates}
        for hostile in HOSTILE:
            rendered = substituted(compact, container, k, hostile, templates)
            for t in templates:
                if rendered[t] is None or shape(rendered[t])[:2] != expected[t]:
                    failures.append((path, repr(hostile), t))
    assert failures == []
    # The walk reached free text in the business, records, evidence, steps (blockers included) and deals.
    printed = {p[-1] for p in checked}
    assert {"name", "concept", "customer", "title", "currency", "route", "jurisdiction"} <= printed
    blockers = [p for _, _, p in fields if "blockers" in p]
    assert set(blockers) <= checked and (blockers or half == "money")
    assert {
        "work": {"details", "owner", "due_date", "description", "note", "actor_name", "source_uri"},
        "money": {"thesis", "label", "reason", "basis", "base"},
    }[half] <= printed


def field(snapshot, deal, name):
    """The container and key of a field whose words must reach the reader unchanged."""

    def one(kind, **match):
        return next(
            o
            for o in snapshot["objects"]
            if o["kind"] == kind and all(o.get(k, o["data"].get(k)) == v for k, v in match.items())
        )

    def reopen():
        history = one("task", key="structure")["data"]["history"]
        return next(h for h in history if h["to"] == "needs_review")

    business = snapshot["business"]["data"]
    return {
        "record title": lambda: (one("customer"), "title"),
        "record details": lambda: (one("customer")["data"], "details"),
        "record owner": lambda: (one("customer")["data"], "owner"),
        "evidence title": lambda: (one("evidence", title="Interview notes"), "title"),
        "step title": lambda: (one("task", key="offer"), "title"),
        "step description": lambda: (one("task", key="offer")["data"], "description"),
        "deal title": lambda: (deal, "title"),
        "deal thesis": lambda: (deal["data"]["inputs"], "thesis"),
        "business name": lambda: (business, "name"),
        "concept": lambda: (business, "concept"),
        "customer text": lambda: (business, "customer"),
        "reopen reason": lambda: (reopen(), "note"),
    }[name]()


FIELDS = [
    "record title",
    "record details",
    "record owner",
    "evidence title",
    "step title",
    "step description",
    "deal title",
    "deal thesis",
    "business name",
    "concept",
    "customer text",
    "reopen reason",
]


@pytest.mark.parametrize("name", FIELDS)
def test_the_reader_sees_exactly_the_words_that_were_typed(compact, name):
    compact = compact["money" if name.startswith("deal") else "work"]
    snapshot, deal = compact
    container, k = field(snapshot, deal, name)
    plain = substituted(compact, container, k, PLAIN)
    for hostile in HOSTILE:
        words = " ".join(hostile.split())
        rendered = substituted(compact, container, k, hostile)
        seen = 0
        for t in TITLES:
            if plain[t] is None or PLAIN not in plain[t]:
                continue
            before, after = shape(plain[t])[2], shape(rendered[t])[2]
            assert len(before) == len(after)
            for was, now in zip(before, after, strict=True):
                if was != now:
                    seen += 1
                    assert now == was.replace(PLAIN, words), (name, hostile, t)
        assert seen, (name, hostile)


@pytest.mark.parametrize("text", ORDINARY)
def test_ordinary_text_is_printed_byte_for_byte(text):
    assert inline(text) == text
    record = {
        "id": "customer_" + "1" * 20,
        "kind": "customer",
        "title": text,
        "state": "reported",
        "version": 1,
        "data": {"details": text, "evidence_ids": []},
    }
    snapshot = {
        "business": {
            "id": "business_" + "0" * 20,
            "version": 1,
            "data": {
                "name": "Bare business",
                "concept": "A synthetic business.",
                "route": "start",
                "jurisdiction": "undecided",
                "currency": "USD",
                "customer": "",
                "is_demo": False,
            },
        },
        "objects": [record],
        "stats": {},
        "source_fingerprint": "f" * 64,
        "catalog": {},
    }
    lines = render_document("business_plan", snapshot)[1].splitlines()
    start = lines.index(f"### {text}")
    assert lines[start + 2] == text
    assert any(x.startswith(f"| {text} ") for x in lines)
