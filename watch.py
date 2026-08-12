#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘面信号监控 — 触发即记档(archive/alerts.jsonl)+ macOS 通知。
两类触发:
  规则类(免费,读 /api/data):跌破分时均价 / 创日内新低 / 分时资金流转负 / 主力大单转流出 / 临近跌停
  AI 类(--ai,DeepSeek reasoner 日内):做T信号 / 强信号(|信号|≥70) / 趋势跳水破位
单次执行(适合 cron)。默认仅在 A股交易时段运行;--force 忽略时段;--ai 额外跑 DeepSeek 日内。
用法: python3 watch.py [--ai] [--force]
"""
import json, os, sys, time, subprocess, urllib.request, re

HERE=os.path.dirname(os.path.abspath(__file__))
API=os.environ.get("PANMIAN_API","http://localhost:8770")
ADIR=os.path.join(HERE,"archive"); os.makedirs(ADIR,exist_ok=True)
ALERTS=os.path.join(ADIR,"alerts.jsonl")
STATE_RULE=os.path.join(ADIR,"_alert_state.json"); STATE_AI=os.path.join(ADIR,"_alert_state_ai.json")
NOTIFY = "--no-notify" not in sys.argv

def now_bj(): return time.localtime()   # 假设 Mac 时区=北京
def in_window():
    t=now_bj()
    if t.tm_wday>=5: return False
    hm=t.tm_hour*60+t.tm_min
    return (9*60+30)<=hm<=(11*60+30) or (13*60)<=hm<=(15*60)

def get(path):
    with urllib.request.urlopen(API+path, timeout=250) as r:
        return json.loads(r.read().decode())

def load_state(fp):
    try:
        with open(fp) as f: return json.load(f)
    except Exception: return {}
def save_state(s,fp):
    with open(fp,"w") as f: json.dump(s,f,ensure_ascii=False)

def notify(title,msg):
    if not NOTIFY: return
    try:
        subprocess.run(["osascript","-e",
            f'display notification "{msg}" with title "{title}" sound name "Ping"'],
            timeout=8, capture_output=True)
    except Exception: pass

# ---- 手机推送(Bark / Server酱 / PushDeer / 通用webhook),多渠道 ----
def load_push():
    try:
        with open(os.path.join(HERE,"push.json"),encoding="utf-8") as f:
            c=json.load(f); return c if isinstance(c,list) else [c]
    except Exception: return []

def _get(url,timeout=10):
    try:
        with urllib.request.urlopen(url,timeout=timeout) as r: return r.status<400
    except Exception as e: print("push err:",e); return False
def _post(url,payload,timeout=10):
    try:
        req=urllib.request.Request(url,data=json.dumps(payload).encode(),
            headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req,timeout=timeout) as r: return r.status<400
    except Exception as e: print("push err:",e); return False

def push_mobile(title,body):
    from urllib.parse import quote
    for ch in load_push():
        t=ch.get("type")
        if t=="bark":
            base=ch.get("server","https://api.day.app").rstrip("/")
            url=f"{base}/{ch['key']}/{quote(title)}/{quote(body)}?group={quote('盘面·新恒汇')}"
            if ch.get("sound"): url+=f"&sound={ch['sound']}"
            if ch.get("level"): url+=f"&level={ch['level']}"
            _get(url)
        elif t in ("serverchan","sc"):
            _get(f"https://sctapi.ftqq.com/{ch['key']}.send?title={quote(title)}&desp={quote(body)}")
        elif t=="pushdeer":
            base=ch.get("server","https://api2.pushdeer.com").rstrip("/")
            _get(f"{base}/message/push?pushkey={ch['key']}&text={quote(title)}&desp={quote(body)}&type=text")
        elif t=="webhook":
            _post(ch["url"], {"title":title,"body":body, **ch.get("extra",{})})

def emit(alerts):
    if not alerts: return
    with open(ALERTS,"a",encoding="utf-8") as f:
        for a in alerts: f.write(json.dumps(a,ensure_ascii=False)+"\n")
    for a in alerts:
        title=f"新恒汇 · {a['type']}"
        notify(title, a["msg"])          # macOS
        push_mobile(title, a["msg"])     # 手机

def rule_alerts(d, st, day):
    q=d["quote"]; m=d["main"]; last=q["last"]
    vwap=q["turnover"]/(q["volume"]*100) if q.get("volume") else last
    fv=m["flow"][-1][1] if m.get("flow") else 0
    out=[]; seen=st.get("seen",{})
    def fire(typ,level,msg,key):
        if seen.get(key)==day: return
        seen[key]=day; out.append({"ts":time.strftime("%Y-%m-%d %H:%M:%S"),"date":day,
            "type":typ,"level":level,"msg":msg,"price":last})
    # 跌破分时均价(从上方)
    if st.get("above_vwap") is True and last<vwap:
        fire("跌破均价","warn",f"现价{last:.2f}跌破分时均价{vwap:.2f},日内转弱",f"{day}:vwap")
    st["above_vwap"]=last>=vwap
    # 创日内新低(每个更低的整数档位提醒一次)
    dl=st.get("day_low")
    if dl is None or last<dl-1e-9:
        st["day_low"]=last
        if dl is not None and last<dl:
            fire("创日内新低","warn",f"现价{last:.2f}创日内新低",f"{day}:low:{round(last,1)}")
    # 分时资金流转负
    if st.get("flow_sign")==1 and fv<0:
        fire("资金转流出","warn",f"分时资金由净流入转净流出({fv:.0f}万)",f"{day}:flowneg")
    st["flow_sign"]=1 if fv>=0 else -1
    # 主力大单转流出
    if st.get("large_sign")==1 and m["net_large"]<0:
        fire("主力转流出","strong",f"主力大单由净流入转净流出({m['net_large']:.0f}万)",f"{day}:largeneg")
    st["large_sign"]=1 if m["net_large"]>=0 else -1
    # 临近跌停
    if q["chg_pct"]<=-9.5:
        fire("临近跌停","strong",f"跌{q['chg_pct']:.1f}%,逼近跌停",f"{day}:limitdown")
    st["seen"]=seen
    return out

def ai_alerts(ai, st, day):
    if not ai.get("ok"): return []
    R=ai.get("result",{}); out=[]; seen=st.get("seen",{})
    def fire(typ,level,msg,key):
        if seen.get(key)==day: return
        seen[key]=day; out.append({"ts":time.strftime("%Y-%m-%d %H:%M:%S"),"date":day,
            "type":typ,"level":level,"msg":msg[:140]})
    sig_txt=str(R.get("日内量化信号",""))
    head=re.split(r"[｜|，,。:：\s（(]", sig_txt, 1)[0]   # 首个建议词
    if head.startswith("做T多") or head.startswith("做T空"):
        direc=head[:3]
        fire(f"AI·{direc}","strong",f"DeepSeek日内做T信号:{sig_txt}",f"{day}:doT:{direc}")
    try: sv=int(R.get("信号",0))
    except Exception: sv=0
    if abs(sv)>=70:
        fire("AI·强信号","strong",f"日内信号强度{sv} · {R.get('日内趋势','')}",f"{day}:strong:{'neg' if sv<0 else 'pos'}")
    trend=str(R.get("日内趋势",""))
    for kw in ("跳水","破位","涨停","跌停"):
        if kw in trend: fire(f"AI·{kw}","strong",f"日内趋势:{trend}",f"{day}:trend:{kw}")
    st["seen"]=seen
    return out

def run(ai_mode=False, force=False, notify_on=True):
    """可被 server 进程内调度器直接调用(绕开 cron 的 macOS 权限限制)。"""
    global NOTIFY
    NOTIFY = notify_on
    if not force and not in_window():
        return []
    day=time.strftime("%Y-%m-%d")
    fp=STATE_AI if ai_mode else STATE_RULE
    st=load_state(fp)
    if st.get("day")!=day: st={"day":day}      # 每日重置
    try:
        if ai_mode:
            alerts=ai_alerts(get("/api/ai?mode=intraday"), st, day)
        else:
            d=get("/api/data"); data=d.get("data") if isinstance(d,dict) and "data" in d else d
            alerts=rule_alerts(data, st, day)
    except Exception as e:
        print(("AI" if ai_mode else "行情")+"获取失败:",e); return []
    save_state(st,fp); emit(alerts)
    tag="AI" if ai_mode else "规则"
    print(f"{time.strftime('%H:%M:%S')} [{tag}] 触发 {len(alerts)} 条" + (": "+"; ".join(a['type'] for a in alerts) if alerts else ""))
    return alerts

def main():
    if "--test-push" in sys.argv:
        chans=load_push()
        if not chans: print("未配置 push.json"); return
        print("推送渠道:",[c.get("type") for c in chans])
        push_mobile("盘面·测试推送","手机推送已接通 ✅ 交易时段信号将实时推送")
        notify("盘面·测试推送","手机推送测试已发送"); print("已发送测试推送,请查看手机"); return
    if "--force" not in sys.argv and not in_window():
        print("非交易时段,跳过"); return
    run(ai_mode="--ai" in sys.argv, force=True, notify_on="--no-notify" not in sys.argv)

if __name__=="__main__": main()
