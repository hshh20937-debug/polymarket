#!/usr/bin/env python3
"""
Polymarket Demo Trading Bot
~ Detects new markets -> searches news -> decides YES/NO -> simulates trades ~
Demo mode: starts with $10 virtual USDC, tracks P&L in real-time.
"""

import json, logging, os, re, time, csv, hashlib
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional
from enum import Enum

import requests

# =============================================================
# CONFIG
# =============================================================
STARTING_BALANCE = 10.0        # $10 demo
POLL_INTERVAL_SEC = 180        # check every 3 minutes
MAX_BET_FRACTION = 0.20        # max 20% of balance per trade
PRICE_EDGE_MIN = 0.05          # minimum edge vs market price to enter
NEWS_CACHE_TTL = 3600          # re-fetch news after 1 hour

# Railway persistent storage volume (set RAILWAY_VOLUME_MOUNT_PATH in Railway)
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".")
TRADES_FILE = os.path.join(DATA_DIR, "trades_log.csv")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
SEEN_MARKETS_FILE = os.path.join(DATA_DIR, "seen_markets.json")

GAMMA_API = "https://gamma-api.polymarket.com"
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")  # optional, free tier at newsapi.org

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("polymarket_bot")


# =============================================================
# DATA MODELS
# =============================================================
class Side(Enum):
    YES = "YES"
    NO = "NO"

@dataclass
class Market:
    id: str
    question: str
    condition_id: str
    outcomes: list
    outcome_prices: list
    end_date: Optional[str]
    volume: float
    slug: str
    active: bool
    closed: bool

@dataclass
class Position:
    market_id: str
    question: str
    side: Side
    entry_price: float
    size: float          # number of outcome tokens bought
    cost: float          # total cost in USD
    timestamp: float
    closed: bool = False
    close_price: float = 0.0
    pnl: float = 0.0

@dataclass
class Portfolio:
    balance: float = STARTING_BALANCE
    positions: list = None
    total_realized_pnl: float = 0.0
    total_unrealized_pnl: float = 0.0
    trade_count: int = 0

    def __post_init__(self):
        if self.positions is None:
            self.positions = []

    @property
    def equity(self) -> float:
        return self.balance + self.total_unrealized_pnl


# =============================================================
# MARKET SCANNER  (Gamma API — no auth needed)
# =============================================================
class MarketScanner:
    def __init__(self):
        self.seen_slugs = set()
        self._load_seen()

    def _load_seen(self):
        try:
            if os.path.exists(SEEN_MARKETS_FILE):
                with open(SEEN_MARKETS_FILE) as f:
                    self.seen_slugs = set(json.load(f))
        except Exception:
            self.seen_slugs = set()

    def _save_seen(self):
        try:
            with open(SEEN_MARKETS_FILE, "w") as f:
                json.dump(list(self.seen_slugs), f)
        except Exception as e:
            log.warning("Could not save seen_markets: %s", e)

    def fetch_active_markets(self) -> list[Market]:
        """Fetch all active markets from Gamma API."""
        markets = []
        offset = 0
        limit = 100
        max_pages = 5  # limit scan to 500 events max
        pages = 0
        while True:
            url = f"{GAMMA_API}/events"
            params = {
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
            }
            try:
                resp = requests.get(url, params=params, timeout=15)
                if resp.status_code != 200:
                    log.warning("Gamma API returned %d", resp.status_code)
                    break
                events = resp.json()
                if not events:
                    break
                pages += 1
                if pages >= max_pages:
                    break
                for ev in events:
                    for m in ev.get("markets", []):
                        if not m.get("active"):
                            continue
                        try:
                            prices = json.loads(m.get("outcomePrices", "[0.5,0.5]"))
                        except Exception:
                            prices = [0.5, 0.5]
                        outcomes_raw = m.get("outcomes", "").strip('"[]').split(",")
                        outcomes = [o.strip().strip('"') for o in outcomes_raw if o.strip()]

                        market = Market(
                            id=str(m.get("conditionId", "")),
                            question=m.get("question", ""),
                            condition_id=str(m.get("conditionId", "")),
                            outcomes=outcomes if len(outcomes) == 2 else ["Yes", "No"],
                            outcome_prices=[float(p) for p in prices],
                            end_date=m.get("endDate"),
                            volume=float(m.get("volume", 0) or 0),
                            slug=m.get("slug", ""),
                            active=m.get("active", False),
                            closed=m.get("closed", False),
                        )
                        if market.slug:
                            markets.append(market)
                offset += limit
            except requests.exceptions.RequestException as e:
                log.warning("Network error fetching markets: %s", e)
                break
        return markets

    def get_new_markets(self, markets: list[Market]) -> list[Market]:
        """Return markets we haven't seen before."""
        new = [m for m in markets if m.slug not in self.seen_slugs]
        for m in new:
            self.seen_slugs.add(m.slug)
        if new:
            self._save_seen()
        return new


# =============================================================
# NEWS ANALYZER
# =============================================================
class NewsAnalyzer:
    def __init__(self):
        self.cache = {}  # question -> (timestamp, score)

    def _extract_keywords(self, question: str) -> list[str]:
        """Pull meaningful keywords from a market question."""
        q = question.lower()
        # Remove common prediction-market boilerplate
        q = re.sub(r"(will |does |is |are |was |were |did |has |have |been )", "", q)
        q = re.sub(r"[?\"'\(\)]", "", q)
        # Keep 2-5 word phrases as search queries
        words = [w for w in q.split() if len(w) > 3 and w not in {
            "will", "this", "that", "with", "from", "what", "when",
            "before", "after", "than", "into", "over", "about",
        }]
        return words[:6]  # max 6 keywords

    def _fetch_newsapi(self, keywords: list[str]) -> list[dict]:
        """Fetch news headlines from NewsAPI (free tier)."""
        if not NEWSAPI_KEY:
            return []
        query = " OR ".join(keywords[:4])
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": query,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 10,
            "apiKey": NEWSAPI_KEY,
        }
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json().get("articles", [])
        except Exception as e:
            log.debug("NewsAPI error: %s", e)
        return []

    def _fetch_ddg(self, keywords: list[str]) -> list[dict]:
        """Search DuckDuckGo for recent headlines about keywords."""
        try:
            from duckduckgo_search import DDGS
            query = " ".join(keywords[:4])
            if len(query) < 10:
                return []
            results = []
            with DDGS(timeout=5) as ddgs:
                for r in ddgs.text(query, max_results=5, region="wt-wt", safesearch="off"):
                    results.append({"title": r.get("title", ""), "body": r.get("body", "")})
            return results
        except Exception as e:
            log.debug("DuckDuckGo search error: %s", e)
            return []

    def _keyword_sentiment(self, question: str) -> tuple[float, str]:
        """Simple keyword-based sentiment as baseline."""
        q = question.lower()
        positive = {
            "win", "pass", "approve", "success", "positive", "growth",
            "increase", "bullish", "rally", "gain", "profit", "breakthrough",
            "support", "green", "upgrade", "recover", "boom", "surge",
            "legalize", "confirm", "achieve", "beat", "adopt", "above",
            "higher", "rise", "rising", "uptrend", "victory", "elected",
            "re-elected", "won", "succeed",
        }
        negative = {
            "lose", "fail", "reject", "deny", "negative", "decline",
            "decrease", "bearish", "crash", "loss", "ban", "restrict",
            "against", "red", "downgrade", "default", "drop", "fall",
            "illegal", "reject", "defeat", "miss", "suspend", "cancel",
            "below", "lower", "falling", "downtrend", "loses", "lost",
            "failed", "rejected", "crisis", "war", "invade", "conflict",
        }
        pos_count = sum(1 for w in positive if re.search(rf"\b{w}\w*\b", q))
        neg_count = sum(1 for w in negative if re.search(rf"\b{w}\w*\b", q))
        total = pos_count + neg_count
        if total == 0:
            return 0.0, "neutral"

        score = (pos_count - neg_count) / total
        label = "bullish" if score > 0.2 else ("bearish" if score < -0.2 else "neutral")
        return score, label

    def _sentiment_from_texts(self, texts: list[str]) -> tuple[float, int]:
        """Score a list of text snippets for positive/negative sentiment."""
        pos_w = {"win", "wins", "surge", "rally", "gain", "approve", "pass", "success",
                 "breakthrough", "bullish", "growth", "positive", "uptrend", "recover",
                 "soar", "jump", "high", "surge"}
        neg_w = {"lose", "loses", "crash", "drop", "fail", "reject", "ban", "loss",
                 "decline", "default", "bearish", "crisis", "war", "invade", "conflict",
                 "fall", "plunge", "slump", "low", "downgrade"}
        pos_n = neg_n = 0
        for text in texts:
            t = text.lower()
            pos_n += sum(1 for w in pos_w if w in t)
            neg_n += sum(1 for w in neg_w if w in t)
        total_n = pos_n + neg_n
        if total_n == 0:
            return 0.0, 0
        return (pos_n - neg_n) / total_n, total_n

    def analyze(self, market: Market) -> dict:
        """
        Return a sentiment analysis result for a market.
        Returns: { score: -1..1, label, confidence, source, details }
        """
        question = market.question
        if not question:
            return {"score": 0.0, "label": "neutral", "confidence": 0.0, "source": "none"}

        now = time.time()
        cached = self.cache.get(question)
        if cached and (now - cached[0]) < NEWS_CACHE_TTL:
            return cached[1]

        kw_score, kw_label = self._keyword_sentiment(question)
        keywords = self._extract_keywords(question)

        blended_score = kw_score
        source = "keyword"
        total_signals = 0

        # Skip DDG search for very short-term / trivial markets
        is_short_term = bool(re.search(r"(Up or Down|minute|:\d{2}(PM|AM))", question, re.I))
        # Try DuckDuckGo (free, no API key) — only for meaningful markets
        ddg_results = []
        if not is_short_term and len(keywords) >= 2:
            ddg_results = self._fetch_ddg(keywords)
        if ddg_results:
            texts = [r["title"] + " " + r.get("body", "") for r in ddg_results]
            ddg_score, sigs = self._sentiment_from_texts(texts)
            blended_score = 0.6 * ddg_score + 0.4 * kw_score
            total_signals += sigs
            source = "duckduckgo"

        # Try NewsAPI (needs free API key)
        if NEWSAPI_KEY:
            articles = self._fetch_newsapi(keywords)
            if articles:
                texts = [(a.get("title", "") + " " + (a.get("description", "") or "")) for a in articles]
                na_score, sigs = self._sentiment_from_texts(texts)
                if source == "duckduckgo":
                    blended_score = 0.5 * na_score + 0.3 * ddg_score + 0.2 * kw_score
                else:
                    blended_score = 0.6 * na_score + 0.4 * kw_score
                total_signals += sigs
                source += "+newsapi"

        confidence = min(abs(blended_score), 1.0)
        if total_signals > 0:
            confidence = min(confidence + 0.1, 1.0)

        label = "bullish" if blended_score > 0.2 else ("bearish" if blended_score < -0.2 else "neutral")
        result = {"score": blended_score, "label": label, "confidence": confidence,
                  "source": source, "signals": total_signals}

        self.cache[question] = (now, result)
        return result

        self.cache[question] = (now, result)
        return result


# =============================================================
# DECISION ENGINE
# =============================================================
class DecisionEngine:
    def decide(self, market: Market, sentiment: dict, portfolio: Portfolio) -> Optional[tuple[Side, float, float]]:
        """
        Decide whether to trade.
        Returns (side, entry_price_max, stake_size) or None.
        """
        score = sentiment["score"]
        confidence = sentiment["confidence"]
        current_price_yes = market.outcome_prices[0]

        # Need minimum confidence
        if confidence < PRICE_EDGE_MIN:
            return None

        # Determine direction from sentiment
        if score > 0.0:
            # Bullish -> we think YES will happen
            predicted_prob = 0.5 + score * 0.4  # map score to probability
            market_prob = current_price_yes
            edge = predicted_prob - market_prob

            if edge > PRICE_EDGE_MIN:
                side = Side.YES
                entry_max = market_prob + 0.02  # willing to pay slightly above current
                stake = portfolio.balance * MAX_BET_FRACTION
                return side, entry_max, stake
        else:
            # Bearish -> we think NO (complement)
            predicted_prob_no = 0.5 + abs(score) * 0.4
            market_prob_no = 1.0 - current_price_yes
            edge = predicted_prob_no - market_prob_no

            if edge > PRICE_EDGE_MIN:
                side = Side.NO
                entry_max = market_prob_no + 0.02
                stake = portfolio.balance * MAX_BET_FRACTION
                return side, entry_max, stake

        return None


# =============================================================
# SIMULATOR / PORTFOLIO
# =============================================================
class Simulator:
    def __init__(self):
        self.portfolio = Portfolio()
        self._load_portfolio()

    def _load_portfolio(self):
        try:
            if os.path.exists(PORTFOLIO_FILE):
                with open(PORTFOLIO_FILE) as f:
                    data = json.load(f)
                    self.portfolio.balance = data.get("balance", STARTING_BALANCE)
                    self.portfolio.total_realized_pnl = data.get("total_realized_pnl", 0.0)
                    self.portfolio.trade_count = data.get("trade_count", 0)
                    for p in data.get("positions", []):
                        self.portfolio.positions.append(Position(**p))
        except Exception as e:
            log.warning("Could not load portfolio: %s", e)

    def _save_portfolio(self):
        try:
            data = {
                "balance": self.portfolio.balance,
                "total_realized_pnl": round(self.portfolio.total_realized_pnl, 2),
                "trade_count": self.portfolio.trade_count,
                "positions": [asdict(p) for p in self.portfolio.positions],
            }
            tmp = PORTFOLIO_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, PORTFOLIO_FILE)
        except Exception as e:
            log.warning("Could not save portfolio: %s", e)

    def open_position(self, market: Market, side: Side, price: float, stake: float):
        """Simulate opening a position."""
        if stake > self.portfolio.balance:
            log.warning("Insufficient balance: have $%.2f, need $%.2f", self.portfolio.balance, stake)
            return

        size = stake / price  # number of outcome tokens
        pos = Position(
            market_id=market.id,
            question=market.question,
            side=side,
            entry_price=price,
            size=size,
            cost=stake,
            timestamp=time.time(),
        )
        self.portfolio.positions.append(pos)
        self.portfolio.balance -= stake
        self.portfolio.trade_count += 1

        # Log trade
        self._log_trade(market, side, price, stake, "OPEN")
        self._save_portfolio()
        log.info("OPEN %s on '%s' at $%.2f, stake $%.2f (%.2f tokens)",
                 side.value, market.question[:50], price, stake, size)

    def close_position(self, pos: Position, close_price: float):
        """Simulate closing a position."""
        proceeds = pos.size * close_price
        pos.pnl = proceeds - pos.cost
        pos.close_price = close_price
        pos.closed = True

        self.portfolio.balance += proceeds
        self.portfolio.total_realized_pnl += pos.pnl

        self._log_trade(None, pos.side, pos.entry_price, pos.cost, "CLOSE",
                        f"exit=${close_price:.2f} pnl=${pos.pnl:.2f}")
        self._save_portfolio()
        log.info("CLOSE %s on '%s' PnL: $%.2f", pos.side.value, pos.question[:40], pos.pnl)

    def update_unrealized_pnl(self, active_markets: dict[str, tuple[float, float]]):
        """Update unrealized P&L from current market prices."""
        total_unrealized = 0.0
        for pos in self.portfolio.positions:
            if pos.closed:
                continue
            prices = active_markets.get(pos.market_id)
            if prices:
                current_price = prices[0] if pos.side == Side.YES else prices[1]
                pos.pnl = (current_price - pos.entry_price) * pos.size
                total_unrealized += pos.pnl
        self.portfolio.total_unrealized_pnl = total_unrealized

    def check_resolved_markets(self, markets: list[Market]):
        """Check if any of our open positions have been resolved."""
        market_map = {m.id: m for m in markets if m.id}
        for pos in self.portfolio.positions[:]:
            if pos.closed:
                continue
            # If market not in active list anymore, assume it resolved
            # In real scenario we'd check resolution; for demo we auto-close at current price
            m = market_map.get(pos.market_id)
            if m is None or m.closed:
                # Market resolved or no longer active
                if m and m.outcome_prices:
                    # If one outcome is 1.0 and other 0.0, it resolved
                    close_price = 1.0 if (pos.side == Side.YES and m.outcome_prices[0] > 0.99) else \
                                  1.0 if (pos.side == Side.NO and m.outcome_prices[1] > 0.99) else 0.0
                else:
                    close_price = 0.0
                self.close_position(pos, close_price)

    def _log_trade(self, market, side, price, stake, action, extra=""):
        try:
            with open(TRADES_FILE, "a", newline="") as f:
                w = csv.writer(f)
                if f.tell() == 0:
                    w.writerow(["timestamp", "action", "market_id", "question", "side",
                                "price", "stake", "balance", "equity", "extra"])
                w.writerow([
                    datetime.now(timezone.utc).isoformat(),
                    action,
                    market.id if market else "",
                    market.question[:60] if market else "",
                    side.value if isinstance(side, Side) else side,
                    round(price, 4),
                    round(stake, 2),
                    round(self.portfolio.balance, 2),
                    round(self.portfolio.equity, 2),
                    extra,
                ])
        except Exception as e:
            log.warning("Log write error: %s", e)

    def report(self):
        """Print portfolio summary."""
        eq = self.portfolio.equity
        pnl = eq - STARTING_BALANCE
        pnl_pct = (pnl / STARTING_BALANCE) * 100
        open_count = sum(1 for p in self.portfolio.positions if not p.closed)

        print(f"""
+------------------------------------------+
|         POLYMARKET DEMO BOT              |
+------------------------------------------+
| Starting Balance:  $ {STARTING_BALANCE:>6.2f}        |
| Current Balance:   $ {self.portfolio.balance:>6.2f}        |
| Unrealized PnL:    $ {self.portfolio.total_unrealized_pnl:>+6.2f}        |
| Realized PnL:      $ {self.portfolio.total_realized_pnl:>+6.2f}        |
| Total Equity:      $ {eq:>6.2f}        |
| Return:            {pnl_pct:>+6.2f}%        |
| Trades:            {self.portfolio.trade_count:>6d}        |
| Open Positions:    {open_count:>6d}        |
+------------------------------------------+
        """)

    def print_positions(self):
        if not self.portfolio.positions:
            print("  No positions yet.")
            return
        print(f"\n  {'SIDE':<5} {'PRICE':<7} {'SIZE':<7} {'COST':<7} {'PnL':<8} {'MARKET'}")
        print(f"  {'-'*5} {'-'*7} {'-'*7} {'-'*7} {'-'*8} {'-'*40}")
        for p in self.portfolio.positions:
            status = "OPEN " if not p.closed else "CLOSED"
            pnl_str = f"${p.pnl:+.2f}" if p.pnl != 0 else "$0.00"
            print(f"  {p.side.value:<5} ${p.entry_price:<5.3f} {p.size:<7.2f} ${p.cost:<5.2f} {pnl_str:<8} {p.question[:45]}")


# =============================================================
# MAIN BOT
# =============================================================
class PolymarketBot:
    def __init__(self):
        self.scanner = MarketScanner()
        self.analyzer = NewsAnalyzer()
        self.decider = DecisionEngine()
        self.simulator = Simulator()
        self.running = False

    def run_once(self):
        """Single scan cycle."""
        # 1. Fetch all active markets
        log.info("Scanning active markets...")
        all_markets = self.scanner.fetch_active_markets()
        log.info("Found %d active markets", len(all_markets))

        # 2. Find new markets
        new_markets = self.scanner.get_new_markets(all_markets)
        if new_markets:
            log.info("NEW markets detected: %d", len(new_markets))
            for m in new_markets:
                self._evaluate_market(m)
        else:
            log.info("No new markets detected")

        # 3. Update unrealized P&L
        price_map = {}
        for m in all_markets:
            if m.id and m.outcome_prices:
                price_map[m.id] = (m.outcome_prices[0], m.outcome_prices[1] if len(m.outcome_prices) > 1 else 1 - m.outcome_prices[0])
        self.simulator.update_unrealized_pnl(price_map)

        # 4. Check for resolved markets
        self.simulator.check_resolved_markets(all_markets)

        # 5. Report
        self.simulator.report()

        return len(new_markets)

    def _evaluate_market(self, market: Market):
        """Analyze a market and decide whether to trade."""
        log.info("  Evaluating: %s | YES=%.2f NO=%.2f | vol=$%.0f",
                 market.question[:60], market.outcome_prices[0],
                 market.outcome_prices[1] if len(market.outcome_prices) > 1 else 1 - market.outcome_prices[0],
                 market.volume)

        # Skip tiny or zero-volume markets (likely spam)
        if market.volume < 100:
            log.info("    -> Skipped (low volume)")
            return

        # Skip already-resolved markets (price too close to 0 or 1)
        p = market.outcome_prices[0]
        if p < 0.01 or p > 0.99:
            log.info("    -> Skipped (market already resolved, YES=%.2f)", p)
            return

        # Sentiment analysis
        sentiment = self.analyzer.analyze(market)
        log.info("    Sentiment: %s (%.2f conf=%s)",
                 sentiment["label"], sentiment["score"],
                 f"{sentiment['confidence']:.2f}" if sentiment["confidence"] else "N/A")

        # Decision
        decision = self.decider.decide(market, sentiment, self.simulator.portfolio)
        if decision is None:
            log.info("    -> No trade (no edge)")
            return

        side, max_price, stake = decision
        current_price = market.outcome_prices[0] if side == Side.YES else (1 - market.outcome_prices[0])

        if current_price > max_price:
            log.info("    -> No trade (price above max: $%.2f > $%.2f)", current_price, max_price)
            return

        stake = min(stake, self.simulator.portfolio.balance)
        if stake < 0.10:
            log.info("    -> No trade (stake too small: $%.2f)", stake)
            return

        self.simulator.open_position(market, side, current_price, stake)

    def run(self):
        """Main loop. Runs forever, polling periodically."""
        self.running = True
        log.info("=" * 50)
        log.info("Polymarket Demo Bot STARTED")
        log.info("Balance: $%.2f | Check every %ds", STARTING_BALANCE, POLL_INTERVAL_SEC)
        log.info("=" * 50)
        self.simulator.report()

        try:
            while self.running:
                self.run_once()
                log.info("Sleeping %d seconds...", POLL_INTERVAL_SEC)
                time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            log.info("Shutting down...")
            self.simulator.report()

    def run_interactive(self):
        """Single-shot interactive mode."""
        self.run_once()


# =============================================================
# ENTRY
# =============================================================
def _start_dashboard():
    """Start Flask dashboard in background thread."""
    import threading, sys
    import importlib.util
    spec = importlib.util.spec_from_file_location("dashboard", os.path.join(os.path.dirname(__file__), "dashboard.py"))
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        port = int(os.environ.get("PORT", 8080))
        log.info("Dashboard starting on http://0.0.0.0:%d", port)
        mod.app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    else:
        log.warning("dashboard.py not found, skipping")

if __name__ == "__main__":
    import sys

    bot = PolymarketBot()

    if "--once" in sys.argv:
        bot.run_interactive()
    elif "--report" in sys.argv:
        bot.simulator.report()
        bot.simulator.print_positions()
    else:
        t = threading.Thread(target=_start_dashboard, daemon=True)
        t.start()
        bot.run()
