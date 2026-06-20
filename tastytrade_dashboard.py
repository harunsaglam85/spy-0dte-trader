import os
import sys
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv('/root/spy-0dte-trader/.env')

def _strip(val):
    return (val or '').strip("'\"")

USE_PRODUCTION = _strip(os.getenv('TASTYTRADE_USE_PRODUCTION', 'true')).lower() == 'true'
BASE_URL       = 'https://api.tastytrade.com' if USE_PRODUCTION else 'https://api.cert.tastytrade.com'

CLIENT_SECRET = _strip(os.getenv('TASTYTRADE_CLIENT_SECRET', ''))
REFRESH_TOKEN = _strip(os.getenv('TASTYTRADE_REFRESH_TOKEN', ''))
ACCOUNT_ID    = _strip(os.getenv('TASTYTRADE_ACCOUNT_ID', ''))


def get_session_token() -> str:
    if not CLIENT_SECRET or not REFRESH_TOKEN:
        sys.exit('ERROR: TASTYTRADE_CLIENT_SECRET and TASTYTRADE_REFRESH_TOKEN must be set in .env')

    resp = requests.post(
        f'{BASE_URL}/oauth/token',
        data={
            'grant_type':    'refresh_token',
            'refresh_token': REFRESH_TOKEN,
            'client_secret': CLIENT_SECRET,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    if not resp.ok:
        sys.exit(f'ERROR: OAuth token refresh failed ({resp.status_code}): {resp.text}')

    payload = resp.json()
    # Tastytrade may return session-token directly or inside a data envelope
    token = (
        payload.get('session-token')
        or payload.get('access_token')
        or (payload.get('data') or {}).get('session-token')
    )
    if not token:
        sys.exit(f'ERROR: No token in OAuth response: {payload}')
    return token


def get_account_balance(token: str, account_id: str) -> dict:
    resp = requests.get(
        f'{BASE_URL}/accounts/{account_id}/balances',
        headers={'Authorization': f'Bearer {token}'},
    )
    if not resp.ok:
        sys.exit(f'ERROR: Could not fetch balance ({resp.status_code}): {resp.text}')
    return resp.json()['data']


def get_quote(symbol: str) -> dict:
    # Yahoo Finance v8 chart endpoint — no auth required, real-time delayed quote
    resp = requests.get(
        f'https://query1.finance.yahoo.com/v8/finance/chart/{symbol}',
        params={'interval': '1m', 'range': '1d'},
        headers={'User-Agent': 'Mozilla/5.0'},
    )
    if not resp.ok:
        sys.exit(f'ERROR: Could not fetch quote for {symbol} ({resp.status_code}): {resp.text}')
    meta = resp.json()['chart']['result'][0]['meta']
    return {
        'symbol': meta.get('symbol', symbol),
        'last':   meta.get('regularMarketPrice', 0),
        'prev':   meta.get('previousClose', 0),
        'high':   meta.get('regularMarketDayHigh', 0),
        'low':    meta.get('regularMarketDayLow', 0),
        'volume': meta.get('regularMarketVolume', 0),
    }


def _f(val, default=0.0) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def build_report(account_id: str, balance: dict, quote: dict) -> str:
    ts  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    sep = '=' * 52
    lines = [
        'TASTYTRADE TRADING DASHBOARD',
        f'Generated : {ts}',
        f'Account   : {account_id}',
        sep,
        '',
        'ACCOUNT BALANCE',
        '-' * 30,
        f'  Net Liquidating Value : ${_f(balance.get("net-liquidating-value")):>13,.2f}',
        f'  Cash Balance          : ${_f(balance.get("cash-balance")):>13,.2f}',
        f'  Buying Power          : ${_f(balance.get("buying-power")):>13,.2f}',
        f'  Day-Trading BP        : ${_f(balance.get("day-trading-buying-power")):>13,.2f}',
        f'  Maintenance Margin    : ${_f(balance.get("maintenance-requirement")):>13,.2f}',
        '',
        'SPY LIVE QUOTE',
        '-' * 30,
        f'  Symbol : {quote.get("symbol", "SPY")}',
        f'  Last   : ${_f(quote.get("last")):>8.2f}',
        f'  Prev   : ${_f(quote.get("prev")):>8.2f}',
        f'  High   : ${_f(quote.get("high")):>8.2f}',
        f'  Low    : ${_f(quote.get("low")):>8.2f}',
        f'  Volume : {int(_f(quote.get("volume"))):>12,}',
        '',
        sep,
    ]
    return '\n'.join(lines)


def main():
    if not ACCOUNT_ID:
        sys.exit('ERROR: TASTYTRADE_ACCOUNT_ID is not set in .env')

    print('Authenticating via OAuth refresh token...')
    token = get_session_token()

    print(f'Fetching balance for account {ACCOUNT_ID}...')
    balance = get_account_balance(token, ACCOUNT_ID)

    print('Fetching SPY quote...')
    quote = get_quote('SPY')

    report = build_report(ACCOUNT_ID, balance, quote)
    print('\n' + report)

    out_path = r'C:\Users\sagla\trading_dashboard.txt'
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(report + '\n')
    print(f'\nSaved to {out_path}')


if __name__ == '__main__':
    main()
