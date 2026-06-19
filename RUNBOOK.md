# Kalshi Bot Runbook

## Architecture

```
main.py              — Entry point, startup checks, main loop
config.py            — Configuration, API auth, retry
execution/           — Order placement, fee tracking, position mgmt
market/              — WebSocket client, local order book
safety/              — Kill switch, risk manager (Kelly, VaR)
resilience/          — Circuit breakers, rate limiter, DLQ
observability/       — Prometheus metrics, health API, alerts, logging
strategy/            — Macroeconomic strategy, FRED API, forecast
data/                — SQLite database, audit log, migrations
```

## Startup Sequence

1. Load `.env` or Docker secrets
2. `setup_structured_logging()` — stdout + rotating file
3. `initialize_db()` — run schema migrations
4. `Config.validate()` — check API key, key file, retry settings
5. Connect WebSocket → subscribe to orderbook_delta
6. Start health server on `127.0.0.1:8080`
7. Enter main loop (60s poll by default)

## Monitoring

- Health: `GET /health` (returns `{"status": "healthy", ...}`)
- Readiness: `GET /ready` (checks circuit breakers, DLQ, memory, DB)
- Metrics: `GET /metrics` (Prometheus format)
- System: `GET /system` (memory, CPU, threads, connections)

### Key Metrics

| Metric | Description | Alert Threshold |
|--------|-------------|-----------------|
| `kalshi_ws_connections` | Active WebSocket connections | 0 = critical |
| `kalshi_circuit_breaker_state` | 0=closed, 1=half_open, 2=open | 2 = critical |
| `kalshi_dlq_size` | Dead letter queue entries | >100 warning, >1000 critical |
| `kalshi_total_capital_dollars` | Account balance | Below kill switch threshold |
| `kalshi_system_memory_usage_bytes` | RSS memory | >1GB warning, >2GB critical |

## Common Procedures

### Starting the Bot
```bash
docker-compose up -d
docker-compose logs -f
```

### Verifying It's Healthy
```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/ready
```

### Triggering Kill Switch
```bash
python cli.py kill-switch
# Or via API: cancel_all_orders() called automatically on balance threshold
```

### Viewing Positions
```bash
python cli.py positions
python cli.py orders --limit 20
```

### Running Diagnostics
```bash
python cli.py test-diagnostics
```

### Viewing Order Book
```bash
python cli.py view-book --ticker FED-24DEC-T4.00
```

## Recovery Procedures

### Crash Recovery
1. Check logs: `docker-compose logs --tail=100`
2. Verify DB integrity: `sqlite3 data/kalshi_shadow.db "PRAGMA integrity_check;"`
3. Restart: `docker-compose restart`

### Data Recovery
1. Restore from backup: `sqlite3 data/kalshi_shadow.db < backup.sql`
2. Verify positions match exchange: `python cli.py positions`

### Kill Switch Triggered
1. Identify root cause in logs
2. Manually investigate open orders on exchange
3. Fix underlying issue (balance, connectivity, etc.)
4. Restart the bot

## Alert Response

### WebSocket Disconnected (CRITICAL)
- Check network connectivity
- Verify API credentials haven't expired
- Check Kalshi status page

### Balance Approaching Kill Switch (WARNING)
- Check recent trades for losses
- Consider adding funds
- Review risk parameters

### High Memory Usage (WARNING/CRITICAL)
- Investigate order book depth growth
- Restart container if >1GB
- Verify `max_depth` in `LocalOrderBook` (default 200)

## Backup

Automatic backup runs daily via cron. Manual:
```bash
sqlite3 data/kalshi_shadow.db ".backup data/backups/kalshi_$(date +%Y%m%d).db"
```

## Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `KALSHI_ENV` | `demo` | `demo` or `prod` |
| `SHADOW_MODE` | `True` | Log-only mode, no real orders |
| `KALSHI_API_KEY_ID` | — | API key from Kalshi |
| `KALSHI_PRIVATE_KEY_PATH` | — | Path to RSA PEM file |
| `KILL_SWITCH_MIN_BALANCE` | `100.00` | Minimum balance before kill |
| `MAX_VAR_LIMIT_PCT` | `0.02` | Max 2% VaR per position |
| `MAX_SECTOR_LIMIT_PCT` | `0.30` | Max 30% per sector |
| `KELLY_MULTIPLIER` | `0.25` | Fractional Kelly (0.25x) |
| `POLL_INTERVAL_SEC` | `60` | Main loop interval |
| `HEALTH_PORT` | `8080` | Health API port |
| `HEALTH_SECRET` | — | Auth key for health endpoints |
