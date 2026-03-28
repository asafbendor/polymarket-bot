"""
Polymarket Bot — Web Dashboard
Run: python dashboard_web.py
Then open: http://localhost:5000
"""

import os
import json
from datetime import datetime, timezone
import db_adapter
from flask import Flask, jsonify, render_template_string

app = Flask(__name__)

DAILY_BUDGET = 5.0


def init_db():
    """Create tables if they don't exist (runs on startup)."""
    conn = db_adapter.connect()
    c = conn.cursor()
    c.execute(db_adapter.adapt("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            condition_id    TEXT NOT NULL,
            question        TEXT,
            direction       TEXT,
            market_price    REAL,
            fair_value      REAL,
            edge            REAL,
            kelly_fraction  REAL,
            position_size   REAL,
            limit_price     REAL,
            order_id        TEXT,
            status          TEXT DEFAULT 'pending',
            fill_price      REAL,
            pnl             REAL DEFAULT 0,
            paper           INTEGER DEFAULT 1,
            reason          TEXT
        )
    """))
    c.execute(db_adapter.adapt("""
        CREATE TABLE IF NOT EXISTS daily_stats (
            date            TEXT PRIMARY KEY,
            spent           REAL DEFAULT 0,
            realized_pnl    REAL DEFAULT 0,
            open_positions  INTEGER DEFAULT 0
        )
    """))
    c.execute(db_adapter.adapt("""
        CREATE TABLE IF NOT EXISTS bot_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT,
            level       TEXT,
            message     TEXT
        )
    """))
    conn.commit()
    conn.close()

    # Migration: each column in its own connection/transaction (PostgreSQL-safe)
    for col, definition in [("end_date", "TEXT DEFAULT ''"), ("slug", "TEXT DEFAULT ''"),
                             ("category", "TEXT DEFAULT ''"), ("market_url", "TEXT DEFAULT ''")]:
        try:
            mconn = db_adapter.connect()
            mc = mconn.cursor()
            mc.execute(db_adapter.adapt(f"ALTER TABLE trades ADD COLUMN {col} {definition}"))
            mconn.commit()
            mconn.close()
        except Exception:
            try:
                mconn.rollback()
                mconn.close()
            except Exception:
                pass


init_db()


# ------------------------------------------------------------------
# DB helpers
# ------------------------------------------------------------------
def query(sql, params=()):
    sql = db_adapter.adapt(sql)
    conn = db_adapter.connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return db_adapter.fetchrows(cur)
    finally:
        conn.close()


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


# ------------------------------------------------------------------
# API endpoints
# ------------------------------------------------------------------
@app.route("/api/stats")
def api_stats():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    stats = query_one(
        "SELECT spent, realized_pnl, open_positions FROM daily_stats WHERE date=?",
        (today,)
    ) or {"spent": 0, "realized_pnl": 0, "open_positions": 0}

    total_trades = query_one(
        "SELECT COUNT(*) as n FROM trades WHERE date(timestamp)=?", (today,)
    ) or {"n": 0}

    filled = query_one(
        "SELECT COUNT(*) as n FROM trades WHERE status='filled' AND date(timestamp)=?", (today,)
    ) or {"n": 0}

    winning = query_one(
        "SELECT COUNT(*) as n FROM trades WHERE pnl>0 AND date(timestamp)=?", (today,)
    ) or {"n": 0}

    win_rate = 0
    if filled["n"] > 0:
        win_rate = round(100 * winning["n"] / filled["n"], 1)

    # Count actual open positions (pending trades, any date)
    open_pos = query_one(
        "SELECT COUNT(*) as n FROM trades WHERE status='pending'"
    ) or {"n": 0}

    # Determine live mode: any non-paper trade exists
    live_check = query_one(
        "SELECT COUNT(*) as n FROM trades WHERE paper=0"
    ) or {"n": 0}
    is_live = live_check["n"] > 0

    return jsonify({
        "date": today,
        "spent": round(stats["spent"] or 0, 2),
        "realized_pnl": round(stats["realized_pnl"] or 0, 2),
        "budget_remaining": round(DAILY_BUDGET - (stats["spent"] or 0), 2),
        "daily_budget": DAILY_BUDGET,
        "open_positions": open_pos["n"],
        "max_positions": 5,
        "total_trades": total_trades["n"],
        "filled_trades": filled["n"],
        "win_rate": win_rate,
        "is_live": is_live,
    })


@app.route("/api/trades")
def api_trades():
    rows = query("""
        SELECT
            id, timestamp, question, direction,
            market_price, fair_value, edge,
            position_size, limit_price, fill_price,
            status, pnl, paper, reason,
            condition_id, end_date, slug, market_url
        FROM trades
        ORDER BY timestamp DESC
        LIMIT 100
    """)

    for r in rows:
        r["edge_pct"] = round((r["edge"] or 0) * 100, 1)
        r["market_price_pct"] = round((r["market_price"] or 0) * 100, 1)
        r["fair_value_pct"] = round((r["fair_value"] or 0) * 100, 1)
        r["pnl"] = round(r["pnl"] or 0, 3)
        r["is_paper"] = bool(r["paper"])
        ts = r.get("timestamp", "")
        r["timestamp_utc"] = ts  # send raw UTC, JS will convert to local
        if ts:
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                r["time_str"] = dt.strftime("%m/%d %H:%M")
            except Exception:
                r["time_str"] = str(ts)[:16]
        # Polymarket link - prefer direct URL from API, then slug, then condition_id
        r["polymarket_url"] = (
            r.get("market_url") or
            (f"https://polymarket.com/event/{r['slug']}" if r.get("slug") else "") or
            ""
        )
        # Hours left until resolution
        end_raw = r.get("end_date") or ""
        if end_raw:
            try:
                ed = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                hours_left = (ed - now).total_seconds() / 3600
                if hours_left > 0:
                    r["end_date_str"] = f"{hours_left:.0f}h left"
                else:
                    r["end_date_str"] = "resolved"
            except Exception:
                r["end_date_str"] = end_raw[:10]
        else:
            r["end_date_str"] = "-"

    return jsonify(rows)


@app.route("/api/pnl")
def api_pnl():
    rows = query("""
        SELECT date, realized_pnl, spent
        FROM daily_stats
        ORDER BY date ASC
        LIMIT 30
    """)
    cumulative = 0
    result = []
    for r in rows:
        cumulative += r["realized_pnl"] or 0
        result.append({
            "date": r["date"],
            "daily_pnl": round(r["realized_pnl"] or 0, 2),
            "cumulative_pnl": round(cumulative, 2),
            "spent": round(r["spent"] or 0, 2),
        })
    return jsonify(result)


@app.route("/api/markets")
def api_markets():
    rows = query("""
        SELECT question, direction, market_price, fair_value, edge,
               position_size, status, timestamp
        FROM trades
        ORDER BY timestamp DESC
        LIMIT 50
    """)
    for r in rows:
        r["edge_pct"] = round((r["edge"] or 0) * 100, 1)
        r["market_price_pct"] = round((r["market_price"] or 0) * 100, 1)
        r["fair_value_pct"] = round((r["fair_value"] or 0) * 100, 1)
    return jsonify(rows)


@app.route("/api/log")
def api_log():
    try:
        rows = query("SELECT timestamp, message FROM bot_log ORDER BY id DESC LIMIT 50")
        lines = [f'{r["timestamp"]}|{r["message"]}' for r in reversed(rows)]
        if not lines:
            lines = ["No log entries yet — waiting for bot to run..."]
    except Exception:
        lines = ["Log table not ready yet..."]
    return jsonify({"lines": lines})


# ------------------------------------------------------------------
# Main page
# ------------------------------------------------------------------
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --green: #3fb950;
    --red: #f85149;
    --yellow: #d29922;
    --blue: #388bfd;
    --purple: #bc8cff;
    --accent: #1f6feb;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace; font-size: 14px; }
  header { background: var(--card); border-bottom: 1px solid var(--border); padding: 16px 24px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 18px; font-weight: 600; color: var(--text); }
  header .badge { background: var(--accent); color: white; border-radius: 12px; padding: 3px 10px; font-size: 12px; }
  .refresh-info { color: var(--muted); font-size: 12px; }
  main { padding: 20px 24px; max-width: 1400px; margin: 0 auto; }

  .kpi-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 14px; margin-bottom: 20px; }
  .kpi { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
  .kpi .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 6px; }
  .kpi .value { font-size: 28px; font-weight: 700; line-height: 1; }
  .kpi .sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .neutral { color: var(--text); }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
  @media(max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }

  .card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
  .card-header { padding: 12px 16px; border-bottom: 1px solid var(--border); font-weight: 600; font-size: 13px; display: flex; align-items: center; justify-content: space-between; }
  .card-body { padding: 0; }

  table { width: 100%; border-collapse: collapse; }
  th { background: #0d1117; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .5px; padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }
  td { padding: 9px 12px; border-bottom: 1px solid var(--border); font-size: 13px; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,.03); }

  .badge-yes { background: rgba(63,185,80,.15); color: var(--green); border-radius: 4px; padding: 2px 6px; font-size: 11px; font-weight: 600; }
  .badge-no  { background: rgba(248,81,73,.15); color: var(--red);   border-radius: 4px; padding: 2px 6px; font-size: 11px; font-weight: 600; }
  .badge-paper { background: rgba(210,153,34,.15); color: var(--yellow); border-radius: 4px; padding: 2px 6px; font-size: 11px; }
  .badge-live  { background: rgba(56,139,253,.15); color: var(--blue);   border-radius: 4px; padding: 2px 6px; font-size: 11px; }
  .status-filled    { color: var(--green); }
  .status-pending   { color: var(--yellow); }
  .status-paper     { color: var(--muted); }
  .status-cancelled { color: var(--red); }

  .progress-wrap { background: var(--border); border-radius: 4px; height: 6px; overflow: hidden; margin-top: 8px; }
  .progress-bar  { height: 100%; border-radius: 4px; transition: width .4s; }

  .log-box { background: #0d1117; font-family: monospace; font-size: 12px; padding: 12px 16px; max-height: 280px; overflow-y: auto; color: var(--muted); }
  .log-box .line { padding: 1px 0; }
  .log-line-WARNING { color: var(--yellow); }
  .log-line-ERROR   { color: var(--red); }

  .chart-wrap { padding: 16px; height: 260px; }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin: 20px 0 10px; }
  .truncate { max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .spinner { display: inline-block; width: 10px; height: 10px; border: 2px solid var(--border); border-top-color: var(--blue); border-radius: 50%; animation: spin .8s linear infinite; margin-right: 6px; }
  @keyframes spin { to { transform: rotate(360deg); } }

  @media (max-width: 640px) {
    main { padding: 12px; }
    #trades-table thead { display: none; }
    #trades-table tr { display: block; background: var(--card); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 10px; padding: 10px 12px; }
    #trades-table td { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; border: none; font-size: 13px; }
    #trades-table td::before { content: attr(data-label); color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .4px; flex-shrink: 0; margin-right: 8px; min-width: 55px; }
    #trades-table td[data-label="Market"] { display: block; margin-bottom: 6px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }
    #trades-table td[data-label="Market"]::before { display: none; }
    #trades-table td[data-label="Fair"] { display: none; }
    .truncate { max-width: 100%; font-size: 13px; font-weight: 500; }
    .kpi-row { grid-template-columns: 1fr 1fr; }
    .grid-2 { grid-template-columns: 1fr; }
    header h1 { font-size: 15px; }
  }
</style>
</head>
<body>

<header>
  <h1>Polymarket Bot</h1>
  <div style="display:flex;align-items:center;gap:12px">
    <span class="badge" id="mode-badge">PAPER</span>
    <span class="refresh-info"><span class="spinner"></span><span id="countdown">10</span>s to refresh</span>
  </div>
</header>

<main>
  <div class="kpi-row">
    <div class="kpi">
      <div class="label">Daily P&L</div>
      <div class="value neutral" id="kpi-pnl">-</div>
      <div class="sub">realized today</div>
    </div>
    <div class="kpi">
      <div class="label">Budget Remaining</div>
      <div class="value neutral" id="kpi-budget">-</div>
      <div class="sub">of $<span id="kpi-budget-total">10.00</span>/day</div>
      <div class="progress-wrap"><div class="progress-bar" id="budget-bar" style="width:100%;background:var(--blue)"></div></div>
    </div>
    <div class="kpi">
      <div class="label">Win Rate</div>
      <div class="value neutral" id="kpi-winrate">-</div>
      <div class="sub" id="kpi-winrate-sub">filled trades</div>
    </div>
    <div class="kpi">
      <div class="label">Open Positions</div>
      <div class="value neutral" id="kpi-open">-</div>
      <div class="sub">max 5</div>
      <div class="progress-wrap"><div class="progress-bar" id="positions-bar" style="width:0%;background:var(--purple)"></div></div>
    </div>
    <div class="kpi">
      <div class="label">Trades Today</div>
      <div class="value neutral" id="kpi-trades">-</div>
      <div class="sub" id="kpi-trades-sub">placed</div>
    </div>
    <div class="kpi">
      <div class="label">Spent Today</div>
      <div class="value neutral" id="kpi-spent">-</div>
      <div class="sub">USDC</div>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <div class="card-header">Cumulative P&L</div>
      <div class="chart-wrap"><canvas id="pnl-chart"></canvas></div>
    </div>
    <div class="card">
      <div class="card-header">Live Bot Log <span style="color:var(--muted);font-weight:400;font-size:11px">last 50 lines</span></div>
      <div class="card-body"><div class="log-box" id="log-box">Loading...</div></div>
    </div>
  </div>

  <div class="section-title">All Trades</div>
  <div class="card" style="margin-bottom:32px">
    <div class="card-body">
      <table id="trades-table">
        <thead>
          <tr>
            <th>Time</th><th>Market</th><th>Ends</th><th>Dir</th><th>Mode</th>
            <th>Entry</th><th>Fair</th><th>Edge</th><th>Size</th><th>Status</th><th>P&L</th>
          </tr>
        </thead>
        <tbody id="trades-body">
          <tr><td colspan="11" style="text-align:center;color:var(--muted);padding:24px">Loading...</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</main>

<script>
let pnlChart = null;

function fmt$(v) { return (v >= 0 ? '+' : '') + '$' + Math.abs(v).toFixed(2); }
function fmtPct(v) { return (v >= 0 ? '+' : '') + v.toFixed(1) + '%'; }
function colorClass(v) { return v > 0 ? 'pos' : v < 0 ? 'neg' : 'neutral'; }
function formatLocalTime(utc) {
  if (!utc) return '';
  try {
    const d = new Date(utc.endsWith('Z') || utc.includes('+') ? utc : utc + 'Z');
    return d.toLocaleDateString('he-IL', {month:'2-digit',day:'2-digit'}) + ' ' + d.toLocaleTimeString('he-IL', {hour:'2-digit',minute:'2-digit',hour12:false});
  } catch(e) { return utc.slice(0,16); }
}

async function loadStats() {
  const d = await fetch('/api/stats').then(r => r.json());
  const pnl = d.realized_pnl;
  document.getElementById('kpi-pnl').textContent = fmt$(pnl);
  document.getElementById('kpi-pnl').className = 'value ' + colorClass(pnl);
  const remaining = d.budget_remaining;
  document.getElementById('kpi-budget').textContent = '$' + remaining.toFixed(2);
  document.getElementById('kpi-budget').className = 'value ' + (remaining < 2 ? 'neg' : remaining < 5 ? 'neutral' : 'pos');
  document.getElementById('kpi-budget-total').textContent = d.daily_budget.toFixed(2);
  document.getElementById('budget-bar').style.width = Math.max(0, 100 * remaining / d.daily_budget) + '%';
  const wr = d.win_rate;
  document.getElementById('kpi-winrate').textContent = wr.toFixed(1) + '%';
  document.getElementById('kpi-winrate').className = 'value ' + (wr >= 55 ? 'pos' : wr >= 45 ? 'neutral' : 'neg');
  document.getElementById('kpi-winrate-sub').textContent = d.filled_trades + ' filled trades';
  const op = d.open_positions;
  const maxPos = d.max_positions || 5;
  document.getElementById('kpi-open').textContent = op + '/' + maxPos;
  document.getElementById('positions-bar').style.width = (100 * op / maxPos) + '%';
  document.getElementById('kpi-trades').textContent = d.total_trades;
  document.getElementById('kpi-trades-sub').textContent = d.filled_trades + ' filled';
  document.getElementById('kpi-spent').textContent = '$' + d.spent.toFixed(2);
  const badge = document.getElementById('mode-badge');
  if (d.is_live) {
    badge.textContent = 'LIVE';
    badge.style.background = 'rgba(56,139,253,.2)';
    badge.style.color = '#388bfd';
  } else {
    badge.textContent = 'PAPER';
    badge.style.background = 'rgba(210,153,34,.2)';
    badge.style.color = '#e3b341';
  }
}

async function loadChart() {
  const rows = await fetch('/api/pnl').then(r => r.json());
  const labels = rows.map(r => r.date.slice(5));
  const data   = rows.map(r => r.cumulative_pnl);
  if (pnlChart) { pnlChart.destroy(); }
  const ctx = document.getElementById('pnl-chart').getContext('2d');
  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{ label: 'Cumulative P&L ($)', data, borderColor: '#388bfd', backgroundColor: 'rgba(56,139,253,.1)', tension: 0.3, fill: true, pointRadius: 3 }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#8b949e', font: { size: 11 } }, grid: { color: '#21262d' } },
        y: { ticks: { color: '#8b949e', font: { size: 11 } }, grid: { color: '#21262d' } }
      }
    }
  });
}

async function loadTrades() {
  const rows = await fetch('/api/trades').then(r => r.json());
  const tbody = document.getElementById('trades-body');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:var(--muted);padding:24px">No trades yet — waiting for next scan...</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => {
    const pnl = r.pnl;
    const pnlStr = pnl !== 0 ? fmt$(pnl) : '-';
    const dirBadge = r.direction === 'YES' ? '<span class="badge-yes">YES</span>' : '<span class="badge-no">NO</span>';
    const modeBadge = r.is_paper ? '<span class="badge-paper">paper</span>' : '<span class="badge-live">live</span>';
    const entry = r.limit_price ? (r.limit_price * 100).toFixed(1) + 'c' : '-';
    const fair  = r.fair_value  ? (r.fair_value  * 100).toFixed(1) + 'c' : '-';
    const q = r.question || '';
    const marketCell = r.polymarket_url
      ? `<div class="truncate"><a href="${r.polymarket_url}" target="_blank" rel="noopener" style="color:var(--blue);text-decoration:none" title="${q}">${q}</a></div>`
      : `<div class="truncate" title="${q}">${q}</div>`;
    return `<tr>
      <td data-label="Time" style="color:var(--muted);white-space:nowrap">${formatLocalTime(r.timestamp_utc)}</td>
      <td data-label="Market">${marketCell}</td>
      <td data-label="Ends" style="color:var(--muted);white-space:nowrap;font-size:12px">${r.end_date_str||'-'}</td>
      <td data-label="Dir">${dirBadge}</td>
      <td data-label="Mode">${modeBadge}</td>
      <td data-label="Entry">${entry}</td>
      <td data-label="Fair">${fair}</td>
      <td data-label="Edge">${fmtPct(r.edge_pct)}</td>
      <td data-label="Size">$${(r.position_size||0).toFixed(2)}</td>
      <td data-label="Status" class="status-${r.status}">${r.status}</td>
      <td data-label="P&L" class="${colorClass(pnl)}">${pnlStr}</td>
    </tr>`;
  }).join('');
}

async function loadLog() {
  const d = await fetch('/api/log').then(r => r.json());
  const box = document.getElementById('log-box');
  box.innerHTML = d.lines.map(line => {
    let cls = '';
    if (line.includes('WARNING')) cls = 'log-line-WARNING';
    if (line.includes('ERROR'))   cls = 'log-line-ERROR';
    // Convert UTC timestamp to local time
    const parts = line.split('|');
    let display = line;
    if (parts.length >= 2) {
      const utc = parts[0];
      const msg = parts.slice(1).join('|');
      try {
        const d = new Date(utc);
        const local = d.toLocaleTimeString('he-IL', {hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
        display = local + ' ' + msg;
      } catch(e) { display = msg; }
    }
    return `<div class="line ${cls}">${display}</div>`;
  }).join('');
  box.scrollTop = box.scrollHeight;
}

let countdown = 10;
function tick() {
  countdown--;
  document.getElementById('countdown').textContent = countdown;
  if (countdown <= 0) { countdown = 10; loadAll(); }
}

async function loadAll() {
  await Promise.all([loadStats(), loadChart(), loadTrades(), loadLog()]);
}

loadAll();
setInterval(tick, 1000);
</script>
</body>
</html>"""


@app.route("/admin/reset-trades", methods=["POST"])
def reset_trades():
    conn = db_adapter.connect()
    c = conn.cursor()
    c.execute(db_adapter.adapt("DELETE FROM trades"))
    c.execute(db_adapter.adapt("UPDATE daily_stats SET spent=0, realized_pnl=0, open_positions=0"))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    print(f"\nDB backend: {'PostgreSQL' if db_adapter.pg() else 'SQLite'}")
    print(f"Dashboard running at http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
