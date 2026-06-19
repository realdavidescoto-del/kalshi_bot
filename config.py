import os
import time
import stat
import certifi
from decimal import Decimal
import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

load_dotenv()

_SENSITIVE_ENV_KEYS = {
    "KALSHI_API_KEY_ID",
    "KALSHI_PRIVATE_KEY_PATH",
    "FRED_API_KEY",
    "KALSHI_ACCESS_KEY",
    "KALSHI_ACCESS_SECRET",
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

    # Risk settings
    MAX_VAR_LIMIT_PCT = Decimal(os.getenv("MAX_VAR_LIMIT_PCT", "0.02"))
    MAX_SECTOR_LIMIT_PCT = Decimal(os.getenv("MAX_SECTOR_LIMIT_PCT", "0.30"))
    KELLY_MULTIPLIER = Decimal(os.getenv("KELLY_MULTIPLIER", "0.25"))

    # Safety balance monitor
    KILL_SWITCH_MIN_BALANCE = Decimal(os.getenv("KILL_SWITCH_MIN_BALANCE", "100.00"))

    # Private key object
    _private_key = None
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
        allowed = any(
            resolved.startswith(os.path.realpath(d)) for d in cls._ALLOWED_KEY_DIRS
        )
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
                pass

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
            return "wss://api.elections.kalshi.com/trade-api/ws/v2"
        return "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"

    @classmethod
    def get_verified_session(cls) -> requests.Session:
        session = requests.Session()
        session.verify = certifi.where()
        return session

    @classmethod
    def request_with_retry(cls, method: str, url: str, **kwargs):
        max_attempts = int(kwargs.pop("max_attempts", cls.RETRY_MAX_ATTEMPTS))
        backoff = float(kwargs.pop("backoff", cls.RETRY_BACKOFF_SEC))

        kwargs.setdefault("timeout", cls.REQUEST_TIMEOUT_SEC)
        if "session" not in kwargs:
            kwargs["session"] = cls.get_verified_session()

        session = kwargs.pop("session")
        last_error = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = session.request(method=method.upper(), url=url, **kwargs)
                if (
                    response.status_code in (429, 500, 502, 503, 504)
                    and attempt < max_attempts
                ):
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
        cls.get_private_key()
