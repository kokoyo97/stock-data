# 向證交所（上市）與櫃買中心（上櫃）官方 OpenAPI 取得盤後收盤價，
# 整理成網頁使用的格式，寫入 data/quotes.json。
import json, os, re, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

URLS = {
    "twse": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
    "twse_date": "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_AVG_ALL",
    "tpex": "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
}
OUT = os.path.join("data", "quotes.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"


def get_json(url, tries=3, timeout=60):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
                "User-Agent": UA,
                "Referer": url.split("/", 3)[0] + "//" + url.split("/", 3)[2] + "/",
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise last


def to_iso(v):
    d = re.sub(r"\D", "", str(v if v is not None else ""))
    if len(d) == 7:
        return f"{int(d[:3]) + 1911}-{d[3:5]}-{d[5:7]}"
    if len(d) == 8:
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return ""


def price(v):
    try:
        n = float(str(v if v is not None else "").replace(",", "").strip())
    except ValueError:
        return None
    return n if n > 0 else None


def max_date(rows):
    return max((to_iso(r.get("Date")) for r in rows), default="")


def days_old(iso):
    if not iso:
        return None
    tw_now = datetime.now(timezone.utc) + timedelta(hours=8)
    d = datetime.strptime(iso, "%Y-%m-%d")
    return (tw_now.replace(tzinfo=None) - d).days


def load_twse():
    all_rows = get_json(URLS["twse"])
    try:
        avg_rows = get_json(URLS["twse_date"])
    except Exception:  # noqa: BLE001
        avg_rows = []
    if not isinstance(all_rows, list) or not all_rows:
        raise RuntimeError("上市資料為空")
    own = max_date(all_rows)
    trade_date = own or max_date(avg_rows if isinstance(avg_rows, list) else [])
    note = ""
    if not own and avg_rows:
        m = {str(r.get("Code", "")).strip(): price(r.get("ClosingPrice")) for r in avg_rows}
        same = total = 0
        for r in all_rows:
            a = m.get(str(r.get("Code", "")).strip())
            b = price(r.get("ClosingPrice"))
            if a and b:
                total += 1
                same += abs(a - b) < 1e-9
        if total >= 50 and same / total < 0.9:
            note = "上市收盤價與日期來源不一致，請核對日期"
    if not trade_date:
        note = note or "上市資料取不到交易日期"
    items = []
    for r in all_rows:
        code, name = str(r.get("Code", "")).strip(), str(r.get("Name", "")).strip()
        if code and name:
            items.append((code, name, price(r.get("ClosingPrice")), to_iso(r.get("Date")) or trade_date))
    return items, trade_date, note


def load_tpex():
    rows = get_json(URLS["tpex"])
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("上櫃資料為空")
    trade_date = max_date(rows)
    items = []
    for r in rows:
        code = str(r.get("SecuritiesCompanyCode", r.get("Code", ""))).strip()
        name = str(r.get("CompanyName", r.get("Name", ""))).strip()
        if code and name:
            items.append((code, name, price(r.get("Close", r.get("ClosingPrice"))), to_iso(r.get("Date")) or trade_date))
    return items, trade_date, "" if trade_date else "上櫃資料取不到交易日期"


def load_previous():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def build(loaders=None):
    loaders = loaders or {"TWSE": load_twse, "TPEx": load_tpex}
    prev = load_previous()
    symbols, markets = {}, {}
    for key, loader in loaders.items():
        label = "TPEX" if key == "TPEx" else "TWSE"
        try:
            items, trade_date, note = loader()
            for code, name, close, d in items:
                symbols[code] = {"symbol": code, "name": name, "market": label, "close": close, "tradeDate": d, "status": "ok"}
            age = days_old(trade_date)
            stale = bool(note) or age is None or age > 7
            markets[key] = {"tradeDate": trade_date, "dataStatus": "stale" if stale else "ok", "fetchStatus": "ok", "error": note or None}
        except Exception as e:  # noqa: BLE001
            # 抓取失敗：沿用上一次的資料，並標示為舊資料
            for code, row in (prev.get("symbols") or {}).items():
                if row.get("market") == label:
                    symbols[code] = row
            old = (prev.get("markets") or {}).get(key, {})
            markets[key] = {"tradeDate": old.get("tradeDate", ""), "dataStatus": "stale", "fetchStatus": "failed", "error": str(e)}
            print(f"[warn] {key} 抓取失敗：{e}", file=sys.stderr)
    if all(m["fetchStatus"] == "failed" for m in markets.values()):
        raise SystemExit("上市與上櫃都抓取失敗，不更新檔案")
    dates = sorted(m["tradeDate"] for m in markets.values() if m["tradeDate"])
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "tradeDate": dates[-1] if dates else "",
        "source": "證交所與櫃買中心官方 OpenAPI（GitHub Actions 定時抓取）",
        "symbols": symbols,
        "markets": markets,
    }


if __name__ == "__main__":
    snap = build()
    os.makedirs("data", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    print(f"完成：{len(snap['symbols'])} 檔；", {k: (v['tradeDate'], v['fetchStatus']) for k, v in snap['markets'].items()})
