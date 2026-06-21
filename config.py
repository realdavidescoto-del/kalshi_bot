import base64
import logging
import os
import stat
import time
from decimal import Decimal

import certifi
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("kalshi_bot.config")

_DOCKER_SECRETS_DIR = "/run/secrets"
if os.path.isdir(_DOCKER_SECRETS_DIR):
    for secret_file in os.listdir(_DOCKER_SECRETS_DIR):
        secret_path = os.path.join(_DOCKER_SECRETS_DIR, secret_file)
        if os.path.isfile(secret_path):
            try:
                env_key = secret_file.upper().replace("-", "_")
                if env_key not in os.environ:
                    with open(secret_path) as f:
                        os.environ[env_key] = f.read().strip()
            except OSError as e:
                logger.warning(f"Docker secret {secret_file} could not be read: {e}")

_SENSITIVE_ENV_KEYS = {
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PATH",
    "FRED_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
}


class Config:
    API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
    PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    ENV = os.getenv("KALSHI_ENV", "demo").lower()
    SHADOW_MODE = os.getenv("SHADOW_MODE", "True").lower() == "true"
    FRED_API_KEY = os.getenv("FRED_API_KEY", "")
    DATABASE_PATH = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "data", "kalshi_shadow.db")
    )
    REQUEST_TIMEOUT_SEC = float(os.getenv("REQUEST_TIMEOUT_SEC", "10.0"))
    RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
    RETRY_BACKOFF_SEC = float(os.getenv("RETRY_BACKOFF_SEC", "1.0"))

    API_VERSION = os.getenv("KALSHI_API_VERSION", "v2")
    API_BASE_PATH = f"/trade-api/{API_VERSION}"

    MAX_VAR_LIMIT_PCT = Decimal(os.getenv("MAX_VAR_LIMIT_PCT", "0.02"))
    MAX_SECTOR_LIMIT_PCT = Decimal(os.getenv("MAX_SECTOR_LIMIT_PCT", "0.30"))
    KELLY_MULTIPLIER = Decimal(os.getenv("KELLY_MULTIPLIER", "0.25"))

    CONVICTION_SLOPE = float(os.getenv("CONVICTION_SLOPE", "0.12"))
    CONVICTION_MAX_DELTA = float(os.getenv("CONVICTION_MAX_DELTA", "0.35"))

    MIN_CONVICTION_SIGMA = float(os.getenv("MIN_CONVICTION_SIGMA", "1.0"))
    MAX_SPREAD_PCT = Decimal(os.getenv("MAX_SPREAD_PCT", "0.05"))

    ALPHA_VANTAGE_API_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")

    TRAILING_STOP_PCT = Decimal(os.getenv("TRAILING_STOP_PCT", "0.10"))
    TAKE_PROFIT_TIER_1_MULTIPLIER = Decimal(os.getenv("TAKE_PROFIT_TIER_1_MULTIPLIER", "2.0"))
    TAKE_PROFIT_TIER_1_FRACTION = Decimal(os.getenv("TAKE_PROFIT_TIER_1_FRACTION", "0.50"))
    TAKE_PROFIT_TIER_2_MULTIPLIER = Decimal(os.getenv("TAKE_PROFIT_TIER_2_MULTIPLIER", "3.0"))
    TAKE_PROFIT_TIER_2_FRACTION = Decimal(os.getenv("TAKE_PROFIT_TIER_2_FRACTION", "0.75"))
    POSITION_REBALANCE_INTERVAL_SEC = float(os.getenv("POSITION_REBALANCE_INTERVAL_SEC", "300.0"))

    TRADE_COOLDOWN_SEC = float(os.getenv("TRADE_COOLDOWN_SEC", "3600.0"))

    KILL_SWITCH_MIN_BALANCE = Decimal(os.getenv("KILL_SWITCH_MIN_BALANCE", "100.00"))

    _private_key = None
    _session = None
    _ALLOWED_KEY_DIRS = [
        os.path.expanduser("~/.kalshi"),
        "/etc/kalshi/keys",
    ]

    @classmethod
    def get_private_key(cls) -> rsa.RSAPrivateKey:
        if cls._private_key is not None:
            return cls._private_key

        if not cls.PRIVATE_KEY_PATH:
            raise ValueError("KALSHI_PRIVATE_KEY_PATH environment variable is not set.")

        resolved = os.path.realpath(cls.PRIVATE_KEY_PATH)
        allowed = any(resolved.startswith(os.path.realpath(d)) for d in cls._ALLOWED_KEY_DIRS)
        if not allowed:
            raise PermissionError(
                f"Private key path must be within allowed directories: {cls._ALLOWED_KEY_DIRS}"
            )

        key_stat = os.stat(resolved)

        if key_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(
                f"Private key file permissions are too permissive ({oct(key_stat.st_mode)}). "
                "Key file must be readable only by the owner (0o600 or stricter)."
            )

        if not os.path.isfile(resolved):
            raise FileNotFoundError("RSA Private Key file not found.")

        with open(resolved, "rb") as key_file:
            key_data = key_file.read()
            try:
                cls._private_key = serialization.load_pem_private_key(
                    key_data,
                    password=None,
                )
            finally:
                for i in range(len(key_data)):
                    key_data = key_data[:i] + b'\x00' + key_data[i+1:]
                del key_data

        if not isinstance(cls._private_key, rsa.RSAPrivateKey):
            cls._private_key = None
            raise ValueError("The provided key is not a valid RSA Private Key.")

        return cls._private_key

    @classmethod
    def clear_private_key(cls) -> None:
        cls._private_key = None

    @classmethod
    def get_rest_url(cls) -> str:
        if cls.ENV == "prod":
            return "https://external-api.kalshi.com"
        return "https://external-api.demo.kalshi.co"

    @classmethod
    def get_ws_url(cls) -> str:
        if cls.ENV == "prod":
            return f"wss://external-api-ws.kalshi.com/trade-api/ws/{cls.API_VERSION}"
        return f"wss://external-api-ws.demo.kalshi.co/trade-api/ws/{cls.API_VERSION}"

    @classmethod
    def build_api_path(cls, path: str) -> str:
        if path.startswith("/"):
            path = path[1:]
        return f"{cls.API_BASE_PATH}/{path}"

    @classmethod
    def verify_api_compat(cls) -> bool:
        try:
            session = cls.get_verified_session()
            response = cls.request_with_retry(
                method="GET",
                url=f"{cls.get_rest_url()}/{cls.API_BASE_PATH.replace('/trade-api/', '')}",
                session=session,
                timeout=5.0,
                max_attempts=1,
            )
            if response.status_code < 500:
                return True
            logging.getLogger("kalshi_bot.config").warning(
                f"API version check returned {response.status_code}"
            )
            return False
        except Exception as e:
            logger.warning(f"API version check failed: {e}")
            return False

    @classmethod
    def get_verified_session(cls) -> requests.Session:
        if cls._session is None:
            cls._session = requests.Session()
            cls._session.verify = certifi.where()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=10, pool_maxsize=20,
            )
            cls._session.mount("https://", adapter)
        return cls._session

    @classmethod
    def request_with_retry(cls, method: str, url: str, **kwargs):
        max_attempts = int(kwargs.pop("max_attempts", cls.RETRY_MAX_ATTEMPTS))
        backoff = float(kwargs.pop("backoff", cls.RETRY_BACKOFF_SEC))
        rate_limiter_tier = kwargs.pop("rate_limiter_tier", None)

        kwargs.setdefault("timeout", cls.REQUEST_TIMEOUT_SEC)
        if "session" not in kwargs:
            kwargs["session"] = cls.get_verified_session()

        session = kwargs.pop("session")
        last_error = None

        if rate_limiter_tier:
            try:
                from resilience.rate_limiter import get_rate_limiter
                if not get_rate_limiter().acquire(rate_limiter_tier, timeout=30.0):
                    raise RuntimeError(f"Rate limiter timeout for tier {rate_limiter_tier}")
            except RuntimeError:
                raise

        for attempt in range(1, max_attempts + 1):
            try:
                response = session.request(method=method.upper(), url=url, **kwargs)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                    time.sleep(backoff * attempt)
                    continue
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt == max_attempts:
                    break
                time.sleep(backoff * attempt)

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Request failed after {max_attempts} attempts: {url}")

    @classmethod
    def validate(cls):
        if os.getenv("KALSHI_TESTING") == "1":
            return
        if not cls.API_KEY_ID:
            raise ValueError("KALSHI_API_KEY_ID environment variable is not set.")
        if cls.REQUEST_TIMEOUT_SEC <= 0:
            raise ValueError("REQUEST_TIMEOUT_SEC must be greater than 0.")
        if cls.RETRY_MAX_ATTEMPTS < 1:
            raise ValueError("RETRY_MAX_ATTEMPTS must be at least 1.")
        if cls.MAX_VAR_LIMIT_PCT <= 0 or cls.MAX_VAR_LIMIT_PCT > 1:
            raise ValueError("MAX_VAR_LIMIT_PCT must be in (0, 1].")
        if cls.MAX_SECTOR_LIMIT_PCT <= 0 or cls.MAX_SECTOR_LIMIT_PCT > 1:
            raise ValueError("MAX_SECTOR_LIMIT_PCT must be in (0, 1].")
        if cls.KELLY_MULTIPLIER <= 0 or cls.KELLY_MULTIPLIER > 1:
            raise ValueError("KELLY_MULTIPLIER must be in (0, 1].")
        cls.get_private_key()


def sign_kalshi_headers(
    api_key_id: str,
    private_key: rsa.RSAPrivateKey,
    method: str,
    path: str,
) -> dict[str, str]:
    if not path.startswith("/"):
        path = "/" + path
    path_for_signing = path.split("?")[0]
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method.upper()}{path_for_signing}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    signature_b64 = base64.b64encode(signature).decode("utf-8")
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": signature_b64,
        "Content-Type": "application/json",
    }
