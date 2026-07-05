# -*- coding: utf-8 -*-
"""
食安雷達 GitHub 鏡像抓取腳本
每日由 GitHub Actions 執行：抓食藥署三支開放資料 → 正規化成 JSON → 存進 repo 的 data/。
GAS 端只要抓 raw.githubusercontent.com/<帳號>/<repo>/main/data/{id}.json 即可（GAS 抓 GitHub 必通）。

設計原則：
- 單一來源失敗不中斷其他來源；狀態全部寫進 data/meta.json（誠實記錄）
- 格式自適應：JSON / ZIP（自動解壓，取內部 json 或 csv）/ CSV
- 52 全量歷史可能很大 → 另存 {id}_recent.json（近 RECENT_DAYS 天）；
  GAS 預設抓全量 {id}.json，若超過 GAS 50MB 限制，把 MIRROR 檔名改抓 {id}_recent.json 即可
"""
import csv
import io
import json
import time
import zipfile
import urllib.request
from datetime import datetime, timedelta, timezone

SOURCES = {
    "52": "https://data.fda.gov.tw/data/opendata/export/52/json",
    "5":  "https://data.fda.gov.tw/data/opendata/export/5/json",
    "22": "https://data.fda.gov.tw/data/opendata/export/22/json",
}
RECENT_DAYS = 1095  # recent 檔保留近三年（GAS 端輕量版）
DATE_KEYS = ["發布日期", "行政處分書日期", "處分日期", "裁處日期", "公告日期", "發文日期"]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json,text/csv,application/zip,*/*",
    "Accept-Language": "zh-TW,zh;q=0.9",
    "Referer": "https://data.fda.gov.tw/",
}


def http_get(url, tries=3):
    last = None
    for i in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code < 500:
                break  # 4xx 重試無意義
        except Exception as e:  # 連線層錯誤
            last = str(e)
        time.sleep(3 * i)
    raise RuntimeError(last or "unknown error")


def parse_csv_bytes(b):
    text = b.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2:
        raise RuntimeError("CSV 內容不足（%d 列）" % len(rows))
    headers = [h.strip() for h in rows[0]]
    return [dict((h, (r[i] if i < len(r) else "")) for i, h in enumerate(headers) if h) for r in rows[1:]]


def parse_payload(body):
    """bytes → (list[dict], note)。自動判斷 ZIP / JSON / CSV。"""
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = z.namelist()
            inner = next((n for n in names if n.lower().endswith(".json")), None) or \
                    next((n for n in names if n.lower().endswith(".csv")), None) or names[0]
            data = z.read(inner)
            if inner.lower().endswith(".json"):
                return json.loads(data.decode("utf-8-sig", errors="replace")), "ZIP內%s" % inner
            return parse_csv_bytes(data), "ZIP內%s" % inner
    text = body.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise RuntimeError("端點回應為空（資料集可能暫停維護）")
    if text[0] in "[{":
        data = json.loads(text)
        if not isinstance(data, list):
            raise RuntimeError("JSON 非陣列")
        return data, "JSON"
    return parse_csv_bytes(body), "CSV"


def pick_date(rec):
    for k in DATE_KEYS:
        v = str(rec.get(k, "")).strip()
        if v:
            return v
    return ""


def to_west_date(s):
    """支援 2023/01/03、2023-01-03、民國 112/01/03 → datetime；解析失敗回 None。"""
    import re
    m = re.match(r"^(\d{2,4})[/\-.](\d{1,2})[/\-.](\d{1,2})", s.strip())
    if not m:
        return None
    y = int(m.group(1))
    if y < 1911:
        y += 1911
    try:
        return datetime(y, int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
    except ValueError:
        return None


def main():
    meta = {"mirrored_at": datetime.now(timezone.utc).isoformat(), "sources": {}}
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)

    for sid, url in SOURCES.items():
        entry = {"url": url}
        try:
            status, body = http_get(url)
            if status != 200:
                raise RuntimeError("HTTP %s" % status)
            rows, note = parse_payload(body)
            entry.update(ok=True, count=len(rows), format=note)

            with open("data/%s.json" % sid, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))

            recent = [r for r in rows if (to_west_date(pick_date(r)) or cutoff) >= cutoff]
            with open("data/%s_recent.json" % sid, "w", encoding="utf-8") as f:
                json.dump(recent, f, ensure_ascii=False, separators=(",", ":"))
            entry["recent_count"] = len(recent)
            print("✅ %s：%d 筆（%s），recent %d 筆" % (sid, len(rows), note, len(recent)))
        except Exception as e:
            entry.update(ok=False, error=str(e))
            print("❌ %s：%s" % (sid, e))
            # 失敗時保留舊檔（不覆蓋），GAS 端仍可用上一次成功的鏡像
        meta["sources"][sid] = entry

    with open("data/meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # 至少一支成功就算成功；全失敗讓 Actions 標紅（你會在 GitHub 看到）
    if not any(v.get("ok") for v in meta["sources"].values()):
        raise SystemExit("所有來源皆失敗，詳見上方輸出")


if __name__ == "__main__":
    import os
    os.makedirs("data", exist_ok=True)
    main()
