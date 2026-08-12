#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DeepSeek 多智能体盘面研判 — 受 TradingAgents / TradingAgents-astock 多分析师辩论模式启发,
原生实现在本平台:把已算好的 A股盘面数据(指标/主力/筹码/同板块/解禁/龙虎榜)喂给 DeepSeek,
多个专职分析师各出观点 → 首席综合研判。OpenAI 兼容接口,零第三方依赖(仅 urllib)。

Key 读取顺序: 环境变量 DEEPSEEK_API_KEY → 文件 ./.deepseek_key
"""
import json, os, urllib.request, urllib.error, time, http.client, socket

HERE   = os.path.dirname(os.path.abspath(__file__))
BASE   = os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com")
MODEL  = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")        # 深度分析(fast/intraday/首席)
SUB_MODEL = os.environ.get("DEEPSEEK_SUB_MODEL", "deepseek-chat") # 多智能体的分析师(求快)

def get_key():
    k = os.environ.get("DEEPSEEK_API_KEY")
    if k: return k.strip()
    p = os.path.join(HERE, ".deepseek_key")
    if os.path.exists(p):
        with open(p) as f: return f.read().strip()
    return None

def _chat(system, user, key, max_tokens=1400, temperature=0.4, model=None):
    mdl = model or MODEL
    reasoner = "reasoner" in mdl
    # ⚠️ reasoner 的 max_tokens 实际上限制"思维链+回答"之和,思维链会吃掉预算导致回答被截断,
    # 所以给足预留(8000),让最终 JSON 回答不被切断。
    mt = max(max_tokens, 8000) if reasoner else max_tokens
    payload = {"model": mdl,
        "messages": [{"role":"system","content":system},{"role":"user","content":user}],
        "max_tokens": mt, "stream": False}
    if not reasoner:                       # reasoner 不支持 temperature,省略
        payload["temperature"] = temperature
    body=json.dumps(payload).encode(); to=240 if reasoner else 90
    last=None
    for attempt in range(3):
        req=urllib.request.Request(BASE.rstrip("/")+"/chat/completions", data=body,
            headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(req, timeout=to) as r:
                data=r.read()
            j=json.loads(data.decode())
            ch=j["choices"][0]; msg=ch["message"]
            txt=msg.get("content") or ""
            if not txt and ch.get("finish_reason")=="length": txt="__TRUNCATED__"
            return txt
        except urllib.error.HTTPError as e:
            if 500<=e.code<600 and attempt<2:      # 5xx 可重试;4xx(如401)直接抛
                last=e; time.sleep(2*(attempt+1)); continue
            raise
        except (http.client.IncompleteRead, urllib.error.URLError, socket.timeout,
                ConnectionError, TimeoutError, json.JSONDecodeError) as e:
            last=e                                  # 网络抖动/连接被切/半包 → 重试
            if attempt<2: time.sleep(2*(attempt+1)); continue
    raise last if last else RuntimeError("DeepSeek 调用失败")

# ---------- 把平台数据压成给模型看的数字摘要 ----------
def digest(d):
    q=d["quote"]; c=d["calc"]; m=d["main"]; ch=d["chips"]; sc=d["score"]
    ma=d["ma"]; strat=d.get("strategies",{})
    last=q["last"]
    ind=(f"现价{last} 涨跌{q['chg_pct']:+.2f}% | MA5 {ma['ma5'][-1]} MA10 {ma['ma10'][-1]} "
         f"MA20 {ma['ma20'][-1]} MA60 {ma['ma60'][-1]} | MACD柱 {d['macd']['hist'][-1]:+.2f} "
         f"DIF {d['macd']['dif'][-1]:+.2f} DEA {d['macd']['dea'][-1]:+.2f} | "
         f"RSI6 {d['rsi']['rsi6'][-1]} RSI12 {d['rsi']['rsi12'][-1]} | "
         f"KDJ K {d['kdj']['k'][-1]} D {d['kdj']['d'][-1]} J {d['kdj']['j'][-1]} | "
         f"BOLL 上{d['boll']['up'][-1]} 中{d['boll']['mid'][-1]} 下{d['boll']['low'][-1]} | "
         f"换手率 {c['turnover_rate']:.2f}% 距高 {q['from_high']:.1f}%")
    money=(f"主力大单净额 {m['net_large']:+.0f}万 | 中单 {m['net_medium']:+.0f}万 | 小单 {m['net_small']:+.0f}万 | "
           f"分时资金净流(尾) {m['flow'][-1][1] if m['flow'] else 0:+.0f}万")
    chips=(f"筹码成本峰 ¥{ch['peak'][0]}(权重{ch['peak'][1]:.1f}%) | 获利比例 {ch['profit_ratio']:.1f}% | "
           f"现价{last}相对成本峰 {(last-ch['peak'][0])/ch['peak'][0]*100:+.0f}%")
    qt=" | ".join(f"{s['name']}:{'持有' if s['signal'] else '空仓'}(回测{s['ret']:+.0f}%/胜率{s['win']:.0f}%)"
                  for s in strat.get("list",[]))
    return {"ind":ind,"money":money,"chips":chips,"quant":qt,
            "score":f"平台量化盘面强弱 {sc['value']}/100",
            "peers":d.get("_peers",""),"supply":d.get("_supply",""),
            "fund":d.get("_fund","") or "无最新财报数据"}

def digest_intraday(d):
    q=d["quote"]; m=d["main"]; intr=d.get("intraday",{}); strat=d.get("strategies",{})
    t=intr.get("t",[]); c=intr.get("c",[]); v=intr.get("v",[])
    o,hi,lo,last=q["open"],q["high"],q["low"],q["last"]
    vwap = q["turnover"]/(q["volume"]*100) if q.get("volume") else last
    pos = (last-lo)/(hi-lo)*100 if hi>lo else 50
    fv=[x[1] for x in m.get("flow",[])]
    fline=(f"分时资金净流(万): 开{fv[0]:.0f} 最低{min(fv):.0f} 最高{max(fv):.0f} 现{fv[-1]:.0f}"
           f"({'全天净流出' if max(fv)<=5 else '全天净流入' if min(fv)>=-5 else '多空拉锯'})") if fv else "分时资金:无"
    recent=""
    for i in range(max(1,len(c)-5),len(c)):
        chg=(c[i]-c[i-1])/c[i-1]*100 if c[i-1] else 0
        recent+=f"{t[i]} {c[i]:.2f}({chg:+.1f}%,量{v[i]:.0f}) "
    qt=" ".join(f"{s['name']}:{'持' if s['signal'] else '空'}" for s in strat.get("list",[]))
    amp=(hi-lo)/(last-q['chg'])*100 if (last-q['chg']) else 0
    return (f"今日 开{o} 高{hi} 低{lo} 现{last} 涨跌{q['chg_pct']:+.2f}% 振幅{amp:.1f}% | "
            f"分时均价(VWAP)≈{vwap:.2f},现价{'在均价上' if last>vwap else '在均价下'}({(last-vwap)/vwap*100:+.1f}%) | "
            f"当前价在日内区间位置{pos:.0f}%(0=最低,100=最高) | 换手{d['calc']['turnover_rate']:.2f}% | "
            f"主力大单{m['net_large']:+.0f}万 中单{m['net_medium']:+.0f}万 小单{m['net_small']:+.0f}万 | {fline} | "
            f"近5根15分钟K线: {recent}| 日线量化信号: {qt} · 平台量化强弱{d['score']['value']}/100")

INTRADAY_SYS = ("你是A股日内量化交易与盯盘专家,做日内量化交易追踪。牢记A股T+1:当日买入不能当日卖出,"
    "做T只能在已有底仓时先卖后买(或先买后补),否则只能规划次日。红涨绿跌(涨=红,跌=绿)。"
    "只基于今日分时/15分钟/分时资金流数据做日内研判,不谈中长期。"
    "输出严格JSON:{\"日内趋势\":\"高开低走/单边下行/震荡整理/探底回升/尾盘跳水 等\",\"信号\":-100到100整数,"
    "\"均价线\":\"现价与分时均价关系及多空含义\",\"关键价位\":\"给出具体数值:日内支撑/压力/均价/前低\","
    "\"盘中资金\":\"分时资金流方向+主力大单是吸筹还是出货\",\"日内量化信号\":\"做T多/做T空/观望/规避 + 明确触发价位\","
    "\"盯盘要点\":\"接下来重点盯什么信号\",\"风险\":\"日内主要风险\"}")

PERSONAS = {
  "技术面": ("你是资深A股技术分析师,只从均线排列/MACD/KDJ/RSI/BOLL/量价关系判断趋势与买卖点,不谈基本面。",
             "技术指标:\n{ind}\n量化策略回测与当前信号:\n{quant}\n{score}\n请给出:趋势判断、关键支撑/压力位、技术面多空结论(1=空 5=多打分)。150字内。"),
  "资金面": ("你是A股资金面/龙虎榜分析师,聚焦主力大单、分时资金流、龙虎榜与游资行为。红涨绿跌。",
             "资金:\n{money}\n龙虎榜/解禁抛压:\n{supply}\n请判断:主力是在吸筹还是出货?资金面多空结论(1-5打分)。150字内。"),
  "筹码面": ("你是筹码分布专家,从成本分布/套牢盘/获利比例/筹码迁移判断压力与见底条件。",
             "筹码:\n{chips}\n请判断:上方套牢压力、下方支撑是否真空、见底所需的筹码条件。筹码面多空结论(1-5打分)。150字内。"),
  "板块面": ("你是A股板块与市场分析师,判断个股走势里有多少是板块/大盘系统性因素。",
             "同板块与指数对比:\n{peers}\n请判断:该股下跌里个股vs板块系统性成分,板块面多空结论(1-5打分)。120字内。"),
  "基本面": ("你是A股基本面分析师,看财报的收入/利润/现金流/毛利/分业务与估值匹配度,识别盘面与基本面背离。",
             "财报:\n{fund}\n请判断:业绩质量、成长vs盈利、估值是否合理、券商预测能否兑现,基本面多空结论(1-5打分)。150字内。"),
}

# ---------- 反思记忆(TradingAgents 式:按实际收益复盘,越用越准) ----------
def _dec_file(): return os.path.join(HERE, "archive", "decisions.jsonl")
def load_last_decision(sym):
    try:
        rows=[json.loads(l) for l in open(_dec_file(),encoding="utf-8") if l.strip()]
        rows=[r for r in rows if r.get("symbol")==sym]
        return rows[-1] if rows else None
    except Exception: return None
def reflection(sym, cur_price):
    last=load_last_decision(sym)
    if not last or not cur_price or not last.get("price"): return ""
    p0=last["price"]; move=(cur_price-p0)/p0*100
    sig=last.get("signal") or 0; rating=last.get("rating","")
    if sig<=-20 and move<=-2: verdict="看空判断正确"
    elif sig>=20 and move>=2: verdict="看多判断正确"
    elif sig<=-20 and move>=3: verdict="看空判断被证伪(低估了反弹)"
    elif sig>=20 and move<=-3: verdict="看多判断被证伪(低估了回调)"
    else: verdict="判断基本符合"
    lesson=("下次勿过度看空、留意反弹" if "低估了反弹" in verdict else
            "下次勿过度看多、留意回调" if "低估了回调" in verdict else "延续既有判断框架")
    return f"上次({last.get('date','')})评级[{rating}]信号{sig}@¥{p0:.2f};至今实际{move:+.1f}% → {verdict}。教训:{lesson}。"
def save_decision(sym, result, price):
    rec={"ts":time.strftime("%Y-%m-%d %H:%M:%S"),"date":time.strftime("%Y-%m-%d"),
         "symbol":sym,"price":price,"rating":result.get("综合评级"),
         "signal":result.get("信号"),"decision":result.get("交易决策")}
    try:
        os.makedirs(os.path.dirname(_dec_file()),exist_ok=True)
        with open(_dec_file(),"a",encoding="utf-8") as f: f.write(json.dumps(rec,ensure_ascii=False)+"\n")
    except Exception: pass

# ---------- 深度决策管线(多空辩论 → 研究经理 → 交易员 → 风控 → 组合经理) ----------
def deep_pipeline(d, key):
    def safe(*a, **k):                     # 子环节失败不拖垮整条管线
        try: return _chat(*a, **k)
        except Exception as e: return f"(该环节获取失败:{e})"
    sym=d.get("_sym",""); cur=d["quote"]["last"]
    dg=digest(d); dg["news"]=d.get("_news","") or "无最新新闻/公告"
    rounds=int(os.environ.get("MAX_DEBATE_ROUNDS","2"))
    refl=reflection(sym, cur)
    base=(f"【数据】技术:{dg['ind']}\n资金:{dg['money']}\n筹码:{dg['chips']}\n"
          f"量化:{dg['quant']} · {dg['score']}\n板块:{dg['peers']}\n解禁抛压:{dg['supply']}\n"
          f"基本面:{dg['fund']}\n消息面:{dg['news']}")
    # A. 多空辩论
    bull=bear=""
    for _ in range(max(1,rounds)):
        bull=safe("你是看多研究员,只找做多理由并反驳对方,遵循红涨绿跌。",
                   f"{base}\n对方(看空)上轮:{bear or '(首轮)'}\n给出本轮看多核心论点,130字内。",
                   key,max_tokens=500,model=SUB_MODEL)
        bear=safe("你是看空研究员,只找做空理由并反驳对方,遵循红涨绿跌。",
                   f"{base}\n对方(看多)上轮:{bull}\n给出本轮看空核心论点,130字内。",
                   key,max_tokens=500,model=SUB_MODEL)
    # 研究经理裁决(reasoner)
    mgr=safe("你是研究经理,中立裁决多空辩论,指出哪方更有说服力及关键分歧。",
              f"{base}\n【看多】{bull}\n【看空】{bear}\n给出裁决与研究结论,160字内。",
              key,max_tokens=900,model=MODEL)
    # C. 交易员 → 风控
    trader=safe("你是A股交易员,严守T+1(当日买入不可当日卖出),红涨绿跌。",
        f"{base}\n研究经理裁决:{mgr}\n过往复盘:{refl or '无'}\n"
        "输出严格JSON:{\"交易决策\":\"买入/卖出/持有/观望\",\"建议仓位\":\"如空仓/3成/半仓/满仓\",\"依据\":\"..\"}",
        key,max_tokens=900,model=SUB_MODEL)
    risk=safe("你是风控官,用距高/日内振幅/解禁抛压/流动性/T+1做风险闸门,可否决或强制降仓。",
        f"{base}\n交易员方案:{trader}\n"
        "输出严格JSON:{\"风控结论\":\"通过/降仓/否决\",\"调整后仓位\":\"..\",\"风险提示\":\"..\"}",
        key,max_tokens=700,model=SUB_MODEL)
    # 组合经理最终拍板(reasoner),吸收复盘教训
    pm_sys=("你是组合经理,综合研究经理裁决+交易员+风控+过往复盘教训做最终决策。"
        "红涨绿跌,只做盘面研判不构成投资建议。输出严格JSON:"
        "{\"综合评级\":\"强空/偏空/中性/偏多/强多\",\"信号\":-100到100整数,"
        "\"复盘\":\"结合过往教训的一句反思\",\"交易决策\":\"买入/卖出/持有/观望\",\"建议仓位\":\"..\","
        "\"核心逻辑\":\"..\",\"风控\":\"..\",\"组合经理结论\":\"..\"}")
    pm_usr=f"研究经理:{mgr}\n交易员:{trader}\n风控:{risk}\n过往复盘教训:{refl or '无'}"
    txt=_chat(pm_sys, pm_usr, key, max_tokens=1600, model=MODEL)
    parsed=_parse_json(txt)
    parsed.setdefault("多方", bull); parsed.setdefault("空方", bear)
    if sym: save_decision(sym, parsed, cur)
    views={"看多研究员":bull,"看空研究员":bear,"研究经理":mgr,
           "交易员":trader,"风控":risk,"新闻面":dg["news"][:220]}
    return parsed, views, refl

def _parse_json(txt):
    """容错解析:去代码围栏 → 直接解析 → 截断修复(补齐引号/括号)。"""
    import re
    s=(txt or "").strip()
    m=re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if m: s=m.group(1).strip()
    i=s.find("{")
    if i<0: return {"综合评级":"解析失败","原文":txt}
    frag=s[i:]
    try: return json.loads(frag)                       # 完整
    except Exception: pass
    j=frag.rfind("}")
    if j>0:
        try: return json.loads(frag[:j+1])             # 掐掉尾部杂讯
        except Exception: pass
    # 截断修复:回退到最后一个完整的 "key":"value" 边界,再补 }
    cut=max(frag.rfind('",'), frag.rfind('"}'))
    if cut>0:
        cand=frag[:cut+1].rstrip().rstrip(",")+"}"
        try: return json.loads(cand)
        except Exception: pass
    return {"综合评级":"解析失败","原文":txt}

def analyze(d, mode="full"):
    key=get_key()
    if not key:
        return {"ok":False,"error":"未配置 DeepSeek Key(设 DEEPSEEK_API_KEY 或写入 .deepseek_key)"}
    try:
        if mode=="deep":
            parsed, views, refl = deep_pipeline(d, key)
            return {"ok":True,"mode":mode,"model":MODEL,"result":parsed,"views":views,
                    "reflection":refl,"ts":time.strftime("%H:%M:%S")}
        if mode=="intraday":
            txt=_chat(INTRADAY_SYS, digest_intraday(d), key, max_tokens=1400, model=MODEL)
            views={}
        elif mode=="fast":
            dg=digest(d)
            sys=("你是顶级A股研判师,融合技术/资金/筹码/板块/基本面五维,遵循红涨绿跌。"
                 "注意基本面与盘面的背离(如利润大跌但估值反升、券商盈利预测落空)。"
                 "输出严格JSON:{\"综合评级\":\"强空/偏空/中性/偏多/强多\",\"信号\":-100到100整数,"
                 "\"技术面\":\"..\",\"资金面\":\"..\",\"筹码面\":\"..\",\"板块面\":\"..\",\"基本面\":\"..\",\"风险\":\"..\",\"操作倾向\":\"..\"}")
            usr=f"{dg['ind']}\n{dg['money']}\n{dg['chips']}\n{dg['quant']}\n{dg['score']}\n同板块:{dg['peers']}\n解禁:{dg['supply']}\n{dg['fund']}"
            txt=_chat(sys,usr,key,max_tokens=1200,temperature=0.3,model=MODEL)
            views={}
        else:
            # 多智能体: 各分析师(求快用SUB_MODEL) → 首席综合(用深度MODEL)
            dg=digest(d); views={}
            for name,(sysp,tmpl) in PERSONAS.items():
                views[name]=_chat(sysp, tmpl.format(**dg), key, max_tokens=500, temperature=0.4, model=SUB_MODEL)
            chief_sys=("你是首席投资决策官,综合下列各分析师观点做最终研判,遵循红涨绿跌,不做投资建议只做盘面研判。"
                "输出严格JSON:{\"综合评级\":\"强空/偏空/中性/偏多/强多\",\"信号\":-100到100整数,"
                "\"核心逻辑\":\"..\",\"多方\":\"..\",\"空方\":\"..\",\"风险\":\"..\",\"操作倾向\":\"..\",\"关注信号\":\"..\"}")
            chief_usr="各分析师观点:\n"+"\n".join(f"【{k}】{v}" for k,v in views.items())
            txt=_chat(chief_sys, chief_usr, key, max_tokens=1400, temperature=0.3, model=MODEL)
        if txt=="__TRUNCATED__":
            return {"ok":False,"error":"DeepSeek 回答被截断(reasoner 思维链过长),请重试"}
        parsed=_parse_json(txt)
        return {"ok":True,"mode":mode,"model":MODEL,"result":parsed,"views":views,
                "ts":time.strftime("%H:%M:%S")}
    except urllib.error.HTTPError as e:
        return {"ok":False,"error":f"DeepSeek API {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"ok":False,"error":str(e)}
