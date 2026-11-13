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
FORECAST_VERSION, ACQUISITION_VERSION = "1.1.1", "1.1.0"


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


# Significant digits of a payment returned by annual_debt_service and total_annual_debt_service. Up to the 1e12
# money limit a payment, or the total of two, is below 4e12, so 28 digits leave 13 below the cent, and ROUND_05UP
# keeps the later half-even rounding to cents exact. The acquisition screen displays this rounded total but never
# calculates with it: cash after debt service and coverage use the exact fraction (_debt_service_fraction).
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
    # Round once, keeping at least 28 digits and two below the cent, so money() of the result equals the
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
    return _rounded(*_sum_of_fractions(parts))


def _sum_of_fractions(parts: list[tuple[Decimal, Decimal]]) -> tuple[Decimal, Decimal]:
    numerator, denominator = parts[0]
    for num, den in parts[1:]:
        numerator = _plus(_times(numerator, den), _times(num, denominator))
        denominator = _times(denominator, den)
    return numerator, denominator


def _debt_service_fraction(*loans: tuple[Decimal, Decimal, int]) -> tuple[Decimal, Decimal]:
    """The exact total annual payment of the loans as numerator / denominator, with a positive denominator.

    Each loan's denominator is its term in years (zero rate) or s^n − 12^n with s = 12 + rate > 12 (a positive
    rate), so it is positive for every accepted loan, and the total's denominator is their product. A rate that
    validation would refuse can give a negative denominator; both parts are then negated, which leaves the
    quotient unchanged. With no loan that has a principal the fraction is 0 / 1.
    """
    parts = [_payment(p, r, y) for p, r, y in loans if p]
    if not parts:
        return ZERO, ONE
    numerator, denominator = _sum_of_fractions(parts)
    if denominator < 0:
        numerator, denominator = numerator.copy_negate(), denominator.copy_negate()
    return numerator, denominator


# Exact arithmetic for the acquisition screen. Every sum, difference and product is formed in a context sized to
# its operands with Inexact trapped, so no intermediate value is ever rounded; the digit counts an accepted input
# can reach at each step are listed in docs/FINANCE.md and checked in tests/test_finance_bounds.py.


def _sized(prec: int) -> Context:
    # A precision beyond the decimal module's limit is only reachable by inputs that bypass validation.
    if prec > MAX_PREC:
        raise InvalidOperation("An exact result would need more digits than Decimal supports.")
    return _exact_context(prec)


def _mul(a: Decimal, b: Decimal) -> Decimal:
    if a.is_zero() or b.is_zero():
        return ZERO
    return _sized(len(a.as_tuple().digits) + len(b.as_tuple().digits)).multiply(a, b)


def _add(a: Decimal, b: Decimal) -> Decimal:
    # A zero is skipped, so a zero written with an extreme exponent never sizes the context.
    if a.is_zero():
        return b
    if b.is_zero():
        return a
    top, bottom = max(a.adjusted(), b.adjusted()), min(a.as_tuple().exponent, b.as_tuple().exponent)
    return _sized(top - bottom + 2).add(a, b)


def _sub(a: Decimal, b: Decimal) -> Decimal:
    return _add(a, b.copy_negate())


def _quotient(numerator: Decimal, denominator: Decimal, places: Decimal) -> Decimal:
    """numerator / denominator for display at `places`: _fixed(result, places) is the exact quotient rounded
    half-even once.

    The precision is sized to the quotient's own magnitude and places, keeping at least two digits below the
    last displayed place, whatever the size of the quotient (a coverage ratio can exceed 1E+25). ROUND_05UP
    leaves a final 0 or 5 only on an exact result, so the later half-even rounding sees ties only where the exact
    quotient has one.
    """
    if numerator.is_zero():
        return ZERO
    # The quotient's adjusted exponent is at most numerator.adjusted() − denominator.adjusted().
    digits = numerator.adjusted() - denominator.adjusted() - places.as_tuple().exponent + 3
    if digits > MAX_PREC:
        raise InvalidOperation("The quotient is too large to display.")
    context = Context(prec=max(digits, 1), rounding=ROUND_05UP, Emax=MAX_EMAX, Emin=MIN_EMIN)
    return context.divide(numerator, denominator)


def _accepted(item, verified_evidence: set[str]) -> bool:
    return bool(item.evidence_id and item.evidence_id in verified_evidence)


SCENARIOS = (("base", ZERO, ZERO), ("downside", D("0.10"), ZERO), ("upside", ZERO, D("0.05")))


def _growth(rate: Decimal, years: int) -> Decimal:
    """(1 + rate)^years, exactly: one year's interest compounds into the next once a year."""
    factor, result = _add(ONE, rate), ONE
    for _ in range(years):
        result = _mul(result, factor)
    return result


def _seller_payments(a: DealInputs) -> list[Decimal | tuple[Decimal, Decimal]]:
    """What the seller is due in each year of the note's term, years 1 to seller_years (docs/FINANCE.md).

    An amortizing note is due its level annual payment every year, kept as the exact fraction
    (numerator, denominator) of _payment. An interest-only note is due principal × rate each year and the
    principal as well in the last. A deferred note is due nothing until the last year, when the principal
    and the interest accrued on it, compounded once a year, are due: principal × (1 + rate)^years.
    """
    n, principal, rate = a.seller_years, a.seller_note, a.seller_rate
    if a.seller_schedule == "amortizing":
        return [_payment(principal, rate, n)] * n
    if a.seller_schedule == "interest_only":
        interest = _mul(principal, rate)
        return [interest] * (n - 1) + [_add(interest, principal)]
    return [ZERO] * (n - 1) + [_mul(principal, _growth(rate, n))]


def _deal_values(a: DealInputs, verified_evidence: set[str]) -> dict:
    """The exact value of every acquisition figure and of each step that forms it (docs/FINANCE.md).

    Nothing here is rounded. Each figure that depends on debt service is kept over the exact debt-service
    denominator: cash after debt service is after_numerator / denominator and coverage is
    scaled_available / numerator, where scaled_available = available × denominator. The debt service is
    what is due in the screened year, year 1: the senior and property loans' level payments and the seller
    note's year-1 payment under its schedule. The amortizing figures (amortizing_numerator over
    amortizing_denominator) count the seller note as if it amortized over its term, as a lender may.

    A loan of zero adds no payment (_debt_service_fraction skips it) and no interest (_mul of a zero is
    zero), so its rate, term and schedule enter no figure. DealInputs.assumed_inputs relies on this: it
    lists one the caller did not give only when its loan is above zero. A zero real estate price, rent or
    investor equity adds no operation (_add and _sub skip a zero), so a deal that gives none of the inputs
    added in 1.1.0 is calculated by exactly the steps of 1.0.1.
    """
    normalized = _sub(a.reported_ebitda, a.replacement_management)
    for item in a.adjustments:
        if _accepted(item, verified_evidence):
            normalized = _add(normalized, item.amount)
    normalized = _sub(normalized, a.annual_rent)
    uses = _add(_add(_add(a.purchase_price, a.fees), a.working_capital), a.integration_cost)
    uses = _add(uses, a.real_estate_price)
    senior = (a.senior_debt, a.debt_rate, a.debt_years)
    seller = (a.seller_note, a.seller_rate, a.seller_years)
    building = (a.property_debt, a.property_rate, a.property_years)
    amortizing_numerator, amortizing_denominator = _debt_service_fraction(senior, seller, building)
    payments = _seller_payments(a) if a.seller_note else []
    if not payments or a.seller_schedule == "amortizing":
        numerator, denominator = amortizing_numerator, amortizing_denominator
    else:
        numerator, denominator = _debt_service_fraction(senior, building)
        numerator = _add(numerator, _mul(payments[0], denominator))
    interest = _add(_mul(a.senior_debt, a.debt_rate), _mul(a.seller_note, a.seller_rate))
    interest = _add(interest, _mul(a.property_debt, a.property_rate))
    buyer_equity = _sub(_sub(_sub(uses, a.senior_debt), a.seller_note), a.property_debt)
    values = {
        "normalized": normalized,
        "uses": uses,
        "buyer_equity": buyer_equity,
        "buyer_cash": _sub(buyer_equity, a.investor_equity),
        "interest": interest,
        "numerator": numerator,
        "denominator": denominator,
        "amortizing_numerator": amortizing_numerator,
        "amortizing_denominator": amortizing_denominator,
        "seller_payments": payments,
        "scenarios": {},
    }
    if payments and a.seller_schedule != "amortizing":
        # The principal is due at the end of the term; a deferred note's last payment adds the accrued interest.
        values["balloon"] = payments[-1] if a.seller_schedule == "deferred" else a.seller_note
    for name, cut, lift in SCENARIOS:
        earnings = _add(_sub(normalized, _mul(a.annual_revenue, cut)), _mul(a.annual_revenue, lift))
        taxable = _sub(earnings, interest)
        tax = _mul(max(taxable, ZERO), a.tax_rate)
        available = _sub(_sub(_sub(earnings, tax), a.annual_capex), a.annual_working_capital_growth)
        scaled = _mul(available, denominator)
        values["scenarios"][name] = {
            "earnings": earnings,
            "taxable": taxable,
            "tax": tax,
            "available": available,
            "scaled_available": scaled,
            "after_numerator": _sub(scaled, numerator),
            "amortizing_scaled_available": _mul(available, amortizing_denominator),
        }
    if normalized > 0:
        low, high = _mul(normalized, a.multiple_low), _mul(normalized, a.multiple_high)
        bridge = _sub(a.cash_acquired, a.debt_assumed)
        values["value_range"] = (low, high)
        values["equity_range"] = (_add(low, bridge), _add(high, bridge))
    return values


def _payment_money(payment: Decimal | tuple[Decimal, Decimal]) -> str:
    """A seller payment shown in cents: an exact Decimal, or an amortizing payment's exact fraction rounded once."""
    return money(_rounded(*payment)) if isinstance(payment, tuple) else money(payment)


def _seller_schedule(a: DealInputs, v: dict) -> dict | None:
    if not a.seller_note:
        return None
    balloon = None
    if "balloon" in v:
        amount = v["balloon"]
        balloon = {
            "year": a.seller_years,
            "amount": money(amount),
            "principal": money(a.seller_note),
            "accrued_interest": money(_sub(amount, a.seller_note)),
        }
    return {
        "schedule": a.seller_schedule,
        "years": a.seller_years,
        "payments": [
            {"year": year, "payment": _payment_money(p)} for year, p in enumerate(v["seller_payments"], 1)
        ],
        "balloon": balloon,
    }


def acquisition(inputs: DealInputs, verified_evidence: set[str]):
    a = inputs
    accepted, excluded = [], []
    for item in a.adjustments:
        row = item.model_dump(mode="json")
        if _accepted(item, verified_evidence):
            accepted.append(row)
        else:
            excluded.append({**row, "reason": "A reviewed evidence record is required."})
    v = _deal_values(a, verified_evidence)
    numerator, denominator = v["numerator"], v["denominator"]
    scenarios = {}
    for name, s in v["scenarios"].items():
        scenarios[name] = {
            "ebitda": money(s["earnings"]),
            "cash_available_for_debt_service": money(s["available"]),
            "cash_after_debt_service": money(_quotient(s["after_numerator"], denominator, CENT)),
            "coverage_ratio": ratio(_quotient(s["scaled_available"], numerator, BASIS))
            if not numerator.is_zero()
            else None,
            # The denominator is positive, so the numerator's sign is the sign of the exact cash after debt service.
            "cash_shortfall": s["after_numerator"] < 0,
            "amortizing_coverage_ratio": ratio(
                _quotient(s["amortizing_scaled_available"], v["amortizing_numerator"], BASIS)
            )
            if not v["amortizing_numerator"].is_zero()
            else None,
        }
    return {
        "model_version": ACQUISITION_VERSION,
        "inputs": a.model_dump(mode="json"),
        "normalized_ebitda": money(v["normalized"]),
        "accepted_adjustments": accepted,
        "excluded_adjustments": excluded,
        "enterprise_value_range": [money(x) for x in v["value_range"]] if "value_range" in v else None,
        "equity_value_range": [money(x) for x in v["equity_range"]] if "equity_range" in v else None,
        "sources_and_uses": {
            "purchase_price": money(a.purchase_price),
            "fees": money(a.fees),
            "working_capital": money(a.working_capital),
            "integration_cost": money(a.integration_cost),
            "total_uses": money(v["uses"]),
            "senior_debt": money(a.senior_debt),
            "seller_note": money(a.seller_note),
            "buyer_equity_required": money(v["buyer_equity"]),
            "real_estate_price": money(a.real_estate_price),
            "property_debt": money(a.property_debt),
            "investor_equity": money(a.investor_equity),
            "buyer_cash_at_closing": money(max(v["buyer_cash"], ZERO)),
            "funding_beyond_uses": money(max(v["buyer_cash"].copy_negate(), ZERO)),
        },
        # Displayed only: the exact fraction rounded once to cents, as total_annual_debt_service rounds it.
        "annual_debt_service": money(_rounded(numerator, denominator)),
        "debt_service_year": 1,
        "amortizing_annual_debt_service": money(
            _rounded(v["amortizing_numerator"], v["amortizing_denominator"])
        ),
        "seller_note_schedule": _seller_schedule(a, v),
        "scenarios": scenarios,
        "limitations": [
            "Multiples are user assumptions, not licensed transaction comparables. They value the business only; real estate is shown at its entered price and is not valued.",
            "Purchase price is the negotiated cash-equivalent consideration; existing claims must be explicitly included in transaction uses.",
            "Senior and property debt service is level monthly amortization, annualized. The seller note follows its schedule, and coverage uses what is due in year 1; the amortizing coverage counts the seller note as if it amortized over its term. Tax uses an approximate first-year interest estimate, paid or accrued, and excludes depreciation.",
            "Rent is deducted from normalized EBITDA; enter only rent the reported EBITDA does not already bear. Investor equity is a source of funds; investor returns are not modeled.",
            "Downside subtracts 10% of revenue from EBITDA with no cost relief. Upside adds 5% of revenue; synergies are excluded.",
            "Evidence review records a human attestation; it is not an audit or a quality-of-earnings opinion.",
        ],
    }
