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
    ("fixed_costs", "Fixed costs per month", fmt_money),
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
        f"Model: {cell(p['title'])} ({p['id']}). Currency: {currency}. "
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
        f"Full monthly statements are stored in financial plan {p['id']} (Financial model view and JSON export).",
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


def deal_section(deal, snapshot, currency) -> list[str]:
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
        [f"Add: {item['label']}", fmt_money(item["amount"]), item.get("evidence_id") or ""]
        for item in a["accepted_adjustments"]
    ]
    rows.append(["Normalized EBITDA", fmt_money(a["normalized_ebitda"]), ""])
    out += fmt_table(["Item", "Amount", "Evidence"], rows, {1})
    out += ["### Excluded adjustments", ""]
    if a["excluded_adjustments"]:
        out += fmt_table(
            ["Adjustment", "Amount", "Evidence", "Reason"],
            [
                [x["label"], fmt_money(x["amount"]), x.get("evidence_id") or "None", x["reason"]]
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


def render_document(template, snapshot, deal=None):
    business = snapshot["business"]["data"]
    objects = snapshot["objects"]
    title = f"{business['name']} — {TITLES[template]}"
    lines = [f"# {title}", "", "Status: DRAFT — human review required.", ""]
    if business["is_demo"]:
        lines += [
            "SYNTHETIC DEMONSTRATION. Names, commercial assumptions and receipts in this workspace are examples, not evidence of a real business.",
            "",
        ]
    lines += [
        f"Concept: {business['concept']}",
        f"Entry path: {business['route']}. Planning jurisdiction: {business['jurisdiction']}. Currency: {business['currency']}.",
        f"Intended customer: {business['customer'] or 'Unknown — research required.'}",
        "",
    ]

    def records(kind, heading):
        lines.extend([f"## {heading}", ""])
        found = [o for o in objects if o["kind"] == kind]
        if not found:
            lines.extend(["Unknown — add a record and supporting evidence.", ""])
        for o in found:
            lines.extend(
                [
                    f"### {o['title']}",
                    f"Status: {o['state']}. Record: {o['id']} (version {o['version']}).",
                    o["data"].get("details", ""),
                    f"Owner: {o['data'].get('owner', 'Unassigned')}. Due: {o['data'].get('due_date') or 'Not set'}.",
                    f"Evidence: {', '.join(o['data'].get('evidence_ids', [])) or 'None attached.'}",
                    "",
                ]
            )

    def finances():
        lines.extend(["## Financial scenarios", ""])
        plans = [o for o in objects if o["kind"] == "financial_plan"]
        if not plans:
            lines.extend(["No financial model has been saved. Revenue, costs and funding are untested.", ""])
        for p in plans[-1:]:
            lines.extend(financial_section(p, business["currency"]))

    if template == "business_plan":
        for kind, heading in [
            ("customer", "Buyer and problem"),
            ("offer", "Offer, price and acceptance"),
            ("asset", "Resources and rights"),
            ("role", "Delivery and accountability"),
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
        lines += [
            f"## Target: {cell(deal['title'])}",
            deal["data"]["inputs"]["thesis"],
            "",
            "## Screening analysis",
            "These values are input-dependent scenarios. Multiples are user assumptions, not fetched market comparables.",
            "",
        ]
        lines += deal_section(deal, snapshot, business["currency"])
        lines += [
            "## Conditions before commitment",
            "Reconcile seller earnings to independent records; assess concentration and owner dependence; verify transferable rights, liabilities and required consents; validate financing terms and downside liquidity; document legal structure, specialist reviews and a funded transition plan.",
            "",
        ]
    if template in {"readiness_report", "business_plan", "operating_manual", "investment_memo"}:
        lines += ["## Work and unresolved requirements", ""]
        for o in objects:
            if o["kind"] == "task" and (
                template != "investment_memo" or o["data"].get("deal_id") == deal["id"]
            ):
                lines += [f"- [{o['state']}] {o['title']} — {o['id']}", f"  {o['data']['description']}"]
                if o.get("blockers"):
                    lines.append("  Requires: " + "; ".join(o["blockers"]))
        lines += [
            "",
            "A completed checklist reflects recorded reviews. It does not establish legal compliance, commercial viability or an independently audited result.",
            "",
        ]
    lines += [
        "## Source and review trail",
        f"Business version: {snapshot['business']['version']}. Source fingerprint: {snapshot['source_fingerprint']}.",
        "This document is an immutable snapshot. Generate a new version when inputs change; retain signed external documents as evidence.",
        "",
    ]
    for name, url in snapshot["catalog"]["sources"].items():
        lines.append(f"- {name}: {url}")
    return title, "\n".join(lines)
