#!/bin/bash
# ============================================================
#  Hermes Trading System — Hetzner Setup Script
#  Run as root on the Hetzner server.
#  Usage: bash /root/spy-0dte-trader/hermes_system/setup.sh
# ============================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; }

echo "============================================================"
echo "  Hermes Trading System — Hetzner Setup"
echo "  $(date)"
echo "============================================================"

# ── 1. Directories ────────────────────────────────────────────
echo ""
echo "[1/6] Creating runtime directories..."
mkdir -p /root/hermes_system/{trades,insights,logs,new_strategies,pending_strategies}
mkdir -p /root/spy-0dte-trader/data
ok "All directories created."

# ── 2. Python packages ────────────────────────────────────────
echo ""
echo "[2/6] Installing Python packages..."
pip install --quiet --upgrade \
    requests \
    pytz \
    pandas \
    numpy \
    python-dotenv \
    schedule \
    yfinance
ok "Packages installed."

# ── 3. Environment file check ─────────────────────────────────
echo ""
echo "[3/6] Checking .env..."
ENV_FILE="/root/spy-0dte-trader/.env"
if [ ! -f "$ENV_FILE" ]; then
    fail ".env not found at $ENV_FILE"
    echo ""
    echo "  Required variables:"
    echo "    TRADIER_API_KEY              # production (market data)"
    echo "    TRADIER_SANDBOX_KEY          # sandbox (paper orders)"
    echo "    TRADIER_SANDBOX_ACCOUNT_ID   # sandbox account ID"
    echo "    ALPACA_API_KEY               # Alpaca data (daily bars)"
    echo "    ALPACA_API_SECRET"
    echo "    TELEGRAM_BOT_TOKEN"
    echo "    TELEGRAM_CHAT_ID"
    echo "    OPENROUTER_API_KEY           # for hermes_trigger.py"
    echo ""
    exit 1
fi

# Load env vars for the tests below
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
ok ".env loaded."

# ── 4. API connectivity tests ─────────────────────────────────
echo ""
echo "[4/6] Testing API connections..."

# Tradier production
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer ${TRADIER_API_KEY}" \
    -H "Accept: application/json" \
    "https://api.tradier.com/v1/markets/quotes?symbols=SPY" 2>/dev/null)
if [ "$HTTP" = "200" ]; then
    ok "Tradier production (market data): HTTP $HTTP"
else
    warn "Tradier production returned HTTP $HTTP — check TRADIER_API_KEY"
fi

# Tradier sandbox
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer ${TRADIER_SANDBOX_KEY}" \
    -H "Accept: application/json" \
    "https://sandbox.tradier.com/v1/accounts/${TRADIER_SANDBOX_ACCOUNT_ID}/balances" 2>/dev/null)
if [ "$HTTP" = "200" ]; then
    ok "Tradier sandbox (paper orders): HTTP $HTTP"
else
    warn "Tradier sandbox returned HTTP $HTTP — check TRADIER_SANDBOX_KEY / TRADIER_SANDBOX_ACCOUNT_ID"
fi

# Telegram
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null)
if [ "$HTTP" = "200" ]; then
    ok "Telegram bot: HTTP $HTTP"
else
    warn "Telegram returned HTTP $HTTP — check TELEGRAM_BOT_TOKEN"
fi

# OpenRouter (just reachability)
HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer ${OPENROUTER_API_KEY}" \
    "https://openrouter.ai/api/v1/models" 2>/dev/null)
if [ "$HTTP" = "200" ]; then
    ok "OpenRouter API: HTTP $HTTP"
else
    warn "OpenRouter returned HTTP $HTTP — check OPENROUTER_API_KEY"
fi

# ── 5. Python smoke test ──────────────────────────────────────
echo ""
echo "[5/7] Python smoke test..."
cd /root/spy-0dte-trader
python - <<'PYEOF'
import sys
sys.path.insert(0, '/root/spy-0dte-trader')

errors = []

# Check all hermes_system modules import cleanly
for mod in [
    'hermes_system.execution_engine',
    'hermes_system.pattern_engine',
    'hermes_system.hermes_daily_brief',
    'hermes_system.hermes_trigger',
    'hermes_system.monitor',
    'hermes_system.kill_switch',
]:
    try:
        __import__(mod)
        print(f"  ✓ {mod}")
    except Exception as e:
        print(f"  ✗ {mod}: {e}")
        errors.append(mod)

# Run pattern engine on empty data
try:
    from hermes_system.pattern_engine import load_all_trades, _strategy_stats
    trades = load_all_trades()
    stats  = _strategy_stats(trades)
    print(f"  ✓ pattern_engine: loaded {len(trades)} trades")
except Exception as e:
    print(f"  ✗ pattern_engine run: {e}")
    errors.append('pattern_engine run')

if errors:
    print(f"\n  {len(errors)} error(s) — check above.")
    sys.exit(1)
else:
    print("\n  All modules OK.")
PYEOF
ok "Smoke test passed."

# ── 6. Cron jobs ──────────────────────────────────────────────
echo ""
echo "[6/7] Installing cron jobs..."

# Preserve existing non-hermes crontab entries
crontab -l 2>/dev/null | grep -v 'hermes' > /tmp/_hermes_cron || true

cat >> /tmp/_hermes_cron << 'CRONEOF'

# ── Hermes Trading System ─────────────────────────────────────────────────────
TZ=America/New_York

# Execution engine: 9:45 AM ET Mon-Fri
45 9 * * 1-5 cd /root/spy-0dte-trader && python hermes_system/execution_engine.py >> /root/hermes_system/logs/execution.log 2>&1

# Force-exit sweep: 3:50 PM ET Mon-Fri (safety net — engine handles exits internally)
50 15 * * 1-5 echo "[$(date)] force-exit sweep — engine should have exited all positions by now" >> /root/hermes_system/logs/force_exit.log

# Pattern analysis: 4:30 PM ET Mon-Fri
30 16 * * 1-5 cd /root/spy-0dte-trader && python hermes_system/pattern_engine.py >> /root/hermes_system/logs/pattern.log 2>&1

# Daily brief: 4:45 PM ET Mon-Fri
45 16 * * 1-5 cd /root/spy-0dte-trader && python hermes_system/hermes_daily_brief.py >> /root/hermes_system/logs/brief.log 2>&1

# AI insights (hermes_trigger): 5:00 PM ET Mon-Fri
0 17 * * 1-5 cd /root/spy-0dte-trader && python hermes_system/hermes_trigger.py >> /root/hermes_system/logs/trigger.log 2>&1

# Weekly review: Friday 8:00 PM ET
0 20 * * 5 cd /root/spy-0dte-trader && python hermes_system/hermes_trigger.py >> /root/hermes_system/logs/weekly_review.log 2>&1

# Hermes Researcher: Sunday 8:00 PM ET
0 20 * * 0 cd /root/spy-0dte-trader && python hermes_researcher.py >> /root/hermes_system/logs/researcher.log 2>&1
CRONEOF

crontab /tmp/_hermes_cron
rm -f /tmp/_hermes_cron
ok "Cron jobs installed:"
crontab -l | grep -E '(hermes|TZ=)'
echo ""

# ── Kill switch service ───────────────────────────────────────
echo ""
echo "[7/7] Installing hermes-kill-switch systemd service..."
cp /root/spy-0dte-trader/hermes_system/systemd/hermes-kill-switch.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable hermes-kill-switch
systemctl restart hermes-kill-switch
KS_STATUS=$(systemctl is-active hermes-kill-switch 2>/dev/null)
if [ "$KS_STATUS" = "active" ]; then
    ok "hermes-kill-switch: active"
else
    warn "hermes-kill-switch status: $KS_STATUS — check 'journalctl -u hermes-kill-switch'"
fi

echo "============================================================"
echo "  Setup complete. Hermes System is ready."
echo ""
echo "  Start monitor:   python /root/spy-0dte-trader/hermes_system/monitor.py"
echo "  Run manual test: python /root/spy-0dte-trader/hermes_system/pattern_engine.py"
echo "  View logs:       tail -f /root/hermes_system/logs/execution.log"
echo "  Kill switch:     journalctl -u hermes-kill-switch -f"
echo "  Phone commands:  /restart /logs /vix /positions /pnl /health"
echo "============================================================"
