import logging
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional

from config import Config

logger = logging.getLogger("kalshi_bot.forecast")


class EconomicDataProvider(ABC):
    @abstractmethod
    def fetch_latest_observation(self, indicator: str) -> dict:
        pass


class ForecastProvider(ABC):
    def get_forecast(self, indicator: str, series_id: str, **kwargs) -> Optional[float]:
        raise NotImplementedError


class FredSurveyForecastProvider(ForecastProvider):
    SURVEY_MAPPING = {
        "CPI": "MICH",
        "PCE": "PCEPILFE",
        "FOMC": "FEDFUNDS",
    }

    def __init__(self):
        self.api_key = Config.FRED_API_KEY
        self.base_url = "https://api.stlouisfed.org/fred/series/observations"

    def get_forecast(self, indicator: str, series_id: str) -> Optional[float]:
        survey_series = self.SURVEY_MAPPING.get(indicator.upper())
        if not survey_series or not self.api_key:
            return None
        return self._fetch_survey_value(survey_series)

    def _fetch_survey_value(self, series_id: str) -> Optional[float]:
        try:
            session = Config.get_verified_session()
            response = Config.request_with_retry(
                method="GET",
                url=self.base_url,
                params={
                    "series_id": series_id,
                    "api_key": self.api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 1,
                },
                session=session,
                timeout=Config.REQUEST_TIMEOUT_SEC,
            )
            if response.status_code == 200:
                data = response.json()
                obs = data.get("observations", [])
                if obs:
                    val = obs[0].get("value", "")
                    if val.strip() not in ("", ".", ".", "NaN"):
                        return float(val)
        except Exception as e:
            logger.warning(f"Failed to fetch FRED survey series {series_id}: {e}")
        return None


class TrailingAverageForecastProvider(ForecastProvider):
    def __init__(self, window: int = 6):
        self.window = window

    def get_forecast(self, indicator: str, series_id: str, **kwargs) -> Optional[float]:
        recent_values = kwargs.get("recent_values", [])
        if not recent_values or len(recent_values) < 2:
            return None
        changes = [recent_values[i] - recent_values[i - 1] for i in range(1, len(recent_values))]
        window = min(self.window, len(changes))
        avg_change = sum(changes[-window:]) / window
        return recent_values[-1] + avg_change


class AlphaVantageEconomicProvider(EconomicDataProvider):
    SERIES_MAPPING = {
        "CPI": "CPI",
        "PCE": "PCE",
        "FOMC": "FEDERAL_FUNDS_RATE",
    }

    def __init__(self):
        self.api_key = Config.ALPHA_VANTAGE_API_KEY
        self.base_url = "https://www.alphavantage.co/query"

    def fetch_latest_observation(self, indicator: str) -> dict:
        series = self.SERIES_MAPPING.get(indicator.upper())
        if not series or not self.api_key:
            return {}

        try:
            session = Config.get_verified_session()
            response = Config.request_with_retry(
                method="GET",
                url=self.base_url,
                params={
                    "function": series,
                    "apikey": self.api_key,
                },
                session=session,
                timeout=Config.REQUEST_TIMEOUT_SEC,
            )
            if response.status_code == 200:
                data = response.json()
                if "data" in data:
                    obs = data["data"]
                    if obs:
                        latest = obs[0]
                        val = _safe_parse_float(latest.get("value"))
                        if val is not None:
                            return {
                                "date": latest.get("date", ""),
                                "value": val,
                                "previous": _safe_parse_float(obs[1].get("value")) if len(obs) > 1 else None,
                                "all_values": [_safe_parse_float(o.get("value")) for o in obs if _safe_parse_float(o.get("value")) is not None],
                            }
            else:
                logger.warning(f"Alpha Vantage returned {response.status_code}: {response.text}")
        except Exception as e:
            logger.warning(f"Alpha Vantage fetch failed: {e}")

        return {}


def _safe_parse_float(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in ("", ".", "NaN", "Inf", "-Inf", "null"):
            return None
        try:
            return float(stripped)
        except (ValueError, TypeError):
            return None
    return None


def compute_surprise_std(recent_values: list[float]) -> float:
    if not recent_values or len(recent_values) < 2:
        return 1.0
    changes = [recent_values[i] - recent_values[i - 1] for i in range(1, len(recent_values))]
    if len(changes) < 2:
        return abs(changes[0]) if changes[0] != 0 else 1.0
    mean = sum(changes) / len(changes)
    variance = sum((c - mean) ** 2 for c in changes) / (len(changes) - 1)
    return variance**0.5 if variance > 0 else 1.0


def scale_conviction(actual: Decimal, forecast: Decimal, std_dev: float) -> Decimal:
    surprise = actual - forecast
    magnitude = abs(float(surprise))
    sigma = magnitude / std_dev if std_dev > 0 else magnitude
    delta = Decimal(str(min(sigma * Config.CONVICTION_SLOPE, Config.CONVICTION_MAX_DELTA)))
    direction = Decimal("1") if surprise > 0 else Decimal("-1")
    return Decimal("0.50") + direction * Decimal(str(delta))


def compute_signal_quality(sigma: float) -> str:
    if sigma >= 2.0:
        return "strong"
    if sigma >= 1.0:
        return "moderate"
    if sigma >= 0.5:
        return "weak"
    return "noise"
