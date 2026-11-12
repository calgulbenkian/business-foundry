"""Text typed by a person or an agent is printed literally: it cannot add, remove or restyle Markdown.

A CommonMark parser (with GFM tables and strikethrough, as most viewers render) is the judge. Every value
in the snapshot, of any type, is replaced, one at a time, by each hostile string, and the parsed document is
compared with the same field holding plain words.
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
    "* * *",
    "_ _ _",
    "* starts the text",
    "_ starts the text",
    "the text ends *",
    "the text ends _",
    "a ** b ** c",
    "a __ b __ c",
    "a *b * c",
    "a * b* c",
    "a _b _ c",
    "a _ b_ c",
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
    # A snapshot built without per-obligation flags prints the overdue count from the stats instead.
    count = [{k: v for k, v in o.items() if k != "overdue"} for o in objects if o["kind"] == "obligation"]
    halves = {}
    for name, kept in (("work", work), ("money", money), ("count", count)):
        half = copy.deepcopy({**snapshot, "objects": kept})
        halves[name] = half, next((o for o in half["objects"] if o["id"] == deal["id"]), None)
    return halves


def leaves(value, path=()):
    """Every leaf of a nested structure, of any type (text, numbers, booleans, None), as (container, key, path)."""
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for k, v in items:
        if isinstance(v, dict | list):
            yield from leaves(v, (*path, k))
        else:
            yield value, k, (*path, k)


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
            except (ArithmeticError, ValueError, TypeError, KeyError, AttributeError):
                out[template] = None
        return out
    finally:
        container[k] = original


@pytest.mark.parametrize("half", ["work", "money", "count"])
def test_no_value_in_the_snapshot_can_change_document_structure_or_add_markup(compact, half):
    compact = compact[half]
    snapshot, deal = compact
    baseline = {t: render(t, snapshot, deal) for t in TITLES if t != "investment_memo" or deal}
    fields = list(leaves(snapshot))
    assert {"stats", "source_fingerprint", "catalog"} <= {p[0] for _, _, p in fields}
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
    # The walk reached free text in the business, records, evidence, steps (blockers included) and deals,
    # and the values the service writes itself: versions, the fingerprint, the horizon and the count.
    printed = {p[-1] for p in checked}
    assert {"name", "concept", "customer", "currency", "route", "jurisdiction"} <= printed
    assert {("business", "version"), ("source_fingerprint",)} <= checked
    blockers = [p for _, _, p in fields if "blockers" in p]
    assert set(blockers) <= checked and (blockers or half != "work")
    assert {
        "work": {"title", "details", "owner", "due_date", "description", "note", "actor_name", "source_uri"},
        "money": {"title", "thesis", "label", "reason", "basis", "base"},
        "count": set(),
    }[half] <= printed
    if half == "money":
        assert any(p[-2:] == ("inputs", "months") for p in checked)
    if half == "count":
        assert ("stats", "overdue_obligations") in checked


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


def bare(record):
    return {
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


def assumption(title, details="", **data):
    return {
        "id": "customer_" + "1" * 20,
        "kind": "customer",
        "title": title,
        "state": "assumption",
        "version": 1,
        "data": {"details": details, "evidence_ids": [], **data},
    }


def shown(content):
    """The text a parser shows, one line per paragraph, heading or list item, and any markup it found."""
    tokens = MD.parse(content)
    markup = [c.type for t in tokens for c in [t, *(t.children or [])] if c.type in MARKUP]
    return "\n".join(text_of(t) for t in tokens if t.type == "inline"), markup


SPACED = ["Cost * quantity", "Hours * rate = fee", "a _ b"]


@pytest.mark.parametrize("text", SPACED)
def test_an_asterisk_or_underscore_with_a_space_on_each_side_is_printed_as_typed(text):
    assert inline(text) == text
    snapshot = bare(assumption(text, details=text))
    plan = render_document("business_plan", snapshot)[1]
    lines = plan.splitlines()
    start = lines.index(f"### {text}")  # as a title
    assert lines[start + 2] == text  # as details
    report = render_document("readiness_report", snapshot)[1]
    assert f"- Customer: {text}" in report.splitlines()  # as a list item
    text_shown, markup = shown(plan)
    assert markup == [] and f"\n{text}\n" in text_shown and f" {text} " in text_shown
    text_shown, markup = shown(report)
    assert markup == [] and f"\nCustomer: {text}\n" in text_shown


@pytest.mark.parametrize(
    ("owner", "due", "line"),
    [
        ("Cost * rate *", "* tax * due", "Owner: Cost * rate \\*. Due: \\* tax * due."),
        ("Cost _ rate _", "_ tax _ due", "Owner: Cost _ rate \\_. Due: \\_ tax _ due."),
    ],
)
def test_two_typed_fields_in_one_paragraph_cannot_pair_into_emphasis(owner, due, line):
    plan = render_document("business_plan", bare(assumption("Pilot buyers", owner=owner, due_date=due)))[1]
    assert line in plan.splitlines()
    text_shown, markup = shown(plan)
    assert markup == []
    assert f" Owner: {owner}. Due: {due}. " in text_shown


@pytest.mark.parametrize(
    ("text", "printed"),
    [
        ("* item", "\\* item"),
        ("_ item", "\\_ item"),
        ("Price*", "Price\\*"),
        ("Price *", "Price \\*"),
        ("Price _", "Price \\_"),
        ("a ** b", "a \\*\\* b"),
        ("a __ b", "a \\_\\_ b"),
        ("a *b c", "a \\*b c"),
        ("a b* c", "a b\\* c"),
        ("a _b c", "a \\_b c"),
        ("a b_ c", "a b\\_ c"),
        ("* * *", "\\* * \\*"),
        ("_ _ _", "\\_ _ \\_"),
        ("snake_case_name", "snake_case_name"),
    ],
)
def test_other_asterisks_and_underscores_keep_their_backslash(text, printed):
    # Behaviour this round leaves unchanged: only a character with a space on each side loses its backslash.
    assert inline(text) == printed


@pytest.mark.parametrize(
    ("text", "printed"),
    [
        ("Invoice [draft]", "Invoice \\[draft]"),
        ("Run `make` first", "Run \\`make\\` first"),
        ("Cash | card", "Cash \\| card"),
    ],
)
def test_brackets_backticks_and_pipes_are_always_escaped(text, printed):
    # Behaviour this round leaves unchanged: what these mean can depend on text printed after them.
    assert inline(text) == printed
    plan = render_document("business_plan", bare(assumption(text, details=text)))[1]
    assert f"### {printed}" in plan.splitlines() and printed in plan.splitlines()
    text_shown, markup = shown(plan)
    assert markup == []
    assert f"\n{text}\n" in text_shown and f" {text} " in text_shown


@pytest.mark.parametrize("control", ["\x00", "\x1b", "\x07", "\x7f", "\x80"])
def test_a_control_character_is_printed_as_the_replacement_character(control):
    assert inline(f"hello{control}world") == "hello�world"
    record = assumption(f"Pilot{control}buyers", details=f"Two{control}firms")
    plan = render_document("business_plan", bare(record))[1]
    assert "### Pilot�buyers" in plan.splitlines() and "Two�firms" in plan.splitlines()
    assert control not in plan
    text_shown, _ = shown(plan)
    assert "\nPilot�buyers\n" in text_shown and " Two�firms " in text_shown
    # Control characters that count as whitespace still collapse to one space.
    for space in ("\t", "\n", "\x85", "\x1f"):
        assert inline(f"hello{space}world") == "hello world"
        assert inline(f"hello {space} world{control}") == "hello world�"
