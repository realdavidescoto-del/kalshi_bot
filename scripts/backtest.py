#!/usr/bin/env python3
"""Backtesting harness for Kalshi bot strategies.

Usage:
    python scripts/backtest.py --indicator CPI --series CPIAUCSL --threshold 0.02

Reads historical FRED data, simulates trades using the current strategy logic,
and reports performance metrics.
"""

import argparse
import logging
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from safety.risk_manager import RiskManager
from strategy.forecast_provider import (
    FredSurveyForecastProvider,
    TrailingAverageForecastProvider,
    compute_surprise_std,
    scale_conviction,
)


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backtest")


def fetch_fred_history(series_id: str, limit: int = 60) -> list[dict]:
    from config import Config

    if not Config.FRED_API_KEY:
        logger.error("FRED_API_KEY not configured. Set it in .env")
        return []

    import requests

    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": Config.FRED_API_KEY,
        "file_type": "json",
        "sort_order": "asc",
        "limit": limit,
    }
    resp = requests.get(url, params=params, timeout=10)
    if resp.status_code != 200:
        logger.error(f"FRED API error: {resp.text}")
        return []

    data = resp.json()
    observations = data.get("observations", [])
    result = []
    for obs in observations:
        val = obs.get("value", "").strip()
        if val not in ("", ".", "NaN"):
            try:
                result.append({"date": obs["date"], "value": float(val)})
            except ValueError:
                pass
    return result


def simulate_trade(
    actual: float,
    forecast: float,
    std_dev: float,
    market_price: Decimal,
    risk_manager: RiskManager,
    total_capital: Decimal,
) -> dict:
    actual_dec = Decimal(str(actual))
    forecast_dec = Decimal(str(forecast))

    if actual_dec == forecast_dec:
        return {"action": "none"}

    side = "yes" if actual_dec > forecast_dec else "no"
    prob = scale_conviction(actual_dec, forecast_dec, std_dev)

    kelly = risk_manager.calculate_kelly_fraction(prob, market_price, side)
    wager = risk_manager.size_order(
        estimated_prob=prob,
        market_price=market_price,
        side=side,
        sector="Economics",
        current_sector_exposure=Decimal("0"),
        total_capital=total_capital,
    )
    return {
        "action": "trade",
        "side": side,
        "prob": float(prob),
        "kelly": float(kelly),
        "wager": float(wager),
    }


def run_backtest(
    indicator: str,
    series_id: str,
    threshold: float,
    capital: Decimal,
    max_trades: int = 50,
):
    logger.info(f"Fetching {limit} observations for {series_id}...")
    history = fetch_fred_history(series_id, limit=max_trades + 12)

    if len(history) < 3:
        logger.error("Need at least 3 data points to backtest")
        return

    logger.info(
        f"Loaded {len(history)} observations from {history[0]['date']} to {history[-1]['date']}"
    )

    risk_manager = RiskManager()
    results = []
    wins = 0
    losses = 0
    total_pnl = Decimal("0")

    for i in range(2, len(history)):
        actual = history[i]["value"]
        recent_values = [h["value"] for h in history[max(0, i - 12) : i]]

        survey_fcst = FredSurveyForecastProvider().get_forecast(indicator, series_id)
        if survey_fcst is not None:
            forecast = survey_fcst
        else:
            trailing = TrailingAverageForecastProvider()
            forecast = trailing.get_forecast(indicator, series_id, recent_values=recent_values)
        if forecast is None:
            forecast = history[i - 1]["value"]

        std_dev = compute_surprise_std(recent_values)
        surprise = abs(actual - forecast)
        sigma = surprise / std_dev if std_dev > 0 else 0

        if sigma < threshold:
            continue

        market_price = Decimal("0.60")
        trade = simulate_trade(actual, forecast, std_dev, market_price, risk_manager, capital)
        if trade["action"] == "none":
            continue

        results.append(
            {
                "date": history[i]["date"],
                "actual": actual,
                "forecast": forecast,
                "sigma": round(sigma, 2),
                "side": trade["side"],
                "prob": round(trade["prob"], 4),
                "wager": round(trade["wager"], 2),
            }
        )

        if len(results) >= max_trades:
            break

    logger.info(f"\n=== BACKTEST RESULTS ({len(results)} trades) ===")
    for r in results:
        logger.info(
            f"{r['date']}: sigma={r['sigma']:.1f} {r['side']} "
            f"prob={r['prob']:.2f} wager=${r['wager']:.2f}"
        )

    if results:
        total_wager = sum(r["wager"] for r in results)
        logger.info(f"\nTotal wagered: ${total_wager:.2f}")
        logger.info(f"Avg sigma: {sum(r['sigma'] for r in results) / len(results):.2f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalshi bot backtest")
    parser.add_argument("--indicator", default="CPI", help="Indicator name")
    parser.add_argument("--series", default="CPIAUCSL", help="FRED series ID")
    parser.add_argument("--threshold", type=float, default=0.5, help="Minimum sigma to trade")
    parser.add_argument("--capital", type=float, default=10000.0, help="Starting capital")
    parser.add_argument("--max-trades", type=int, default=50, help="Max trades to simulate")
    args = parser.parse_args()

    limit = args.max_trades + 12
    run_backtest(
        indicator=args.indicator,
        series_id=args.series,
        threshold=args.threshold,
        capital=Decimal(str(args.capital)),
        max_trades=args.max_trades,
    )
