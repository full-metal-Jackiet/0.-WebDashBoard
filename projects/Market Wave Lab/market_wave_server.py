#!/usr/bin/env python3
"""Local helper server for Market Wave Lab.

Run: python3 market_wave_server.py
Open: http://localhost:8765
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen
import sys

ROOT = Path(__file__).resolve().parent
HTML = ROOT / "market_wave_real_ticker.html"
PORT = 8765


def _send_json(handler: SimpleHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()
    handler.wfile.write(body)


def _fetch_text(url: str, accept: str) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 MarketWaveLab/1.0",
            "Accept": accept,
        },
    )
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


def _coerce_float(value):
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value):
    if value in ("", None):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _utc_date(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), tz=timezone.utc).strftime("%Y-%m-%d")


def _normalize_symbol(symbol: str) -> str:
    s = symbol.strip().upper().replace(" ", "")
    aliases = {
        "SP500": "^GSPC",
        "S&P500": "^GSPC",
        "S&P": "^GSPC",
        "SPX": "^GSPC",
        "GSPC": "^GSPC",
        "NASDAQ100": "^NDX",
        "NDX": "^NDX",
        "DOW": "^DJI",
        "DJI": "^DJI",
    }
    return aliases.get(s, s)


def _stooq_symbol(symbol: str) -> str:
    s = symbol.strip().lower()
    if not s:
        return s
    return s if "." in s else f"{s}.us"


def _fetch_yahoo(symbol: str, period: str, interval: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{quote(symbol)}?range={quote(period)}&interval={quote(interval)}"
        "&events=history&includeAdjustedClose=true"
    )
    raw = _fetch_text(url, "application/json,text/plain,*/*")
    data = json.loads(raw)

    chart = data.get("chart") or {}
    errors = chart.get("error")
    if errors:
        raise RuntimeError(errors.get("description") or errors.get("code") or "Yahoo chart error")

    results = chart.get("result") or []
    if not results:
        raise RuntimeError("Yahoo returned no chart data")

    result = results[0]
    timestamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    quotes = indicators.get("quote") or [{}]
    quote_data = quotes[0] or {}
    adjclose = (indicators.get("adjclose") or [{}])[0].get("adjclose") or []

    rows = []
    for idx, ts in enumerate(timestamps):
        close = _coerce_float((quote_data.get("close") or [None])[idx] if idx < len(quote_data.get("close") or []) else None)
        if close is None:
            continue
        open_ = _coerce_float((quote_data.get("open") or [None])[idx] if idx < len(quote_data.get("open") or []) else None)
        high = _coerce_float((quote_data.get("high") or [None])[idx] if idx < len(quote_data.get("high") or []) else None)
        low = _coerce_float((quote_data.get("low") or [None])[idx] if idx < len(quote_data.get("low") or []) else None)
        volume = _coerce_int((quote_data.get("volume") or [None])[idx] if idx < len(quote_data.get("volume") or []) else None)
        adj_close = _coerce_float(adjclose[idx] if idx < len(adjclose) else None)
        rows.append(
            {
                "date": _utc_date(ts),
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "adjClose": adj_close if adj_close is not None else close,
                "volume": volume,
            }
        )

    if not rows:
        raise RuntimeError("Yahoo returned no usable price rows")

    return rows


def _fetch_stooq(symbol: str):
    stooq_symbol = _stooq_symbol(symbol)
    url = f"https://stooq.com/q/d/l/?s={quote(stooq_symbol)}&i=d"
    raw = _fetch_text(url, "text/csv,text/plain,*/*")
    text = raw.lstrip("\ufeff").strip()

    if not text:
        raise RuntimeError("empty response")
    if text[:1] == "<" or "<html" in text[:200].lower():
        raise RuntimeError("provider returned HTML instead of CSV")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise RuntimeError("missing CSV headers")

    normalized_headers = {name.strip().lower() for name in reader.fieldnames if name}
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(normalized_headers):
        raise RuntimeError(f"invalid CSV headers: {', '.join(reader.fieldnames)}")

    rows = []
    for row in reader:
        date = (row.get("Date") or row.get("date") or "").strip()
        close = _coerce_float(row.get("Close") or row.get("close"))
        if not date or close is None:
            continue
        open_ = _coerce_float(row.get("Open") or row.get("open"))
        high = _coerce_float(row.get("High") or row.get("high"))
        low = _coerce_float(row.get("Low") or row.get("low"))
        volume = _coerce_int(row.get("Volume") or row.get("volume"))
        rows.append(
            {
                "date": date,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "adjClose": close,
                "volume": volume,
            }
        )

    if not rows:
        raise RuntimeError("no usable CSV rows")

    return rows


class Handler(SimpleHTTPRequestHandler):
    def do_OPTIONS(self):
        if urlparse(self.path).path == "/api/history":
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            data = HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/history":
            qs = parse_qs(parsed.query)
            symbol = _normalize_symbol((qs.get("symbol") or qs.get("s") or [""])[0])
            period = (qs.get("period") or ["max"])[0].strip() or "max"
            interval = (qs.get("interval") or ["1d"])[0].strip() or "1d"

            if not symbol:
                _send_json(self, 400, {"ok": False, "symbol": "", "error": "missing symbol", "details": {}})
                return

            errors = {}
            rows = None
            provider = None

            print(f"[history] requested symbol={symbol} period={period} interval={interval}", flush=True)
            print("[history] trying provider=yahoo", flush=True)
            try:
                rows = _fetch_yahoo(symbol, period, interval)
                provider = "yahoo"
                print(f"[history] provider=yahoo rows={len(rows)}", flush=True)
            except Exception as exc:
                errors["yahoo"] = str(exc)
                print(f"[history] provider=yahoo error={exc}", flush=True)

            if rows is None:
                print("[history] trying provider=stooq", flush=True)
                try:
                    rows = _fetch_stooq(symbol)
                    provider = "stooq"
                    print(f"[history] provider=stooq rows={len(rows)}", flush=True)
                except Exception as exc:
                    errors["stooq"] = str(exc)
                    print(f"[history] provider=stooq error={exc}", flush=True)

            if rows is None:
                print(f"[history] failed symbol={symbol} errors={errors}", flush=True)
                _send_json(
                    self,
                    502,
                    {
                        "ok": False,
                        "symbol": symbol,
                        "error": "Could not fetch data",
                        "details": errors,
                    },
                )
                return

            print(f"[history] used provider={provider} symbol={symbol} rows={len(rows)}", flush=True)
            _send_json(
                self,
                200,
                {
                    "ok": True,
                    "symbol": symbol,
                    "provider": provider,
                    "rows": rows,
                },
            )
            return

        return super().do_GET()


if __name__ == "__main__":
    print(f"Market Wave Lab server running: http://localhost:{PORT}")
    print("Default history range: max. Examples: AAPL, DIA, SPY, SP500, ^GSPC")
    print("Press Ctrl+C to stop.")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
