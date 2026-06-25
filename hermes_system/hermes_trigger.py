#!/usr/bin/env python3
"""
Hermes Trigger — Reads daily_brief.txt, calls OpenRouter API (claude-haiku-4-5),
answers 4 focused questions, saves to insights/YYYY-MM-DD.md, sends to Telegram.
Cron: 0 17 * * 1-5  (5:00 PM ET)
Target: <3 000 tokens/call ≈ $0.05/day
"""
import sys
sys.path.insert(0, '/root/spy-0dte-trader')

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Tuple

import requests
from dotenv import load_dotenv

from core.telegram_alerts import send as tg_send

load_dotenv('/root/spy-0dte-trader/.env')

HERMES_ROOT    = Path('/root/hermes_system')
BRIEF_PATH     = HERMES_ROOT / 'daily_brief.txt'
INSIGHTS_DIR   = HERMES_ROOT / 'insights'
INSIGHTS_DIR.mkdir(parents=True, exist_ok=True)

OPENROUTER_KEY = os.getenv('OPENROUTER_API_KEY', '')
MODEL          = 'anthropic/claude-haiku-4-5'
MAX_TOKENS     = 550    # tight cap — keeps total well under 3k tokens

SYSTEM_PROMPT = (
    "You are a systematic options trading analyst reviewing daily performance data "
    "for an 8-strategy automated SPY 0DTE trading system. "
    "Be concise, data-driven, and specific. No markdown headers. Plain text only."
)

USER_TEMPLATE = """\
Daily trading brief:

{brief}

Answer ONLY these 4 questions. Total response must be under 500 words.

1. BEST/WORST: Which strategy performed best and worst today, and what market conditions explain it?

2. CONDITIONS TO AVOID: Based on today's data and historical patterns, what specific conditions (VIX level, day, regime) should be avoided tomorrow?

3. PARAMETER ADJUSTMENT: Name one specific parameter to adjust for tomorrow. Format: [strategy] [parameter]: [current_value] → [proposed_value]. Give one concrete reason.

4. NEW HYPOTHESIS: State one testable hypothesis based on today's results. Format: "IF [condition] THEN [expected outcome] — test via [backtest approach]."

Use the actual numbers in the brief. No generic advice.\
"""


def call_openrouter(brief: str) -> Tuple[str, int]:
    """Returns (response_text, tokens_used)."""
    if not OPENROUTER_KEY:
        return 'ERROR: OPENROUTER_API_KEY not configured in .env', 0

    brief_trimmed = brief[:2800]  # hard cap on input length
    prompt        = USER_TEMPLATE.format(brief=brief_trimmed)

    try:
        r = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {OPENROUTER_KEY}',
                'Content-Type':  'application/json',
                'HTTP-Referer':  'https://github.com/hermes-trader',
                'X-Title':       'HermesTrader',
            },
            json={
                'model':      MODEL,
                'max_tokens': MAX_TOKENS,
                'messages': [
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {'role': 'user',   'content': prompt},
                ],
            },
            timeout=45,
        )
        r.raise_for_status()
        data   = r.json()
        text   = data['choices'][0]['message']['content']
        tokens = data.get('usage', {}).get('total_tokens', 0)
        return text, tokens
    except requests.HTTPError as exc:
        return f'HTTP error {exc.response.status_code}: {exc.response.text[:300]}', 0
    except Exception as exc:
        return f'ERROR: {exc}', 0


def main() -> None:
    if not BRIEF_PATH.exists():
        print(f'No brief found at {BRIEF_PATH} — run hermes_daily_brief.py first.')
        tg_send('⚠️ Hermes Trigger: daily_brief.txt not found.')
        return

    brief    = BRIEF_PATH.read_text()
    today    = date.today().isoformat()
    now_utc  = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

    print(f'Calling OpenRouter ({MODEL})...')
    insights, tokens = call_openrouter(brief)
    print(f'Response received ({tokens} tokens).')

    # Save to markdown file
    out_path = INSIGHTS_DIR / f'{today}.md'
    out_path.write_text(
        f'# Hermes Insights — {today}\n\n'
        f'_Generated: {now_utc} | Model: {MODEL} | Tokens: {tokens}_\n\n'
        f'{insights}\n'
    )
    print(f'Insights saved → {out_path}')

    # Send to Telegram (truncate to 4000 chars — Telegram limit is 4096)
    tg_msg = f'🤖 HERMES INSIGHTS ({today})\n\n{insights}'
    tg_send(tg_msg[:4000])
    print('Sent to Telegram.')


if __name__ == '__main__':
    main()
