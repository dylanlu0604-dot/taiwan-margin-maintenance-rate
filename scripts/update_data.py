#!/usr/bin/env python3
"""Update Taiwan margin maintenance data and TWII close prices.

The script is intentionally incremental: by default it fetches the latest
three business days, merges them into the CSV, and only backfills missing
TWII values.
"""

from __future__ import annotations

import argparse
import math
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CSV_PATH = ROOT_DIR / "data" / "margin_maintenance.csv"
DEFAULT_MACROMICRO_XLSX_PATH = ROOT_DIR / "macromicro-old-maintenance-margin-rate.xlsx"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TWSE_BASE_URL = "https://www.twse.com.tw/rwd/zh"
TPEX_BASE_URL = "https://www.tpex.org.tw/www/zh-tw"
YAHOO_CHART_URLS = (
    "https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII",
    "https://query2.finance.yahoo.com/v8/finance/chart/%5ETWII",
)

REQUEST_RETRY_ATTEMPTS = int(os.getenv("REQUEST_RETRY_ATTEMPTS", "3"))
REQUEST_RETRY_SLEEP_SECONDS = float(os.getenv("REQUEST_RETRY_SLEEP_SECONDS", "3"))
DATE_RETRY_ATTEMPTS = int(os.getenv("DATE_RETRY_ATTEMPTS", "2"))
DATE_RETRY_SLEEP_SECONDS = float(os.getenv("DATE_RETRY_SLEEP_SECONDS", "30"))
REQUEST_SLEEP_SECONDS = float(os.getenv("REQUEST_SLEEP_SECONDS", "1.5"))

CSV_COLUMNS = [
    "Date",
    "TWSEMarginMaintenanceRate_IncludeETF",
    "TWSEMarginMaintenanceRate_ExcludeETF",
    "TWSEMarginMarketValueK_IncludeETF",
    "TWSEMarginMarketValueK_ExcludeETF",
    "TWSEETFMarginMarketValueK",
    "TWSETotalMarginAmountK",
    "TPEXMarginMaintenanceRate_IncludeETF",
    "TPEXMarginMaintenanceRate_ExcludeETF",
    "TPEXMarginMarketValueK_IncludeETF",
    "TPEXMarginMarketValueK_ExcludeETF",
    "TPEXETFMarginMarketValueK",
    "TPEXTotalMarginAmountK",
    "CombinedMarginMaintenanceRate_IncludeETF",
    "CombinedMarginMaintenanceRate_ExcludeETF",
    "CombinedMarginMarketValueK_IncludeETF",
    "CombinedMarginMarketValueK_ExcludeETF",
    "CombinedETFMarginMarketValueK",
    "CombinedTotalMarginAmountK",
    "TWIIOpen",
    "TWIIHigh",
    "TWIILow",
    "TWIIClose",
    "TWIIVolume",
    "MacroMicroOldMarginMaintenanceRate",
    "Status",
]


class FetchDataError(RuntimeError):
    pass


class TemporaryFetchError(FetchDataError):
    pass


class DataUnavailableError(FetchDataError):
    pass


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        }
    )
    return session


TWSE_SESSION = make_session()
TPEX_SESSION = make_session()
YAHOO_SESSION = make_session()


def to_number(value: Any) -> float:
    if pd.isna(value):
        return math.nan

    value = str(value).replace(",", "").replace("%", "").strip()
    if value in ("", "--", "-", "---", "nan", "None"):
        return math.nan

    return float(value)


def to_date_key(value: date | datetime | str | pd.Timestamp) -> str:
    return pd.Timestamp(value).strftime("%Y%m%d")


def to_display_date(value: date | datetime | str | pd.Timestamp) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def to_tpex_query_date(date_key: str) -> str:
    return pd.Timestamp(date_key).strftime("%Y/%m/%d")


def parse_twse_roc_date(value: Any) -> str:
    parts = str(value).strip().split("/")
    if len(parts) != 3:
        return to_display_date(value)

    year = int(parts[0])
    if year < 1911:
        year += 1911
    month = int(parts[1])
    day = int(parts[2])
    return date(year, month, day).isoformat()


def normalize_date_text(value: Any) -> str:
    return str(value or "").replace("/", "").replace("-", "").strip()


def compact_text(text: Any, limit: int = 240) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit] + ("..." if len(value) > limit else "")


def summarize_json_payload(data: Any) -> str:
    if not isinstance(data, dict):
        return f"type={type(data).__name__}"

    keys = ",".join(list(data.keys())[:8])
    parts = [f"keys={keys}"]
    for key in ("stat", "date", "title", "subtitle", "msg", "message", "errmsg"):
        if key in data:
            parts.append(f"{key}={compact_text(data.get(key), 80)!r}")
    return "; ".join(parts)


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    label: str,
    retries: int = REQUEST_RETRY_ATTEMPTS,
    retry_sleep_seconds: float = REQUEST_RETRY_SLEEP_SECONDS,
    **kwargs: Any,
) -> dict[str, Any]:
    last_exc: Exception | None = None

    for attempt in range(retries):
        try:
            response = session.request(method, url, **kwargs)
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:
                sample = compact_text(response.text)
                content_type = response.headers.get("Content-Type", "")
                raise TemporaryFetchError(
                    f"{label}: non-JSON response; status={response.status_code}; "
                    f"content_type={content_type}; sample={sample!r}"
                ) from exc
        except TemporaryFetchError as exc:
            last_exc = exc
        except Exception as exc:  # requests wraps many retryable network errors.
            last_exc = TemporaryFetchError(f"{label}: {exc}")

        if attempt == retries - 1:
            break

        sleep_seconds = retry_sleep_seconds * (attempt + 1)
        print(f"{label}: retry {attempt + 2}/{retries} after {sleep_seconds:.1f}s: {last_exc}")
        time.sleep(sleep_seconds)

    assert last_exc is not None
    raise last_exc


def validate_returned_date(data: dict[str, Any], date_key: str, label: str) -> None:
    returned_date = normalize_date_text(data.get("date"))
    if returned_date and returned_date != date_key:
        raise DataUnavailableError(f"{label}: returned date {returned_date}, expected {date_key}")


def require_tables(data: dict[str, Any], minimum_count: int, label: str) -> list[dict[str, Any]]:
    tables = data.get("tables")
    if not tables or len(tables) < minimum_count:
        raise DataUnavailableError(f"{label}: missing tables ({summarize_json_payload(data)})")
    return tables


def require_data_fields(data: dict[str, Any], label: str) -> tuple[list[Any], list[str]]:
    if "data" not in data or "fields" not in data:
        raise DataUnavailableError(f"{label}: missing data/fields ({summarize_json_payload(data)})")
    return data["data"], data["fields"]


def first_existing(fields: list[str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in fields:
            return candidate
    return None


def is_taiwan_etf_code(code: Any) -> bool:
    return str(code).strip().startswith("00")


def etf_mask(frame: pd.DataFrame) -> pd.Series:
    return frame["IsETF"].fillna(False).astype(bool)


def empty_price_frame() -> pd.DataFrame:
    df = pd.DataFrame(columns=["Code", "Name", "ClosingPrice", "IsETF"])
    df["IsETF"] = df["IsETF"].astype(bool)
    return df.set_index(["Code", "Name"])


def parse_twse_price_payload(data: dict[str, Any], label: str) -> pd.DataFrame:
    table_sources: list[dict[str, Any]] = []

    if "data" in data and "fields" in data:
        table_sources.append({"fields": data["fields"], "data": data["data"]})

    table_sources.extend(data.get("tables", []))

    frames: list[pd.DataFrame] = []
    available_fields: list[list[str]] = []
    for table in table_sources:
        fields = table.get("fields", [])
        rows = table.get("data", [])
        available_fields.append(fields)
        if not fields or not rows:
            continue

        code_col = first_existing(fields, ["證券代號", "代號"])
        name_col = first_existing(fields, ["證券名稱", "名稱"])
        close_col = first_existing(fields, ["收盤價", "收盤"])
        if not (code_col and name_col and close_col):
            continue

        df = pd.DataFrame(rows, columns=fields)
        df = df[[code_col, name_col, close_col]].rename(
            columns={code_col: "Code", name_col: "Name", close_col: "ClosingPrice"}
        )
        frames.append(df)

    if not frames:
        raise DataUnavailableError(
            f"{label}: no table with closing price; available fields sample={available_fields[:5]}"
        )

    df = pd.concat(frames, ignore_index=True)
    df["Code"] = df["Code"].astype(str).str.strip()
    df["Name"] = df["Name"].astype(str).str.strip()
    df["ClosingPrice"] = df["ClosingPrice"].map(to_number)
    df["IsETF"] = df["Code"].map(is_taiwan_etf_code)
    df = df.dropna(subset=["ClosingPrice"]).drop_duplicates(subset=["Code"], keep="last")

    if df.empty:
        raise DataUnavailableError(f"{label}: closing price table is empty after numeric cleanup")

    return df.set_index(["Code", "Name"])


def get_twse_total_margin_value(date_key: str) -> tuple[pd.DataFrame, float]:
    label = f"TWSE MI_MARGN total {date_key}"
    data = request_json(
        TWSE_SESSION,
        "GET",
        f"{TWSE_BASE_URL}/marginTrading/MI_MARGN",
        label=label,
        params={"date": date_key, "response": "json"},
        timeout=30,
    )
    validate_returned_date(data, date_key, label)
    tables = require_tables(data, 1, label)

    df = pd.DataFrame(tables[0]["data"], columns=tables[0]["fields"]).set_index("項目")
    total_margin_amount_k = to_number(df.loc["融資金額(仟元)", "今日餘額"])
    return df, total_margin_amount_k


def get_twse_margin_balance(date_key: str) -> pd.DataFrame:
    label = f"TWSE MI_MARGN detail {date_key}"
    data = request_json(
        TWSE_SESSION,
        "GET",
        f"{TWSE_BASE_URL}/marginTrading/MI_MARGN",
        label=label,
        params={"date": date_key, "selectType": "ALL", "response": "json"},
        timeout=30,
    )
    validate_returned_date(data, date_key, label)
    tables = require_tables(data, 2, label)

    df = pd.DataFrame(tables[1]["data"], columns=tables[1]["fields"])
    df = df.iloc[:, :8]
    df = df[["代號", "名稱", "今日餘額"]].rename(
        columns={"代號": "Code", "名稱": "Name", "今日餘額": "MarginLoanShares"}
    )
    df = df[df["Name"] != "合計"]
    df["Code"] = df["Code"].astype(str).str.strip()
    df["Name"] = df["Name"].astype(str).str.strip()
    df["MarginLoanShares"] = df["MarginLoanShares"].map(to_number)
    return df.set_index(["Code", "Name"])


def get_twse_stock_price_from_mi_index(date_key: str) -> pd.DataFrame:
    label = f"TWSE MI_INDEX price {date_key}"
    data = request_json(
        TWSE_SESSION,
        "GET",
        f"{TWSE_BASE_URL}/afterTrading/MI_INDEX",
        label=label,
        params={"date": date_key, "type": "ALLBUT0999", "response": "json"},
        timeout=30,
    )
    validate_returned_date(data, date_key, label)
    return parse_twse_price_payload(data, label)


def get_twse_stock_price_from_stock_day_all(date_key: str) -> pd.DataFrame:
    label = f"TWSE STOCK_DAY_ALL price {date_key}"
    data = request_json(
        TWSE_SESSION,
        "GET",
        f"{TWSE_BASE_URL}/afterTrading/STOCK_DAY_ALL",
        label=label,
        params={"date": date_key, "response": "json"},
        timeout=30,
    )
    validate_returned_date(data, date_key, label)
    return parse_twse_price_payload(data, label)


def get_twse_stock_price(date_key: str) -> pd.DataFrame:
    errors = []
    for fetcher in (get_twse_stock_price_from_mi_index, get_twse_stock_price_from_stock_day_all):
        try:
            return fetcher(date_key)
        except DataUnavailableError as exc:
            errors.append(str(exc))
        except TemporaryFetchError:
            raise
        except Exception as exc:
            errors.append(str(exc))

    raise DataUnavailableError(" | ".join(errors))


def get_twse_etf_price(date_key: str) -> pd.DataFrame:
    label = f"TWSE ETFDaily price {date_key}"
    data = request_json(
        TWSE_SESSION,
        "GET",
        f"{TWSE_BASE_URL}/ETFReport/ETFDaily",
        label=label,
        params={"date": date_key, "response": "json"},
        timeout=30,
    )
    validate_returned_date(data, date_key, label)

    if data.get("stat") != "OK":
        return empty_price_frame()

    table_data, fields = require_data_fields(data, label)
    df = pd.DataFrame(table_data, columns=fields)
    close_col = first_existing(fields, ["收盤價", "收盤"])
    if close_col is None:
        return empty_price_frame()

    df = df[["證券代號", "證券名稱", close_col]].rename(
        columns={"證券代號": "Code", "證券名稱": "Name", close_col: "ClosingPrice"}
    )
    df["Code"] = df["Code"].astype(str).str.strip()
    df["Name"] = df["Name"].astype(str).str.strip()
    df["ClosingPrice"] = df["ClosingPrice"].map(to_number)
    df["IsETF"] = True
    return df.dropna(subset=["ClosingPrice"]).set_index(["Code", "Name"])


def calculate_rate(
    df_margin: pd.DataFrame,
    df_price: pd.DataFrame,
    total_margin_amount_k: float,
    include_etf: bool,
) -> tuple[pd.DataFrame, float, float]:
    margin = df_margin.reset_index()
    price = df_price.reset_index().drop_duplicates(subset=["Code"], keep="last")

    if not include_etf:
        margin = margin[~etf_mask(margin)] if "IsETF" in margin else margin
        price = price[~etf_mask(price)]

    merged = pd.merge(margin, price[["Code", "ClosingPrice", "IsETF"]], on="Code", how="inner")
    merged["MarginLoanShares"] = merged["MarginLoanShares"].map(to_number)
    merged["ClosingPrice"] = merged["ClosingPrice"].map(to_number)
    merged = merged.dropna(subset=["MarginLoanShares", "ClosingPrice"])
    merged["MarginMarketValueK"] = merged["MarginLoanShares"] * merged["ClosingPrice"]
    merged = merged.set_index(["Code", "Name"])

    margin_market_value_k = float(merged["MarginMarketValueK"].sum())
    margin_maintenance_rate = margin_market_value_k / total_margin_amount_k * 100
    return merged, margin_market_value_k, margin_maintenance_rate


@dataclass
class MarketResult:
    total_margin_amount_k: float
    market_value_include_etf_k: float
    market_value_exclude_etf_k: float
    rate_include_etf: float
    rate_exclude_etf: float


def get_twse_margin_maintenance_rate(date_key: str) -> MarketResult:
    _, total_margin_amount_k = get_twse_total_margin_value(date_key)
    time.sleep(REQUEST_SLEEP_SECONDS)
    df_margin = get_twse_margin_balance(date_key)
    time.sleep(REQUEST_SLEEP_SECONDS)
    df_stock_price = get_twse_stock_price(date_key)
    time.sleep(REQUEST_SLEEP_SECONDS)
    df_etf_price = get_twse_etf_price(date_key)

    df_price_include_etf = pd.concat([df_stock_price, df_etf_price]).reset_index()
    df_price_include_etf = (
        df_price_include_etf.sort_values(["Code", "IsETF"])
        .drop_duplicates(subset=["Code"], keep="last")
        .set_index(["Code", "Name"])
        .sort_index()
    )

    _, market_value_include_etf_k, rate_include_etf = calculate_rate(
        df_margin, df_price_include_etf, total_margin_amount_k, include_etf=True
    )
    _, market_value_exclude_etf_k, rate_exclude_etf = calculate_rate(
        df_margin, df_price_include_etf, total_margin_amount_k, include_etf=False
    )

    return MarketResult(
        total_margin_amount_k=total_margin_amount_k,
        market_value_include_etf_k=market_value_include_etf_k,
        market_value_exclude_etf_k=market_value_exclude_etf_k,
        rate_include_etf=rate_include_etf,
        rate_exclude_etf=rate_exclude_etf,
    )


def post_tpex_json(action: str, date_key: str, referer: str) -> dict[str, Any]:
    label = f"TPEX {action} {date_key}"
    data = request_json(
        TPEX_SESSION,
        "POST",
        f"{TPEX_BASE_URL}/{action}",
        label=label,
        data={"date": to_tpex_query_date(date_key), "response": "json"},
        headers={"Referer": referer},
        timeout=30,
    )

    if data.get("stat") not in ("ok", "OK", None):
        raise DataUnavailableError(f"{label}: stat={data.get('stat')!r} ({summarize_json_payload(data)})")

    returned_date = normalize_date_text(data.get("date"))
    if returned_date and returned_date != date_key:
        raise DataUnavailableError(f"{label}: returned date {returned_date}, expected {date_key}")

    return data


def get_tpex_margin_balance(date_key: str) -> tuple[pd.DataFrame, float]:
    label = f"TPEX margin/balance {date_key}"
    data = post_tpex_json(
        "margin/balance",
        date_key,
        "https://www.tpex.org.tw/zh-tw/mainboard/trading/margin-trading/transactions.html",
    )
    table = require_tables(data, 1, label)[0]
    fields = table["fields"]
    if not table.get("data"):
        raise DataUnavailableError(f"{label}: empty table ({summarize_json_payload(data)})")

    df = pd.DataFrame(table["data"], columns=fields).rename(
        columns={"代號": "Code", "名稱": "Name", "資餘額": "MarginLoanShares"}
    )
    df = df[["Code", "Name", "MarginLoanShares"]]
    df["Code"] = df["Code"].astype(str).str.strip()
    df["Name"] = df["Name"].astype(str).str.strip()
    df["MarginLoanShares"] = df["MarginLoanShares"].map(to_number)
    df["IsETF"] = df["Code"].map(is_taiwan_etf_code)
    df = df.set_index(["Code", "Name"])

    margin_amount_row = None
    for row in table.get("summary", []):
        if len(row) > 1 and row[1] == "融資金(仟元)":
            margin_amount_row = row
            break

    if margin_amount_row is None:
        raise DataUnavailableError(f"{label}: no 融資金(仟元) summary row")

    margin_balance_index = fields.index("資餘額")
    total_margin_amount_k = to_number(margin_amount_row[margin_balance_index])
    return df, total_margin_amount_k


def get_tpex_daily_quotes(date_key: str) -> pd.DataFrame:
    label = f"TPEX afterTrading/dailyQuotes {date_key}"
    data = post_tpex_json(
        "afterTrading/dailyQuotes",
        date_key,
        "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/pricing.html",
    )

    frames: list[pd.DataFrame] = []
    available_fields: list[list[str]] = []
    for table in data.get("tables", []):
        if not table.get("data"):
            continue

        fields = table["fields"]
        available_fields.append(fields)
        close_col = first_existing(fields, ["收盤", "收盤價"])
        if "代號" not in fields or close_col is None:
            continue

        frame = pd.DataFrame(table["data"], columns=fields)
        frame = frame[["代號", "名稱", close_col]].rename(
            columns={"代號": "Code", "名稱": "Name", close_col: "ClosingPrice"}
        )
        frames.append(frame)

    if not frames:
        raise DataUnavailableError(
            f"{label}: no table with closing price; available fields sample={available_fields[:5]}"
        )

    df = pd.concat(frames, ignore_index=True)
    df["Code"] = df["Code"].astype(str).str.strip()
    df["Name"] = df["Name"].astype(str).str.strip()
    df["ClosingPrice"] = df["ClosingPrice"].map(to_number)
    df["IsETF"] = df["Code"].map(is_taiwan_etf_code)
    return df.dropna(subset=["ClosingPrice"]).set_index(["Code", "Name"])


def get_tpex_margin_maintenance_rate(date_key: str) -> MarketResult:
    df_margin, total_margin_amount_k = get_tpex_margin_balance(date_key)
    time.sleep(REQUEST_SLEEP_SECONDS)
    df_price = get_tpex_daily_quotes(date_key)

    _, market_value_include_etf_k, rate_include_etf = calculate_rate(
        df_margin, df_price, total_margin_amount_k, include_etf=True
    )
    _, market_value_exclude_etf_k, rate_exclude_etf = calculate_rate(
        df_margin, df_price, total_margin_amount_k, include_etf=False
    )

    return MarketResult(
        total_margin_amount_k=total_margin_amount_k,
        market_value_include_etf_k=market_value_include_etf_k,
        market_value_exclude_etf_k=market_value_exclude_etf_k,
        rate_include_etf=rate_include_etf,
        rate_exclude_etf=rate_exclude_etf,
    )


def build_daily_row(
    display_date: str,
    twse_result: MarketResult | None = None,
    tpex_result: MarketResult | None = None,
    status: str = "ok",
) -> dict[str, Any]:
    row: dict[str, Any] = {column: math.nan for column in CSV_COLUMNS}
    row["Date"] = display_date
    row["Status"] = status

    if twse_result is not None:
        row.update(
            {
                "TWSEMarginMaintenanceRate_IncludeETF": twse_result.rate_include_etf,
                "TWSEMarginMaintenanceRate_ExcludeETF": twse_result.rate_exclude_etf,
                "TWSEMarginMarketValueK_IncludeETF": twse_result.market_value_include_etf_k,
                "TWSEMarginMarketValueK_ExcludeETF": twse_result.market_value_exclude_etf_k,
                "TWSEETFMarginMarketValueK": (
                    twse_result.market_value_include_etf_k - twse_result.market_value_exclude_etf_k
                ),
                "TWSETotalMarginAmountK": twse_result.total_margin_amount_k,
            }
        )

    if tpex_result is not None:
        row.update(
            {
                "TPEXMarginMaintenanceRate_IncludeETF": tpex_result.rate_include_etf,
                "TPEXMarginMaintenanceRate_ExcludeETF": tpex_result.rate_exclude_etf,
                "TPEXMarginMarketValueK_IncludeETF": tpex_result.market_value_include_etf_k,
                "TPEXMarginMarketValueK_ExcludeETF": tpex_result.market_value_exclude_etf_k,
                "TPEXETFMarginMarketValueK": (
                    tpex_result.market_value_include_etf_k - tpex_result.market_value_exclude_etf_k
                ),
                "TPEXTotalMarginAmountK": tpex_result.total_margin_amount_k,
            }
        )

    if twse_result is not None and tpex_result is not None:
        combined_market_value_include_etf_k = (
            twse_result.market_value_include_etf_k + tpex_result.market_value_include_etf_k
        )
        combined_market_value_exclude_etf_k = (
            twse_result.market_value_exclude_etf_k + tpex_result.market_value_exclude_etf_k
        )
        combined_total_margin_amount_k = twse_result.total_margin_amount_k + tpex_result.total_margin_amount_k
        row.update(
            {
                "CombinedMarginMarketValueK_IncludeETF": combined_market_value_include_etf_k,
                "CombinedMarginMarketValueK_ExcludeETF": combined_market_value_exclude_etf_k,
                "CombinedETFMarginMarketValueK": (
                    combined_market_value_include_etf_k - combined_market_value_exclude_etf_k
                ),
                "CombinedTotalMarginAmountK": combined_total_margin_amount_k,
                "CombinedMarginMaintenanceRate_IncludeETF": (
                    combined_market_value_include_etf_k / combined_total_margin_amount_k * 100
                ),
                "CombinedMarginMaintenanceRate_ExcludeETF": (
                    combined_market_value_exclude_etf_k / combined_total_margin_amount_k * 100
                ),
            }
        )

    return row


def fetch_margin_row(fetch_date: pd.Timestamp) -> dict[str, Any]:
    date_key = to_date_key(fetch_date)
    display_date = to_display_date(fetch_date)

    for date_attempt in range(DATE_RETRY_ATTEMPTS):
        try:
            twse_result = get_twse_margin_maintenance_rate(date_key)
            time.sleep(REQUEST_SLEEP_SECONDS)
            tpex_result = get_tpex_margin_maintenance_rate(date_key)
            row = build_daily_row(display_date, twse_result=twse_result, tpex_result=tpex_result, status="ok")
            print(
                f"{display_date}: ok | "
                f"combined ex-ETF {row['CombinedMarginMaintenanceRate_ExcludeETF']:.2f}% | "
                f"combined inc-ETF {row['CombinedMarginMaintenanceRate_IncludeETF']:.2f}%"
            )
            return row
        except DataUnavailableError as exc:
            message = f"data unavailable: {exc}"
            print(f"{display_date}: {message}")
            return build_daily_row(display_date, status=message)
        except Exception as exc:
            if date_attempt == DATE_RETRY_ATTEMPTS - 1:
                message = f"failed: {exc}"
                print(f"{display_date}: {message}")
                return build_daily_row(display_date, status=message)

            sleep_seconds = DATE_RETRY_SLEEP_SECONDS * (date_attempt + 1)
            print(
                f"{display_date}: attempt {date_attempt + 1}/{DATE_RETRY_ATTEMPTS} failed "
                f"({exc}); sleep {sleep_seconds:.1f}s"
            )
            time.sleep(sleep_seconds)

    raise RuntimeError("unreachable")


def fetch_twii_history_from_twse(start_date: str, end_date: str) -> pd.DataFrame:
    start_month = pd.Timestamp(start_date).to_period("M")
    end_month = pd.Timestamp(end_date).to_period("M")
    months = pd.period_range(start=start_month, end=end_month, freq="M")
    frames: list[pd.DataFrame] = []

    for month in months:
        date_param = month.to_timestamp().strftime("%Y%m01")
        label = f"TWSE TAIEX MI_5MINS_HIST {date_param}"
        data = request_json(
            TWSE_SESSION,
            "GET",
            f"{TWSE_BASE_URL}/TAIEX/MI_5MINS_HIST",
            label=label,
            params={"date": date_param, "response": "json"},
            timeout=30,
        )
        if data.get("stat") != "OK":
            print(f"{label}: skipped ({summarize_json_payload(data)})")
            continue

        rows, fields = require_data_fields(data, label)
        if not rows:
            continue

        df = pd.DataFrame(rows, columns=fields)
        required = ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"]
        if not all(column in df.columns for column in required):
            raise DataUnavailableError(f"{label}: missing TAIEX OHLC fields")

        frame = pd.DataFrame(
            {
                "Date": df["日期"].map(parse_twse_roc_date),
                "TWIIOpen": df["開盤指數"].map(to_number),
                "TWIIHigh": df["最高指數"].map(to_number),
                "TWIILow": df["最低指數"].map(to_number),
                "TWIIClose": df["收盤指數"].map(to_number),
                "TWIIVolume": math.nan,
            }
        )
        frames.append(frame)
        time.sleep(min(REQUEST_SLEEP_SECONDS, 0.5))

    if not frames:
        raise DataUnavailableError(f"TWSE TAIEX history {start_date}..{end_date}: empty monthly results")

    result = pd.concat(frames, ignore_index=True)
    result = result[(result["Date"] >= start_date) & (result["Date"] <= end_date)]
    return result.drop_duplicates(subset=["Date"], keep="last")


def fetch_twii_history_from_yahoo(start_date: str, end_date: str) -> pd.DataFrame:
    start = pd.Timestamp(start_date).date() - timedelta(days=7)
    end = pd.Timestamp(end_date).date() + timedelta(days=2)
    period1 = int(datetime.combine(start, datetime.min.time(), TAIPEI_TZ).timestamp())
    period2 = int(datetime.combine(end, datetime.min.time(), TAIPEI_TZ).timestamp())

    last_error: Exception | None = None
    for url in YAHOO_CHART_URLS:
        label = f"Yahoo ^TWII {start_date}..{end_date}"
        try:
            data = request_json(
                YAHOO_SESSION,
                "GET",
                url,
                label=label,
                params={
                    "period1": period1,
                    "period2": period2,
                    "interval": "1d",
                    "events": "history",
                    "includeAdjustedClose": "true",
                },
                timeout=30,
            )
            chart = data.get("chart", {})
            error = chart.get("error")
            if error:
                raise TemporaryFetchError(f"{label}: {error}")

            result = (chart.get("result") or [None])[0]
            if not result:
                raise DataUnavailableError(f"{label}: empty chart result")

            timestamps = result.get("timestamp") or []
            quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
            rows = []
            for idx, timestamp in enumerate(timestamps):
                row_date = datetime.fromtimestamp(timestamp, TAIPEI_TZ).date().isoformat()
                rows.append(
                    {
                        "Date": row_date,
                        "TWIIOpen": quote.get("open", [None] * len(timestamps))[idx],
                        "TWIIHigh": quote.get("high", [None] * len(timestamps))[idx],
                        "TWIILow": quote.get("low", [None] * len(timestamps))[idx],
                        "TWIIClose": quote.get("close", [None] * len(timestamps))[idx],
                        "TWIIVolume": quote.get("volume", [None] * len(timestamps))[idx],
                    }
                )

            frame = pd.DataFrame(rows)
            if frame.empty:
                raise DataUnavailableError(f"{label}: empty parsed rows")

            return frame.drop_duplicates(subset=["Date"], keep="last")
        except Exception as exc:
            last_error = exc
            print(f"{url}: {exc}")

    assert last_error is not None
    raise last_error


def fetch_twii_history(start_date: str, end_date: str) -> pd.DataFrame:
    try:
        return fetch_twii_history_from_twse(start_date, end_date)
    except Exception as exc:
        print(f"TWSE TAIEX history fallback to Yahoo: {exc}")
        return fetch_twii_history_from_yahoo(start_date, end_date)


def load_csv(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        return pd.DataFrame(columns=CSV_COLUMNS)

    frame = pd.read_csv(csv_path, encoding="utf-8-sig")
    if "Date" not in frame.columns and frame.index.name == "Date":
        frame = frame.reset_index()
    if "Unnamed: 0" in frame.columns and "Date" not in frame.columns:
        frame = frame.rename(columns={"Unnamed: 0": "Date"})

    frame["Date"] = pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d")
    for column in CSV_COLUMNS:
        if column not in frame.columns:
            frame[column] = math.nan

    return frame[CSV_COLUMNS]


def is_ok_status(status: Any) -> bool:
    return str(status).strip().lower() == "ok"


def merge_margin_rows(existing: pd.DataFrame, new_rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = existing.copy()
    if frame.empty:
        frame = pd.DataFrame(columns=CSV_COLUMNS)
    frame["Date"] = frame["Date"].astype(str)

    for row in new_rows:
        row_date = str(row["Date"])
        existing_matches = frame.index[frame["Date"] == row_date].tolist()
        row_frame = pd.DataFrame([{column: row.get(column, math.nan) for column in CSV_COLUMNS}])

        if not existing_matches:
            frame = pd.concat([frame, row_frame], ignore_index=True)
            continue

        idx = existing_matches[-1]
        old_status = frame.at[idx, "Status"]
        new_status = row.get("Status")
        should_replace = is_ok_status(new_status) or not is_ok_status(old_status)
        if should_replace:
            replacement = row_frame.iloc[0].copy()
            for column in CSV_COLUMNS:
                if pd.isna(replacement[column]) and column in frame.columns and not pd.isna(frame.at[idx, column]):
                    replacement[column] = frame.at[idx, column]
            frame.loc[idx, CSV_COLUMNS] = replacement

    frame = frame.drop_duplicates(subset=["Date"], keep="last")
    frame = frame.sort_values("Date").reset_index(drop=True)
    return frame[CSV_COLUMNS]


def fill_twii_values(frame: pd.DataFrame, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    if frame.empty:
        return frame

    frame = frame.copy()
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d")
    for column in ("TWIIOpen", "TWIIHigh", "TWIILow", "TWIIClose", "TWIIVolume"):
        if column not in frame.columns:
            frame[column] = math.nan

    missing_twii = frame["TWIIClose"].isna()
    if start_date is not None and end_date is not None:
        in_requested_range = (frame["Date"] >= start_date) & (frame["Date"] <= end_date)
        needs_fetch = missing_twii & in_requested_range
    else:
        needs_fetch = missing_twii

    if not needs_fetch.any():
        return frame

    fetch_start = frame.loc[needs_fetch, "Date"].min()
    fetch_end = max(frame.loc[needs_fetch, "Date"].max(), frame["Date"].max())
    twii = fetch_twii_history(fetch_start, fetch_end).set_index("Date")
    frame = frame.set_index("Date")
    aligned_twii = twii.reindex(frame.index)
    for column in ("TWIIOpen", "TWIIHigh", "TWIILow", "TWIIClose", "TWIIVolume"):
        frame[column] = frame[column].combine_first(aligned_twii[column])

    return frame.reset_index()[CSV_COLUMNS]


def fill_macromicro_old_values(frame: pd.DataFrame, xlsx_path: Path) -> pd.DataFrame:
    if frame.empty or not xlsx_path.exists():
        return frame

    frame = frame.copy()
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d")
    macro = pd.read_excel(xlsx_path)
    required_columns = {"Date", "Value"}
    if not required_columns.issubset(macro.columns):
        raise DataUnavailableError(f"{xlsx_path}: expected columns Date and Value")

    macro = macro[["Date", "Value"]].copy()
    macro["Date"] = pd.to_datetime(macro["Date"]).dt.strftime("%Y-%m-%d")
    macro["MacroMicroOldMarginMaintenanceRate"] = pd.to_numeric(macro["Value"], errors="coerce")
    macro = macro.drop(columns=["Value"]).dropna(subset=["MacroMicroOldMarginMaintenanceRate"])
    macro = macro.drop_duplicates(subset=["Date"], keep="last")

    frame = frame.drop(columns=["MacroMicroOldMarginMaintenanceRate"], errors="ignore")
    merged = pd.merge(frame, macro, on="Date", how="left")
    print(
        "MacroMicro old rows filled: "
        f"{int(merged['MacroMicroOldMarginMaintenanceRate'].notna().sum())}"
    )
    return merged[CSV_COLUMNS]


def write_csv(frame: pd.DataFrame, csv_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    frame = frame.copy()
    frame["Date"] = pd.to_datetime(frame["Date"]).dt.strftime("%Y-%m-%d")

    numeric_columns = [column for column in CSV_COLUMNS if column not in ("Date", "Status")]
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").round(6)

    frame[CSV_COLUMNS].to_csv(csv_path, index=False, encoding="utf-8")


def build_fetch_dates(args: argparse.Namespace) -> list[pd.Timestamp]:
    if args.start_date or args.end_date:
        if not (args.start_date and args.end_date):
            raise ValueError("--start-date and --end-date must be provided together")
        return list(pd.bdate_range(start=args.start_date, end=args.end_date))

    if args.today:
        today = pd.Timestamp(args.today)
    else:
        today = pd.Timestamp(datetime.now(TAIPEI_TZ).date())

    return list(pd.bdate_range(end=today, periods=args.days))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update margin maintenance rate CSV.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH, help="CSV path to read and update.")
    parser.add_argument("--days", type=int, default=3, help="Recent business days to fetch.")
    parser.add_argument("--start-date", help="Inclusive fetch start date, YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Inclusive fetch end date, YYYY-MM-DD.")
    parser.add_argument("--today", help="Override today's date, YYYY-MM-DD, for scheduled backfills/tests.")
    parser.add_argument("--skip-margin", action="store_true", help="Only fill missing TWII columns.")
    parser.add_argument("--skip-twii", action="store_true", help="Only update margin columns.")
    parser.add_argument(
        "--macromicro-xlsx",
        type=Path,
        default=DEFAULT_MACROMICRO_XLSX_PATH,
        help="Optional MacroMicro old xlsx source with Date and Value columns.",
    )
    parser.add_argument("--skip-macromicro", action="store_true", help="Do not refresh MacroMicro old values.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    existing = load_csv(args.csv)
    fetch_dates = build_fetch_dates(args)
    start_date = to_display_date(fetch_dates[0]) if fetch_dates else None
    end_date = to_display_date(fetch_dates[-1]) if fetch_dates else None

    print(f"CSV: {args.csv}")
    print(f"Fetch dates: {', '.join(to_display_date(item) for item in fetch_dates)}")

    updated = existing
    if not args.skip_margin:
        rows = []
        for fetch_date in fetch_dates:
            rows.append(fetch_margin_row(fetch_date))
            time.sleep(REQUEST_SLEEP_SECONDS)
        updated = merge_margin_rows(existing, rows)

    if not args.skip_twii:
        updated = fill_twii_values(updated, start_date=start_date, end_date=end_date)
        if updated["TWIIClose"].isna().any():
            updated = fill_twii_values(updated)

    if not args.skip_macromicro:
        updated = fill_macromicro_old_values(updated, args.macromicro_xlsx)

    write_csv(updated, args.csv)
    ok_rows = int((updated["Status"].astype(str).str.lower() == "ok").sum())
    latest_ok = updated.loc[updated["Status"].astype(str).str.lower() == "ok", "Date"].max()
    print(f"Rows: {len(updated)}; ok rows: {ok_rows}; latest ok date: {latest_ok}")


if __name__ == "__main__":
    main()
