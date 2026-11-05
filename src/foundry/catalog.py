"""Versioned planning guides. These identify work, not a certification of compliance."""

PACK_VERSION = "2026.11.1"
SOURCE_CHECKED = "2026-11-05"

SOURCES = {
    "sba": "https://www.sba.gov/counseling/launch-your-business/",
    "buy": "https://www.sba.gov/counseling/plan-your-business/",
    "gb": "https://www.gov.uk/limited-company-formation",
    "se": "https://verksamt.se/en/setting-up/choose-business-type/limited-company",
    "contracts": "https://commonpaper.com/standards/",
}


def task(key, title, category, description, dependencies=(), records=(), external=False, source=None):
    return {
        "key": key,
        "category": category,
        "description": description,
        "depends_on_keys": list(dependencies),
        "required_record_kinds": list(records),
        "external": external,
        "title": title,
        "source_uri": source,
        "pack_version": PACK_VERSION,
        "source_checked_on": SOURCE_CHECKED if source else None,
        "applicability": "requires_review",
        "owner": "Founder",
        "evidence_ids": [],
        "history": [],
    }


BASE = [
    task(
        "demand",
        "Establish customer demand",
        "Market",
        "Record the buyer, problem, alternatives and interview or purchase evidence. Separate a stated interest from a paid commitment.",
        records=["customer"],
    ),
    task(
        "offer",
        "Define the offer and acceptance standard",
        "Market",
        "Set scope, price, delivery, acceptance, support and payment terms.",
        records=["offer"],
    ),
    task(
        "economics",
        "Review cash needs and downside",
        "Finance",
        "Build and review linked statements, cash collection timing and downside assumptions.",
        records=["financial_plan"],
    ),
    task(
        "structure",
        "Decide the entity and ownership structure",
        "Setup",
        "Review founder residence, operations, financing, ownership and liabilities with suitable advisers. Record the decision and unresolved issues.",
        records=["decision"],
        source=SOURCES["sba"],
    ),
    task(
        "founders",
        "Record founder and IP arrangements",
        "Rights",
        "Record contributions, ownership, decision rights, existing IP and departure arrangements.",
        ["structure"],
        ["contract"],
        source=SOURCES["contracts"],
    ),
    task(
        "rights",
        "Verify data, software and asset rights",
        "Rights",
        "Record licenses, attribution, redistribution, expiration and transfer restrictions for resources used in delivery.",
        records=["asset"],
    ),
    task(
        "registration",
        "Verify applicable registrations",
        "Setup",
        "Confirm applicable entity and operating registrations. Attach actual acceptance evidence, or a reviewed non-applicability decision.",
        ["structure"],
        external=True,
        source=SOURCES["sba"],
    ),
    task(
        "tax",
        "Review and establish tax obligations",
        "Setup",
        "Establish relevant tax registrations, reporting and payment duties from the actual operating facts.",
        ["structure"],
        ["obligation"],
        True,
        SOURCES["sba"],
    ),
    task(
        "bank",
        "Establish banking and payment access",
        "Finance",
        "Complete the chosen provider's onboarding and verify account readiness independently of entity formation.",
        ["structure"],
        external=True,
        source=SOURCES["sba"],
    ),
    task(
        "permissions",
        "Review permits, insurance and activity rules",
        "Setup",
        "Check requirements for the actual activity and locations. Record reviewer, sources and any continuing duties.",
        ["structure"],
        ["obligation"],
        source=SOURCES["sba"],
    ),
    task(
        "delivery",
        "Prepare a repeatable delivery process",
        "Delivery",
        "Define responsibility, quality checks, acceptance, recovery and customer support.",
        ["offer", "rights"],
        ["role"],
    ),
    task(
        "agreement",
        "Confirm the customer agreement",
        "Sales to cash",
        "Keep the executed agreement and confirm authority, scope, price, rights and payment terms.",
        ["offer", "rights", "structure"],
        ["contract"],
        True,
        SOURCES["contracts"],
    ),
    task(
        "acceptance",
        "Verify delivery and customer acceptance",
        "Sales to cash",
        "Retain evidence of the completed deliverable and agreed acceptance conditions.",
        ["agreement", "delivery"],
        external=True,
    ),
    task(
        "invoice",
        "Issue and record the invoice",
        "Sales to cash",
        "Record an externally issued invoice and evidence of its issue. Creating a draft does not send it.",
        ["acceptance", "bank", "tax"],
        ["invoice"],
        True,
    ),
    task(
        "collection",
        "Reconcile customer payment",
        "Sales to cash",
        "Match the receipt to the invoice and retain bank or provider evidence. Handle partial payments explicitly.",
        ["invoice"],
        ["payment"],
        True,
    ),
    task(
        "close",
        "Review actuals and continuing commitments",
        "Operate",
        "Reconcile the ledger, compare actuals with the forecast, and assign tax, renewal, support and contractual deadlines.",
        ["collection", "economics"],
        ["obligation"],
    ),
]

JURISDICTIONS = {
    "undecided": [
        task(
            "jurisdiction",
            "Resolve operating and founder jurisdictions",
            "Setup",
            "Establish founder residence, entity location, customers and workers before selecting an applicable jurisdiction package.",
        )
    ],
    "other": [
        task(
            "jurisdiction",
            "Obtain a local requirements review",
            "Setup",
            "This release has no dedicated package for the selected jurisdiction. Record a qualified local review before treating requirements as covered.",
        )
    ],
    "US-DE": [
        task(
            "local_review",
            "Review Delaware and operating-state requirements",
            "Setup",
            "A Delaware entity does not settle other operating-state registrations, taxes, worker obligations or licensing. Review the complete factual footprint.",
            ["structure"],
            source=SOURCES["sba"],
        )
    ],
    "GB": [
        task(
            "local_review",
            "Review UK director, registration and reporting duties",
            "Setup",
            "Confirm current Companies House requirements, identity checks where applicable, company records, ownership reporting and tax registrations.",
            ["structure"],
            source=SOURCES["gb"],
        )
    ],
    "SE": [
        task(
            "local_review",
            "Review Swedish company and tax setup",
            "Setup",
            "Review company form, capital and formation documents, ownership reporting and applicable F-tax, VAT and employer registrations.",
            ["structure"],
            source=SOURCES["se"],
        )
    ],
}

DEAL_TASKS = [
    task(
        "mandate",
        "Approve the acquisition thesis",
        "Acquisition",
        "Compare buying with building, licensing and partnering. Establish capital limits and walk-away conditions.",
        records=["deal"],
    ),
    task(
        "financial_diligence",
        "Reconcile earnings and working capital",
        "Acquisition",
        "Reconcile financials, taxes, bank receipts and customer concentration. Support each adjustment independently.",
        ["mandate"],
        source=SOURCES["buy"],
    ),
    task(
        "legal_diligence",
        "Review liabilities and transfer rights",
        "Acquisition",
        "Inspect contracts, ownership, IP and data rights, staff, permissions, security and disputes. Identify consents and asset/share purchase implications.",
        ["mandate"],
        source=SOURCES["buy"],
    ),
    task(
        "investment",
        "Review valuation, financing and downside",
        "Acquisition",
        "Review normalized earnings, assumptions, value range, funding, debt service, integration costs and downside liquidity.",
        ["financial_diligence", "legal_diligence"],
    ),
    task(
        "closing",
        "Verify agreement, funds and closing conditions",
        "Acquisition",
        "Track specialist review, approvals, signed documents, required consents and actual transfer receipts.",
        ["investment"],
        external=True,
    ),
    task(
        "integration",
        "Verify the transition into operations",
        "Acquisition",
        "Confirm opening balances, system access, responsibilities, customer continuity and the first-100-days plan.",
        ["closing"],
        external=True,
    ),
    task(
        "thesis_review",
        "Review results against the acquisition thesis",
        "Acquisition",
        "Compare verified operating outcomes with purchase assumptions and retain the differences.",
        ["integration"],
    ),
]


def catalog(jurisdiction):
    return [dict(x) for x in BASE + JURISDICTIONS[jurisdiction]]


def description():
    return {
        "version": PACK_VERSION,
        "source_checked_on": SOURCE_CHECKED,
        "review_status": "Planning guidance; local applicability and professional review remain required.",
        "jurisdictions": list(JURISDICTIONS),
        "sources": SOURCES,
        "provider_execution": "External filings, bank transfers and signatures are recorded with receipts; this release does not execute them.",
    }
