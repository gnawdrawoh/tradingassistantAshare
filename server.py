#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘面 tracker backend — A股/港股/美股 panel analytics
Zero-dependency stdlib HTTP server. Pulls live data from an authenticated
Longbridge CLI on a reachable host (over SSH, set LB_SSH_HOST) and computes:
  - technical indicators (MA / EMA / MACD / RSI / KDJ / BOLL)
  - 主力资金 (大/中/小单净额 + 分时资金流)
  - 筹码分布 (cost / volume-profile with turnover decay)
  - a composite 盘面强弱 score with transparent sub-signals
Run:  python3 server.py [port]
"""
import json, subprocess, time, threading, sys, os, math, hashlib
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------- config ----------------
HERE        = os.path.dirname(os.path.abspath(__file__))
def _load_json(name, default):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f: return json.load(f)
    except Exception: return default
CONFIG      = _load_json("config.json", {})     # 可选;见 config.example.json
SSH_HOST    = os.environ.get("LB_SSH_HOST") or CONFIG.get("ssh_host") or "localhost"
RUN_LOCAL   = SSH_HOST.lower() in ("local","localhost","127.0.0.1","")   # 直接跑 longbridge,不经 SSH
def _cmd(script):
    """在本机(RUN_LOCAL,如部署在装了 Longbridge CLI 的主机上)或经 SSH 到远程主机执行。"""
    return ["bash","-lc",script] if RUN_LOCAL else ["ssh","-o","ConnectTimeout=12",SSH_HOST,script]

# ---------------- 登录口令(可选;公网暴露时启用)----------------
AUTH_PW = os.environ.get("PANMIAN_PASSWORD") or CONFIG.get("password") or ""
_SESS   = hashlib.sha256(("panmian-sess::"+AUTH_PW).encode()).hexdigest() if AUTH_PW else ""
LOGIN_HTML = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
  "<title>盘面 Tracker · 登录</title><style>"
  "body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;"
  "background:radial-gradient(1000px 500px at 50% -10%,#12202e,#0a0e14);font-family:-apple-system,'PingFang SC',sans-serif}"
  ".box{background:rgba(19,26,35,.7);backdrop-filter:blur(14px);border:1px solid rgba(255,255,255,.08);"
  "border-radius:16px;padding:34px 30px;width:300px;text-align:center;color:#e6edf3}"
  ".box h1{font-size:17px;margin:0 0 4px;letter-spacing:1px}.box p{color:#77879a;font-size:12px;margin:0 0 18px}"
  "input{width:100%;box-sizing:border-box;padding:11px 13px;border-radius:10px;border:1px solid rgba(255,255,255,.12);"
  "background:rgba(0,0,0,.25);color:#e6edf3;font-size:14px;margin-bottom:12px;outline:none}"
  "input:focus{border-color:#e8b84b}button{width:100%;padding:11px;border:0;border-radius:10px;cursor:pointer;"
  "background:linear-gradient(135deg,#1f6feb,#3b82f6);color:#fff;font-size:14px;font-weight:600}"
  ".err{color:#ff4d51;font-size:12px;min-height:16px;margin-top:8px}</style>"
  "<form class=box method=post action=/login><h1>盘面 Tracker</h1><p>请输入访问口令</p>"
  "<input type=password name=password autofocus placeholder='口令'>"
  "<button type=submit>进入</button><div class=err>__ERR__</div></form>")
CIRC_SHARES = 100000000          # 流通股 fallback (会被 static 覆盖)
CACHE_TTL   = 15                 # seconds
WL_FILE     = os.path.join(HERE, "watchlist.json")
DEFAULT_WL  = CONFIG.get("watchlist") or [
    {"symbol":"AAPL.US","name":"Apple"},
    {"symbol":"700.HK","name":"Tencent"},
    {"symbol":"600519.SH","name":"Kweichow Moutai"}]

def load_wl():
    try:
        with open(WL_FILE,encoding="utf-8") as f:
            wl=json.load(f)
            return wl if isinstance(wl,list) and wl else list(DEFAULT_WL)
    except Exception: return list(DEFAULT_WL)
def save_wl(wl):
    with open(WL_FILE,"w",encoding="utf-8") as f: json.dump(wl,f,ensure_ascii=False,indent=1)
def default_symbol():
    return os.environ.get("STOCK_SYMBOL") or load_wl()[0]["symbol"]
def wl_name(sym):
    for x in load_wl():
        if x["symbol"]==sym: return x.get("name") or sym
    return sym

_lock   = threading.Lock()
_cache  = {}   # sym -> {ts,data}
_pcache = {}   # sym -> {ts,data}
_scache = {}   # sym -> {ts,data}

# 同板块:可在 peers.json 里给某标的配同业(见 peers.example.json);未配置的只跟大盘指数比
IDX_A  = [("000001.SH","上证指数"),("399006.SZ","创业板指")]
IDX_HK = [("800000.HK","恒生指数")]
IDX_US = [(".IXIC.US","纳斯达克"),(".INX.US","标普500")]
PEER_MAP = {s:[tuple(x) for x in grp] for s,grp in _load_json("peers.json", {}).items()}
def _idx_for(sym):
    return IDX_HK if sym.endswith(".HK") else IDX_US if sym.endswith(".US") else IDX_A
def peers_for(sym):
    grp = PEER_MAP.get(sym) or [(sym, wl_name(sym))]
    return grp + _idx_for(sym)

# ---------------- data fetch ----------------
def _ssh_batch(SYMBOL):
    """One SSH round-trip that emits every feed we need, marker-delimited."""
    cmds = [
        ("QUOTE",  f"longbridge quote {SYMBOL} --format json"),
        ("STATIC", f"longbridge static {SYMBOL} --lang zh-CN --format json"),
        ("CALC",   f"longbridge calc-index {SYMBOL} --format json"),
        ("DAY",    f"longbridge kline {SYMBOL} --period day --count 250 --format json"),
        ("M15",    f"longbridge kline {SYMBOL} --period 15m --count 260 --format json"),
        ("CAP",    f"longbridge capital {SYMBOL} --format json"),
        ("FLOW",   f"longbridge capital {SYMBOL} --flow --format json"),
        ("TS",     f"longbridge intraday {SYMBOL} --format json"),
    ]
    script = "; ".join(f'echo "<<<{k}>>>"; {c}' for k, c in cmds)
    out = subprocess.run(_cmd(script),
                         capture_output=True, text=True, timeout=60).stdout
    parts = {}
    cur = None
    buf = []
    for line in out.splitlines():
        if line.startswith("<<<") and line.endswith(">>>"):
            if cur is not None:
                parts[cur] = "\n".join(buf).strip()
            cur = line[3:-3]; buf = []
        else:
            buf.append(line)
    if cur is not None:
        parts[cur] = "\n".join(buf).strip()
    def pj(k, default):
        try:    return json.loads(parts.get(k, "") or "null") or default
        except Exception: return default
    return {
        "quote":  pj("QUOTE", [{}]),
        "static": pj("STATIC", [{}]),
        "calc":   pj("CALC", [{}]),
        "day":    pj("DAY", []),
        "m15":    pj("M15", []),
        "cap":    pj("CAP", {}),
        "flow":   pj("FLOW", []),
        "ts":     pj("TS", {}),
    }

# ---------------- indicator math ----------------
def _sma(xs, n):
    out = [None]*len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n: s -= xs[i-n]
        if i >= n-1: out[i] = s/n
    return out

def _ema(xs, n):
    out = [None]*len(xs); k = 2/(n+1); prev = None
    for i, x in enumerate(xs):
        prev = x if prev is None else x*k + prev*(1-k)
        out[i] = prev
    return out

def _macd(closes):
    e12, e26 = _ema(closes,12), _ema(closes,26)
    dif = [ (a-b) if a is not None and b is not None else 0.0 for a,b in zip(e12,e26)]
    dea = _ema(dif,9)
    hist= [2*(d-s) for d,s in zip(dif,dea)]
    return dif, dea, hist

def _rsi(closes, n=14):
    out=[None]*len(closes); gain=loss=0.0
    for i in range(1,len(closes)):
        ch=closes[i]-closes[i-1]; g=max(ch,0); l=max(-ch,0)
        if i<=n: gain+=g; loss+=l
        else:    gain=(gain*(n-1)+g)/n; loss=(loss*(n-1)+l)/n
        if i>=n:
            rs = gain/loss if loss else 99
            out[i]=100-100/(1+rs) if i>n else 100-100/(1+(gain/loss if loss else 99))
    return out

def _kdj(h,l,c,n=9):
    K=[None]*len(c); D=[None]*len(c); J=[None]*len(c); pk=pd=50.0
    for i in range(len(c)):
        lo=min(l[max(0,i-n+1):i+1]); hi=max(h[max(0,i-n+1):i+1])
        rsv = (c[i]-lo)/(hi-lo)*100 if hi>lo else 50
        pk = 2/3*pk + 1/3*rsv
        pd = 2/3*pd + 1/3*pk
        K[i],D[i],J[i]=pk,pd,3*pk-2*pd
    return K,D,J

def _boll(closes,n=20,k=2):
    mid=_sma(closes,n); up=[None]*len(closes); lo=[None]*len(closes)
    for i in range(len(closes)):
        if i>=n-1:
            seg=closes[i-n+1:i+1]; m=mid[i]
            sd=math.sqrt(sum((x-m)**2 for x in seg)/n)
            up[i]=m+k*sd; lo[i]=m-k*sd
    return up,mid,lo

# ---------------- 筹码分布 (cost distribution) ----------------
def _chips(day, circ):
    """Triangular cost distribution with turnover decay -> {price: weight}."""
    if not day: return [], 0.0
    step = 0.5
    dist = {}
    for b in day:
        hi=float(b["high"]); lo=float(b["low"]); c=float(b["close"])
        vol=float(b["volume"])*100.0            # 手 -> shares
        if vol<=0 or hi<=0: continue
        t = min(0.9, vol/circ*1.0)              # turnover fraction
        # decay existing chips
        if dist:
            for p in list(dist): dist[p]*=(1-t)
        # today's chips: triangular, peak at avg price
        avg = float(b["turnover"])/vol if vol else c
        lo_b=math.floor(lo/step)*step; hi_b=math.ceil(hi/step)*step
        buckets=[]; wsum=0.0
        p=lo_b
        while p<=hi_b+1e-9:
            # triangular weight peaking at avg
            if avg>lo:
                w = 1-abs(p-avg)/max(avg-lo, hi-avg, step)
            else: w=1.0
            w=max(w,0.05); buckets.append((round(p,1),w)); wsum+=w; p+=step
        for pr,w in buckets:
            dist[pr]=dist.get(pr,0.0)+vol*w/wsum
    tot=sum(dist.values()) or 1.0
    arr=sorted(([p, v/tot*100] for p,v in dist.items()), key=lambda x:x[0])
    # trim negligible tails
    arr=[a for a in arr if a[1]>0.02]
    return arr, tot

# ---------------- 量化策略回测 ----------------
def _bt(closes, pos):
    """position 0/1 per day; pos[i-1] earns return i-1→i (no look-ahead)."""
    eq=1.0; peak=1.0; mdd=0.0; wins=0; held=0; trades=0; prev=0
    for i in range(1,len(closes)):
        p=pos[i-1]
        if p:
            r=closes[i]/closes[i-1]-1; eq*=(1+r); held+=1; wins+=1 if r>0 else 0
        peak=max(peak,eq); mdd=min(mdd,(eq-peak)/peak)
        if p!=prev: trades+=1; prev=p
    total=(eq-1)*100
    bh=(closes[-1]/closes[0]-1)*100 if closes and closes[0] else 0
    return {"ret":round(total,1),"bh":round(bh,1),"excess":round(total-bh,1),
            "win":round(wins/held*100) if held else 0,"trades":trades,
            "mdd":round(mdd*100,1),"signal":pos[-1] if pos else 0}

def _strategies(closes, vols, ma5, ma20, ma60, hist, K, D, bmid, rsi):
    n=len(closes)
    def ok(a,i): return i<len(a) and a[i] is not None
    out=[]
    def add(name,desc,pos):
        r=_bt(closes,pos); r["name"]=name; r["desc"]=desc; out.append(r)
    add("均线金叉","MA5>MA20 持有",[1 if ok(ma5,i) and ok(ma20,i) and ma5[i]>ma20[i] else 0 for i in range(n)])
    add("MACD","DIF>DEA(红柱) 持有",[1 if hist[i]>0 else 0 for i in range(n)])
    add("KDJ金叉","K>D 持有",[1 if ok(K,i) and ok(D,i) and K[i]>D[i] else 0 for i in range(n)])
    add("布林中轨","收盘>中轨 持有",[1 if ok(bmid,i) and closes[i]>bmid[i] else 0 for i in range(n)])
    add("趋势跟踪","MA20>MA60 持有",[1 if ok(ma20,i) and ok(ma60,i) and ma20[i]>ma60[i] else 0 for i in range(n)])
    # RSI 超卖反转 (stateful)
    p=[0]*n; s=0
    for i in range(1,n):
        if ok(rsi,i) and ok(rsi,i-1):
            if rsi[i-1]<30 and rsi[i]>=30: s=1
            elif rsi[i]>70: s=0
        p[i]=s
    add("RSI反转","超卖(<30)转上买入·超买(>70)卖",p)
    # 量价突破 (stateful): 破20日新高+放量 进;跌破MA20 出
    p=[0]*n; s=0
    for i in range(n):
        if i>=20:
            hi20=max(closes[i-20:i]); av=sum(vols[i-20:i])/20 or 1
            if closes[i]>hi20 and vols[i]>1.3*av: s=1
            elif ok(ma20,i) and closes[i]<ma20[i]: s=0
        p[i]=s
    add("量价突破","破20日高+放量 进·破MA20 出",p)
    # 综合共识: 多数策略持有则持有
    cur=sum(1 for s in out if s["signal"]==1); tot=len(out)
    consensus={"hold":cur,"total":tot,"bull":cur>=tot/2}
    return {"list":out,"consensus":consensus}

def _hhmm_bj(iso):
    """'2026-07-21T01:30:00Z' -> '09:30' (UTC+8 北京/香港时区)"""
    try:
        h=int(iso[11:13]); m=iso[14:16]
        return f"{(h+8)%24:02d}:{m}"
    except Exception:
        return iso[11:16] if len(iso)>=16 else str(iso)

# ---------------- 今日分时(含均价线) ----------------
def _timeshare(ts_raw, prev_close):
    """longbridge intraday -> {t,p,avg,v,prev}; 空则返回空结构。"""
    rows = (ts_raw or {}).get("timeshares") if isinstance(ts_raw, dict) else ts_raw
    rows = rows or []
    t=[];p=[];a=[];v=[]
    for x in rows:
        tm=x.get("time") or x.get("timestamp") or ""
        t.append(_hhmm_bj(tm))
        p.append(_f(x.get("price")))
        a.append(_f(x.get("avg_price")))
        v.append(_f(x.get("volume")))
    return {"t":t,"p":p,"avg":a,"v":v,"prev":round(prev_close,3) if prev_close else (p[0] if p else 0)}

# ---------------- assemble ----------------
def _f(x, d=0.0):
    try: return float(x)
    except Exception: return d

def build(SYMBOL=None):
    SYMBOL = SYMBOL or default_symbol()
    raw = _ssh_batch(SYMBOL)
    q   = (raw["quote"] or [{}])[0]
    st  = (raw["static"] or [{}])[0]
    ci  = (raw["calc"] or [{}])[0]
    circ= int(_f(st.get("circ._shares"), CIRC_SHARES)) or CIRC_SHARES
    day = raw["day"] or []
    m15 = raw["m15"] or []

    closes=[_f(b["close"]) for b in day]
    highs =[_f(b["high"])  for b in day]
    lows  =[_f(b["low"])   for b in day]
    vols  =[_f(b["volume"])for b in day]
    times =[b["time"][:10] for b in day]
    turns =[_f(b["turnover"]) for b in day]

    ma = {n:_sma(closes,n) for n in (5,10,20,60,120)}
    dif,dea,hist=_macd(closes)
    rsi6,rsi12=_rsi(closes,6),_rsi(closes,12)
    K,D,J=_kdj(highs,lows,closes)
    bu,bm,bl=_boll(closes)
    turnover_rate=[v*100/circ*100 for v in vols]   # %

    chips, _ = _chips(day, circ)
    price = _f(q.get("last")) or (closes[-1] if closes else 0)
    profit_ratio = sum(w for p,w in chips if p<=price)          # 获利比例 %
    # chip peak (套牢/成本重心)
    peak = max(chips, key=lambda x:x[1]) if chips else [0,0]

    # 主力资金
    cin=raw["cap"].get("capital_in",{}); cout=raw["cap"].get("capital_out",{})
    net_large  = _f(cin.get("large"))  - _f(cout.get("large"))
    net_medium = _f(cin.get("medium")) - _f(cout.get("medium"))
    net_small  = _f(cin.get("small"))  - _f(cout.get("small"))
    net_main   = net_large            # 主力 = 大单
    net_total  = net_large+net_medium+net_small
    flow=[[_hhmm_bj(b["time"]), _f(b["inflow"])] for b in raw["flow"]]

    # ---- 量化盘面评分 ----
    signals=[]; score=50.0
    def sig(name, good, txt, w):
        nonlocal score
        score += w if good else -w
        signals.append({"name":name,"state":"多" if good else "空","txt":txt})
    if len(closes)>60:
        bull = ma[5][-1]>ma[10][-1]>ma[20][-1]  # short alignment
        above60 = price>ma[60][-1]
        sig("趋势", bull and above60, ("均线多头排列" if bull else "均线空头排列")+("·站上MA60" if above60 else "·MA60下方"), 14)
        sig("动量", hist[-1]>0 and hist[-1]>hist[-2], ("MACD红柱" if hist[-1]>0 else "MACD绿柱")+("放大" if hist[-1]>hist[-2] else "缩短"), 10)
        rv=rsi12[-1] or 50
        sig("超买超卖", 30<rv<70, f"RSI12={rv:.0f}"+("·超卖" if rv<=30 else "·超买" if rv>=70 else "·中性"), 6)
        sig("主力资金", net_main>0, f"大单净{'流入' if net_main>0 else '流出'} {abs(net_main):.0f}万", 14)
        # 量能: 今日量比 vs 20日均量
        av=sum(vols[-21:-1])/20 if len(vols)>21 else vols[-1]
        vr=vols[-1]/av if av else 1
        sig("量能", 0.8<vr<2.5 and closes[-1]>closes[-2], f"量比{vr:.2f}"+("·缩量" if vr<0.8 else "·放量" if vr>2.5 else ""), 6)
        # 位置: vs BOLL
        if bl[-1]:
            sig("位置", price>bm[-1], f"{'布林上轨区' if price>bu[-1] else '中轨上方' if price>bm[-1] else '中轨下方' if price>bl[-1] else '跌破下轨'}", 6)
    score=max(0,min(100,round(score)))

    # 距高
    hi_all=max(highs) if highs else price
    from_high=(price-hi_all)/hi_all*100 if hi_all else 0

    payload = {
        "meta":{"symbol":SYMBOL,"name":(st.get("name") or wl_name(SYMBOL)),"circ":circ,
                "currency":st.get("currency","CNY"),
                "updated":time.strftime("%Y-%m-%d %H:%M:%S")},
        "quote":{"last":price,"chg":_f(q.get("change_value")),
                 "chg_pct":_f(q.get("change_percentage")),
                 "open":_f(q.get("open")),"high":_f(q.get("high")),
                 "low":_f(q.get("low")),"volume":_f(q.get("volume")),
                 "turnover":_f(q.get("turnover")),"from_high":from_high},
        "calc":{"pe":_f(ci.get("pe")),"pb":_f(ci.get("pb")),
                "mktcap":_f(ci.get("mktcap")),"turnover_rate":_f(ci.get("turnover_rate"))*100},
        "kline":{"t":times,"o":[_f(b["open"]) for b in day],"h":highs,"l":lows,
                 "c":closes,"v":vols,"turnover_rate":turnover_rate},
        "ma":{f"ma{n}":[round(x,2) if x else None for x in ma[n]] for n in ma},
        "macd":{"dif":[round(x,3) for x in dif],"dea":[round(x,3) for x in dea],
                "hist":[round(x,3) for x in hist]},
        "kdj":{"k":[round(x,1) if x else None for x in K],"d":[round(x,1) if x else None for x in D],
               "j":[round(x,1) if x else None for x in J]},
        "rsi":{"rsi6":[round(x,1) if x else None for x in rsi6],"rsi12":[round(x,1) if x else None for x in rsi12]},
        "boll":{"up":[round(x,2) if x else None for x in bu],"mid":[round(x,2) if x else None for x in bm],
                "low":[round(x,2) if x else None for x in bl]},
        "main":{"net_large":net_large,"net_medium":net_medium,"net_small":net_small,
                "net_main":net_main,"net_total":net_total,"flow":flow},
        "chips":{"dist":chips,"peak":peak,"profit_ratio":profit_ratio},
        "score":{"value":score,"signals":signals},
        "strategies":_strategies(closes,vols,ma[5],ma[20],ma[60],hist,K,D,bm,rsi12),
        "intraday":{"t":[_hhmm_bj(b["time"]) for b in m15],
                    "o":[_f(b["open"]) for b in m15],
                    "h":[_f(b["high"]) for b in m15],
                    "l":[_f(b["low"]) for b in m15],
                    "c":[_f(b["close"]) for b in m15],
                    "v":[_f(b["volume"]) for b in m15],
                    "d":[b["time"][:10] for b in m15]},
        "timeshare":_timeshare(raw["ts"], price-_f(q.get("change_value"))),
    }
    return payload

def get_data(sym=None):
    sym = sym or default_symbol()
    with _lock:
        now=time.time(); e=_cache.get(sym)
        if e and now-e["ts"]<CACHE_TTL: return e["data"], None
        try:
            d=build(sym); _cache[sym]={"ts":now,"data":d}; return d,None
        except Exception as ex:
            if e: return e["data"], f"stale: {ex}"
            return None, str(ex)

# ---------------- 同板块对比 ----------------
def compute_peers(target=None):
    target = target or default_symbol()
    PEERS = peers_for(target)
    syms=[s for s,_ in PEERS]
    script='echo "<<Q>>"; longbridge quote '+" ".join(syms)+" --format json"
    for s in syms: script+=f'; echo "<<K:{s}>>"; longbridge kline {s} --period day --count 20 --format json'
    out=subprocess.run(_cmd(script),
                       capture_output=True,text=True,timeout=60).stdout
    blocks={}; cur=None; buf=[]
    for ln in out.splitlines():
        if ln.startswith("<<") and ln.endswith(">>"):
            if cur is not None: blocks[cur]="\n".join(buf).strip()
            cur=ln[2:-2]; buf=[]
        else: buf.append(ln)
    if cur is not None: blocks[cur]="\n".join(buf).strip()
    def pj(k):
        try: return json.loads(blocks.get(k,"") or "null")
        except Exception: return None
    quotes=pj("Q") or []
    rows=[]
    for i,(sym,name) in enumerate(PEERS):
        q=quotes[i] if i<len(quotes) else {}
        kl=pj(f"K:{sym}") or []
        closes=[_f(b["close"]) for b in kl]; highs=[_f(b["high"]) for b in kl]
        last=_f(q.get("last")) or (closes[-1] if closes else 0)
        chg=_f(q.get("change_percentage"))
        mh=max(highs) if highs else last
        from_high=(last-mh)/mh*100 if mh else 0
        c630=closes[-15] if len(closes)>=15 else (closes[0] if closes else last)
        prev=closes[-2] if len(closes)>=2 else last
        r2w=(prev-c630)/c630*100 if c630 else 0
        rows.append({"sym":sym,"name":name,"last":last,"chg":chg,
                     "from_high":from_high,"r2w":r2w,
                     "is_idx":sym in {s for s,_ in IDX_A+IDX_HK+IDX_US},
                     "self":sym==target})
    return {"rows":rows,"updated":time.strftime("%H:%M:%S")}

def get_peers(sym=None):
    sym = sym or default_symbol()
    with _lock:
        e=_pcache.get(sym)
        if e and time.time()-e["ts"]<300: return e["data"]
    try:
        d=compute_peers(sym)
        with _lock: _pcache[sym]={"ts":time.time(),"data":d}
        return d
    except Exception as ex:
        e=_pcache.get(sym)
        return (e or {}).get("data") or {"rows":[],"error":str(ex)}

# ---------------- 限售/解禁结构 + 龙虎榜 ----------------
def _dragon(code):
    url=("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILS"
         f"&columns=ALL&filter=(SECURITY_CODE=%22{code}%22)&pageNumber=1&pageSize=8"
         "&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB")
    req=urllib.request.Request(url,headers={"Referer":"https://data.eastmoney.com/","User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req,timeout=12) as r:
        j=json.loads(r.read().decode())
    out=[]
    for x in ((j.get("result") or {}).get("data") or []):
        out.append({"date":(x.get("TRADE_DATE") or "")[:10],"reason":x.get("EXPLANATION"),
                    "net_wan":round((x.get("BILLBOARD_NET_AMT") or 0)/1e4),
                    "chg":x.get("CHANGE_RATE")})
    return out

def get_supply(sym=None):
    sym = sym or default_symbol()
    with _lock:
        e=_scache.get(sym)
        if e and time.time()-e["ts"]<3600: return e["data"]
    supply={}
    try:
        with open(os.path.join(HERE,"supply.json"),encoding="utf-8") as f:
            allsup=json.load(f)
        supply = allsup.get(sym, {}) if isinstance(allsup,dict) else {}
    except Exception: pass
    dragon=[]
    if not sym.endswith(".HK"):            # 龙虎榜仅 A 股
        try: dragon=_dragon(sym.split(".")[0])
        except Exception as ex: dragon=[{"error":str(ex)}]
    d={"supply":supply,"dragon":dragon,"updated":time.strftime("%H:%M:%S")}
    with _lock: _scache[sym]={"ts":time.time(),"data":d}
    return d

# ---------------- 新闻/公告(消息面) ----------------
_ncache={}   # sym -> {ts, text}
def _news_text(sym=None):
    sym = sym or default_symbol()
    with _lock:
        e=_ncache.get(sym)
        if e and time.time()-e["ts"]<1800: return e["text"]
    parts=[]
    # 1) Longbridge 新闻标题
    try:
        out=subprocess.run(_cmd(f"longbridge news {sym} --count 8 --format json"),
            capture_output=True,text=True,timeout=40).stdout
        arr=json.loads(out or "[]")
        ts=[f"{x.get('published_at','')[:10]} {x.get('title','')}" for x in arr[:6] if x.get("title")]
        if ts: parts.append("新闻:"+" | ".join(ts))
    except Exception: pass
    # 2) 东财公告(A股)
    if not sym.endswith(".US"):
        try:
            code=sym.split(".")[0]
            url=(f"https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=6&page_index=1"
                 f"&ann_type=A&stock_list={code}")
            req=urllib.request.Request(url,headers={"Referer":"https://data.eastmoney.com/","User-Agent":"Mozilla/5.0"})
            with urllib.request.urlopen(req,timeout=12) as r: j=json.loads(r.read().decode())
            ann=[f"{x.get('notice_date','')[:10]} {x.get('title','')}" for x in ((j.get("data") or {}).get("list") or [])[:5]]
            if ann: parts.append("公告:"+" | ".join(ann))
        except Exception: pass
    text=" || ".join(parts) if parts else ""
    with _lock: _ncache[sym]={"ts":time.time(),"text":text}
    return text

# ---------------- 基本面 ----------------
def get_fundamentals(sym=None):
    sym = sym or default_symbol()
    try:
        with open(os.path.join(HERE,"fundamentals.json"),encoding="utf-8") as f:
            allf=json.load(f)
        return allf.get(sym, {})
    except Exception: return {}

# ---------------- DeepSeek 研判 ----------------
_aicache={}   # mode -> {ts,data}
def _peers_text(sym):
    rows=get_peers(sym).get("rows",[])
    return " | ".join(f"{r['name']} {r['chg']:+.1f}% 距高{r['from_high']:.0f}% 两周半{r['r2w']:.0f}%" for r in rows)
def _supply_text(sym):
    d=get_supply(sym); s=d.get("supply",{}); dr=d.get("dragon",[])
    parts=[s.get("read","")]
    if dr and not dr[0].get("error"):
        parts.append("龙虎榜近上榜:"+"; ".join(f"{x['date']} {x['reason']} 净{x['net_wan']:+d}万" for x in dr[:3]))
    return " ".join(p for p in parts if p)
def _fund_text(sym):
    f=get_fundamentals(sym)
    if not f: return ""
    m=" ".join(f"{x['k']} {x['v']}({x.get('yoy','') and str(x['yoy'])+'%'}{x.get('delta','')})" for x in f.get("metrics",[]))
    seg=" ".join(f"{x['name']} {x['rev']}({x['yoy']:+g}%)" for x in f.get("segments",[]))
    return f"【{f.get('period','')}财报】{m} | 分业务:{seg} | 要点:{f.get('read','')}"
def get_ai(mode, sym=None):
    import deepseek_analyst as da
    sym = sym or default_symbol()
    now=time.time(); ttl=180 if mode=="intraday" else 600
    k=(sym,mode); e=_aicache.get(k)
    if e and now-e["ts"]<ttl: return e["data"]
    d,_=get_data(sym)
    if not d: return {"ok":False,"error":"行情数据不可用"}
    d=dict(d); d["_sym"]=sym; d["_peers"]=_peers_text(sym); d["_supply"]=_supply_text(sym); d["_fund"]=_fund_text(sym)
    d["_news"]=_news_text(sym) if mode=="deep" else ""
    res=da.analyze(d,mode=mode)
    if res.get("ok"): _aicache[k]={"ts":now,"data":res}
    return res

# ---------------- 聊天 (DeepSeek function-calling,连通全部数据/agent) ----------------
def _lb(cmd, timeout=45, limit=2600):
    try:
        out=subprocess.run(_cmd(cmd),capture_output=True,text=True,timeout=timeout).stdout or ""
    except Exception as e: return f"(取数失败:{e})"
    return out.strip()[:limit]

def _tool(name, desc, extra=None):
    props={"symbol":{"type":"string","description":"标的代码如301678.SZ/700.HK/AAPL.US;省略=当前标的"}}
    if extra: props.update(extra)
    return {"type":"function","function":{"name":name,"description":desc,
            "parameters":{"type":"object","properties":props}}}
CHAT_TOOLS=[
 _tool("platform_snapshot","当前/指定标的的实时盘面:现价涨跌、技术指标(MA/MACD/KDJ/RSI/BOLL)、主力大中小单资金、筹码成本分布与获利比例、量化策略信号、平台量化评分、距高。回答技术面/资金面/走势问题时用。"),
 _tool("financials","公司财务:资产负债/利润/现金流关键科目(带行业排名)+ 估值(PE/PB历史分位、行业排名)。回答业绩、财务、估值贵不贵等问题时用。"),
 _tool("business_and_dividend","业务分部收入构成(各板块占比与同比)+ 分红派息历史。回答主营构成、分红问题时用。"),
 _tool("news_and_disclosures","最新新闻 + 公司公告/信息披露(东财)。可选 keyword 做新闻搜索。回答最近有什么消息、公告、事件时用。",
       {"keyword":{"type":"string","description":"可选,新闻关键词搜索"}}),
 _tool("analyst_and_fundamentals","券商评级/目标价/盈利预测 + 平台录入的财报要点。回答机构怎么看、业绩要点时用。"),
 _tool("ai_research","调用平台的多智能体AI研判(融合技术/资金/筹码/板块/基本面),返回综合评级+信号+各维度结论。需要综合判断/买卖倾向时用。"),
]

def run_tool(name, args, cur_sym):
    sym=(args.get("symbol") or cur_sym or default_symbol()).strip().upper()
    try:
        if name=="platform_snapshot":
            d,_=get_data(sym)
            if not d: return "数据不可用"
            dg=deepseek_import().digest(dict(d,_peers="",_supply="",_fund=""))
            return (f"{d['meta']['name']}({sym}) | {dg['ind']} | {dg['money']} | {dg['chips']} | "
                    f"量化:{dg['quant']} | {dg['score']}")
        if name=="financials":
            return f"[财务]{_lb(f'longbridge financial-report {sym} --format json',limit=1800)}\n[估值]{_lb(f'longbridge valuation {sym} --format json',limit=1400)}"
        if name=="business_and_dividend":
            return f"[业务分部]{_lb(f'longbridge business-segments {sym} --format json',limit=1400)}\n[分红]{_lb(f'longbridge dividend {sym} --format json',limit=900)}"
        if name=="news_and_disclosures":
            kw=args.get("keyword")
            if kw: news=_lb(f'longbridge news search "{kw}" --count 6 --format json',limit=1600)
            else:  news=_news_text(sym) or _lb(f'longbridge news {sym} --count 8 --format json',limit=1600)
            return news[:2400]
        if name=="analyst_and_fundamentals":
            return (f"[券商评级]{_lb(f'longbridge institution-rating {sym} --format json',limit=900)}\n"
                    f"[盈利预测]{_lb(f'longbridge forecast-eps {sym} --format json',limit=500)}\n"
                    f"[财报要点]{_fund_text(sym) or '无'}")
        if name=="ai_research":
            r=get_ai("fast", sym)
            return json.dumps(r.get("result",{}),ensure_ascii=False)[:2000] if r.get("ok") else f"AI研判失败:{r.get('error')}"
    except Exception as e:
        return f"(工具 {name} 执行出错:{e})"
    return "未知工具"

def deepseek_import():
    import deepseek_analyst as da; return da

CHAT_SYS=("你是嵌在A股/港股/美股盘面分析平台里的投研助手。用户问什么,先判断需要哪些数据,"
    "用工具去取(盘面/财务/业务分红/新闻公告/券商预测/AI研判),再基于真实数据简洁作答。"
    "遵循红涨绿跌(涨红跌绿)。默认围绕“当前标的”,除非用户指定了别的代码。"
    "有具体数字就引用,不编造;工具没取到就如实说。只做数据解读,不构成投资建议。回答用中文,简明扼要。")

def chat_answer(messages, sym):
    da=deepseek_import(); key=da.get_key()
    if not key: return {"ok":False,"error":"未配置 DeepSeek Key"}
    convo=[{"role":"system","content":CHAT_SYS+f"\n(当前标的:{sym} {wl_name(sym)})"}]+messages
    used=[]
    try:
        for _ in range(6):
            msg=da.api_call(convo, key, tools=CHAT_TOOLS, model="deepseek-chat")
            convo.append(msg)
            tcs=msg.get("tool_calls")
            if not tcs:
                return {"ok":True,"answer":msg.get("content") or "(无回答)","tools":used}
            for tc in tcs:
                fn=tc["function"]["name"]
                try: a=json.loads(tc["function"].get("arguments") or "{}")
                except Exception: a={}
                used.append(fn)
                res=run_tool(fn, a, sym)
                convo.append({"role":"tool","tool_call_id":tc["id"],"content":str(res)[:2800]})
        return {"ok":True,"answer":(convo[-1].get("content") or "(工具调用过多,请重问)"),"tools":used}
    except Exception as e:
        return {"ok":False,"error":str(e)}

# ---------------- http ----------------
class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _send(self,code,body,ctype):
        self.send_response(code); self.send_header("Content-Type",ctype)
        self.send_header("Content-Length",str(len(body)))
        self.send_header("Cache-Control","no-store"); self.end_headers()
        self.wfile.write(body)
    def _sym(self):
        from urllib.parse import urlparse, parse_qs
        q=parse_qs(urlparse(self.path).query)
        return (q.get("symbol",[None])[0] or default_symbol()).strip().upper()
    def _authed(self):
        if not AUTH_PW: return True
        c=self.headers.get("Cookie","") or ""
        return ("panmian_sess="+_SESS) in c.replace(" ","")
    def _login_page(self, err=""):
        html=LOGIN_HTML.replace("__ERR__", err)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
    def do_GET(self):
        p=self.path.split("?")[0]
        if p=="/login":
            return self._login_page()
        if not self._authed():
            self.send_response(302); self.send_header("Location","/login"); self.end_headers(); return
        if p in ("/","/index.html"):
            with open(os.path.join(HERE,"index.html"),"rb") as f: b=f.read()
            return self._send(200,b,"text/html; charset=utf-8")
        if p=="/api/watchlist":
            return self._send(200,json.dumps({"list":load_wl()},ensure_ascii=False).encode("utf-8"),"application/json")
        if p=="/api/data":
            d,err=get_data(self._sym())
            body=json.dumps({"ok":d is not None,"error":err,"data":d}).encode()
            return self._send(200 if d else 503, body,"application/json")
        if p=="/api/peers":
            return self._send(200,json.dumps(get_peers(self._sym()),ensure_ascii=False).encode("utf-8"),"application/json")
        if p=="/api/supply":
            return self._send(200,json.dumps(get_supply(self._sym()),ensure_ascii=False).encode("utf-8"),"application/json")
        if p=="/api/fundamentals":
            return self._send(200,json.dumps(get_fundamentals(self._sym()),ensure_ascii=False).encode("utf-8"),"application/json")
        if p=="/api/alerts":
            fp=os.path.join(HERE,"archive","alerts.jsonl"); rows=[]
            try:
                with open(fp,encoding="utf-8") as f:
                    rows=[json.loads(l) for l in f if l.strip()]
            except Exception: pass
            rows=rows[-40:][::-1]
            return self._send(200,json.dumps({"alerts":rows},ensure_ascii=False).encode("utf-8"),"application/json")
        if p=="/api/quant":
            d,_=get_data(self._sym())
            return self._send(200,json.dumps((d or {}).get("strategies",{}),ensure_ascii=False).encode("utf-8"),"application/json")
        if p=="/api/ai":
            from urllib.parse import urlparse, parse_qs
            mode=(parse_qs(urlparse(self.path).query).get("mode",["full"])[0])
            return self._send(200,json.dumps(get_ai(mode,self._sym()),ensure_ascii=False).encode("utf-8"),"application/json")
        if p=="/echarts.min.js":
            with open(os.path.join(HERE,"echarts.min.js"),"rb") as f: b=f.read()
            return self._send(200,b,"application/javascript")
        self._send(404,b"not found","text/plain")

    def do_POST(self):
        p=self.path.split("?")[0]
        if p=="/login":
            from urllib.parse import parse_qs
            n=int(self.headers.get("Content-Length") or 0)
            body=parse_qs(self.rfile.read(n).decode())
            pw=(body.get("password",[""])[0])
            if AUTH_PW and pw==AUTH_PW:
                self.send_response(302); self.send_header("Location","/")
                self.send_header("Set-Cookie", f"panmian_sess={_SESS}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")
                self.end_headers(); return
            return self._login_page("口令错误")
        if AUTH_PW and not self._authed():
            return self._send(401,json.dumps({"ok":False,"error":"未登录"}).encode(),"application/json")
        if p=="/api/chat":
            try:
                n=int(self.headers.get("Content-Length") or 0)
                req=json.loads(self.rfile.read(n).decode() or "{}")
            except Exception:
                return self._send(400,json.dumps({"ok":False,"error":"bad json"}).encode(),"application/json")
            msgs=req.get("messages") or []
            sym=(req.get("symbol") or default_symbol()).strip().upper()
            res=chat_answer(msgs, sym)
            return self._send(200,json.dumps(res,ensure_ascii=False).encode("utf-8"),"application/json")
        if p!="/api/watchlist": return self._send(404,b"not found","text/plain")
        try:
            n=int(self.headers.get("Content-Length") or 0)
            req=json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return self._send(400,json.dumps({"ok":False,"error":"bad json"}).encode(),"application/json")
        act=req.get("action"); sym=(req.get("symbol") or "").strip().upper()
        wl=load_wl()
        if act=="add" and sym:
            if any(x["symbol"]==sym for x in wl):
                return self._send(200,json.dumps({"ok":True,"list":wl},ensure_ascii=False).encode("utf-8"),"application/json")
            # 校验并取中文名
            try:
                out=subprocess.run(_cmd(f"longbridge static {sym} --lang zh-CN --format json"),
                    capture_output=True,text=True,timeout=40).stdout
                arr=json.loads(out or "[]"); nm=(arr[0].get("name") if arr else None)
            except Exception as ex: nm=None
            if not nm:
                return self._send(200,json.dumps({"ok":False,"error":f"找不到标的 {sym}(格式如 600519.SH / 700.HK / AAPL.US)"},ensure_ascii=False).encode("utf-8"),"application/json")
            wl.append({"symbol":sym,"name":nm}); save_wl(wl)
        elif act=="remove" and sym:
            wl=[x for x in wl if x["symbol"]!=sym]
            if not wl: wl=list(DEFAULT_WL)
            save_wl(wl)
        return self._send(200,json.dumps({"ok":True,"list":wl},ensure_ascii=False).encode("utf-8"),"application/json")

# ---------------- 进程内调度器(替代 cron,绕开 macOS TCC 权限) ----------------
def _scheduler():
    """交易时段:规则告警 5min / AI告警 20min;收盘后存档一次。"""
    last={"rule":0,"ai":0}; archived=None
    time.sleep(20)                                  # 等服务起来
    while True:
        try:
            import watch
            now=time.time(); t=time.localtime()
            if watch.in_window():
                if now-last["rule"]>=300:
                    last["rule"]=now; watch.run(ai_mode=False)
                if now-last["ai"]>=1200:
                    last["ai"]=now; watch.run(ai_mode=True)
            # 收盘后(>=15:05 工作日)当日存档一次
            day=time.strftime("%Y-%m-%d")
            if archived!=day and t.tm_wday<5 and (t.tm_hour*60+t.tm_min)>=15*60+5:
                try:
                    import archive; archive.main(); archived=day
                    print(f"[sched] archived {day}")
                except Exception as e: print("[sched] archive err:",e)
        except Exception as e:
            print("[sched] err:",e)
        time.sleep(30)

if __name__=="__main__":
    port=int(sys.argv[1]) if len(sys.argv)>1 else 8770
    print(f"盘面 tracker → http://localhost:{port}  (default {default_symbol()}, host {SSH_HOST})")
    if os.environ.get("PANMIAN_NO_SCHED")!="1":
        threading.Thread(target=_scheduler,daemon=True).start()
        print("[sched] 进程内调度已启动:规则告警5min · AI告警20min · 收盘存档")
    ThreadingHTTPServer(("127.0.0.1",port),H).serve_forever()
