"""Covenant ratio math. Code only: the model never computes a number.

Inputs come from XBRL year-end values. EBITDA here is operating income plus depreciation
and amortization as reported; the agreement's own add-backs are not applied, so the ratio
is an approximation of the covenant-defined one, and the memo says so.
"""

from datetime import date

from covenant_monitor.agent.models import Approval, ExtractedCovenant, Fact, RatioResult
from covenant_monitor.golden import CovenantType

# First concept present wins.
DA = [
    "DepreciationDepletionAndAmortization",
    "DepreciationAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
]
INTEREST = ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"]
CASH = ["CashAndCashEquivalentsAtCarryingValue"]
SHORT_TERM = ["ShortTermBorrowings", "CommercialPaper"]
# Long-term debt is tagged several ways. LongTermDebt includes current maturities; the
# others are the noncurrent part only, so the current part is added separately.
NONCURRENT = ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"]
CURRENT = ["DebtCurrent", "LongTermDebtCurrent"]
CONCEPTS = sorted(
    {
        "OperatingIncomeLoss",
        "LongTermDebt",
        *NONCURRENT,
        *CURRENT,
        *DA,
        *INTEREST,
        *CASH,
        *SHORT_TERM,
    }
)

MAX_TYPES = {CovenantType.MAX_LEVERAGE, CovenantType.MAX_SECURED_LEVERAGE}
MIN_TYPES = {CovenantType.MIN_INTEREST_COVERAGE, CovenantType.MIN_FIXED_CHARGE_COVERAGE}


class NotComputable(Exception):
    pass


def _first(facts: dict[str, Fact], names: list[str]) -> Fact | None:
    return next((facts[n] for n in names if n in facts), None)


def component(name: str, facts: dict[str, Fact]) -> tuple[float, dict[str, Fact]]:
    """Value of one ratio component and the facts it was built from."""
    if name == "ebitda":
        op, da = facts.get("OperatingIncomeLoss"), _first(facts, DA)
        if not op or not da:
            raise NotComputable("EBITDA needs operating income and D&A, not both reported")
        return op.value + da.value, {"OperatingIncomeLoss": op, da.concept: da}
    if name == "interest_expense":
        fact = _first(facts, INTEREST)
        if not fact:
            raise NotComputable("no interest expense reported")
        return fact.value, {fact.concept: fact}
    if name in ("total_debt", "net_debt"):
        used: dict[str, Fact] = {}
        if "LongTermDebt" in facts:
            used["LongTermDebt"] = facts["LongTermDebt"]
            extra = SHORT_TERM
        elif noncurrent := _first(facts, NONCURRENT):
            used[noncurrent.concept] = noncurrent
            # DebtCurrent already includes short-term borrowings; LongTermDebtCurrent doesn't.
            current = _first(facts, CURRENT)
            if current:
                used[current.concept] = current
            extra = [] if current and current.concept == "DebtCurrent" else SHORT_TERM
        else:
            raise NotComputable("no long-term debt reported for this fiscal year")
        for n in extra:
            if n in facts:
                used[n] = facts[n]
        total = sum(f.value for f in used.values())
        if name == "total_debt":
            return total, used
        cash = _first(facts, CASH)
        if not cash:
            raise NotComputable("net debt needs cash, which is not reported")
        used[cash.concept] = cash
        return total - cash.value, used
    raise NotComputable(f"{name.replace('_', ' ')} is not available from XBRL filings")


def _starts(step) -> date:
    return date.min if step.effective_from is None else date.fromisoformat(step.effective_from)


def applicable_threshold(cov: ExtractedCovenant, period_end: str) -> float:
    """The step in effect on the period end: the latest step that started on or before it."""
    end = date.fromisoformat(period_end)
    in_effect = [s for s in sorted(cov.thresholds, key=_starts) if _starts(s) <= end]
    if not in_effect:
        raise NotComputable(f"no threshold step in effect on {period_end}")
    return in_effect[-1].ratio


def compute(
    index: int, cov: ExtractedCovenant, facts: dict[str, Fact], approval: Approval | None = None
) -> RatioResult:
    base = RatioResult(covenant_index=index, name=cov.name, type=cov.type.value, computable=False)
    kind = cov.type.value
    if kind not in MAX_TYPES | MIN_TYPES:
        return base.model_copy(update={"reason": f"{kind} is not a ratio covenant"})
    try:
        num, num_facts = component(cov.numerator.value, facts)
        den, den_facts = component(cov.denominator.value, facts)
        if den <= 0:
            raise NotComputable("denominator is zero or negative")
        inputs = num_facts | den_facts
        period_end = max(f.period_end for f in inputs.values())
        overrides = approval.threshold_overrides if approval else {}
        threshold = overrides.get(index) or applicable_threshold(cov, period_end)
    except (NotComputable, ValueError) as e:
        return base.model_copy(update={"reason": str(e)})

    ratio = num / den
    headroom = threshold - ratio if kind in MAX_TYPES else ratio - threshold
    return base.model_copy(
        update={
            "computable": True,
            "ratio": round(ratio, 4),
            "threshold": threshold,
            "headroom": round(headroom, 4),
            "headroom_pct": round(headroom / threshold, 4),
            "period_end": period_end,
            "numerator": num,
            "denominator": den,
            "inputs": inputs,
            "threshold_overridden": index in overrides,
        }
    )
