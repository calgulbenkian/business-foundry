"""Deterministic, versioned working documents. No fabricated business facts."""

import json

TITLES = {
    "business_plan": "Business plan",
    "structure_options": "Structure decision brief",
    "contract_brief": "Customer contract brief",
    "operating_manual": "Operating manual",
    "readiness_report": "Readiness report",
    "investment_memo": "Investment memo",
}


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
            lines.extend(
                [
                    f"Model: {p['title']} ({p['id']}). These are forecasts, not booked actuals.",
                    "```json",
                    json.dumps(p["data"], indent=2),
                    "```",
                    "",
                ]
            )

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
            f"## Target: {deal['title']}",
            deal["data"]["inputs"]["thesis"],
            "",
            "## Screening analysis",
            "These values are input-dependent scenarios. Multiples are user assumptions, not fetched market comparables.",
            "```json",
            json.dumps(deal["data"], indent=2),
            "```",
            "",
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
