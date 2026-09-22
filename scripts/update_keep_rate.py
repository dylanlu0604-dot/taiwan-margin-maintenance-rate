#!/usr/bin/env python3
"""Update TWSE dashboard 全市場擔保維持率 (keepRate) into its own CSV."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

CSV_PATH = Path(__file__).resolve().parents[1] / "data" / "twse_all_market_keep_rate.csv"
API_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/BFIJ3U_TREND"
FIELD = "TWSEAllMarketKeepRate"


def main() -> None:
    today = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y%m%d")
    resp = requests.get(
        API_URL,
        params={"response": "json", "days": 60, "date": today},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("stat") != "OK":
        raise SystemExit(f"TWSE API stat: {payload.get('stat')}")

    rows: dict[str, str] = {}
    if CSV_PATH.exists():
        with CSV_PATH.open(newline="", encoding="utf-8") as f:
            rows = {r["Date"]: r[FIELD] for r in csv.DictReader(f)}

    for item in payload.get("data") or []:
        if item and item.get("keepRate") is not None:
            d = item["date"]
            rows[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = f"{float(item['keepRate']):.2f}"

    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Date", FIELD])
        for date in sorted(rows):
            writer.writerow([date, rows[date]])
    print(f"Wrote {len(rows)} rows to {CSV_PATH}")


if __name__ == "__main__":
    main()
