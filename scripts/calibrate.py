#!/usr/bin/env python3
"""Calibrate conviction params against historical Kalshi trade data.

Reads strategy_performance from SQLite and finds params that maximize
the signal-to-conviction mapping quality: correlation between sigma
(z-score) and conviction, dynamic range, minimal saturation, and
actual trade profitability (populated by PositionManager on position close).

Usage:
    python scripts/calibrate.py
    python scripts/calibrate.py --indicator CPI
    python scripts/calibrate.py --walk-forward
"""

import argparse
import itertools
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data.database import get_strategy_performance, initialize_db

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("calibrate")

DEFAULT_SLOPES = [0.02, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30, 0.40]
DEFAULT_MAX_DELTAS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]


def compute_conviction(sigma: float, surprise: float, slope: float, max_delta: float) -> float:
    magnitude = abs(sigma)
    delta = min(magnitude * slope, max_delta)
    direction = 1.0 if surprise > 0 else -1.0
    return 0.50 + direction * delta


def evaluate_params(trades: list[dict], slope: float, max_delta: float) -> dict:
    sigmas = []
    convictions_norm = []
    saturated = 0
    total_trades = 0
    total_wager = 0.0
    bucket_counts: dict[str, int] = {}

    wins = 0
    total_labeled = 0

    for t in trades:
        sigma = t["sigma"]
        surprise = t["surprise"]
        wager = t["wager"] or 0.0
        p = t.get("profitable")
        if p is not None:
            total_labeled += 1
            if p == 1:
                wins += 1

        new_conviction = compute_conviction(sigma, surprise, slope, max_delta)

        bucket = _conviction_bucket(new_conviction)
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1

        sigmas.append(sigma)
        conv_norm = abs(new_conviction - 0.50) * 2.0
        convictions_norm.append(conv_norm)
        total_trades += 1
        total_wager += wager

        raw_delta = min(abs(sigma) * slope, max_delta)
        if raw_delta >= max_delta:
            saturated += 1

    unique_sigmas = len(set(sigmas))
    if unique_sigmas >= 3:
        correlation = _pearson(sigmas, convictions_norm)
    elif unique_sigmas == 2:
        correlation = 1.0
    else:
        correlation = 0.0
    dynamic_range = max(convictions_norm) - min(convictions_norm) if convictions_norm else 0.0
    sat_rate = saturated / total_trades if total_trades > 0 else 0.0

    if unique_sigmas <= 2:
        avg_sigma = sum(sigmas) / len(sigmas) if sigmas else 0.0
        delta_raw = avg_sigma * slope
        delta_used = min(delta_raw, max_delta)
        ideal_delta = 0.20
        delta_quality = max(0.0, 1.0 - abs(delta_used - ideal_delta) / ideal_delta)
        score = delta_quality * max(0.0, 1.0 - sat_rate * 0.5) * 3.0
    win_rate = wins / total_labeled if total_labeled > 0 else 0.0
    score = score * (1.0 + win_rate)

    bucket_pct = {b: round(c / total_trades, 4) for b, c in sorted(bucket_counts.items())}

    return {
        "trades": total_trades,
        "total_wager": round(total_wager, 2),
        "correlation": round(correlation, 4),
        "dynamic_range": round(dynamic_range, 4),
        "saturation": round(sat_rate, 4),
        "win_rate": round(win_rate, 4),
        "labeled_trades": total_labeled,
        "score": round(score, 4),
        "buckets": bucket_pct,
        "unique_sigmas": unique_sigmas,
    }


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 3:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    dx = sum((xi - mx) ** 2 for xi in x) ** 0.5
    dy = sum((yi - my) ** 2 for yi in y) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def _conviction_bucket(conviction: float) -> str:
    diff = abs(conviction - 0.50)
    if diff < 0.01:
        return "neutral"
    if diff < 0.05:
        return "low"
    if diff < 0.10:
        return "medium"
    if diff < 0.20:
        return "high"
    return "very_high"


def walk_forward_optimize(
    trades: list[dict],
    slopes: list[float],
    max_deltas: list[float],
    min_windows: int = 3,
) -> list[dict]:
    oos_results = []
    window_size = max(len(trades) // 4, 5)
    test_size = max(window_size // 2, 3)
    train_size = window_size - test_size
    start = train_size

    while start + test_size <= len(trades):
        train = trades[start - train_size:start]
        test = trades[start:start + test_size]

        grid_results = []
        for slope, max_delta in itertools.product(slopes, max_deltas):
            r = evaluate_params(train, slope, max_delta)
            grid_results.append({**r, "slope": slope, "max_delta": max_delta})

        grid_results.sort(key=lambda x: x["score"], reverse=True)
        best = grid_results[0]

        if best["trades"] < 3:
            start += test_size
            continue

        oos = evaluate_params(test, best["slope"], best["max_delta"])
        oos_results.append({
            "train_end": train[-1].get("timestamp", ""),
            "best_slope": best["slope"],
            "best_max_delta": best["max_delta"],
            "train_score": best["score"],
            "train_corr": best["correlation"],
            "train_trades": best["trades"],
            "oos_score": oos["score"],
            "oos_corr": oos["correlation"],
            "oos_trades": oos["trades"],
        })

        logger.info(
            f"  Window {len(oos_results)}: best slope={best['slope']:.2f} delta={best['max_delta']:.2f} "
            f"train_score={best['score']:.4f} (corr={best['correlation']:.3f}, {best['trades']}t)  ->  "
            f"OOS score={oos['score']:.4f} (corr={oos['correlation']:.3f}, {oos['trades']}t)"
        )

        start += test_size

    return oos_results


def main():
    parser = argparse.ArgumentParser(description="Calibrate Kalshi conviction params")
    parser.add_argument("--indicator", default=None, help="Filter by indicator (e.g. CPI, PCE)")
    parser.add_argument("--walk-forward", action="store_true", help="Run walk-forward optimization")
    parser.add_argument("--top-n", type=int, default=10, help="Number of top results to show")
    args = parser.parse_args()

    initialize_db()
    raw = get_strategy_performance(indicator=args.indicator)
    trades = [t for t in raw if t.get("wager") is not None and t["wager"] > 0]

    if not trades:
        logger.error("No executed trades found in strategy_performance table.")
        logger.error("Run the bot in shadow mode first to populate trade data.")
        sys.exit(1)

    logger.info(f"Loaded {len(trades)} executed trades from local database")
    if args.indicator:
        logger.info(f"  Filtered by indicator: {args.indicator}")

    slopes = DEFAULT_SLOPES
    max_deltas = DEFAULT_MAX_DELTAS

    if args.walk_forward:
        oos_results = walk_forward_optimize(trades, slopes, max_deltas)
        if not oos_results:
            logger.error("No walk-forward windows produced results.")
            sys.exit(1)

        print("\n=== WALK-FORWARD RESULTS ===")
        cols = f"{'Window':>10} | {'Slope':>5} | {'Delta':>5} | {'Train Score':>11} | {'OOS Score':>10} | {'OOS Corr':>8} | {'OOS Trades':>10}"
        print(cols)
        print("-" * len(cols))
        for r in oos_results:
            print(f"{r['train_end'][:10]:>10} | {r['best_slope']:>5.2f} | {r['best_max_delta']:>5.2f} | {r['train_score']:>11.4f} | {r['oos_score']:>10.4f} | {r['oos_corr']:>8.3f} | {r['oos_trades']:>10}")

        avg_slope = sum(r["best_slope"] for r in oos_results) / len(oos_results)
        avg_delta = sum(r["best_max_delta"] for r in oos_results) / len(oos_results)
        print(f"\nAverage best params: CONVICTION_SLOPE={avg_slope:.4f}, CONVICTION_MAX_DELTA={avg_delta:.4f}")
    else:
        results = []
        for slope, max_delta in itertools.product(slopes, max_deltas):
            r = evaluate_params(trades, slope, max_delta)
            r["slope"] = slope
            r["max_delta"] = max_delta
            results.append(r)

        results.sort(key=lambda x: x["score"], reverse=True)

        print(f"\n=== TOP {args.top_n} PARAMETER COMBINATIONS (by score) ===")
        cols = f"{'slope':>5} | {'max_delta':>5} | {'score':>7} | {'corr':>6} | {'dyn_range':>9} | {'sat_rate':>8} | {'trades':>6} | {'buckets'}"
        print(cols)
        print("-" * len(cols))
        for r in results[:args.top_n]:
            b = ", ".join(f"{k}={v:.0%}" for k, v in r.get("buckets", {}).items())
            print(f"{r['slope']:>5.2f} | {r['max_delta']:>5.2f} | {r['score']:>7.4f} | {r['correlation']:>6.3f} | {r['dynamic_range']:>9.4f} | {r['saturation']:>8.2%} | {r['trades']:>6} | {b}")

        best = results[0]
        print(f"\nRecommended: CONVICTION_SLOPE={best['slope']}, CONVICTION_MAX_DELTA={best['max_delta']}")

        print("\n=== CURRENT DEFAULTS (slope=0.12, delta=0.35) ===")
        default = evaluate_params(trades, 0.12, 0.35)
        print(f"  score={default['score']:.4f}  correlation={default['correlation']:.3f}  dyn_range={default['dynamic_range']:.4f}  sat={default['saturation']:.2%}")
        print(f"  bucket distribution: {default.get('buckets', {})}")


if __name__ == "__main__":
    main()
