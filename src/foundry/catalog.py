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

# What each line is, in one sentence, versioned with the steps above. A line is a step's category.
LINES = {
    "Market": "Who buys, what problem they pay to solve, and what you will offer them.",
    "Finance": "How much cash the business needs and when, what happens if things go worse, "
    "and a bank account that can take payments.",
    "Setup": "Where the business is based, its legal and tax shape, and the registrations, permits and "
    "insurance it needs.",
    "Rights": "Who owns what: the founders' arrangements, intellectual property, data and assets.",
    "Delivery": "How you will do the work the same way every time.",
    "Sales to cash": "From a signed agreement to money in the bank.",
    "Operate": "Looking back at actual results, and the taxes, renewals and other deadlines still to meet.",
    "Acquisition": "Buying a business: the decision to pursue it, the checks on its books and contracts, "
    "the price, closing, the handover and a look back at results.",
}

# The lines of the start-up guide, read on a business that is being bought. Every business has them;
# these say what they mean when the work is buying rather than starting.
ON_ACQUISITION = {
    "Market": "When you buy a business, this is about its customers: who they are, why they pay, "
    "and what it offers them.",
    "Finance": "When you buy a business, this is the cash it needs once it is yours, and the bank "
    "account it will be paid into.",
    "Setup": "When you buy a business, this is the legal and tax shape you will own it through, and "
    "the registrations and permits that must be in your name.",
    "Rights": "When you buy a business, this is who owns what after the sale: your own arrangements, "
    "and the data, software and assets that pass to you.",
    "Delivery": "When you buy a business, the work already happens. This line is where you write down "
    "how, so it runs the same way after the handover.",
    "Sales to cash": "When you buy a business, this follows one sale after you take over: the "
    "agreement, the delivery, the invoice and the payment, so you see money arrive while you own it.",
    "Operate": "When you buy a business, this is looking back at its results once it is yours, and the "
    "deadlines you take on with it.",
}
ACQUISITION_ORDER = (
    "You are buying this business, so the Acquisition line comes first. The other lines come from the "
    "start-up guide. Most of their work happens once the business is yours."
)
ACQUISITION_LATER = "This line appears when you save your first acquisition analysis."

# The guided path for each entry path: five or six stops, each tied to real records and steps, so the
# guide works out whether it is done. Which records and steps decide that is in guide.py, by stop key.
PATHS = {
    "acquire": {
        "name": "Buying a business",
        "stops": [
            {
                "key": "workspace",
                "done_when": "Done once the workspace exists.",
                "title": "Say you are buying, and name the business",
                "detail": "You made this workspace with the entry path Acquire a business.",
                "why": "Every step, record and piece of evidence about this purchase lives in this "
                "workspace.",
                "line": None,
                "steps": [],
            },
            {
                "key": "evidence",
                "done_when": "Done when at least one piece of evidence is attached and not withdrawn.",
                "title": "Attach the listing and anything the seller sent",
                "detail": "Paste each item in as text. It is kept exactly as you add it.",
                "why": "Every later check leans on what the seller said. Keeping the original text "
                "lets you show where each figure came from.",
                "line": "Acquisition",
                "steps": ["financial_diligence", "legal_diligence"],
                "action": "Add the listing or a document from the seller",
            },
            {
                "key": "reviewed",
                "done_when": "Done when at least one piece of evidence is reviewed and none is waiting for review.",
                "title": "Check what you attached",
                "detail": "Read each item and record that you reviewed it.",
                "why": "Only reviewed evidence can support an approval, and the acquisition analysis "
                "counts an add-back only when reviewed evidence supports it.",
                "line": "Acquisition",
                "steps": ["financial_diligence", "legal_diligence"],
            },
            {
                "key": "deal",
                "done_when": "Done when an acquisition analysis is saved and the newest one is not out of date.",
                "title": "Screen the deal",
                "detail": "An acquisition analysis needs the revenue, the earnings and the price of the "
                "business itself; a building sold with it has a price of its own, which can be left "
                "empty. Any other figure you leave out, and the seller note's schedule, takes an "
                "example value, and the analysis lists each one it used.",
                "why": "Screening shows whether the price can make sense before you spend money on "
                "checks. Saving it lays out the Acquisition line.",
                "line": "Acquisition",
                "steps": ["mandate", "investment"],
                "action": "Screen the deal in an acquisition analysis",
            },
            {
                "key": "decide",
                "done_when": "Done when Approve the acquisition thesis, for the newest analysis, is approved or further along.",
                "title": "Decide whether to keep going",
                "detail": "Approve the acquisition thesis. It is the first real decision.",
                "why": "The checks on the books and the contracts cannot be approved until this step "
                "is finished: verified, or ruled out as not applicable. Decide here before you pay anyone "
                "for those checks.",
                "line": "Acquisition",
                "steps": ["mandate"],
            },
        ],
    },
    "start": {
        "name": "Starting a business",
        "stops": [
            {
                "key": "workspace",
                "done_when": "Done once the workspace exists.",
                "title": "Say you are starting, and name the business",
                "detail": "You made this workspace with the entry path Start a business.",
                "why": "Every step, record and piece of evidence about this business lives in this "
                "workspace.",
                "line": None,
                "steps": [],
            },
            {
                "key": "customer",
                "done_when": "Done when the business has a customer record.",
                "title": "Describe the buyer and their problem",
                "detail": "Add a customer record: who buys, the problem they have, and what they use today.",
                "why": "The offer, the price and the forecast all follow from who buys and why.",
                "line": "Market",
                "steps": ["demand"],
                "action": "Add a customer record",
            },
            {
                "key": "reviewed",
                "done_when": "Done when at least one piece of evidence is reviewed and none is waiting for review.",
                "title": "Talk to buyers and attach the notes",
                "detail": "Keep what buyers said, word for word, and record that you reviewed it.",
                "why": "A stated interest is not a paid commitment. Reviewed notes are what lets "
                "Establish customer demand be approved.",
                "line": "Market",
                "steps": ["demand"],
                "action": "Add your notes from a buyer conversation",
            },
            {
                "key": "offer",
                "done_when": "Done when the business has an offer record.",
                "title": "Define the offer",
                "detail": "Add an offer record: what you deliver, the price, and how the buyer accepts it.",
                "why": "Delivery, the customer agreement and the invoice all wait on the offer.",
                "line": "Market",
                "steps": ["offer"],
                "action": "Add an offer record",
            },
            {
                "key": "forecast",
                "done_when": "Done when a forecast is saved and the newest one is not out of date.",
                "title": "Review cash needs",
                "detail": "Build a forecast: what the business earns and spends each month, and what "
                "happens if things go worse.",
                "why": "It shows how much more cash the business needs, in a base case and in a worse case.",
                "line": "Finance",
                "steps": ["economics"],
                "action": "Build a forecast",
            },
            {
                "key": "structure",
                "done_when": "Done when Decide the entity and ownership structure is approved or further along.",
                "title": "Decide the structure",
                "detail": "Decide the entity and ownership structure, with a decision record.",
                "why": "Registrations, tax, banking, permits and the founders' arrangements all wait "
                "on this decision.",
                "line": "Setup",
                "steps": ["structure"],
            },
        ],
    },
}


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
        "lines": LINES,
    }
