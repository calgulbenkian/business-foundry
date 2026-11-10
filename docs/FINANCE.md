# Financial methodology

All models are deterministic and use Python Decimal arithmetic. Outputs are decimal strings rounded to cents for money and four decimal places for ratios. Inputs are assumptions unless supported by reviewed evidence; the models are not audited accounts, licensed market comparables, a fairness opinion or individualized investment advice.

## Operating model

The forecast supports 1–60 months. A month is 30 days. Model version 1.1.0 calculates active units and fixed costs in month `t` (1-indexed) as:

- `new_units_t = monthly_units × volume_factor × (1 + monthly_growth)^(t−1)`
- `units_t = new_units_t + monthly_retention × units_{t−1}`, with `units_0 = 0`
- `fixed_t = (fixed_costs + (fixed_cost_step if t ≥ fixed_cost_step_month else 0)) × fixed_factor`

Revenue is units × price. Cost of sales is units × unit cost; fixed costs are paid in their month. With the default `monthly_retention = 0` and no fixed-cost step, the results equal version 1.0.0.

`monthly_retention` (0–1) carries that share of the prior month's active units into the next month, an aggregate recurring-unit carryover. It is not a per-cohort ledger and has no contract terms, tiers or aging. `fixed_cost_step` is an extra monthly fixed cost (for example a hire or a lease) from `fixed_cost_step_month` onward. A positive step requires a start month within the forecast horizon, and a start month requires a positive step. The assumptions should include wages, benefits, software, support, insurance and other operating expenses appropriate to the business.

Receipts lag billed revenue by the selected collection delay, including a fractional-month interpolation. For a 45-day delay, half of a month's sales is collected the following month and half in the month after that. Revenue less receipts accumulates in receivables; initial receivables are zero. Costs do not wait for customer payment.

Opening cash equals contributed equity plus opening debt. Interest accrues on opening monthly debt. Principal repayments stop at zero outstanding debt. Constant monthly capex is depreciated straight-line over the selected asset life, starting in the purchase month. Tax is a planning rate on positive EBIT less interest, paid in the same month; no tax refund is invented for losses.

The income statement calculates revenue, cost of sales, fixed costs, EBITDA, depreciation, interest, tax and net income. The cash flow statement starts from actual modeled receipts and cash expenses, then deducts capex and debt principal. The balance sheet includes cash, receivables, net fixed assets, outstanding debt and equity plus retained income. The unrounded identity `assets − liabilities − equity = 0` is checked each month. Individually displayed rounded lines can differ by a cent.

Negative cash explicitly means an unfunded requirement. It remains visible rather than creating hidden borrowing. Additional cash needed is the absolute value of the lowest negative balance. It is an estimate under the chosen assumptions, not a committed financing amount.

| Scenario | Units relative to base | Fixed costs relative to base |
|---|---:|---:|
| Base | 100% | 100% |
| Downside | 75% | 110% |
| Upside | 125% | 105% |

The unit factor multiplies new units. Because the retention recurrence is linear, it scales retained units and total units in every month by the same factor. The retention rate itself is the same in every scenario. The fixed-cost factor applies to both base fixed costs and the scheduled step.

These stresses are not probability estimates. Create additional model versions to explore price, churn or retention, sales timing, funding, collection delays or other assumptions. There is no per-cohort recurring-revenue engine, inventory, accounts payable, multi-currency consolidation, sales/VAT tax engine, tax-loss carryforward, deferred tax, complex financing or statutory reporting in v0.1. The tax-rate input is not a jurisdictional tax recommendation.

## Discounted cash flow

Unlevered free cash flow equals EBIT less modeled tax on positive EBIT, plus depreciation, less capex and the change in receivables. Each monthly flow is discounted at the selected effective annual rate with a fractional-year exponent.

The terminal cash-flow base is the last 12 months of unlevered free cash flow, multiplied by `1 + terminal growth`. Terminal value is that amount divided by `discount rate − terminal growth`, discounted to the present. The discount rate must exceed terminal growth. Fewer than 12 months, or a nonpositive terminal cash-flow base, suppresses DCF output.

Enterprise value is the sum of the discounted operating and terminal values. Equity value adds the separate excess-cash input and subtracts opening debt. Do not treat operating cash already needed by the business as excess cash. Terminal share is displayed because it reveals how much of the result depends on an assumption beyond the forecast horizon. High growth in the final forecast year can materially distort the terminal base; normalize a new scenario before relying on it. A scenario value is not evidence of a buyer's price.

## Acquisition screening

Normalized EBITDA starts with reported EBITDA, subtracts the entered replacement-management cost, and includes only adjustments linked to human-reviewed evidence. Unsupported seller add-backs remain visible and excluded. Supported adjustments can be positive or negative. Review does not make a source independently audited, and normalization does not verify the reported starting EBITDA.

The enterprise-value range is normalized EBITDA × the user-entered low/high multiples. No market multiple is fetched or inferred. Nonpositive earnings suppress the range. The valuation equity bridge adds the entered cash and subtracts entered debt; this bridge is distinct from transaction funding.

Transaction uses comprise negotiated purchase consideration, fees, initial working-capital funding and integration costs. Sources comprise new senior debt, seller financing and required buyer equity. Debt funding cannot exceed uses. Enter a cash-equivalent consideration consistent with the chosen asset/share transaction. Existing debt repayment, retained liabilities, acquired cash availability, working-capital pegs and closing adjustments require explicit treatment in these inputs and the diligence record. The `debt_assumed` field is only a valuation bridge; it does not add financing or debt service. A deal retaining separate target debt needs an extended debt schedule before commitment.

Senior and seller financing each use level monthly amortization, annualized. Zero-interest loans divide principal evenly over the term. The base tax approximation uses initial annual interest and excludes depreciation. Cash available for debt service is normalized earnings less that tax, maintenance capex and working-capital growth. The displayed coverage ratio divides this cash by annual debt service; it is not necessarily the definition used in a lender covenant. With no debt service, the ratio is null rather than infinite.

The downside subtracts 10% of revenue from EBITDA with no cost relief. The upside adds 5% of revenue. These explicit illustrative stresses exclude automatic synergies, debt repricing, purchase accounting, earnouts, exit proceeds and equity IRR. A source change marks the saved deal stale; save and review a new analysis to use the changed evidence.

## Validation

The tests compare the first operating cycle with independent hand calculations (including unit retention and fixed-cost step cases in every scenario), check 0/15/30/45/60/90-day receipt timing and revenue conservation, trace interest/principal/depreciation through all statements, and verify loss treatment, funding gaps, asset lives and DCF. Acquisition tests check supported versus unsupported adjustments, amortization, zero debt, sources equaling uses and invalid input combinations. No live company financials are used in the test fixtures.
