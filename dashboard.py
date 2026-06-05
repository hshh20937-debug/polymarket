#!/usr/bin/env python3
"""
Portfolio Dashboard for Polymarket Demo Bot
Run: python dashboard.py  (default port 5000)
"""

import json, os, csv, re
from datetime import datetime, timezone
from pathlib import Path

try:
    from flask import Flask, jsonify, render_template_string
except ImportError:
    print("Install Flask first: pip install flask")
    exit(1)

DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades_log.csv")

app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot Dashboard</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: 'Segoe UI', Arial, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
  h1 { color: #58a6ff; margin-bottom: 20px; font-size: 24px; }
  h2 { color: #8b949e; font-size: 16px; text-transform: uppercase; letter-spacing: 1px; margin: 20px 0 10px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
  .card .label { font-size: 11px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; }
  .card .value { font-size: 26px; font-weight: 700; margin-top: 4px; }
  .card .value.green { color: #3fb950; }
  .card .value.red { color: #f85149; }
  .card .value.yellow { color: #d29922; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 13px; }
  th { background: #161b22; color: #8b949e; text-align: left; padding: 10px 12px; border-bottom: 2px solid #30363d; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
  td { padding: 10px 12px; border-bottom: 1px solid #21262d; }
  tr:hover { background: #1c2128; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .badge.open { background: #1f6feb33; color: #58a6ff; }
  .badge.closed { background: #3fb95033; color: #3fb950; }
  .badge.yes { background: #3fb95033; color: #3fb950; }
  .badge.no { background: #f8514933; color: #f85149; }
  .pnl-pos { color: #3fb950; }
  .pnl-neg { color: #f85149; }
  .status { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .status.running { background: #3fb950; }
  .status.stopped { background: #f85149; }
  .footer { text-align: center; color: #484f58; font-size: 12px; margin-top: 30px; }
  .refresh { float: right; color: #8b949e; font-size: 13px; }
</style>
</head>
<body>
<div style="max-width: 1000px; margin: 0 auto;">

<h1>
  <span class="status running"></span>Polymarket Demo Bot
  <span class="refresh" id="lastUpdate"></span>
</h1>

<div class="cards" id="summaryCards"></div>

<h2>Open Positions</h2>
<table><thead><tr>
  <th>Market</th><th>Side</th><th>Entry</th><th>Size</th><th>Cost</th><th>Current</th><th>PnL</th>
</tr></thead><tbody id="openPositions"></tbody></table>

<h2>Trade History</h2>
<table><thead><tr>
  <th>Time</th><th>Action</th><th>Market</th><th>Side</th><th>Price</th><th>Stake</th><th>Balance</th><th>Extra</th>
</tr></thead><tbody id="tradeHistory"></tbody></table>

<div class="footer">Data refreshes every 10 seconds</div>
</div>

<script>
function fmt(n) { return Number(n).toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}); }
function pct(n) { return (n >= 0 ? '+' : '') + (n*100).toFixed(2) + '%'; }
function timeAgo(ts) {
  const s = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  return Math.floor(s/3600) + 'h ago';
}
function short(q) { return q ? q.length > 45 ? q.slice(0,42)+'...' : q : ''; }

async function load() {
  try {
    const [pfRes, tradesRes] = await Promise.all([
      fetch('/api/portfolio'),
      fetch('/api/trades')
    ]);
    const pf = await pfRes.json();
    const trades = await tradesRes.json();

    const eq = pf.balance + pf.total_unrealized_pnl;
    const ret = ((eq - 10) / 10);
    document.getElementById('lastUpdate').textContent = 'Last: ' + new Date().toLocaleTimeString();

    document.getElementById('summaryCards').innerHTML = `
      <div class="card"><div class="label">Balance</div><div class="value ${pf.balance < 5 ? 'red' : 'green'}">$${fmt(pf.balance)}</div></div>
      <div class="card"><div class="label">Equity</div><div class="value ${eq < 10 ? 'red' : 'green'}">$${fmt(eq)}</div></div>
      <div class="card"><div class="label">Unrealized PnL</div><div class="value ${pf.total_unrealized_pnl >= 0 ? 'green' : 'red'}">${pf.total_unrealized_pnl >= 0 ? '+' : ''}$${fmt(pf.total_unrealized_pnl)}</div></div>
      <div class="card"><div class="label">Realized PnL</div><div class="value ${pf.total_realized_pnl >= 0 ? 'green' : 'red'}">${pf.total_realized_pnl >= 0 ? '+' : ''}$${fmt(pf.total_realized_pnl)}</div></div>
      <div class="card"><div class="label">Return</div><div class="value ${ret >= 0 ? 'green' : 'red'}">${pct(ret)}</div></div>
      <div class="card"><div class="label">Trades</div><div class="value">${pf.trade_count}</div></div>
    `;

    const openRows = pf.positions.filter(p => !p.closed).map(p => `
      <tr><td>${short(p.question)}</td><td><span class="badge ${p.side.toLowerCase()}">${p.side}</span></td>
      <td>$${fmt(p.entry_price)}</td><td>${fmt(p.size)}</td><td>$${fmt(p.cost)}</td>
      <td>${p.close_price ? '$'+fmt(p.close_price) : '-'}</td>
      <td class="${p.pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}">${p.pnl >= 0 ? '+' : ''}$${fmt(p.pnl)}</td></tr>
    `).join('');
    document.getElementById('openPositions').innerHTML = openRows || '<tr><td colspan="7" style="text-align:center;color:#484f58;">No open positions</td></tr>';

    const tradeRows = trades.slice(-50).reverse().map(t => `
      <tr><td style="white-space:nowrap">${t.timestamp ? t.timestamp.slice(11,19) : ''}</td>
      <td><span class="badge ${t.action === 'OPEN' ? 'open' : 'closed'}">${t.action}</span></td>
      <td>${short(t.question)}</td><td>${t.side || '-'}</td>
      <td>$${fmt(t.price)}</td><td>$${fmt(t.stake)}</td>
      <td>$${fmt(t.balance)}</td><td>${t.extra || ''}</td></tr>
    `).join('');
    document.getElementById('tradeHistory').innerHTML = tradeRows || '<tr><td colspan="8" style="text-align:center;color:#484f58;">No trades yet</td></tr>';

  } catch(e) { console.error(e); }
}
load();
setInterval(load, 10000);
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/api/portfolio")
def api_portfolio():
    try:
        with open(PORTFOLIO_FILE) as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({
            "balance": 10.0,
            "total_realized_pnl": 0.0,
            "total_unrealized_pnl": 0.0,
            "trade_count": 0,
            "positions": [],
            "error": str(e)
        })

@app.route("/api/trades")
def api_trades():
    trades = []
    try:
        if os.path.exists(TRADES_FILE):
            with open(TRADES_FILE, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    trades.append(row)
    except Exception:
        pass
    return jsonify(trades)

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "data_dir": DATA_DIR})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Dashboard running on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
