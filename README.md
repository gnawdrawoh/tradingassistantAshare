# 盘面 Tracker · 多标的

顶部**自选栏**可切换/添加/移除标的(A股/港股/美股皆可,如 `002475.SZ` `2590.HK` `AAPL.US`)。
自选保存在 `watchlist.json`,当前:新恒汇 301678.SZ · 立讯精密 002475.SZ · 极智嘉-W 2590.HK。
所有面板(K线/筹码/指标/主力/同板块/量化/AI)都跟随当前标的;所有 API 支持 `?symbol=`。
- 同板块对比:`PEER_MAP`(server.py)按标的配同业;未配置的自动只跟大盘指数比(港股用恒生指数)
- 限售/龙虎榜:`supply.json` 按标的存限售结构(目前仅 301678);港股无龙虎榜时该面板自动隐藏
- ⚠️ **信号提醒(watch.py)目前只盯自选第一只**(默认 301678.SZ);**收盘存档已覆盖全部自选**

A local, single-page 盘面 dashboard that tracks capital flow (资金), technical
indicators (技术指标), main-force money (主力), and a quantitative score (量化)
for 新恒汇. Data is pulled live from the authenticated **Longbridge CLI** running
on the coffee EC2 (`temp-01-coffee`) over SSH — no API keys stored locally.

## What it shows
- **Header** — realtime price, 涨跌, 换手率, 量比, 流通市值, 距高, 主力净额, 获利比例
- **K线 / 日内图** — 三种视图切换:
  - **日K**:蜡烛(红涨绿跌) + MA5/10/20/60 + BOLL + 量能 + 缩放
  - **15分K**:真蜡烛(含OHLC) + MA5/MA20 + 量能(日内多日回看)
  - **分时**:今日逐分钟价格线 + **均价线(金)** + 昨收虚线 + 量能(A股经典分时图)
  ⚠️ 所有日内时间轴已转北京时区(接口返回 UTC)
- **筹码分布** — cost/volume-profile (the signature panel): 套牢 peak in red above
  price, 获利 chips in green below, gold 现价 line, 获利比例
- **技术指标** — MACD / KDJ / RSI
- **主力资金** — 大/中/小单净额 + intraday 分时资金流 curve
- **量化盘面强弱** — 0–100 gauge with transparent sub-signals + a plain-language readout
- **同板块 · 封测/引线框架** — 康强/长电/通富/华天/晶方/紫光国微 + 创业板指/科创50 横向对比
  (今日/距高/两周半),新恒汇高亮并标注是否板块跌幅第一 (Longbridge, cached 5min)
- **限售 · 减持结构 · 龙虎榜** — 控股锁定(虞仁荣/任志军至2028)、2026-06-22 解禁逐户
  (武岳峰🐋领衔创投)、真实抛压解读(来自上市公告书 `supply.json`)+ 东财龙虎榜 (cached 1h)

- **量化策略 · 回测** — 7 个经典策略(均线金叉/MACD/KDJ/布林/趋势/RSI反转/量价突破)在本股近250日的
  累计收益·超额·胜率·最大回撤 + 当前信号 + 多数共识 (`/api/quant`,随主数据计算)
- **基本面** — 财报关键指标(营收/净利/扣非/现金流/EPS/ROE/净利率/净资产 + 同比,红涨绿跌)、
  分业务收入增速、要点与总结。数据在 `fundamentals.json`(按标的键;目前 301678 中报);无数据时面板自动隐藏
- **AI 盘面研判 · DeepSeek** — 把已算好的**五维**(技术/资金/筹码/板块/**基本面**)数据喂给 DeepSeek (`/api/ai`,按需触发,10min缓存)。
  基本面接入后能识别"利润跌得比股价快→估值反升""券商盈利锚落空"等**基本面×盘面背离**。
  受 [TradingAgents](https://github.com/TauricResearch/TradingAgents) / [TradingAgents-astock](https://github.com/simonlin1212/TradingAgents-astock) 多分析师辩论模式启发,原生实现:
  - **快速**(1次调用):单模型融合技术/资金/筹码/板块出综合评级+信号
  - **多智能体**(5次调用):技术/资金/筹码/板块 4 位分析师各出观点 → 首席综合(bull/bear)
  - **日内追踪**(1次调用):读今日分时/15分钟/分时资金流,给日内量化交易追踪(日内趋势/关键价位/
    做T信号/盯盘要点,T+1 规则感知)。可勾选 **盘中自动追踪(5m)** 每 5 分钟自动重跑
  - **深度决策**(TradingAgents 式,约1-3分钟):**多空辩论**(看多⇄看空研究员多轮,`MAX_DEBATE_ROUNDS`默认2)
    → **研究经理**裁决 → **交易员**(买卖/仓位)→ **风控**(距高/振幅/解禁/T+1 闸门,可降仓否决)
    → **组合经理**(reasoner 拍板)。含 **消息面**(Longbridge news+东财公告)与 **反思记忆**
    (`archive/decisions.jsonl`:每次决策记录,下次拉实际涨跌自动复盘"上次判断对错+教训"注入本次)
  - **模型**:深度环节用 `deepseek-reasoner`;分析师/辩论/交易员/风控用 `deepseek-chat` 求快。
    `DEEPSEEK_MODEL`/`DEEPSEEK_SUB_MODEL`/`MAX_DEBATE_ROUNDS` 可覆盖(run.sh 已默认)

Refreshes every 20s (核心) / 2min (板块+限售). Backend caches SSH pulls 15s, peers 5min, supply 1h, AI 10min.

## DeepSeek Key 配置(AI 研判需要)
用你自己的 DeepSeek key(OpenAI 兼容,https://platform.deepseek.com)。二选一:
```bash
# 方式一:写入本地文件(已 gitignore,不会外泄)
echo "sk-你的key" > ~/Documents/Claude/9-Stock/.deepseek_key
./run.sh restart
# 方式二:环境变量
DEEPSEEK_API_KEY=sk-... python3 server.py 8770
```
可选:`DEEPSEEK_MODEL=deepseek-reasoner`(更深、更慢)。默认 `deepseek-chat`。
调用会把当前盘面数字摘要发给 DeepSeek(你的账号、你的 key);不含任何账户/持仓隐私。

## 盘中信号提醒(watch.py)
触发即写档 `archive/alerts.jsonl` + 弹 macOS 通知,并在面板顶部「信号提醒」显示当日信号。两类:
- **规则类**(免费,读 `/api/data`):跌破分时均价 / 创日内新低 / 分时资金流转负 / 主力大单转流出 / 临近跌停
- **AI 类**(`--ai`,DeepSeek reasoner 日内):做T多/空信号 / 强信号(|信号|≥70) / 趋势跳水破位

单次执行,默认仅交易时段(`--force` 忽略;`--no-notify` 静默)。已装 cron(工作日交易时段,Mac时区=北京):
```
*/5  9-11,13-15 * * 1-5   python3 watch.py        # 规则类,免费,每5分钟
*/20 9-11,13-15 * * 1-5   python3 watch.py --ai   # AI类,reasoner,每20分钟
```
手动:`python3 watch.py --force --ai`。改频率/关闭:`crontab -e`。规则与AI用独立 state 文件,无竞争。
⚠️ AI 类每次触发一次 reasoner 调用(有成本);嫌贵可调大间隔或去掉 `--ai` 那行。

### 手机推送(Bark / Server酱 / PushDeer / webhook)
信号除了 macOS 通知,还推到手机。配置 `push.json`(已 gitignore,见 `push.example.json`),支持多渠道:
```json
[ { "type": "bark", "key": "xxxx", "sound": "alarm", "level": "timeSensitive" } ]
```
已配置 **Bark**。测试:`python3 watch.py --test-push`。Server酱用 `{"type":"serverchan","key":"SCT.."}`,
PushDeer 用 `{"type":"pushdeer","key":".."}`,自建 `{"type":"webhook","url":".."}`。key 明文存本地、已 gitignore。

## 收盘后自动存档
`archive.py` 抓当日盘面快照(价/资金/筹码/评分/同板块)存 `archive/YYYY-MM-DD.json` + 追加
`archive/_ledger.jsonl` 便于回溯趋势。已装 cron(工作日 15:05 本机时间,Mac 时区=北京):
```
5 15 * * 1-5 cd ~/Documents/Claude/9-Stock && /usr/bin/python3 archive.py >> /tmp/panmian_archive.log 2>&1
```
手动跑:`python3 archive.py`。移除:`crontab -e` 删该行。⚠️ 需 Mac 当时处于开机状态。

## Run
```bash
cd ~/Documents/Claude/9-Stock
./run.sh start      # starts on http://localhost:8770
./run.sh stop
./run.sh restart
python3 server.py 8770   # or run directly (foreground)
```
Then open http://localhost:8770

## Track a different stock
```bash
STOCK_SYMBOL=002119.SZ python3 server.py 8770   # e.g. 康强电子
```
(Edit `STOCK_NAME` in `server.py` for the display name.)

## Requirements
- Python 3 (stdlib only — no pip installs)
- SSH access to `temp-01-coffee` with the Longbridge CLI authenticated there
  (`ssh temp-01-coffee longbridge check`)

## Files
- `server.py` — stdlib HTTP backend: SSH batch fetch + indicator/筹码/主力/score math
- `index.html` — dashboard UI (ECharts, dark A-share terminal theme)
- `echarts.min.js` — vendored charting lib (offline)
- `run.sh` — start/stop helper
