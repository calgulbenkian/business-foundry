from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Short = Annotated[str, Field(min_length=1, max_length=240)]
Money = Annotated[Decimal, Field(ge=0, le=Decimal("1000000000000"), allow_inf_nan=False)]
Rate = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BusinessCreate(Model):
    name: Short
    concept: Annotated[str, Field(min_length=10, max_length=12000)]
    route: Literal["start", "acquire"] = "start"
    jurisdiction: Literal["undecided", "US-DE", "GB", "SE", "other"] = "undecided"
    currency: Currency = "USD"
    customer: Annotated[str, Field(max_length=2000)] = ""
    owner: Short = "Founder"
    is_demo: bool = False


class BusinessUpdate(BusinessCreate):
    expected_version: Annotated[int, Field(ge=1)]


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
    monthly_units: Annotated[Decimal, Field(ge=0, le=100000000, allow_inf_nan=False)] = Decimal("5")
    unit_price: Money = Decimal("2000")
    unit_cost: Money = Decimal("300")
    monthly_growth: Annotated[Decimal, Field(ge=Decimal("-0.8"), le=Decimal("0.5"), allow_inf_nan=False)] = (
        Decimal("0.03")
    )
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
    discount_rate: Annotated[Decimal, Field(gt=0, le=1, allow_inf_nan=False)] = Decimal("0.20")
    terminal_growth: Annotated[Decimal, Field(ge=Decimal("-0.2"), le=Decimal("0.1"), allow_inf_nan=False)] = (
        Decimal("0.02")
    )
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
    amount: Annotated[
        Decimal, Field(ge=Decimal("-1000000000"), le=Decimal("1000000000"), allow_inf_nan=False)
    ]
    evidence_id: str | None = None


class DealInputs(Model):
    target: Short
    thesis: Annotated[str, Field(min_length=10, max_length=8000)]
    annual_revenue: Money = Decimal("1000000")
    reported_ebitda: Annotated[
        Decimal, Field(ge=Decimal("-1000000000"), le=Decimal("1000000000"), allow_inf_nan=False)
    ] = Decimal("200000")
    adjustments: list[Adjustment] = Field(default_factory=list, max_length=50)
    replacement_management: Money = Decimal("0")
    multiple_low: Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)] = Decimal("3")
    multiple_high: Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)] = Decimal("5")
    purchase_price: Money = Decimal("800000")
    fees: Money = Decimal("30000")
    working_capital: Money = Decimal("50000")
    integration_cost: Money = Decimal("0")
    senior_debt: Money = Decimal("400000")
    debt_rate: Rate = Decimal("0.09")
    debt_years: Annotated[int, Field(ge=1, le=30)] = 7
    seller_note: Money = Decimal("0")
    seller_rate: Rate = Decimal("0.06")
    seller_years: Annotated[int, Field(ge=1, le=30)] = 5
    annual_capex: Money = Decimal("15000")
    annual_working_capital_growth: Money = Decimal("0")
    tax_rate: Rate = Decimal("0.25")
    cash_acquired: Money = Decimal("0")
    debt_assumed: Money = Decimal("0")
    evidence_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def valid_deal(self):
        if self.multiple_high < self.multiple_low:
            raise ValueError("High multiple must not be below low multiple.")
        uses = self.purchase_price + self.fees + self.working_capital + self.integration_cost
        if self.senior_debt + self.seller_note > uses:
            raise ValueError("Debt funding exceeds transaction uses.")
        if self.reported_ebitda > self.annual_revenue:
            raise ValueError("Reported EBITDA exceeds revenue; review the inputs.")
        return self


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
