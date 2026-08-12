#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收盘后自动存档 — 抓当日盘面快照(价/资金/筹码/评分/同板块)存 JSON。
用法: python3 archive.py   (自带 build()，无需先启动 server)
建议在 A股收盘后(15:05 北京时间)由 cron 触发，见 README。"""
import json, os, time
import server   # reuse the same fetch/compute pipeline

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive")

def snap_one(sym):
    s = {"symbol": sym}
    try:
        d = server.build(sym)
        s["name"]      = d["meta"]["name"]
        s["quote"]     = d["quote"]
        s["calc"]      = d["calc"]
        s["main"]      = {k: d["main"][k] for k in ("net_large","net_medium","net_small","net_total")}
        s["flow_last"] = d["main"]["flow"][-1] if d["main"]["flow"] else None
        s["chips"]     = {"peak": d["chips"]["peak"], "profit_ratio": d["chips"]["profit_ratio"]}
        s["score"]     = d["score"]
        s["ma"]        = {k: v[-1] for k, v in d["ma"].items()}
        s["strategies"]= [{"name":x["name"],"signal":x["signal"]} for x in d.get("strategies",{}).get("list",[])]
    except Exception as e:
        s["error_data"] = str(e)
    try:
        s["peers"] = server.compute_peers(sym)["rows"]
    except Exception as e:
        s["error_peers"] = str(e)
    return s

def main():
    os.makedirs(OUTDIR, exist_ok=True)
    day = time.strftime("%Y-%m-%d")
    wl = server.load_wl()
    snap = {"date": day, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "stocks": {}}
    for item in wl:
        sym = item["symbol"]
        snap["stocks"][sym] = snap_one(sym)

    path = os.path.join(OUTDIR, f"{day}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    # 每只一行,便于回溯趋势
    led = os.path.join(OUTDIR, "_ledger.jsonl")
    with open(led, "a", encoding="utf-8") as f:
        for sym, s in snap["stocks"].items():
            q = s.get("quote", {})
            f.write(json.dumps({"date": day, "symbol": sym, "name": s.get("name"),
                                "last": q.get("last"), "chg_pct": q.get("chg_pct"),
                                "net_main": s.get("main", {}).get("net_large"),
                                "score": s.get("score", {}).get("value"),
                                "profit_ratio": s.get("chips", {}).get("profit_ratio")},
                               ensure_ascii=False) + "\n")
    print(f"archived {len(snap['stocks'])} stocks → {path}")

if __name__ == "__main__":
    main()
