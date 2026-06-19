import logging
import time
from datetime import UTC, datetime
from decimal import Decimal

from config import Config
from data.database import get_connection, log_release, log_strategy_signal
from execution.engine import ExecutionEngine
from execution.position_manager import get_position_manager
from market.order_book import LocalOrderBook
from safety.risk_manager import RiskManager
from strategy.forecast_provider import (
    AlphaVantageEconomicProvider,
    FredSurveyForecastProvider,
    TrailingAverageForecastProvider,
    scale_conviction,
    compute_surprise_std,
    compute_signal_quality,
    _safe_parse_float,
)

logger = logging.getLogger("kalshi_bot.macro_tracker")


def _evaluate_and_place(
    indicator: str,
    actual: float,
    forecast: float,
    previous: float,
    ticker: str,
    sector: str,
    risk_manager: RiskManager,
    execution_engine: ExecutionEngine,
    order_book: LocalOrderBook,
    total_capital: Decimal,
    surprise_std: float = 1.0,
    current_sector_exposure: Decimal = Decimal("0.00"),
) -> dict:
    logger.info(
        f"Evaluating release for {indicator}: Actual={actual}, Forecast={forecast}, Previous={previous}"
    )

    release_date = datetime.now(UTC).isoformat(timespec="seconds")
    release_id = log_release(indicator, release_date, actual, forecast, previous)

    actual_dec = Decimal(str(actual))
    forecast_dec = Decimal(str(forecast))

    if actual_dec == forecast_dec:
        logger.info("Actual matches forecast. Neutral signal, no trade action taken.")
        log_strategy_signal(indicator, forecast, actual, 0.0, 0.0, "neutral", 0.0, "none", 0.0)
        return {"status": "no_signal", "release_id": release_id}

    sigma = abs(actual - forecast) / surprise_std if surprise_std > 0 else abs(actual - forecast)

    if sigma < Config.MIN_CONVICTION_SIGMA:
        logger.info(
            f"Surprise sigma={sigma:.2f} below MIN_CONVICTION_SIGMA={Config.MIN_CONVICTION_SIGMA}. "
            f"No trade action taken."
        )
        log_strategy_signal(indicator, forecast, actual, actual - forecast, sigma, "below_threshold", 0.0, "none", 0.0)
        return {"status": "low_conviction", "release_id": release_id}

    side_to_buy = "yes" if actual_dec > forecast_dec else "no"
    estimated_prob = scale_conviction(actual_dec, forecast_dec, surprise_std)
    signal_quality = compute_signal_quality(sigma)

    logger.info(
        f"Surprise magnitude: {abs(actual - forecast):.4f} (std: {surprise_std:.4f}, "
        f"sigma: {sigma:.2f}) -> {signal_quality}, "
        f"estimated_prob: {estimated_prob:.4f}, side: {side_to_buy}"
    )

    best_yes_ask, yes_ask_qty = order_book.get_best_yes_ask()
    best_no_ask, no_ask_qty = order_book.get_best_no_ask()

    if side_to_buy == "yes":
        price_to_buy = best_yes_ask if best_yes_ask is not None else Decimal("0.6000")
        synthetic_ask = best_yes_ask if best_yes_ask is not None else Decimal("0.6000")
    else:
        price_to_buy = best_no_ask if best_no_ask is not None else Decimal("0.4000")
        best_yes_bid, _ = order_book.get_best_yes_bid()
        synthetic_ask = (
            best_no_ask
            if best_no_ask is not None
            else (
                Decimal("1.0000")
                - (best_yes_bid if best_yes_bid is not None else Decimal("0.6000"))
            )
        )

    best_bid_price, _ = order_book.get_best_yes_bid() if side_to_buy == "yes" else order_book.get_best_no_bid()
    if best_bid_price is not None and price_to_buy is not None:
        spread = abs(price_to_buy - best_bid_price)
        if spread > Config.MAX_SPREAD_PCT:
            logger.info(
                f"Bid-ask spread {spread:.4f} exceeds MAX_SPREAD_PCT={Config.MAX_SPREAD_PCT}. "
                f"Skipping trade."
            )
            log_strategy_signal(indicator, forecast, actual, actual - forecast, sigma,
                                 "wide_spread", 0.0, side_to_buy, 0.0, notes=f"spread={float(spread):.4f}")
            return {"status": "wide_spread", "release_id": release_id}

    raw_kelly = risk_manager.calculate_kelly_fraction(
        estimated_prob,
        price_to_buy if side_to_buy == "yes" else Decimal("1.0000") - price_to_buy,
        side_to_buy,
    )

    wager = risk_manager.size_order(
        estimated_prob=estimated_prob,
        market_price=price_to_buy if side_to_buy == "yes" else Decimal("1.0000") - price_to_buy,
        side=side_to_buy,
        sector=sector,
        current_sector_exposure=current_sector_exposure,
        total_capital=total_capital,
    )

    if wager <= 0:
        logger.info(
            f"Signal generated side={side_to_buy} but risk sizing calculated wager size is 0. Aborting order."
        )
        log_strategy_signal(indicator, forecast, actual, actual - forecast, sigma,
                             signal_quality, float(estimated_prob), side_to_buy, 0.0,
                             notes="zero_wager")
        return {"status": "zero_wager", "release_id": release_id}

    ask_qty = yes_ask_qty if side_to_buy == "yes" else no_ask_qty
    if ask_qty is not None:
        max_contracts_at_price = ask_qty
        wager_in_contracts = (wager / price_to_buy).to_integral_value(rounding="ROUND_DOWN")
        if wager_in_contracts > max_contracts_at_price:
            logger.info(
                f"Requested {wager_in_contracts} contracts but only {max_contracts_at_price} available at best price. "
                f"Capping to {max_contracts_at_price}."
            )
            wager = max_contracts_at_price * price_to_buy

    quantity = wager_in_contracts

    signal_id = log_strategy_signal(
        indicator=indicator,
        forecast_value=forecast,
        actual_value=actual,
        surprise=actual - forecast,
        sigma=sigma,
        signal_quality=signal_quality,
        conviction=float(estimated_prob),
        side=side_to_buy,
        wager=float(wager),
        series_id=FredCalendarProvider.SERIES_MAPPING.get(indicator.upper()),
    )

    order_resp = execution_engine.place_order(
        ticker=ticker,
        outcome_side=side_to_buy,
        price=price_to_buy,
        quantity=quantity,
        action="buy",
        synthetic_ask=synthetic_ask,
        release_id=release_id,
        proposed_kelly=raw_kelly,
        final_wager=wager,
        signal_id=signal_id,
    )

    return {
        "status": "executed",
        "release_id": release_id,
        "side": side_to_buy,
        "wager": float(wager),
        "quantity": float(quantity),
        "price": float(price_to_buy),
        "order_response": order_resp,
    }


class CalendarProvider:
    @staticmethod
    def trigger_mock_release(
        indicator: str,
        actual: float,
        forecast: float,
        previous: float,
        ticker: str,
        sector: str,
        risk_manager: RiskManager,
        execution_engine: ExecutionEngine,
        order_book: LocalOrderBook,
        total_capital: Decimal,
        surprise_std: float = 1.0,
        current_sector_exposure: Decimal = Decimal("0.00"),
    ) -> dict:
        return _evaluate_and_place(
            indicator=indicator,
            actual=actual,
            forecast=forecast,
            previous=previous,
            ticker=ticker,
            sector=sector,
            risk_manager=risk_manager,
            execution_engine=execution_engine,
            order_book=order_book,
            total_capital=total_capital,
            surprise_std=surprise_std,
            current_sector_exposure=current_sector_exposure,
        )


class MockCalendarProvider(CalendarProvider):
    pass
    def trigger_mock_release(
        indicator: str,
        actual: float,
        forecast: float,
        previous: float,
        ticker: str,
        sector: str,
        risk_manager: RiskManager,
        execution_engine: ExecutionEngine,
        order_book: LocalOrderBook,
        total_capital: Decimal,
        surprise_std: float = 1.0,
        current_sector_exposure: Decimal = Decimal("0.00"),
    ) -> dict:
        return _evaluate_and_place(
            indicator=indicator,
            actual=actual,
            forecast=forecast,
            previous=previous,
            ticker=ticker,
            sector=sector,
            risk_manager=risk_manager,
            execution_engine=execution_engine,
            order_book=order_book,
            total_capital=total_capital,
            surprise_std=surprise_std,
            current_sector_exposure=current_sector_exposure,
        )


class FredCalendarProvider:
    SERIES_MAPPING = {"CPI": "CPIAUCSL", "FOMC": "FEDFUNDS", "PCE": "PCE"}

    def __init__(self):
        self.api_key = Config.FRED_API_KEY
        self.base_url = "https://api.stlouisfed.org/fred/series/observations"
        self.alpha_vantage = AlphaVantageEconomicProvider()

    def fetch_latest_observation(self, indicator: str) -> dict:
        series_id = self.SERIES_MAPPING.get(indicator.upper())
        if not series_id:
            raise ValueError(f"Unknown economic indicator: {indicator}")

        if not self.api_key:
            logger.warning("FRED_API_KEY is not configured. Bypassing live FRED pull.")
            return self.alpha_vantage.fetch_latest_observation(indicator)

        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 13,
        }

        try:
            session = Config.get_verified_session()
            response = Config.request_with_retry(
                method="GET",
                url=self.base_url,
                params=params,
                session=session,
                timeout=Config.REQUEST_TIMEOUT_SEC,
            )
            if response.status_code == 200:
                data = response.json()
                observations = data.get("observations", [])
                if observations:
                    latest = observations[0]

                    parsed_value = _safe_parse_float(latest.get("value"))
                    if parsed_value is None:
                        logger.warning(
                            f"FRED returned unparseable value for {indicator} on {latest.get('date')}: '{latest.get('value')}'"
                        )
                        return self.alpha_vantage.fetch_latest_observation(indicator)

                    previous_value = None
                    if len(observations) > 1:
                        previous_value = _safe_parse_float(observations[1].get("value"))

                    all_values = []
                    for obs in observations:
                        v = _safe_parse_float(obs.get("value"))
                        if v is not None:
                            all_values.append(v)

                    return {
                        "date": latest["date"],
                        "value": parsed_value,
                        "previous": previous_value,
                        "all_values": all_values,
                    }
            else:
                logger.error(
                    f"FRED API returned status code {response.status_code}: {response.text}"
                )
        except Exception as e:
            logger.error(f"Error fetching data from FRED API: {e}")

        fallback = self.alpha_vantage.fetch_latest_observation(indicator)
        if fallback:
            logger.info(f"Fell back to Alpha Vantage for {indicator}")
            return fallback
        return {}


class MacroTrackerStrategy:
    def __init__(
        self,
        ticker: str,
        sector: str,
        indicator: str | None = None,
        indicators: list[str] | None = None,
    ):
        if indicator and indicators:
            raise ValueError("Provide either indicator or indicators, not both")
        self.ticker = ticker
        self.sector = sector
        if indicators:
            self.indicators = [i.upper() for i in indicators]
        else:
            self.indicators = [indicator.upper()] if indicator else ["CPI"]
        self.indicator = self.indicators[0]
        self.fred = FredCalendarProvider()
        self.survey_forecast = FredSurveyForecastProvider()
        self.trailing_forecast = TrailingAverageForecastProvider()
        self.risk_manager = RiskManager()
        self.execution_engine = ExecutionEngine()
        self.order_book = LocalOrderBook()
        self.position_manager = get_position_manager()
        self._ticker_to_sector: dict[str, str] = {}
        self._last_trade_time: dict[str, float] = {}

    def set_ticker_to_sector(self, mapping: dict[str, str]):
        self._ticker_to_sector = mapping

    def _resolve_forecast(
        self, indicator: str, series_id: str, obs: dict
    ) -> tuple[float, float, float | None] | None:
        prev_val = obs.get("previous")
        all_values = obs.get("all_values", [])
        actual_val = obs["value"]

        survey_fcst = self.survey_forecast.get_forecast(indicator, series_id)
        if survey_fcst is not None:
            logger.info(f"Using FRED survey forecast: {survey_fcst:.4f}")
            std_dev = compute_surprise_std(all_values)
            return survey_fcst, std_dev, prev_val

        trailing_fcst = self.trailing_forecast.get_forecast(indicator, series_id, recent_values=all_values)
        if trailing_fcst is not None:
            logger.info(f"Using trailing-average forecast: {trailing_fcst:.4f}")
            std_dev = compute_surprise_std(all_values)
            return trailing_fcst, std_dev, prev_val

        if prev_val is not None:
            logger.info(f"No forecast source available. Falling back to previous value: {prev_val}")
            all_values = (
                all_values or [actual_val, prev_val] if prev_val is not None else [actual_val]
            )
            std_dev = compute_surprise_std(all_values)
            return prev_val, std_dev, prev_val

        logger.info("No forecast source and no previous value available. No trade possible.")
        return None

    def _compute_signal(self, indicator: str) -> dict | None:
        series_id = FredCalendarProvider.SERIES_MAPPING.get(indicator)
        if not series_id:
            return None

        obs = self.fred.fetch_latest_observation(indicator)
        if not obs:
            return None

        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM macro_releases WHERE indicator = ? AND release_date = ?",
            (indicator, obs["date"]),
        )
        row = cursor.fetchone()
        conn.close()

        if row:
            return None

        logger.info(f"NEW LIVE MACRO RELEASE DETECTED: {indicator} on date {obs['date']} = {obs['value']}")

        forecast_result = self._resolve_forecast(indicator, series_id, obs)
        if forecast_result is None:
            logger.info("No forecast available for {indicator} — cannot compute trade signal.")
            return None

        forecast_val, std_dev, _ = forecast_result
        actual_val = obs["value"]
        side = "yes" if actual_val > forecast_val else "no"

        return {
            "indicator": indicator,
            "actual": actual_val,
            "forecast": forecast_val,
            "previous": obs.get("previous"),
            "std_dev": std_dev,
            "side": side,
        }

    def check_for_new_release(self, total_capital: Decimal, current_sector_exposure: Decimal | None = None) -> bool:
        now = time.time()

        # Check cooldown: skip if any indicator was traded recently
        for ind in self.indicators:
            last = self._last_trade_time.get(ind, 0.0)
            if now - last < Config.TRADE_COOLDOWN_SEC:
                remaining = Config.TRADE_COOLDOWN_SEC - (now - last)
                logger.info(
                    f"Cooldown active for {ind}: {remaining:.0f}s remaining. Skipping."
                )
                return False

        # Collect signals from all watched indicators
        signals = {}
        for ind in self.indicators:
            sig = self._compute_signal(ind)
            if sig:
                signals[ind] = sig

        if not signals:
            return False

        # Multi-indicator consensus: all must agree on direction
        if len(signals) > 1:
            sides = {ind: s["side"] for ind, s in signals.items()}
            unique_sides = set(sides.values())
            if len(unique_sides) != 1:
                logger.info(
                    f"Multi-indicator disagreement: {sides}. No trade."
                )
                return False
            logger.info(
                f"Multi-indicator consensus: all {len(signals)} indicators agree on {unique_sides.pop()}"
            )

        # Use the first signal for the actual trade
        lead_indicator = self.indicator if self.indicator in signals else next(iter(signals))
        sig = signals[lead_indicator]

        if current_sector_exposure is None:
            current_sector_exposure = self.position_manager.get_sector_exposure(
                self.sector, self._ticker_to_sector
            )

        result = CalendarProvider.trigger_mock_release(
            indicator=sig["indicator"],
            actual=sig["actual"],
            forecast=sig["forecast"],
            previous=sig["previous"],
            ticker=self.ticker,
            sector=self.sector,
            risk_manager=self.risk_manager,
            execution_engine=self.execution_engine,
            order_book=self.order_book,
            total_capital=total_capital,
            surprise_std=sig["std_dev"],
            current_sector_exposure=current_sector_exposure,
        )

        if result.get("status") == "executed":
            for ind in self.indicators:
                self._last_trade_time[ind] = now
            return True

        return False
