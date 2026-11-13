from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Short = Annotated[str, Field(min_length=1, max_length=240)]
# Every decimal input to the financial models carries at most 12 decimal places, counted on the value, so
# every value the models can be given is bounded in size and in digits (docs/FINANCE.md).
DECIMAL_PLACES = 12


def _decimal(**bounds):
    return Annotated[Decimal, Field(**bounds, decimal_places=DECIMAL_PLACES, allow_inf_nan=False)]


Money = _decimal(ge=0, le=Decimal("1000000000000"))
Rate = _decimal(ge=0, le=1)
Units = _decimal(ge=0, le=100000000)
MonthlyGrowth = _decimal(ge=Decimal("-0.8"), le=Decimal("0.5"))
DiscountRate = _decimal(gt=0, le=1)
TerminalGrowth = _decimal(ge=Decimal("-0.2"), le=Decimal("0.1"))
Earnings = _decimal(ge=Decimal("-1000000000"), le=Decimal("1000000000"))
Multiple = _decimal(ge=0, le=100)
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Jurisdiction = Literal["undecided", "US-DE", "GB", "SE", "other"]
# How a seller note is repaid over seller_years (docs/FINANCE.md): level payments, interest each year with the
# principal at the end, or nothing until the end, when the principal and the interest accrued are due.
SellerSchedule = Literal["amortizing", "interest_only", "deferred"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BusinessCreate(Model):
    name: Short
    concept: Annotated[str, Field(min_length=10, max_length=12000)]
    route: Literal["start", "acquire"] = "start"
    jurisdiction: Jurisdiction = "undecided"
    currency: Currency = "USD"
    customer: Annotated[str, Field(max_length=2000)] = ""
    owner: Short = "Founder"
    is_demo: bool = False


class BusinessUpdate(BusinessCreate):
    expected_version: Annotated[int, Field(ge=1)]


class JurisdictionChange(Model):
    """A human decision to plan under another jurisdiction. It is not an entity move or legal advice."""

    expected_version: Annotated[int, Field(ge=1)]
    jurisdiction: Jurisdiction
    reason: Annotated[str, Field(min_length=3, max_length=4000)]
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


RecordKind = Literal[
    "customer",
    "offer",
    "asset",
    "contract",
    "obligation",
    "role",
    "entity",
    "person",
    "ownership",
    "decision",
    "risk",
]


class RecordCreate(Model):
    kind: RecordKind
    title: Short
    details: Annotated[str, Field(max_length=12000)] = ""
    certainty: Literal["assumption", "reported", "verified"] = "assumption"
    owner: Short = "Founder"
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)
    related_ids: list[str] = Field(default_factory=list, max_length=50)
    due_date: date | None = None
    properties: dict[str, str | int | bool | None] = Field(default_factory=dict, max_length=30)


class RecordUpdate(RecordCreate):
    expected_version: Annotated[int, Field(ge=1)]


class EvidenceCreate(Model):
    title: Short
    content: Annotated[str, Field(min_length=1, max_length=100000)]
    source_type: Literal["document", "external_receipt", "observation", "attestation"] = "document"
    source_uri: Annotated[str, Field(max_length=2000)] = ""
    effective_date: date | None = None

    @field_validator("source_uri")
    @classmethod
    def safe_uri(cls, value: str) -> str:
        if value and not value.startswith(("https://", "http://")):
            raise ValueError("Use an HTTP(S) source reference; local files are not fetched.")
        return value


class Review(Model):
    note: Annotated[str, Field(min_length=8, max_length=4000)]
    expected_version: Annotated[int, Field(ge=1)]


class TaskTransition(Model):
    state: Literal[
        "in_progress", "prepared", "approved", "submitted", "verified", "needs_review", "not_applicable"
    ]
    expected_version: Annotated[int, Field(ge=1)]
    note: Annotated[str, Field(min_length=3, max_length=4000)]
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)


class DocumentCreate(Model):
    template: Literal[
        "business_plan",
        "structure_options",
        "contract_brief",
        "operating_manual",
        "readiness_report",
        "investment_memo",
    ]
    deal_id: str | None = None


class ForecastInputs(Model):
    months: Annotated[int, Field(ge=1, le=60)] = 36
    opening_equity: Money = Decimal("50000")
    opening_debt: Money = Decimal("0")
    monthly_units: Units = Decimal("5")
    unit_price: Money = Decimal("2000")
    unit_cost: Money = Decimal("300")
    monthly_growth: MonthlyGrowth = Decimal("0.03")
    monthly_retention: Rate = Decimal("0")
    fixed_costs: Money = Decimal("6000")
    fixed_cost_step: Money = Decimal("0")
    fixed_cost_step_month: Annotated[int, Field(ge=1, le=60)] | None = None
    receivable_days: Annotated[int, Field(ge=0, le=90)] = 30
    monthly_capex: Money = Decimal("0")
    asset_life_months: Annotated[int, Field(ge=1, le=240)] = 36
    tax_rate: Rate = Decimal("0.25")
    annual_interest_rate: Rate = Decimal("0.08")
    monthly_principal: Money = Decimal("0")
    discount_rate: DiscountRate = Decimal("0.20")
    terminal_growth: TerminalGrowth = Decimal("0.02")
    excess_cash: Money = Decimal("0")

    @field_validator("fixed_cost_step_month", mode="before")
    @classmethod
    def step_month_not_bool(cls, value):
        if isinstance(value, bool):
            raise ValueError("Fixed-cost step start month must be a whole month number.")
        return value

    @model_validator(mode="after")
    def valid_terminal(self):
        if self.terminal_growth >= self.discount_rate:
            raise ValueError("Discount rate must exceed terminal growth.")
        if self.fixed_cost_step > 0 and self.fixed_cost_step_month is None:
            raise ValueError("Fixed-cost step requires a start month.")
        if self.fixed_cost_step == 0 and self.fixed_cost_step_month is not None:
            raise ValueError("Fixed-cost step start month requires a positive step amount.")
        if self.fixed_cost_step_month is not None and self.fixed_cost_step_month > self.months:
            raise ValueError("Fixed-cost step start month cannot exceed forecast horizon.")
        return self


class ForecastCreate(Model):
    title: Short = "Operating plan"
    inputs: ForecastInputs = Field(default_factory=ForecastInputs)
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)


class Adjustment(Model):
    label: Short
    amount: Earnings
    evidence_id: str | None = None


class DealInputs(Model):
    """The inputs of an acquisition screen.

    The target's revenue and reported EBITDA and the purchase price have no default: nothing stands in
    for the figures that describe the business and its price. Every other figure has an example value
    that the model uses when the caller leaves it out; assumed_inputs names those (docs/FINANCE.md).
    Financing is the buyer's choice, not a fact about the target, so the example senior debt, property
    loan, seller note and investor equity are zero: a deal given no financing is screened as bought
    without debt and with the buyer's own cash. The real estate price and the rent are zero too: a deal
    that gives neither buys no building and pays no rent the reported EBITDA does not already bear.

    purchase_price is the price of the business. A building sold with it is priced separately in
    real_estate_price, so the valuation multiples apply to the business alone.
    """

    target: Short
    thesis: Annotated[str, Field(min_length=10, max_length=8000)]
    annual_revenue: Money
    reported_ebitda: Earnings
    adjustments: list[Adjustment] = Field(default_factory=list, max_length=50)
    replacement_management: Money = Decimal("0")
    multiple_low: Multiple = Decimal("3")
    multiple_high: Multiple = Decimal("5")
    purchase_price: Money
    real_estate_price: Money = Decimal("0")
    fees: Money = Decimal("30000")
    working_capital: Money = Decimal("50000")
    integration_cost: Money = Decimal("0")
    senior_debt: Money = Decimal("0")
    debt_rate: Rate = Decimal("0.09")
    debt_years: Annotated[int, Field(ge=1, le=30)] = 7
    property_debt: Money = Decimal("0")
    property_rate: Rate = Decimal("0.08")
    property_years: Annotated[int, Field(ge=1, le=30)] = 25
    seller_note: Money = Decimal("0")
    seller_rate: Rate = Decimal("0.06")
    seller_years: Annotated[int, Field(ge=1, le=30)] = 5
    seller_schedule: SellerSchedule = "amortizing"
    investor_equity: Money = Decimal("0")
    annual_rent: Money = Decimal("0")
    annual_capex: Money = Decimal("15000")
    annual_working_capital_growth: Money = Decimal("0")
    tax_rate: Rate = Decimal("0.25")
    cash_acquired: Money = Decimal("0")
    debt_assumed: Money = Decimal("0")
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def valid_deal(self):
        """Refuse inputs that contradict each other.

        A check that counted a figure the caller did not give names that figure and its example value,
        and asks for it, so a refusal is never blamed on a figure the caller never entered. The debt
        check names only figures whose example value is not zero: a zero adds nothing to either side.
        A check that counted only given figures keeps its plain words.
        """
        if self.multiple_high < self.multiple_low:
            raise ValueError(
                self._refusal(
                    "High multiple must not be below low multiple.", ("multiple_low", "multiple_high")
                )
            )
        uses = (
            self.purchase_price
            + self.real_estate_price
            + self.fees
            + self.working_capital
            + self.integration_cost
        )
        if self.senior_debt + self.property_debt + self.seller_note > uses:
            counted = (
                "real_estate_price",
                "fees",
                "working_capital",
                "integration_cost",
                "senior_debt",
                "property_debt",
                "seller_note",
            )
            raise ValueError(
                self._refusal(
                    "Debt funding exceeds transaction uses.",
                    tuple(name for name in counted if getattr(self, name)),
                )
            )
        if self.reported_ebitda > self.annual_revenue:
            raise ValueError("Reported EBITDA exceeds revenue; review the inputs.")
        return self

    def _refusal(self, message: str, involved: tuple[str, ...]) -> str:
        """The message, followed by a sentence naming each involved figure the caller did not give."""
        assumed = [name for name in involved if name not in self.model_fields_set]
        if not assumed:
            return message
        names = [DEAL_REFUSAL_NAMES[name] for name in assumed]
        values = [f"{getattr(self, name):,}" for name in assumed]
        if len(assumed) == 1:
            (subject, ask, plural), value = names[0], values[0]
            verb, whose = ("were", "their") if plural else ("was", "its")
            note = f"{subject} {verb} not given, so {whose} example value {value} was used; give {ask}."
        else:
            subjects = _listed([subject for subject, _, _ in names])
            ask = "both" if len(assumed) == 2 else "them"
            note = (
                f"{subjects} were not given, so their example values {_listed(values)} were used; give {ask}."
            )
        return f"{message} {note[0].upper()}{note[1:]}"

    def assumed_inputs(self) -> list[str]:
        """The figures the caller did not give that the model used, in field order.

        Read from the fields the caller set, not from the values: a figure sent with the same value as its
        example is the caller's own and is not listed. A loan's rate and term enter the arithmetic only
        when the loan is above zero, so a rate or term not given is listed only then; with no loan it is
        not used and not listed. Adjustments and evidence are lists of what the caller claims, not figures
        with an example value, so they are never listed.
        """
        unused = set()
        if not self.senior_debt:
            unused |= {"debt_rate", "debt_years"}
        if not self.property_debt:
            unused |= {"property_rate", "property_years"}
        if not self.seller_note:
            unused |= {"seller_rate", "seller_years", "seller_schedule"}
        return [
            name for name in DEAL_EXAMPLE_FIGURES if name not in self.model_fields_set and name not in unused
        ]

    def request(self) -> dict:
        """The request an idempotency key guards: every input, plus the assumed list when it is not empty.

        A field added in acquisition 1.1.0 that the caller did not give is left out of both, so a request
        that gives none of them has the fingerprint it had before they existed and an earlier key still
        replays. Left out, each always takes the same example value, so nothing that decides the figures is
        lost; given, even at its example value, it is part of the request.
        """
        new = {name for name in DEAL_FIELDS_ADDED_IN_1_1 if name not in self.model_fields_set}
        inputs = {k: v for k, v in self.model_dump(mode="json").items() if k not in new}
        assumed = [name for name in self.assumed_inputs() if name not in new]
        return {**inputs, "assumed_inputs": assumed} if assumed else inputs


def _listed(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# How a refusal names a figure that was not given: the subject, what to give, and whether it is plural.
DEAL_REFUSAL_NAMES = {
    "multiple_low": ("the low multiple", "a low multiple", False),
    "multiple_high": ("the high multiple", "a high multiple", False),
    "fees": ("the transaction fees", "the transaction fees", True),
    "working_capital": ("the working capital funding", "the working capital funding", False),
    "integration_cost": ("the integration costs", "the integration costs", True),
    "senior_debt": ("the senior debt", "the senior debt", False),
    "seller_note": ("the seller note", "the seller note", False),
    "real_estate_price": ("the real estate price", "the real estate price", False),
    "property_debt": ("the property loan", "the property loan", False),
}

# The inputs acquisition 1.1.0 added. DealInputs.request leaves each out when it was not given.
DEAL_FIELDS_ADDED_IN_1_1 = (
    "real_estate_price",
    "property_debt",
    "property_rate",
    "property_years",
    "seller_schedule",
    "investor_equity",
    "annual_rent",
)


# The figures of DealInputs that have an example value, in field order.
DEAL_EXAMPLE_FIGURES = tuple(
    name
    for name, field in DealInputs.model_fields.items()
    if not field.is_required() and name not in {"adjustments", "evidence_ids"}
)


class InvoiceCreate(Model):
    customer_id: Short
    description: Short
    amount: Annotated[
        Decimal, Field(gt=0, le=Decimal("1000000000"), max_digits=15, decimal_places=2, allow_inf_nan=False)
    ]
    due_date: date
    contract_evidence_id: Short
    acceptance_evidence_id: Short


class InvoiceIssue(Review):
    evidence_id: Short


class PaymentCreate(Model):
    amount: Annotated[
        Decimal, Field(gt=0, le=Decimal("1000000000"), max_digits=15, decimal_places=2, allow_inf_nan=False)
    ]
    reference: Short
    evidence_id: Short
    received_on: date


class PrincipalCreate(Model):
    name: Short
    role: Literal["owner", "operator", "reviewer", "viewer", "agent"]
    business_ids: list[str] = Field(default_factory=list, max_length=100)


class AIRequest(Model):
    question: Annotated[str, Field(min_length=10, max_length=8000)]
    share_with_provider: Literal[True]
    max_output_tokens: Annotated[int, Field(ge=256, le=8000)] = 2000


class Login(Model):
    token: Annotated[str, Field(min_length=20, max_length=300)]
