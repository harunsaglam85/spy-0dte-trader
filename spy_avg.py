import os
import requests
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv(r"C:\Users\sagla\.tastytrade-mcp\.env")
API_KEY = os.getenv("POLYGON_API_KEY")

def get_last_n_trading_days(n=3):
    trading_days = []
    d = date.today()
    while len(trading_days) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            trading_days.append(d)
    return list(reversed(trading_days))

def fetch_spy_close(trading_date: date) -> float:
    url = (
        f"https://api.polygon.io/v1/open-close/SPY/{trading_date.isoformat()}"
        f"?adjusted=true&apiKey={API_KEY}"
    )
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    return data["close"]

days = get_last_n_trading_days(3)
closes = []

print("SPY Closing Prices")
print("-" * 30)
for d in days:
    close = fetch_spy_close(d)
    closes.append(close)
    print(f"  {d.strftime('%A %b %d, %Y'):25s}  ${close:.2f}")

avg = sum(closes) / len(closes)
print("-" * 30)
print(f"  {'3-Day Average':25s}  ${avg:.2f}")
