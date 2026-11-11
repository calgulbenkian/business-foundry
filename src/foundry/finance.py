"""Deterministic Decimal models. No model-generated arithmetic or market multiples."""

from __future__ import annotations

from decimal import (
    MAX_EMAX,
    MAX_PREC,
    MIN_EMIN,
    ROUND_05UP,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    localcontext,
)

from .models import DealInputs, ForecastInputs

D = Decimal
ZERO = D(0)
ONE = D(1)
CENT, BASIS = D("0.01"), D("0.0001")
FORECAST_VERSION, ACQUISITION_VERSION = "1.1.1", "1.0.1"


def _fixed(value: Decimal, places: Decimal) -> str:
    # A zero of either sign and any exponent is shown as zero; its exponent says nothing about its size.
    if value.is_zero():
        return str(ZERO.quantize(places))
    # The context is sized to the value, so rounding to the fixed places is never refused for lack of digits.
    # Only a value whose fixed-point form has more than MAX_PREC digits is refused (InvalidOperation).
    digits = min(max(value.adjusted() - places.as_tuple().exponent + 2, 1), MAX_PREC)
    context = Context(prec=digits, rounding=ROUND_HALF_EVEN, Emax=MAX_EMAX, Emin=MIN_EMIN)
    result = value.quantize(places, context=context)
    return str(result.copy_abs() if result.is_zero() else result)


def money(value: Decimal) -> str:
    return _fixed(value, CENT)


def ratio(value: Decimal | None) -> str | None:
    return _fixed(value, BASIS) if value is not None else None


def _forecast(a: ForecastInputs, volume_factor=ONE, fixed_factor=ONE):
    with localcontext() as ctx:
        ctx.prec = 40
        cash = a.opening_equity + a.opening_debt
        debt, ar, retained, gross_assets, accumulated_dep = a.opening_debt, ZERO, ZERO, ZERO, ZERO
        revenues, rows, flows = [], [], []
        lag, fraction = divmod(a.receivable_days, 30)
        fraction = D(fraction) / 30
        minimum_cash, first_shortfall, max_balance_error = cash, None, ZERO
        prior_units = ZERO
        for index in range(a.months):
            month = index + 1
            new_units = a.monthly_units * volume_factor * (1 + a.monthly_growth) ** index
            # Disabled features add no Decimal operations, so v1.0.0 rounding is preserved exactly.
            units = (
                new_units + a.monthly_retention * prior_units
                if (a.monthly_retention and prior_units)
                else new_units
            )
            prior_units = units
            step_active = (
                a.fixed_cost_step and a.fixed_cost_step_month is not None and month >= a.fixed_cost_step_month
            )
            fixed_base = a.fixed_costs + a.fixed_cost_step if step_active else a.fixed_costs
            revenue = units * a.unit_price
            cogs, fixed = units * a.unit_cost, fixed_base * fixed_factor
            revenues.append(revenue)

            def billed(i):
                return revenues[i] if i >= 0 else ZERO

            receipts = billed(index - lag) * (1 - fraction) + billed(index - lag - 1) * fraction
            ar_change = revenue - receipts
            ar += ar_change
            capex = a.monthly_capex
            gross_assets += capex
            depreciation = min(index + 1, a.asset_life_months) * capex / a.asset_life_months
            accumulated_dep += depreciation
            net_assets = gross_assets - accumulated_dep
            ebitda = revenue - cogs - fixed
            ebit = ebitda - depreciation
            interest = debt * a.annual_interest_rate / 12
            taxes = max(ebit - interest, ZERO) * a.tax_rate
            net_income = ebit - interest - taxes
            retained += net_income
            principal = min(debt, a.monthly_principal)
            debt -= principal
            operating_cash = receipts - cogs - fixed - interest - taxes
            net_cash = operating_cash - capex - principal
            cash += net_cash
            assets = cash + ar + net_assets
            equity = a.opening_equity + retained
            error = assets - debt - equity
            max_balance_error = max(max_balance_error, abs(error))
            unlevered_fcf = ebit - max(ebit, ZERO) * a.tax_rate + depreciation - capex - ar_change
            flows.append(unlevered_fcf)
            if cash < 0 and first_shortfall is None:
                first_shortfall = index + 1
            minimum_cash = min(cash, minimum_cash)
            row = {"month": index + 1, "units": ratio(units)}
            for key, val in {
                "revenue": revenue,
                "cost_of_sales": cogs,
                "fixed_costs": fixed,
                "ebitda": ebitda,
                "depreciation": depreciation,
                "ebit": ebit,
                "interest": interest,
                "tax": taxes,
                "net_income": net_income,
                "receipts": receipts,
                "operating_cash_flow": operating_cash,
                "capex": capex,
                "debt_principal": principal,
                "net_cash_flow": net_cash,
                "cash": cash,
                "receivables": ar,
                "net_fixed_assets": net_assets,
                "total_assets": assets,
                "debt": debt,
                "equity": equity,
                "balance_check": error,
                "unlevered_free_cash_flow": unlevered_fcf,
            }.items():
                row[key] = money(val)
            rows.append(row)
        dcf = None
        if a.months >= 12:
            pv = sum((flow / (1 + a.discount_rate) ** (D(i + 1) / 12) for i, flow in enumerate(flows)), ZERO)
            terminal_fcf = sum(flows[-12:]) * (1 + a.terminal_growth)
            # A perpetual negative terminal cash flow is not a going-concern valuation.
            if terminal_fcf > 0:
                terminal = terminal_fcf / (a.discount_rate - a.terminal_growth)
                terminal_pv = terminal / (1 + a.discount_rate) ** (D(a.months) / 12)
                ev = pv + terminal_pv
                dcf = {
                    "enterprise_value": money(ev),
                    "equity_value": money(ev + a.excess_cash - a.opening_debt),
                    "present_value_of_forecast": money(pv),
                    "present_value_of_terminal": money(terminal_pv),
                    "terminal_share": ratio(terminal_pv / ev) if ev > 0 else None,
                    "basis": "Projected unlevered cash flow, monthly discounting, terminal growth; excess cash and opening debt bridge to equity.",
                }
        summary = {
            "total_revenue": money(sum(revenues)),
            "ending_cash": money(cash),
            "minimum_cash": money(minimum_cash),
            "additional_cash_needed": money(max(-minimum_cash, ZERO)),
            "first_shortfall_month": first_shortfall,
            "total_net_income": money(retained),
            "ending_debt": money(debt),
            "statements_balance": max_balance_error < D("0.005"),
            "max_balance_error": money(max_balance_error),
            "dcf": dcf,
        }
        return {"months": rows, "summary": summary}


def forecast(inputs: ForecastInputs):
    cases = {
        "base": _forecast(inputs),
        "downside": _forecast(inputs, D("0.75"), D("1.10")),
        "upside": _forecast(inputs, D("1.25"), D("1.05")),
    }
    return {
        "model_version": FORECAST_VERSION,
        "inputs": inputs.model_dump(mode="json"),
        "scenarios": cases,
        "scenario_definitions": {
            "base": "Entered assumptions",
            "downside": "25% fewer units; 10% higher fixed costs",
            "upside": "25% more units; 5% higher fixed costs",
        },
        "assumptions": [
            "Monthly periods of 30 days; receipts are delayed by the chosen collection period.",
            "Costs, taxes and interest are paid in the month incurred; no inventory or payables model.",
            "Monthly retention carries that share of prior-month active units into the next month, without cohort or contract-term aging. Scenario unit stresses scale new and retained units proportionally; the retention rate itself is unchanged across scenarios.",
            "A fixed-cost step adds to monthly fixed costs from its start month onward and is stressed with fixed costs.",
            "Opening cash is opening equity plus opening debt. No unmodeled rescue financing is assumed.",
            "Constant monthly capital spending; straight-line depreciation starts immediately.",
            "No tax-loss carryforwards, deferred tax or jurisdiction-specific tax calculation.",
            "DCF requires at least 12 months and positive terminal free cash flow. Multiples are not inferred.",
            "These are planning scenarios, not a market valuation or statutory accounts.",
        ],
    }


FLOW_KEYS = (
    "revenue",
    "cost_of_sales",
    "fixed_costs",
    "ebitda",
    "depreciation",
    "interest",
    "tax",
    "net_income",
    "receipts",
    "operating_cash_flow",
    "capex",
    "debt_principal",
    "net_cash_flow",
)
STOCK_KEYS = ("cash", "receivables", "net_fixed_assets", "total_assets", "debt", "equity")


def period_rollups(rows: list[dict]) -> list[dict]:
    """Group monthly rows into 12-month periods: flows are summed, balances taken at period close."""
    result = []
    with localcontext() as ctx:
        # Monthly rows are produced at precision 40; quantizing their sums at the default 28 raises.
        ctx.prec = 50
        for start in range(0, len(rows), 12):
            chunk = rows[start : start + 12]
            first, last = chunk[0]["month"], chunk[-1]["month"]
            if len(chunk) == 12:
                label = f"Year {start // 12 + 1}"
            elif first == last:
                label = f"Month {first}"
            else:
                label = f"Months {first}\u2013{last}"
            period = {"period": label}
            for k in FLOW_KEYS:
                period[k] = money(sum((D(r[k]) for r in chunk), ZERO))
            for k in STOCK_KEYS:
                period[k] = money(D(chunk[-1][k]))
            result.append(period)
    return result


# Significant digits of a returned payment. Up to the 1e12 money limit a payment, or the total of two, is below
# 4e12, so 28 digits leave 13 below the cent, and ROUND_05UP keeps the later half-even rounding to cents exact.
DEBT_SERVICE_DIGITS = 28


def _exact_context(prec: int) -> Context:
    # Inexact is trapped, so an undersized precision raises instead of silently rounding.
    return Context(
        prec=max(prec, 1), Emax=MAX_EMAX, Emin=MIN_EMIN, traps=[Inexact, InvalidOperation, Overflow]
    )


def _payment(principal: Decimal, rate: Decimal, years: int) -> tuple[Decimal, Decimal]:
    """The exact annual payment as numerator / denominator: principal × rate × s^n / (s^n − 12^n), s = 12 + rate.

    The equivalent form principal × m / (1 − (1 + m)^−n) × 12, with m = rate / 12, needs 1 + m, which has
    no exact decimal. Here both parts are exact Decimals; only their quotient needs rounding.
    """
    if not rate:
        return principal, D(years)
    n = years * 12
    exact_digits = len(principal.as_tuple().digits) + 8
    # A validated rate has at most 12 decimal places, so s has at most 14 digits and s^n at most 14n.
    rate = rate.normalize(Context(prec=len(rate.as_tuple().digits)))
    s_digits = max(rate.adjusted() + 2, 3) + max(-rate.as_tuple().exponent, 0)
    ctx = _exact_context(exact_digits + len(rate.as_tuple().digits) + s_digits * n)
    growth = ctx.power(ctx.add(D(12), rate), n)
    numerator = ctx.multiply(ctx.multiply(principal, rate), growth)
    return numerator, ctx.subtract(growth, ctx.power(D(12), n))


def _rounded(numerator: Decimal, denominator: Decimal) -> Decimal:
    # Round once, keeping at least 28 digits and three below the cent, so money() of the result equals the
    # exact quotient rounded half-even to cents.
    magnitude = numerator.adjusted() - denominator.adjusted() + 2
    digits = max(DEBT_SERVICE_DIGITS, magnitude + 3)
    context = Context(prec=digits, rounding=ROUND_05UP, Emax=MAX_EMAX, Emin=MIN_EMIN)
    return context.divide(numerator, denominator)


def _times(a: Decimal, b: Decimal) -> Decimal:
    return _exact_context(len(a.as_tuple().digits) + len(b.as_tuple().digits)).multiply(a, b)


def _plus(a: Decimal, b: Decimal) -> Decimal:
    # Enough digits to span both values from the highest digit to the lowest, plus one for a carry.
    top, bottom = max(a.adjusted(), b.adjusted()), min(a.as_tuple().exponent, b.as_tuple().exponent)
    return _exact_context(top - bottom + 2).add(a, b)


def annual_debt_service(principal: Decimal, rate: Decimal, years: int) -> Decimal:
    """Level monthly payment of one loan, annualized, rounded once from its exact value."""
    if not principal:
        return ZERO
    return _rounded(*_payment(principal, rate, years))


def total_annual_debt_service(*loans: tuple[Decimal, Decimal, int]) -> Decimal:
    """Total annualized level payment of several (principal, rate, years) loans.

    The loans' exact payments are added as one exact fraction and rounded once, as annual_debt_service rounds
    one payment, so money() of the total equals the exact total rounded half-even to cents.
    """
    parts = [_payment(p, r, y) for p, r, y in loans if p]
    if not parts:
        return ZERO
    numerator, denominator = parts[0]
    for num, den in parts[1:]:
        numerator = _plus(_times(numerator, den), _times(num, denominator))
        denominator = _times(denominator, den)
    return _rounded(numerator, denominator)


def acquisition(inputs: DealInputs, verified_evidence: set[str]):
    a = inputs
    accepted, excluded = [], []
    normalized = a.reported_ebitda - a.replacement_management
    for item in a.adjustments:
        row = item.model_dump(mode="json")
        if item.evidence_id and item.evidence_id in verified_evidence:
            accepted.append(row)
            normalized += item.amount
        else:
            excluded.append({**row, "reason": "A reviewed evidence record is required."})
    uses = a.purchase_price + a.fees + a.working_capital + a.integration_cost
    service = total_annual_debt_service(
        (a.senior_debt, a.debt_rate, a.debt_years), (a.seller_note, a.seller_rate, a.seller_years)
    )
    debt_interest = a.senior_debt * a.debt_rate + a.seller_note * a.seller_rate
    scenarios = {}
    for name, earnings in [
        ("base", normalized),
        ("downside", normalized - a.annual_revenue * D("0.10")),
        ("upside", normalized + a.annual_revenue * D("0.05")),
    ]:
        tax = max(earnings - debt_interest, ZERO) * a.tax_rate
        available = earnings - tax - a.annual_capex - a.annual_working_capital_growth
        after = available - service
        scenarios[name] = {
            "ebitda": money(earnings),
            "cash_available_for_debt_service": money(available),
            "cash_after_debt_service": money(after),
            "coverage_ratio": ratio(available / service) if service else None,
            "cash_shortfall": after < 0,
        }
    low, high = normalized * a.multiple_low, normalized * a.multiple_high
    return {
        "model_version": ACQUISITION_VERSION,
        "inputs": a.model_dump(mode="json"),
        "normalized_ebitda": money(normalized),
        "accepted_adjustments": accepted,
        "excluded_adjustments": excluded,
        "enterprise_value_range": [money(low), money(high)] if normalized > 0 else None,
        "equity_value_range": [
            money(low + a.cash_acquired - a.debt_assumed),
            money(high + a.cash_acquired - a.debt_assumed),
        ]
        if normalized > 0
        else None,
        "sources_and_uses": {
            "purchase_price": money(a.purchase_price),
            "fees": money(a.fees),
            "working_capital": money(a.working_capital),
            "integration_cost": money(a.integration_cost),
            "total_uses": money(uses),
            "senior_debt": money(a.senior_debt),
            "seller_note": money(a.seller_note),
            "buyer_equity_required": money(uses - a.senior_debt - a.seller_note),
        },
        "annual_debt_service": money(service),
        "scenarios": scenarios,
        "limitations": [
            "Multiples are user assumptions, not licensed transaction comparables.",
            "Purchase price is the negotiated cash-equivalent consideration; existing claims must be explicitly included in transaction uses.",
            "Debt service is level monthly amortization, annualized. Tax uses an approximate first-year interest estimate and excludes depreciation.",
            "Downside subtracts 10% of revenue from EBITDA with no cost relief. Upside adds 5% of revenue; synergies are excluded.",
            "Evidence review records a human attestation; it is not an audit or a quality-of-earnings opinion.",
        ],
    }
