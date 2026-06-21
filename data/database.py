import contextlib
import logging
import os
import sqlite3
import stat
import time
from datetime import datetime

from config import Config

logger = logging.getLogger("kalshi_bot.database")

_SECURE_DB_PERMISSIONS = stat.S_IRUSR | stat.S_IWUSR


def get_connection() -> sqlite3.Connection:
    db_path = Config.DATABASE_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    if os.getenv("KALSHI_TESTING") == "1":
        test_db = os.path.join(
            os.path.dirname(db_path),
            f"kalshi_test_{os.getpid()}.db",
        )
        db_path = test_db

    db_exists = os.path.exists(db_path)
    conn = sqlite3.connect(db_path, timeout=30)

    if not db_exists:
        os.chmod(db_path, _SECURE_DB_PERMISSIONS)
    else:
        current_mode = os.stat(db_path).st_mode & 0o777
        if current_mode != _SECURE_DB_PERMISSIONS:
            os.chmod(db_path, _SECURE_DB_PERMISSIONS)

    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")

    return conn


@contextlib.contextmanager
def db_scope():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS macro_releases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        indicator TEXT NOT NULL,
        release_date TEXT NOT NULL,
        actual_value REAL NOT NULL,
        forecast_value REAL,
        previous_value REAL
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS shadow_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        action TEXT NOT NULL,
        outcome_side TEXT NOT NULL,
        price REAL NOT NULL,
        quantity REAL NOT NULL,
        synthetic_ask REAL,
        proposed_kelly REAL,
        final_wager REAL,
        fee_accumulator REAL,
        release_id INTEGER,
        FOREIGN KEY (release_id) REFERENCES macro_releases(id) ON DELETE SET NULL
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS market_data_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        best_bid REAL,
        best_ask REAL,
        source TEXT NOT NULL
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS strategy_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        indicator TEXT NOT NULL,
        forecast_value REAL,
        actual_value REAL,
        surprise REAL,
        sigma REAL,
        signal_quality TEXT,
        conviction REAL,
        side TEXT,
        wager REAL,
        profitable INTEGER,
        series_id TEXT,
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_order_id TEXT NOT NULL UNIQUE,
        ticker TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        action TEXT,
        outcome_side TEXT,
        price REAL,
        quantity REAL,
        signal_id INTEGER,
        kalshi_order_id TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (signal_id) REFERENCES strategy_signals(id) ON DELETE SET NULL
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        balance REAL NOT NULL,
        total_exposure REAL,
        open_positions INTEGER,
        total_realized_pnl REAL,
        total_unrealized_pnl REAL,
        sector TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """)

    conn.commit()

    _run_migrations(conn)

    conn.close()
    logger.info("SQLite Database successfully initialized.")


def _run_migrations(conn):
    cursor = conn.cursor()
    current = 0
    try:
        cursor.execute("SELECT MAX(version) FROM schema_version")
        row = cursor.fetchone()
        if row and row[0] is not None:
            current = row[0]
    except sqlite3.OperationalError:
        pass

    migrations = [
        _migration_v1_initial,
        _migration_v2_kalshi_order_id,
        _migration_v3_strategy_signals,
        _migration_v4_rename_order_id,
        _migration_v5_signal_id,
        _migration_v6_portfolio_snapshots,
    ]

    for i, migration in enumerate(migrations, start=1):
        if i > current:
            try:
                migration(cursor)
                cursor.execute(
                    "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, datetime('now'))",
                    (i,),
                )
                conn.commit()
                logger.info(f"Applied schema migration v{i}")
            except Exception as e:
                logger.warning(f"Migration v{i} skipped ({e})")
                cursor.execute(
                    "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, datetime('now'))",
                    (i,),
                )
                conn.commit()


def _migration_v1_initial(cursor):
    pass


def _migration_v2_kalshi_order_id(cursor):
    try:
        cursor.execute("ALTER TABLE orders ADD COLUMN kalshi_order_id TEXT")
    except sqlite3.OperationalError:
        pass


def _migration_v3_strategy_signals(cursor):
    try:
        cursor.execute("ALTER TABLE strategy_signals ADD COLUMN series_id TEXT")
        cursor.execute("ALTER TABLE strategy_signals ADD COLUMN notes TEXT")
    except sqlite3.OperationalError:
        pass


def _migration_v4_rename_order_id(cursor):
    try:
        cursor.execute("SELECT kalshi_order_id FROM orders LIMIT 1")
    except sqlite3.OperationalError:
        try:
            cursor.execute("ALTER TABLE orders ADD COLUMN kalshi_order_id TEXT")
        except sqlite3.OperationalError:
            pass


def _migration_v5_signal_id(cursor):
    try:
        cursor.execute("ALTER TABLE orders ADD COLUMN signal_id INTEGER REFERENCES strategy_signals(id)")
    except sqlite3.OperationalError:
        pass


def _migration_v6_portfolio_snapshots(cursor):
    pass


def log_release(
    indicator: str,
    release_date: str,
    actual: float,
    forecast: float = None,
    previous: float = None,
) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO macro_releases (indicator, release_date, actual_value, forecast_value, previous_value)
        VALUES (?, ?, ?, ?, ?)
    """,
        (indicator, release_date, actual, forecast, previous),
    )
    release_id = cursor.lastrowid
    conn.commit()
    conn.close()
    logger.info(
        f"Logged macro release {indicator} (ID: {release_id}) with actual={actual}"
    )
    return release_id


def log_shadow_trade(
    ticker: str,
    timestamp: str,
    action: str,
    outcome_side: str,
    price: float,
    quantity: float,
    synthetic_ask: float = None,
    proposed_kelly: float = None,
    final_wager: float = None,
    fee_accumulator: float = None,
    release_id: int = None,
) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO shadow_trades (
            ticker, timestamp, action, outcome_side, price, quantity, 
            synthetic_ask, proposed_kelly, final_wager, fee_accumulator, release_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            ticker,
            timestamp,
            action,
            outcome_side,
            price,
            quantity,
            synthetic_ask,
            proposed_kelly,
            final_wager,
            fee_accumulator,
            release_id,
        ),
    )
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    logger.info(
        f"Logged shadow trade (ID: {trade_id}) for {ticker} (outcome: {outcome_side})"
    )
    return trade_id


def log_market_data(
    ticker: str, timestamp: str, best_bid: float, best_ask: float, source: str
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO market_data_history (ticker, timestamp, best_bid, best_ask, source)
        VALUES (?, ?, ?, ?, ?)
    """,
        (ticker, timestamp, best_bid, best_ask, source),
    )
    conn.commit()
    conn.close()


def order_exists(client_order_id: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM orders WHERE client_order_id = ?", (client_order_id,)
    )
    exists = cursor.fetchone() is not None
    conn.close()
    return exists


def store_order(
    client_order_id: str,
    ticker: str,
    status: str = "pending",
    action: str = None,
    outcome_side: str = None,
    price: float = None,
    quantity: float = None,
    signal_id: int = None,
) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO orders (client_order_id, ticker, status, action, outcome_side, price, quantity, signal_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (client_order_id, ticker, status, action, outcome_side, price, quantity, signal_id),
    )
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id


def update_order_status(
    client_order_id: str,
    status: str,
    error_message: str = None,
    kalshi_order_id: str = None,
):
    conn = get_connection()
    cursor = conn.cursor()
    fields = ["status = ?", "updated_at = datetime('now')"]
    params = [status]
    if error_message is not None:
        fields.append("error_message = ?")
        params.append(error_message)
    if kalshi_order_id is not None:
        fields.append("kalshi_order_id = ?")
        params.append(kalshi_order_id)
    params.append(client_order_id)
    cursor.execute(
        f"UPDATE orders SET {', '.join(fields)} WHERE client_order_id = ?", params
    )
    conn.commit()
    conn.close()


def get_orders_for_position(ticker: str, side: str) -> list[dict]:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, client_order_id, ticker, status, action, outcome_side,
               price, quantity, signal_id, kalshi_order_id, created_at
        FROM orders
        WHERE ticker = ? AND outcome_side = ?
        ORDER BY created_at ASC
    """,
        (ticker, side),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_strategy_signal(
    indicator: str,
    forecast_value: float = None,
    actual_value: float = None,
    surprise: float = None,
    sigma: float = None,
    signal_quality: str = None,
    conviction: float = None,
    side: str = None,
    wager: float = None,
    series_id: str = None,
    notes: str = None,
    profitable: bool = None,
) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO strategy_signals
            (indicator, forecast_value, actual_value, surprise, sigma,
             signal_quality, conviction, side, wager, series_id, notes, profitable)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (indicator, forecast_value, actual_value, surprise, sigma,
         signal_quality, conviction, side, wager, series_id, notes, profitable),
    )
    sig_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return sig_id


def update_signal_profitability(signal_id: int, profitable: bool):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT profitable FROM strategy_signals WHERE id = ?", (signal_id,))
    row = cursor.fetchone()
    current = row[0] if row else None
    if current is None:
        profitable_val = int(profitable)
    elif current == 0 and profitable:
        profitable_val = 1
    else:
        profitable_val = current
    cursor.execute(
        "UPDATE strategy_signals SET profitable = ? WHERE id = ?",
        (profitable_val, signal_id),
    )
    conn.commit()
    conn.close()


def get_strategy_performance(indicator: str = None) -> list[dict]:
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if indicator:
        cursor.execute(
            """
            SELECT id, indicator, forecast_value, actual_value, surprise, sigma,
                   signal_quality, conviction, side, wager, profitable, created_at
            FROM strategy_signals
            WHERE indicator = ?
            ORDER BY created_at DESC
        """,
            (indicator,),
        )
    else:
        cursor.execute(
            """
            SELECT id, indicator, forecast_value, actual_value, surprise, sigma,
                   signal_quality, conviction, side, wager, profitable, created_at
            FROM strategy_signals
            ORDER BY created_at DESC
        """
        )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_portfolio_snapshot(
    balance: float,
    total_exposure: float = None,
    open_positions: int = None,
    total_realized_pnl: float = None,
    total_unrealized_pnl: float = None,
    sector: str = None,
) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO portfolio_snapshots
            (balance, total_exposure, open_positions, total_realized_pnl, total_unrealized_pnl, sector)
        VALUES (?, ?, ?, ?, ?, ?)
    """,
        (balance, total_exposure, open_positions, total_realized_pnl, total_unrealized_pnl, sector),
    )
    snap_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return snap_id


def vacuum_database():
    conn = get_connection()
    conn.execute("VACUUM")
    conn.close()
    logger.info("Database vacuum completed.")


def get_db_stats() -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    tables = [
        "macro_releases",
        "shadow_trades",
        "market_data_history",
        "strategy_signals",
        "orders",
        "portfolio_snapshots",
    ]
    stats = {}
    for table in tables:
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            stats[table] = cursor.fetchone()[0]
        except sqlite3.OperationalError:
            stats[table] = 0

    try:
        cursor.execute("PRAGMA page_count")
        pages = cursor.fetchone()[0]
        cursor.execute("PRAGMA page_size")
        page_size = cursor.fetchone()[0]
        stats["db_size_bytes"] = pages * page_size
    except sqlite3.OperationalError:
        stats["db_size_bytes"] = 0

    conn.close()
    return stats
