"""
spread_gate.py — Hard NO-TRADE gate for bull call spreads.

Evaluates a proposed spread using optionlab and returns a binary
TRADE / NO-TRADE verdict with reasons for any failed checks.
Failure to evaluate is always a block, never a pass.
"""

from optionlab import run_strategy, Inputs


def evaluate_spread_gate(
    spot: float,
    iv: float,
    long_strike: float,
    short_strike: float,
    long_premium: float,
    short_premium: float,
    entry_date: str,
    exit_date: str,
    expiration: str,
    risk_free_rate: float = 0.045,
    commission_per_contract: float = 3.0,
    min_pop: float = 0.40,
    min_ev_per_contract: float = 0.0,
) -> dict:
    """
    Evaluate a bull call spread against hard NO-TRADE criteria.

    Parameters
    ----------
    spot : float
        Current underlying price.
    iv : float
        Implied volatility as a decimal (e.g. 0.28 for 28%).
    long_strike : float
        Strike of the leg we buy.
    short_strike : float
        Strike of the leg we sell.
    long_premium : float
        Mid price of the long leg.
    short_premium : float
        Mid price of the short leg.
    entry_date : str
        Trade entry date "YYYY-MM-DD".
    exit_date : str
        Hard exit date "YYYY-MM-DD" (e.g. day before earnings).
    expiration : str
        Option expiration date "YYYY-MM-DD".
    risk_free_rate : float
        Annualised risk-free rate (default 4.5%).
    commission_per_contract : float
        Round-trip commission for both legs in dollars (default $3).
    min_pop : float
        Minimum acceptable probability of profit (default 0.40).
    min_ev_per_contract : float
        Minimum acceptable expected value per contract in dollars (default $0).

    Returns
    -------
    dict with keys:
        verdict : "TRADE" or "NO-TRADE" (no other values ever returned)
        pop : float — probability of profit from optionlab
        ev_per_contract : float — EV in dollars after commission
        reasons : list[str] — failed checks; empty when verdict is TRADE
    """
    try:
        inputs = Inputs(
            stock_price=spot,
            volatility=iv,
            interest_rate=risk_free_rate,
            min_stock=spot * 0.6,
            max_stock=spot * 1.4,
            start_date=entry_date,
            target_date=exit_date,
            calculations=["pop", "expectation"],
            strategy=[
                {
                    "type": "call",
                    "strike": long_strike,
                    "premium": long_premium,
                    "n": 1,
                    "action": "buy",
                    "expiration": expiration,
                },
                {
                    "type": "call",
                    "strike": short_strike,
                    "premium": short_premium,
                    "n": 1,
                    "action": "sell",
                    "expiration": expiration,
                },
            ],
        )
        out = run_strategy(inputs)
    except Exception as exc:
        return {
            "verdict": "NO-TRADE",
            "pop": 0.0,
            "ev_per_contract": 0.0,
            "reasons": [f"gate error: {exc}"],
        }

    pop = out.probability_of_profit
    ev_per_share = (
        pop * out.expected_profit_if_profitable
        + (1 - pop) * out.expected_loss_if_unprofitable
    )
    ev_per_contract = ev_per_share * 100 - commission_per_contract

    reasons = []
    if pop < min_pop:
        reasons.append(f"PoP {pop:.2f} < {min_pop:.2f}")
    if ev_per_contract < min_ev_per_contract:
        reasons.append(
            f"EV ${ev_per_contract:.2f} < ${min_ev_per_contract:.2f}"
        )

    verdict = "TRADE" if not reasons else "NO-TRADE"

    return {
        "verdict": verdict,
        "pop": pop,
        "ev_per_contract": ev_per_contract,
        "reasons": reasons,
    }
