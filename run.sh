#!/bin/bash
# 盘面 tracker start/stop helper (robust: kills ALL instances on the port, verifies startup)
cd "$(dirname "$0")"
PORT="${2:-8770}"
# 深度分析(快速/日内/首席)用 reasoner;多智能体的4位分析师用 chat 求快
export DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-deepseek-reasoner}"
export DEEPSEEK_SUB_MODEL="${DEEPSEEK_SUB_MODEL:-deepseek-chat}"
PIDF="/tmp/panmian_$PORT.pid"
LOG="/tmp/panmian_$PORT.log"

port_pids(){ lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null; }

_stop(){
  [ -f "$PIDF" ] && kill "$(cat "$PIDF")" 2>/dev/null
  pkill -f "server.py $PORT" 2>/dev/null
  for p in $(port_pids); do kill "$p" 2>/dev/null; done
  rm -f "$PIDF"
  # 等端口释放(最多5s)
  for i in 1 2 3 4 5 6 7 8 9 10; do [ -z "$(port_pids)" ] && break; sleep 0.5; done
}

case "$1" in
  start)
    if [ -n "$(port_pids)" ]; then echo "already running → http://localhost:$PORT"; exit 0; fi
    nohup python3 -u server.py "$PORT" >"$LOG" 2>&1 &
    echo $! > "$PIDF"; sleep 2
    # 验证真的起来了(轮询端口)
    for i in 1 2 3 4 5 6 7 8; do [ -n "$(port_pids)" ] && break; sleep 0.5; done
    if [ -n "$(port_pids)" ]; then
      echo "started pid $(cat "$PIDF") → http://localhost:$PORT"
    else
      echo "❌ 启动失败,最后日志:"; tail -5 "$LOG"; exit 1
    fi ;;
  stop) _stop; echo "stopped" ;;
  restart) _stop; sleep 1; "$0" start "$PORT" ;;
  status)
    if [ -n "$(port_pids)" ]; then echo "running (pid $(port_pids)) → http://localhost:$PORT"
    else echo "not running"; fi ;;
  *) echo "usage: ./run.sh {start|stop|restart|status} [port]" ;;
esac
