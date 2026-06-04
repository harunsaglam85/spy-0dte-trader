"""
sentiment.py — Simple news sentiment scorer using NewsAPI free tier.

Fetches recent headlines for market-relevant queries and scores them
using keyword matching. Returns a float in [-1.0, +1.0].

Gracefully returns 0.0 if NEWSAPI_KEY is not configured.
"""

import logging
import os
from typing import Optional

import requests

_NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
_NEWSAPI_URL = "https://newsapi.org/v2/everything"
_logger = logging.getLogger("sentiment")

# Queries to search for morning sentiment
_QUERIES = ["SPY ETF", "S&P 500", "Federal Reserve", "inflation"]

# Positive keywords — each match adds weight
_POSITIVE_WORDS = [
    "rally", "surge", "gain", "rise", "record", "high", "bull", "strong",
    "growth", "positive", "optimism", "rebound", "recovery", "beat", "boost",
    "upgrade", "buy", "upside", "outperform", "green",
]

# Negative keywords — each match subtracts weight
_NEGATIVE_WORDS = [
    "fall", "drop", "plunge", "crash", "loss", "bear", "weak", "recession",
    "inflation", "selloff", "sell-off", "decline", "downgrade", "miss",
    "concern", "risk", "fear", "warning", "cut", "downside", "red",
    "tariff", "war", "crisis", "panic", "correction",
]


def _score_headline(text: str) -> float:
    """Return a sentiment score for a single headline string."""
    lower = text.lower()
    pos = sum(1 for w in _POSITIVE_WORDS if w in lower)
    neg = sum(1 for w in _NEGATIVE_WORDS if w in lower)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def fetch_sentiment() -> float:
    """
    Fetch recent headlines and return aggregate sentiment in [-1.0, +1.0].
    Returns 0.0 if NEWSAPI_KEY not set or on any error.
    """
    if not _NEWSAPI_KEY:
        _logger.debug("NEWSAPI_KEY not set — sentiment defaults to 0.0")
        return 0.0

    scores: list = []
    for query in _QUERIES:
        try:
            resp = requests.get(
                _NEWSAPI_URL,
                params={
                    "q": query,
                    "language": "en",
                    "pageSize": 10,
                    "sortBy": "publishedAt",
                    "apiKey": _NEWSAPI_KEY,
                },
                timeout=5,
            )
            if not resp.ok:
                _logger.warning("NewsAPI error for query '%s': %s", query, resp.status_code)
                continue
            articles = resp.json().get("articles", [])
            for article in articles:
                title = article.get("title") or ""
                description = article.get("description") or ""
                text = f"{title} {description}"
                scores.append(_score_headline(text))
        except Exception as exc:
            _logger.warning("NewsAPI fetch error for query '%s': %s", query, exc)

    if not scores:
        return 0.0

    raw = sum(scores) / len(scores)
    # Clamp to [-1.0, +1.0]
    result = max(-1.0, min(1.0, raw))
    _logger.info("Sentiment score: %.3f  (from %d headlines)", result, len(scores))
    return result
