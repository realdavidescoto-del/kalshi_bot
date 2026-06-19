# Kalshi Bot

This project is a Python-based bot for monitoring Kalshi prediction markets, evaluating macroeconomic signals, and applying safety/risk controls.

## Production-readiness notes
- The bot includes startup validation, timeout configuration, retry handling, and a graceful shutdown path.
- Use shadow mode first for validation before enabling live order placement.
- Logs are written to stdout and to a rotating log file in the `logs` directory.

## Setup
1. Install dependencies with `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in your secrets
3. Ensure the private key path points to a valid PEM file
4. Run the bot with `python main.py`

## Recommended deployment steps
- Start with `SHADOW_MODE=True` and verify output logs
- Set `REQUEST_TIMEOUT_SEC` and retry values for your environment
- Use the Docker compose configuration for restart and log rotation support

## Structure
- `main.py`: Runtime entry point and startup checks
- `config.py`: Configuration and retry/timeout helpers
- `execution/`: Order placement and fee accounting
- `market/`: WebSocket and order book handling
- `safety/`: Risk and kill-switch logic
- `strategy/`: Macroeconomic strategy logic
- `data/`: Database and persisted logs