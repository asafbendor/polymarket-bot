# Polymarket Trading Bot

Autonomous trading bot that scans 500-1000 Polymarket markets,
identifies mispricings >8%, and executes Kelly-sized trades.

**Daily budget: $10 USDC. Default: paper trading mode.**

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env with your keys
```

### 3. Run in paper mode (safe, no real money)
```bash
python main.py
```

### 4. Single scan (test it works)
```bash
python main.py --once
```

### 5. Live trading (real money — be careful)
```bash
python main.py --live
```

---

## Architecture

| File | Role |
|------|------|
| `main.py` | Async orchestrator — runs scan every 15 min |
| `market_scanner.py` | Fetches markets from Gamma + CLOB APIs |
| `fair_value.py` | Domain-specific probability estimation |
| `edge_calculator.py` | Kelly formula, position sizing |
| `risk_manager.py` | Hard rules, budget tracking, trade logging |
| `executor.py` | Order placement via py-clob-client |

---

## Fair Value Sources

| Category | Data Source |
|----------|-------------|
| Crypto | Binance REST API (price → lognormal prob) |
| Sports | ESPN unofficial API (win probabilities, spreads) |
| Weather | Open-Meteo API (free, no key needed) |
| Political | Headlines → Claude AI |
| Other | Claude AI fallback |

---

## Wallet Setup (for live trading)

Polymarket uses a proxy wallet system:

1. Create a standard Ethereum wallet (MetaMask etc.)
2. Deposit USDC to Polymarket — this creates your **proxy wallet**
3. Your `.env` needs:
   - `POLYMARKET_PRIVATE_KEY` = private key of your EOA (the wallet you sign with)
   - `POLYMARKET_PROXY_ADDRESS` = the proxy wallet address (shown in Polymarket UI)

> The bot uses `signature_type=2` (EIP-712) which is correct for proxy wallets.

---

## Risk Controls

- Max $10/day total
- Max $2 per trade
- Max 3 simultaneous open positions
- No trades within 6 hours of resolution
- Stop trading if daily P&L < -$8
- 25% fractional Kelly (conservative sizing)
- Limit orders only (no market orders)
- Auto-cancel unfilled orders after 10 minutes

---

## Output Example

```
[14:30] Scanned 847 markets | 3 opportunities found | 1 trades placed
[14:30] BTC > $70k by March: market=45% fair=38% NO edge=7% (below threshold)
[14:30] Lakers win tonight: market=52% fair=63% YES edge=11% ✓ TRADING
[14:30] [PAPER] Position: $1.23 YES @ 0.617 limit | Kelly: 14.2% → adj: 3.6%
[14:30] Daily P&L: +$0.43 | Budget remaining: $8.77/$10.00 | Open positions: 1/3
```

---

## Database

`trades.db` is auto-created on first run with tables:
- `markets` — all scanned markets with prices
- `trades` — all placed orders with Kelly/edge data
- `daily_stats` — daily budget and P&L tracking
- `rejected_trades` — audit log of rejected opportunities

View trades:
```bash
sqlite3 trades.db "SELECT timestamp, direction, question, edge, position_size, status FROM trades ORDER BY id DESC LIMIT 20"
```
