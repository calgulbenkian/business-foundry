"""Deterministic, versioned working documents. No fabricated business facts."""

from decimal import Decimal

from foundry.finance import period_rollups

TITLES = {
    "business_plan": "Business plan",
    "structure_options": "Structure decision brief",
    "contract_brief": "Customer contract brief",
    "operating_manual": "Operating manual",
    "readiness_report": "Readiness report",
    "investment_memo": "Investment memo",
}


def cell(value) -> str:
    """Make any value safe for a single GitHub-Flavored Markdown table cell."""
    text = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()
    return text.replace("|", "\\|")


def fmt_money(value) -> str:
    return f"{Decimal(str(value)):,.2f}"


def fmt_share(value) -> str:
    return f"{Decimal(str(value)):.1%}"


def fmt_rate(value) -> str:
    return f"{Decimal(str(value)):.2%}"


def fmt_table(headers: list[str], rows: list[list[str]], numeric_cols: set[int] = frozenset()) -> list[str]:
    """Render a padded GFM table. Every cell is escaped; numeric columns are right-aligned."""
    body = [[cell(c) for c in headers]] + [[cell(c) for c in row] for row in rows]
    widths = [max(3, *(len(r[i]) for r in body)) for i in range(len(headers))]

    def line(values):
        padded = [
            v.rjust(widths[i]) if i in numeric_cols else v.ljust(widths[i]) for i, v in enumerate(values)
        ]
        return "| " + " | ".join(padded) + " |"

    rule = (
        "| "
        + " | ".join(
            "-" * (widths[i] - 1) + ":" if i in numeric_cols else "-" * widths[i] for i in range(len(headers))
        )
        + " |"
    )
    return [line(body[0]), rule, *(line(r) for r in body[1:]), ""]


SCENARIOS = (("base", "Base"), ("downside", "Downside"), ("upside", "Upside"))

SUMMARY_ROWS = (
    ("total_revenue", "Total revenue", fmt_money),
    ("total_net_income", "Total net income", fmt_money),
    ("ending_cash", "Ending cash", fmt_money),
    ("minimum_cash", "Minimum cash", fmt_money),
    ("additional_cash_needed", "Additional cash needed", fmt_money),
    (
        "first_shortfall_month",
        "First shortfall month",
        lambda v: f"Month {v}" if v is not None else "None modeled",
    ),
    ("ending_debt", "Ending debt", fmt_money),
    ("statements_balance", "Statements balance", lambda v: "Yes" if v else "No"),
)

INCOME_ROWS = (
    ("revenue", "Revenue"),
    ("cost_of_sales", "Cost of sales"),
    ("fixed_costs", "Fixed costs"),
    ("ebitda", "EBITDA"),
    ("depreciation", "Depreciation"),
    ("interest", "Interest"),
    ("tax", "Tax"),
    ("net_income", "Net income"),
)

CASH_ROWS = (
    ("receipts", "Receipts"),
    ("operating_cash_flow", "Operating cash flow"),
    ("capex", "Capital spending"),
    ("debt_principal", "Debt principal"),
    ("net_cash_flow", "Net cash flow"),
)

BALANCE_ROWS = (
    ("cash", "Cash"),
    ("receivables", "Receivables"),
    ("net_fixed_assets", "Fixed assets, net"),
    ("total_assets", "Total assets"),
    ("debt", "Debt"),
    ("equity", "Equity"),
)

INPUT_ROWS = (
    ("months", "Horizon (months)", str),
    ("opening_equity", "Opening equity", fmt_money),
    ("opening_debt", "Opening debt", fmt_money),
    ("monthly_units", "Monthly units", str),
    ("unit_price", "Unit price", fmt_money),
    ("unit_cost", "Unit cost", fmt_money),
    ("monthly_growth", "Monthly growth", fmt_rate),
    ("monthly_retention", "Monthly unit retention", fmt_rate),
    ("fixed_costs", "Fixed costs per month", fmt_money),
    ("fixed_cost_step", "Fixed-cost step per month", fmt_money),
    (
        "fixed_cost_step_month",
        "Fixed-cost step start month",
        lambda v: f"Month {v}" if v is not None else "None",
    ),
    ("receivable_days", "Collection period (days)", str),
    ("monthly_capex", "Capital spending per month", fmt_money),
    ("asset_life_months", "Asset life (months)", str),
    ("tax_rate", "Tax rate", fmt_rate),
    ("annual_interest_rate", "Annual interest rate", fmt_rate),
    ("monthly_principal", "Debt principal per month", fmt_money),
    ("discount_rate", "Discount rate", fmt_rate),
    ("terminal_growth", "Terminal growth", fmt_rate),
    ("excess_cash", "Excess cash", fmt_money),
)

DCF_NOT_AVAILABLE = "Not available — needs at least 12 months and positive terminal cash flow"


def financial_section(p, currency) -> list[str]:
    data = p["data"]
    scenarios = data["scenarios"]
    out = [
        f"Model: {cell(p['title'])}. Currency: {currency}. "
        f"Horizon: {data['inputs']['months']} months. These are forecasts, not booked actuals.",
        "",
        "### Scenario comparison",
        "",
    ]
    out += fmt_table(
        ["Metric", *(label for _, label in SCENARIOS)],
        [
            [label, *(fmt(scenarios[c]["summary"][key]) for c, _ in SCENARIOS)]
            for key, label, fmt in SUMMARY_ROWS
        ],
        {1, 2, 3},
    )
    definitions = data.get("scenario_definitions", {})
    out += [f"- {label}: {cell(definitions.get(c, ''))}" for c, label in SCENARIOS] + [""]
    rollups = period_rollups(scenarios["base"]["months"])
    periods = ["Line item", *(r["period"] for r in rollups)]
    numeric = set(range(1, len(rollups) + 1))
    out += ["Period figures sum cent-rounded months and can differ from scenario totals by cents.", ""]
    for heading, spec in (
        ("Base income statement by period", INCOME_ROWS),
        ("Base cash flow by period", CASH_ROWS),
        ("Base closing balance sheet by period", BALANCE_ROWS),
    ):
        out += [f"### {heading}", ""]
        out += fmt_table(
            periods, [[label, *(fmt_money(r[k]) for r in rollups)] for k, label in spec], numeric
        )
    out += ["### Discounted cash flow (DCF)", ""]
    rows, basis = [], None
    for c, label in SCENARIOS:
        dcf = scenarios[c]["summary"].get("dcf")
        if dcf is None:
            rows.append([label, DCF_NOT_AVAILABLE, "—", "—", "—", "—"])
            continue
        basis = basis or dcf.get("basis")
        share = dcf["terminal_share"]
        rows.append(
            [
                label,
                fmt_money(dcf["enterprise_value"]),
                fmt_money(dcf["equity_value"]),
                fmt_money(dcf["present_value_of_forecast"]),
                fmt_money(dcf["present_value_of_terminal"]),
                fmt_share(share) if share is not None else "Not meaningful",
            ]
        )
    out += fmt_table(
        [
            "Scenario",
            "Enterprise value",
            "Equity value",
            "PV of forecast",
            "PV of terminal",
            "Terminal share",
        ],
        rows,
        {1, 2, 3, 4, 5},
    )
    if basis:
        out += [f"DCF basis: {basis}", ""]
    inputs = data["inputs"]
    out += ["### Model inputs", ""]
    out += fmt_table(
        ["Input", "Value"],
        [[label, fmt(inputs[key])] for key, label, fmt in INPUT_ROWS if key in inputs],
        {1},
    )
    out += ["### Model assumptions", ""]
    out += [f"- {a}" for a in data.get("assumptions", [])]
    out += [
        "",
        f"Full monthly statements are stored in the financial plan {cell(p['title'])} "
        "(Financial model view and JSON export; see the reference index).",
        "",
    ]
    return out


def range_text(values) -> str:
    if values is None:
        return "Not meaningful — normalized EBITDA is not positive"
    low, high = values
    return f"{fmt_money(low)} – {fmt_money(high)}"


def coverage(value) -> str:
    return "No debt service" if value is None else f"{Decimal(str(value)):.2f}x"


def deal_section(deal, snapshot, currency, evidence_name=lambda eid: eid) -> list[str]:
    a, inp = deal["data"]["analysis"], deal["data"]["inputs"]
    snap_deal = next((o for o in snapshot["objects"] if o["id"] == deal["id"]), None)
    out = []
    if snap_deal and snap_deal.get("stale"):
        out += [
            "WARNING: Supporting evidence has changed since this deal analysis was saved. "
            "Run a revised analysis before relying on these figures.",
            "",
        ]
    out += [f"Currency: {currency}.", "", "### Earnings normalization", ""]
    rows = [
        ["Reported EBITDA", fmt_money(inp["reported_ebitda"]), ""],
        ["Less: replacement management", fmt_money(inp["replacement_management"]), ""],
    ]
    rows += [
        [
            f"Add: {item['label']}",
            fmt_money(item["amount"]),
            evidence_name(item["evidence_id"]) if item.get("evidence_id") else "",
        ]
        for item in a["accepted_adjustments"]
    ]
    rows.append(["Normalized EBITDA", fmt_money(a["normalized_ebitda"]), ""])
    out += fmt_table(["Item", "Amount", "Evidence"], rows, {1})
    out += ["### Excluded adjustments", ""]
    if a["excluded_adjustments"]:
        out += fmt_table(
            ["Adjustment", "Amount", "Evidence", "Reason"],
            [
                [
                    x["label"],
                    fmt_money(x["amount"]),
                    evidence_name(x["evidence_id"]) if x.get("evidence_id") else "None",
                    x["reason"],
                ]
                for x in a["excluded_adjustments"]
            ],
            {1},
        )
    else:
        out += ["None — no unsupported adjustments were submitted.", ""]
    out += ["### Valuation range", ""]
    multiples = f"{Decimal(str(inp['multiple_low'])):.2f}x – {Decimal(str(inp['multiple_high'])):.2f}x"
    out += fmt_table(
        ["Measure", "Value"],
        [
            ["Multiple range", multiples],
            ["Assumed enterprise value range", range_text(a["enterprise_value_range"])],
            ["Cash in valuation equity bridge", fmt_money(inp["cash_acquired"])],
            ["Debt in valuation equity bridge", fmt_money(inp["debt_assumed"])],
            ["Assumed equity value range", range_text(a["equity_value_range"])],
        ],
        {1},
    )
    su = a["sources_and_uses"]
    out += ["### Sources and uses", ""]
    out += fmt_table(
        ["Category", "Item", "Amount"],
        [
            [group, label, fmt_money(su[key])]
            for group, key, label in (
                ("Uses", "purchase_price", "Purchase price"),
                ("Uses", "fees", "Transaction fees"),
                ("Uses", "working_capital", "Working capital"),
                ("Uses", "integration_cost", "Integration cost"),
                ("Uses", "total_uses", "Total uses"),
                ("Sources", "senior_debt", "Senior debt"),
                ("Sources", "seller_note", "Seller note"),
                ("Sources", "buyer_equity_required", "Buyer equity required"),
            )
        ],
        {2},
    )
    out += [
        "### Debt service and scenario coverage",
        "",
        f"Annual debt service: {fmt_money(a['annual_debt_service'])} {currency}.",
        "",
    ]
    out += fmt_table(
        [
            "Case",
            "EBITDA",
            "Cash available for debt service",
            "Cash after debt service",
            "Coverage ratio",
            "Cash shortfall",
        ],
        [
            [
                label,
                fmt_money(a["scenarios"][c]["ebitda"]),
                fmt_money(a["scenarios"][c]["cash_available_for_debt_service"]),
                fmt_money(a["scenarios"][c]["cash_after_debt_service"]),
                coverage(a["scenarios"][c]["coverage_ratio"]),
                "Yes" if a["scenarios"][c]["cash_shortfall"] else "No",
            ]
            for c, label in SCENARIOS
        ],
        {1, 2, 3, 4},
    )
    out += ["### Screening limitations", ""]
    out += [f"- {x}" for x in a.get("limitations", [])] + [""]
    return out


def inline(value) -> str:
    """Make user text safe on one Markdown line: no line breaks, no pipes, no leading heading marker."""
    text = " ".join(cell(value).split())
    return "\\" + text if text.startswith("#") else text


KIND_LABELS = {
    "customer": "Customer",
    "offer": "Offer",
    "asset": "Resource or right",
    "contract": "Contract",
    "obligation": "Obligation",
    "role": "Role",
    "entity": "Entity",
    "person": "Person",
    "ownership": "Ownership",
    "decision": "Decision",
    "risk": "Risk",
}
OTHER_KIND_LABELS = {
    "business": "Business",
    "task": "Step",
    "evidence": "Evidence",
    "financial_plan": "Financial plan",
    "deal": "Acquisition analysis",
    "invoice": "Invoice",
    "payment": "Payment",
    "document": "Document",
}

CERTAINTY = {
    "assumption": "Assumption — a working belief that nobody has confirmed yet.",
    "reported": "Reported — stated by the business or a source, not yet confirmed against reviewed evidence.",
    "verified": "Verified — a person confirmed it against reviewed evidence.",
}

# Step states in the order they are counted. Finished states first, then open work.
FINISHED_STATES = {"verified": "Verified as done", "not_applicable": "Confirmed not applicable"}
OPEN_STATES = {
    "needs_review": "Sent back for review",
    "submitted": "Submitted, waiting for confirmation",
    "approved": "Approved, waiting for the next action",
    "prepared": "Prepared, waiting for approval",
    "in_progress": "In progress",
    "pending": "Not started",
}
REPLACED_STATE = "superseded"
READY_STATES = {"pending", "in_progress", "needs_review"}

CHECKLIST_LIMITATION = (
    "A completed checklist reflects recorded reviews. It does not establish legal compliance, "
    "commercial viability or an independently audited result."
)


def upper_first(text) -> str:
    return text[:1].upper() + text[1:]


def kind_label(kind) -> str:
    return KIND_LABELS.get(kind) or OTHER_KIND_LABELS.get(kind) or inline(kind)


def state_label(state) -> str:
    known = FINISHED_STATES.get(state) or OPEN_STATES.get(state)
    return known or f'In a state this version does not recognise ("{inline(state)}")'


def step_class(state) -> str:
    if state in FINISHED_STATES:
        return "finished"
    if state in OPEN_STATES:
        return "open"
    return "replaced" if state == REPLACED_STATE else "unknown"


def review_standing(evidence) -> str:
    """Plain-words review standing of one evidence item; None means it is not in the snapshot."""
    if evidence is None:
        return "unknown — this evidence is not in the snapshot"
    review = evidence["data"].get("review") or {}
    who = inline(review.get("actor_name") or "a reviewer")
    when = str(review.get("at") or "")[:10]
    on = f" on {when}" if when else ""
    if evidence["state"] == "verified":
        return f"reviewed by {who}{on}"
    if evidence["state"] == "rejected":
        return f"rejected on review by {who}{on}"
    if evidence["state"] == "unverified":
        return "not yet reviewed"
    return f'in the unrecognised review state "{inline(evidence["state"])}"'


def latest_reopen_reason(task):
    history = task["data"].get("history")
    for entry in reversed(history if isinstance(history, list) else []):
        if isinstance(entry, dict) and entry.get("to") == "needs_review":
            return entry.get("note") or None
    return None


class Render:
    """Collects the ids a document mentions so the reference index can trace every one."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.objects = snapshot["objects"]
        self.by_id = {o["id"]: o for o in self.objects}
        self.mentioned = {}

    def mention(self, object_id, kind="evidence"):
        """Record a mention; kind labels an id that is missing from the snapshot."""
        self.mentioned.setdefault(object_id, kind)
        return self.by_id.get(object_id)

    def evidence_name(self, evidence_id) -> str:
        evidence = self.mention(evidence_id)
        return evidence["title"] if evidence else "Unknown evidence (not in this snapshot)"

    def step_name(self, task) -> str:
        self.mention(task["id"])
        name = inline(task["title"])
        deal_id = task["data"].get("deal_id")
        if deal_id:
            deal = self.mention(deal_id, "deal")
            name += f" (acquisition: {inline(deal['title'])})" if deal else " (acquisition)"
        return name

    def record_block(self, o) -> list[str]:
        self.mention(o["id"])
        data = o["data"]
        out = [f"### {inline(o['title'])}", "Certainty: " + CERTAINTY.get(o["state"], inline(o["state"]))]
        if inline(data.get("details", "")):
            out.append(inline(data["details"]))
        out.append(
            f"Owner: {inline(data.get('owner') or 'Unassigned')}. Due: {inline(data.get('due_date') or 'Not set')}."
        )
        evidence_ids = data.get("evidence_ids") or []
        if not evidence_ids:
            out.append("Evidence: None attached.")
        else:
            out.append("Evidence:")
            for eid in evidence_ids:
                evidence = self.mention(eid)
                name = inline(evidence["title"]) if evidence else "Unknown evidence"
                out.append(f"- {name}: {review_standing(evidence)}.")
        return out + [""]

    def reference_index(self) -> list[str]:
        business = self.snapshot["business"]
        rows = [[business["data"]["name"], "Business", business["id"], business["version"], "—", "—"]]
        known = [o for o in self.objects if o["id"] in self.mentioned]
        missing = [i for i in self.mentioned if i not in self.by_id]
        for o in known:
            evidence = o["kind"] == "evidence"
            rows.append(
                [
                    o["title"],
                    kind_label(o["kind"]),
                    o["id"],
                    o["version"],
                    upper_first(review_standing(o)) if evidence else "—",
                    (o["data"].get("content_sha256") or "")[:12] or "—" if evidence else "—",
                ]
            )
        for i in missing:
            kind = self.mentioned[i]
            review = "Not in this snapshot" if kind == "evidence" else "—"
            rows.append([f"Unknown {kind_label(kind).lower()}", kind_label(kind), i, "—", review, "—"])
        return [
            "## Reference index",
            "",
            "Machine identifiers for every item this document mentions, for tracing it back to the workspace.",
            "",
            *fmt_table(["Title", "Kind", "Id", "Version", "Evidence review", "Content hash (start)"], rows),
        ]


def step_overview(r, tasks) -> list[str]:
    """Plain-words counts of steps by state. Replaced steps are listed, never counted."""
    counted = [t for t in tasks if step_class(t["state"]) != "replaced"]
    replaced = [t for t in tasks if step_class(t["state"]) == "replaced"]
    for t in tasks:
        r.mention(t["id"])
    out = []
    if not counted:
        out.append("No steps are recorded.")
    else:
        groups = {
            c: sum(step_class(t["state"]) == c for t in counted) for c in ("finished", "open", "unknown")
        }
        tracked = "1 step is tracked" if len(counted) == 1 else f"{len(counted)} steps are tracked"
        summary = f"{tracked}: {groups['finished']} finished and {groups['open']} still open"
        if groups["unknown"]:
            summary += f", plus {groups['unknown']} in a state this version does not recognise"
        out += [summary + ".", ""]
        states = list(FINISHED_STATES) + list(OPEN_STATES)
        states += sorted({t["state"] for t in counted} - set(states), key=str)
        for state in states:
            n = sum(t["state"] == state for t in counted)
            if n:
                out.append(f"- {state_label(state)}: {n}")
    if replaced:
        out += [
            "",
            "Replaced steps (no longer part of this plan): "
            + "; ".join(r.step_name(t) for t in replaced)
            + ".",
        ]
    return out + [""]


def sentence(value) -> str:
    text = inline(value)
    return text if text.endswith((".", "!", "?")) else text + "."


def needs_first(task) -> str:
    """A step's blockers in plain words: "Complete: X" names the unfinished step X."""
    return "; ".join(inline(b.removeprefix("Complete: ")) for b in task["blockers"])


def bullets(items, empty) -> list[str]:
    return [*(f"- {x}" for x in items), ""] if items else [empty, ""]


def readiness_audit(r, tasks, currency) -> list[str]:
    stats = r.snapshot.get("stats") or {}
    out = ["## Where things stand", ""] + step_overview(r, tasks)

    def of(*states):
        return [t for t in tasks if t["state"] in states]

    waiting = [o for o in r.objects if o["kind"] == "evidence" and o["state"] == "unverified"]
    for e in waiting:
        r.mention(e["id"])
    out += ["## What needs a person now", "", "### Evidence waiting for review", ""]
    out += bullets([inline(e["title"]) for e in waiting], "No evidence is waiting for review.")
    out += ["### Steps prepared and waiting for approval", ""]
    out += bullets([r.step_name(t) for t in of("prepared")], "No step is waiting for approval.")
    out += ["### Steps waiting for the next human action", ""]
    actions = []
    for t in of("approved", "submitted"):
        if t["state"] == "submitted":
            action = "submitted; confirm the result against the reviewed receipt"
        elif t["data"].get("external"):
            action = "approved; record the external submission with its receipt"
        else:
            action = "approved; confirm it as verified"
        actions.append(f"{r.step_name(t)} — {action}.")
    out += bullets(actions, "No approved or submitted step is waiting.")
    out += ["### Steps sent back for review", ""]
    out += bullets(
        [
            f"{r.step_name(t)} — reason: {sentence(latest_reopen_reason(t) or 'no reason was recorded')}"
            for t in of("needs_review")
        ],
        "No step has been sent back for review.",
    )
    unknown = [t for t in tasks if step_class(t["state"]) == "unknown"]
    if unknown:
        out += ["### Steps in a state this version does not recognise", ""]
        out += bullets(
            [f"{r.step_name(t)} — {state_label(t['state'])}. Check it by hand." for t in unknown], ""
        )

    out += ["## What can be worked on next", ""]
    out += bullets(
        [r.step_name(t) for t in tasks if t["state"] in READY_STATES and not t.get("blockers")],
        "No open step is free of prerequisites right now.",
    )
    out += ["## What is blocked", ""]
    out += bullets(
        [
            f"{r.step_name(t)} — needs first: {needs_first(t)}."
            for t in tasks
            if step_class(t["state"]) == "open" and t.get("blockers")
        ],
        "No open step is blocked.",
    )

    records = [o for o in r.objects if o["kind"] in KIND_LABELS]
    records.sort(key=lambda o: list(KIND_LABELS).index(o["kind"]))
    for o in records:
        r.mention(o["id"])
    none_yet = "No business records have been added yet."
    out += ["## What is still an assumption", "", "### Records marked as assumptions", ""]
    out += bullets(
        [f"{kind_label(o['kind'])}: {inline(o['title'])}" for o in records if o["state"] == "assumption"],
        none_yet if not records else "No record is marked as an assumption.",
    )
    out += ["### Records with no evidence attached", ""]
    out += bullets(
        [
            f"{kind_label(o['kind'])}: {inline(o['title'])}"
            for o in records
            if not o["data"].get("evidence_ids")
        ],
        none_yet if not records else "Every record has evidence attached.",
    )

    out += ["## Money and deadlines", ""]
    money = []
    if Decimal(str(stats.get("outstanding_invoices") or 0)):
        money.append(f"Outstanding invoice balance: {fmt_money(stats['outstanding_invoices'])} {currency}.")
    if Decimal(str(stats.get("cash_collected") or 0)):
        money.append(f"Cash collected: {fmt_money(stats['cash_collected'])} {currency}.")
    if stats.get("overdue_obligations"):
        money.append(f"Obligations past their due date: {stats['overdue_obligations']}.")
    out += bullets(money, "No outstanding invoices, collected payments or overdue obligations are recorded.")

    out += ["## Every step", ""]
    listed = [t for t in tasks if step_class(t["state"]) != "replaced"]
    if not listed:
        out += ["No steps are recorded.", ""]
    for t in listed:
        out += [f"- {r.step_name(t)} — {state_label(t['state']).lower()}"]
        if inline(t["data"].get("description", "")):
            out.append(f"  {inline(t['data']['description'])}")
        if step_class(t["state"]) == "open" and t.get("blockers"):
            out.append(f"  Needs first: {needs_first(t)}")
    if listed:
        out.append("")
    return out + [CHECKLIST_LIMITATION, ""]


def open_work(r, tasks, heading) -> list[str]:
    out = [f"## {heading}", ""] + step_overview(r, tasks)
    unfinished = [t for t in tasks if step_class(t["state"]) in {"open", "unknown"}]
    if unfinished:
        out += ["Unfinished steps:", ""]
        out += bullets([f"{r.step_name(t)} — {state_label(t['state']).lower()}" for t in unfinished], "")
    elif tasks:
        out += ["No step is unfinished.", ""]
    return out + [
        "The readiness report lists what needs a person now, what can be worked on next and what is blocked.",
        "",
        CHECKLIST_LIMITATION,
        "",
    ]


def cited_sources(r, tasks, catalog) -> list[str]:
    cited = {}
    for t in tasks:
        if step_class(t["state"]) != "replaced" and t["data"].get("source_uri"):
            cited.setdefault(t["data"]["source_uri"], []).append(t)
    if not cited:
        return ["These steps cite no external sources.", ""]
    versions = sorted(
        {
            str(t["data"].get("pack_version") or catalog.get("version") or "unknown")
            for ts in cited.values()
            for t in ts
        }
    )
    checked = sorted(
        {
            str(t["data"].get("source_checked_on") or catalog.get("source_checked_on") or "unknown")
            for ts in cited.values()
            for t in ts
        }
    )
    out = [
        f"Sources cited by these steps (planning guide {', '.join(versions)}; sources checked on {', '.join(checked)}):",
        "",
    ]
    for uri, ts in cited.items():
        out.append(f"- {inline(uri)} — cited by: " + "; ".join(r.step_name(t) for t in ts))
    return out + [""]


def render_document(template, snapshot, deal=None):
    business = snapshot["business"]["data"]
    r = Render(snapshot)
    objects = r.objects
    currency = business["currency"]
    title = f"{business['name']} — {TITLES[template]}"
    lines = [f"# {inline(title)}", "", "Status: DRAFT — human review required.", ""]
    if business["is_demo"]:
        lines += [
            "SYNTHETIC DEMONSTRATION. Names, commercial assumptions and receipts in this workspace are examples, not evidence of a real business.",
            "",
        ]
    lines += [
        f"Concept: {inline(business['concept'])}",
        f"Entry path: {business['route']}. Planning jurisdiction: {business['jurisdiction']}. Currency: {currency}.",
        f"Intended customer: {inline(business['customer']) or 'Unknown — research required.'}",
        "",
    ]
    tasks = [o for o in objects if o["kind"] == "task"]
    if template == "investment_memo":
        tasks = [t for t in tasks if t["data"].get("deal_id") == deal["id"]]

    def records(kind, heading):
        lines.extend([f"## {heading}", ""])
        found = [o for o in objects if o["kind"] == kind]
        if not found:
            lines.extend(["Unknown — add a record and supporting evidence.", ""])
        for o in found:
            lines.extend(r.record_block(o))

    def finances():
        lines.extend(["## Financial scenarios", ""])
        plans = [o for o in objects if o["kind"] == "financial_plan"]
        if not plans:
            lines.extend(["No financial model has been saved. Revenue, costs and funding are untested.", ""])
        for p in plans[-1:]:
            r.mention(p["id"])
            lines.extend(financial_section(p, currency))

    if template == "business_plan":
        for kind, heading in [
            ("customer", "Buyer and problem"),
            ("offer", "Offer, price and acceptance"),
            ("asset", "Resources and rights"),
            ("role", "Delivery and accountability"),
            ("decision", "Decisions made"),
            ("obligation", "Commitments and duties"),
            ("risk", "Risks and experiments"),
        ]:
            records(kind, heading)
        finances()
        lines += [
            "## Validation milestones",
            "Confirm willingness to pay; deliver an agreed result; collect payment; reconcile actual unit economics; repeat delivery with documented responsibilities.",
            "",
        ]
    elif template == "structure_options":
        lines += [
            "## Decision questions",
            "Establish founder residence, operating locations, beneficial ownership, contributions, funding, personal liability, staff, tax presence and customer requirements. The selected planning jurisdiction is not a tax or entity recommendation.",
            "",
            "## Alternatives to review",
            "Compare individual trading, a limited-liability company, a partnership and any holding/operating structure against the actual facts. Record taxes, reporting, liability, ownership and financing consequences with appropriate local advisers. Additional entities need a concrete purpose.",
            "",
        ]
        records("decision", "Recorded decisions")
        records("entity", "Proposed or existing entities")
        records("person", "People and signing authority")
        records("ownership", "Ownership and economic interests")
        records("contract", "Founder and ownership arrangements")
        records("obligation", "Continuing obligations")
    elif template == "contract_brief":
        lines += [
            "## Instructions for contract preparation",
            "This is a commercial brief, not an executable agreement. Confirm parties and signing authority, scope, exclusions, deliverables, acceptance tests, fees, invoicing, taxes, payment, term, renewal, termination, confidentiality, privacy, IP ownership, licensed data, warranties, liability, indemnities, dispute resolution and governing law. Resolve omissions with qualified review before signing.",
            "",
        ]
        records("customer", "Counterparty")
        records("offer", "Commercial terms")
        records("asset", "Licensing and third-party restrictions")
        records("contract", "Existing terms and commitments")
    elif template == "operating_manual":
        for kind, heading in [
            ("role", "Responsibilities and authority"),
            ("offer", "Delivery standards"),
            ("asset", "Resources and rights"),
            ("obligation", "Recurring work"),
            ("risk", "Exceptions and recovery"),
        ]:
            records(kind, heading)
        lines += [
            "## Operating cadence",
            "Weekly: review customer commitments, cash, delivery and exceptions. Monthly: reconcile actual records, renew forecasts and review obligations. Keep source documents and review decisions with each material change.",
            "",
        ]
    elif template == "investment_memo":
        r.mention(deal["id"])
        lines += [
            f"## Target: {inline(deal['title'])}",
            inline(deal["data"]["inputs"]["thesis"]),
            "",
            "## Screening analysis",
            "These values are input-dependent scenarios. Multiples are user assumptions, not fetched market comparables.",
            "",
        ]
        lines += deal_section(deal, snapshot, currency, r.evidence_name)
        lines += [
            "## Conditions before commitment",
            "Reconcile seller earnings to independent records; assess concentration and owner dependence; verify transferable rights, liabilities and required consents; validate financing terms and downside liquidity; document legal structure, specialist reviews and a funded transition plan.",
            "",
        ]
    if template == "readiness_report":
        lines += readiness_audit(r, tasks, currency)
    elif template in {"business_plan", "operating_manual"}:
        lines += open_work(r, tasks, "Open work")
    elif template == "investment_memo":
        lines += open_work(r, tasks, "Open work on this acquisition")
    lines += [
        "## Source and review trail",
        f"Business version: {snapshot['business']['version']}. Source fingerprint: {snapshot['source_fingerprint']}.",
        "This document is an immutable snapshot. Generate a new version when inputs change; retain signed external documents as evidence.",
        "",
    ]
    lines += cited_sources(r, tasks, snapshot.get("catalog") or {})
    lines += r.reference_index()
    return title, "\n".join(lines).rstrip("\n")
