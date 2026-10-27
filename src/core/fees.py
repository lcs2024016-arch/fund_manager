"""Fee model (Delta Exchange India) + 18% GST.

Options schedule (VERIFIED against live probing + collector research,
validate/analyze_options.py, API_NOTES.md):
    fee = min(0.01% * notional, 3.5% * premium)  then x (1 + 18% GST)
Perps/futures schedule (UNVERIFIED from docs — treat as config):
    linear = rate * notional, then x (1 + GST); defaults are the published
    standard tier but MUST be confirmed against the fee schedule page before
    any live run; the config `fees.linear_taker_bps / linear_maker_bps` allow
    changing without code.
"""

from __future__ import annotations

from decimal import Decimal

ZERO = Decimal("0")
FEE_NOTIONAL_BPS = Decimal("0.0001")  # 1bp of notional  (verified)
FEE_CAP_OF_PREMIUM = Decimal("0.035")  # cap 3.5% of premium (verified)
GST = Decimal("0.18")  # 18% GST            (verified)
LinearDefaults = {"maker": Decimal("0.0005"), "taker": Decimal("0.0020")}
# NOTE(UNVERIFIED): LinearDefaults must be re-read from the official fee
# schedule before any live run; see RISK.md "Fees".


def option_fee(
    underlying_price: Decimal,
    premium_per_coin: Decimal,
    contract_value: Decimal,
    size: int,
) -> Decimal:
    """Total fee (incl. GST) for an option fill; >= 0."""
    if size <= 0:
        return ZERO
    notional = underlying_price * contract_value * size
    premium_usd_ = premium_per_coin * contract_value * size
    fee = min(FEE_NOTIONAL_BPS * notional, FEE_CAP_OF_PREMIUM * premium_usd_)
    return fee * (Decimal("1") + GST)


def linear_fee(
    notional_usd: Decimal,
    side_taker: bool,
    maker_bps: Decimal | None = None,
    taker_bps: Decimal | None = None,
) -> Decimal:
    """Perp/futures fee (incl. GST rate of the option schedule; UNVERIFIED
    rate — pass explicit config values once confirmed)."""
    if notional_usd <= ZERO:
        return ZERO
    bps = (taker_bps if side_taker else maker_bps) or (
        LinearDefaults["taker" if side_taker else "maker"]
    )
    return notional_usd * bps * (Decimal("1") + GST)
