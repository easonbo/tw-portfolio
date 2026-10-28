#!/usr/bin/env python3
"""
抓取台股每日收盤資料，給持股損益網頁使用。

每個交易日會抓：
  - 上市全部股票/ETF 收盤價 + 發行量加權股價指數（證交所 MI_INDEX）
  - 上櫃全部股票/ETF 收盤價（櫃買中心 每日收盤行情）
  - 櫃買指數（櫃買中心；失敗時改用 Yahoo ^TWOII）

輸出（都在 data/ 底下）：
  data/stocks/<代號>.json   {"c": 代號, "n": 名稱, "m": "TWSE"/"TPEx", "d": {日期: 收盤價}}
  data/index.json           {"日期": {"taiex": 收盤, "otc": 收盤}}
  data/names.json           {代號: [名稱, 市場]}  （最新一天的清單，給網頁自動完成用）
  data/meta.json            {"last_date": ..., "updated_at": ..., "sources": {...}}

用法：
  python scripts/update.py                         # 從上次最後日期補到今天
  python scripts/update.py --start 2026-01-02      # 指定回補起日
  python scripts/update.py --start 2026-01-02 --end 2026-03-31
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STOCKS = DATA / "stocks"
TW = dt.timezone(dt.timedelta(hours=8))

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}
CODE_RE = re.compile(r"^[0-9A-Z]{4,6}$")
SLEEP = 3.5  # 證交所/櫃買有頻率限制，每次請求間隔

session = requests.Session()
session.headers.update(UA)


# ---------------------------------------------------------------- helpers
def log(*a):
    print(*a, flush=True)


def num(s):
    """'1,050.00' -> 1050.0；'--'、''、'X' -> None"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = re.sub(r"<[^>]+>", "", str(s)).replace(",", "").strip()
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def get_json(url, params=None, tries=3):
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 200 and r.text.strip():
                return r.json()
            log(f"    HTTP {r.status_code} {url}")
        except Exception as e:  # noqa: BLE001
            log(f"    error {type(e).__name__}: {e}")
        time.sleep(SLEEP * (i + 1))
    return None


def iter_tables(js):
    """同時支援新版 {"tables":[{title,fields,data}]} 與舊版 fieldsN/dataN 格式。"""
    if not isinstance(js, dict):
        return
    for t in js.get("tables") or []:
        if isinstance(t, dict) and t.get("fields") and t.get("data") is not None:
            yield t.get("title", ""), t["fields"], t["data"]
    for k in list(js.keys()):
        m = re.fullmatch(r"fields(\d*)", k)
        if m:
            data = js.get("data" + m.group(1))
            if data:
                yield js.get("subtitle" + m.group(1), ""), js[k], data
    if js.get("aaData"):
        yield "aaData", None, js["aaData"]


def find_col(fields, *names):
    for i, f in enumerate(fields):
        f2 = re.sub(r"\s", "", str(f))
        if any(n == f2 for n in names):
            return i
    for i, f in enumerate(fields):
        f2 = re.sub(r"\s", "", str(f))
        if any(n in f2 for n in names):
            return i
    return None


def roc(d: dt.date, sep="/"):
    return f"{d.year - 1911}{sep}{d.month:02d}{sep}{d.day:02d}"


# ---------------------------------------------------------------- TWSE
def fetch_twse(d: dt.date):
    """回傳 (stocks{code:(name,close)}, taiex_close)；休市回傳 ({}, None)"""
    ds = d.strftime("%Y%m%d")
    urls = [
        ("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
         {"date": ds, "type": "ALLBUT0999", "response": "json"}),
        ("https://www.twse.com.tw/exchangeReport/MI_INDEX",
         {"date": ds, "type": "ALLBUT0999", "response": "json"}),
    ]
    for url, params in urls:
        js = get_json(url, params)
        time.sleep(SLEEP)
        if not js:
            continue
        stat = str(js.get("stat", "")).upper()
        if stat and stat != "OK":
            # 例如「很抱歉，沒有符合條件的資料!」= 休市
            return {}, None
        stocks, taiex = {}, None
        for title, fields, data in iter_tables(js):
            if not fields:
                continue
            ci = find_col(fields, "證券代號")
            cc = find_col(fields, "收盤價")
            if ci is not None and cc is not None:
                cn = find_col(fields, "證券名稱")
                for row in data:
                    code = str(row[ci]).strip()
                    if not CODE_RE.match(code):
                        continue
                    close = num(row[cc])
                    if close:
                        stocks[code] = (str(row[cn]).strip() if cn is not None else code, close)
                continue
            ic = find_col(fields, "收盤指數")
            if ic is not None:
                for row in data:
                    if str(row[0]).strip() == "發行量加權股價指數":
                        taiex = num(row[ic])
        if stocks or taiex:
            return stocks, taiex
    return None, None  # 抓取失敗（不是休市）


# ---------------------------------------------------------------- TPEx
def fetch_tpex(d: dt.date):
    """回傳 stocks{code:(name,close)}；休市回傳 {}；失敗回傳 None"""
    tries = [
        ("https://www.tpex.org.tw/www/zh-tw/afterTrading/otcQuotes",
         {"date": d.strftime("%Y/%m/%d"), "type": "EW", "response": "json"}),
        ("https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php",
         {"l": "zh-tw", "d": roc(d), "se": "EW"}),
    ]
    got_any_response = False
    for url, params in tries:
        js = get_json(url, params)
        time.sleep(SLEEP)
        if not js:
            continue
        got_any_response = True
        stocks = {}
        for title, fields, data in iter_tables(js):
            if fields:
                ci = find_col(fields, "代號", "證券代號")
                cn = find_col(fields, "名稱", "證券名稱")
                cc = find_col(fields, "收盤", "收盤價")
            else:  # 舊版 aaData：代號, 名稱, 收盤, ...
                ci, cn, cc = 0, 1, 2
            if ci is None or cc is None:
                continue
            for row in data:
                code = str(row[ci]).strip()
                if not CODE_RE.match(code):
                    continue
                close = num(row[cc])
                if close:
                    stocks[code] = (str(row[cn]).strip() if cn is not None else code, close)
        if stocks:
            return stocks
    return {} if got_any_response else None


_otc_month_cache = {}


def fetch_otc_index_official(d: dt.date):
    key = (d.year, d.month)
    if key not in _otc_month_cache:
        rows = {}
        tries = [
            ("https://www.tpex.org.tw/www/zh-tw/indexInfo/inx",
             {"date": d.strftime("%Y/%m/01"), "response": "json"}),
            ("https://www.tpex.org.tw/web/stock/iNdex_info/inxh/Inx_result.php",
             {"l": "zh-tw", "d": f"{d.year - 1911}/{d.month:02d}", "o": "json"}),
        ]
        for url, params in tries:
            js = get_json(url, params)
            time.sleep(SLEEP)
            if not js:
                continue
            for title, fields, data in iter_tables(js):
                cd = find_col(fields, "日期") if fields else 0
                cc = find_col(fields, "收市", "收盤") if fields else 4
                if cd is None or cc is None:
                    continue
                for row in data:
                    day = parse_any_date(str(row[cd]))
                    v = num(row[cc])
                    if day and v:
                        rows[day] = v
            if rows:
                break
        _otc_month_cache[key] = rows
    return _otc_month_cache[key].get(d)


def parse_any_date(s):
    s = re.sub(r"<[^>]+>", "", s).strip()
    m = re.match(r"(\d{2,4})[/\-](\d{1,2})[/\-](\d{1,2})", s)
    if not m:
        m = re.match(r"(\d{4})(\d{2})(\d{2})$", s)
        if not m:
            return None
    y, mo, da = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 1911:
        y += 1911
    try:
        return dt.date(y, mo, da)
    except ValueError:
        return None


_yahoo_cache = {}


def fetch_yahoo(symbol, d: dt.date):
    """Yahoo 日收盤（備援用），一次抓一年快取。"""
    if symbol not in _yahoo_cache:
        out = {}
        js = get_json(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(symbol)}",
            {"range": "2y", "interval": "1d"},
        )
        try:
            res = js["chart"]["result"][0]
            off = res["meta"].get("gmtoffset", 28800)
            closes = res["indicators"]["quote"][0]["close"]
            for ts, c in zip(res["timestamp"], closes):
                if c:
                    day = dt.datetime.fromtimestamp(ts + off, dt.timezone.utc).date()
                    out[day] = round(float(c), 2)
        except Exception as e:  # noqa: BLE001
            log(f"    Yahoo {symbol} 解析失敗: {e}")
        _yahoo_cache[symbol] = out
    return _yahoo_cache[symbol].get(d)


# ---------------------------------------------------------------- storage
def load_json(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def dump_json(p, obj):
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                       encoding="utf-8")


class Store:
    def __init__(self):
        self.index = load_json(DATA / "index.json", {})
        self.meta = load_json(DATA / "meta.json", {})
        self.stocks = {}  # code -> dict（懶載入）
        self.dirty = set()
        self.names = load_json(DATA / "names.json", {})

    def stock(self, code):
        if code not in self.stocks:
            self.stocks[code] = load_json(STOCKS / f"{code}.json", None)
        return self.stocks[code]

    def put_day(self, d: dt.date, market, rows):
        ds = d.isoformat()
        for code, (name, close) in rows.items():
            s = self.stock(code)
            if s is None:
                s = {"c": code, "n": name, "m": market, "d": {}}
                self.stocks[code] = s
            s["n"], s["m"] = name, market
            if s["d"].get(ds) != close:
                s["d"][ds] = close
                self.dirty.add(code)
            self.names[code] = [name, market]

    def save(self):
        for code in self.dirty:
            dump_json(STOCKS / f"{code}.json", self.stocks[code])
        dump_json(DATA / "index.json", self.index)
        dump_json(DATA / "names.json", self.names)
        dump_json(DATA / "meta.json", self.meta)
        log(f"已寫入 {len(self.dirty)} 檔股票資料")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    a = ap.parse_args()

    store = Store()
    today = dt.datetime.now(TW).date()
    end = dt.date.fromisoformat(a.end) if a.end else today
    if a.start:
        start = dt.date.fromisoformat(a.start)
    elif store.meta.get("last_date"):
        start = dt.date.fromisoformat(store.meta["last_date"])  # 重抓最後一天，補遲到的資料
    else:
        start = today - dt.timedelta(days=45)
    start = min(start, end)

    log(f"更新區間 {start} ~ {end}")
    sources = store.meta.get("sources", {})
    failures = []
    d = start
    while d <= end:
        if d.weekday() >= 5:
            d += dt.timedelta(days=1)
            continue
        log(f"[{d}]")
        tw_stocks, taiex = fetch_twse(d)
        if tw_stocks is None:
            failures.append(f"{d} 上市")
            log("  上市：抓取失敗")
            d += dt.timedelta(days=1)
            continue
        if not tw_stocks:
            log("  休市或尚無資料")
            d += dt.timedelta(days=1)
            continue
        otc_stocks = fetch_tpex(d)
        if otc_stocks is None:
            failures.append(f"{d} 上櫃")
            otc_stocks = {}

        if taiex is None:
            taiex = fetch_yahoo("^TWII", d)
            if taiex:
                sources["taiex"] = "yahoo"
        else:
            sources["taiex"] = "twse"
        otc = fetch_otc_index_official(d)
        if otc:
            sources["otc"] = "tpex"
        else:
            otc = fetch_yahoo("^TWOII", d)
            if otc:
                sources["otc"] = "yahoo"

        store.put_day(d, "TWSE", tw_stocks)
        store.put_day(d, "TPEx", otc_stocks)
        rec = store.index.get(d.isoformat(), {})
        if taiex:
            rec["taiex"] = taiex
        if otc:
            rec["otc"] = otc
        store.index[d.isoformat()] = rec
        store.meta["last_date"] = max(store.meta.get("last_date", ""), d.isoformat())
        log(f"  上市 {len(tw_stocks)} 檔、上櫃 {len(otc_stocks)} 檔、加權 {taiex}、櫃買 {otc}")
        d += dt.timedelta(days=1)

    store.meta["updated_at"] = dt.datetime.now(TW).isoformat(timespec="seconds")
    store.meta["sources"] = sources
    store.meta["failures"] = failures[-20:]
    store.save()
    if failures:
        log("以下日期抓取失敗：", ", ".join(failures))
    # 全部失敗才讓 workflow 標紅
    if failures and not store.dirty:
        sys.exit(1)


if __name__ == "__main__":
    main()
