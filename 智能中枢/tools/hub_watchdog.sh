#!/bin/bash
# 桌宠中枢看门狗：cron 每 2 分钟调用一次，中枢不在线就自动拉起。
#
# 为什么存在：中枢此前跑在 WorkBuddy 会话的后台任务里，会话结束进程就被
# 回收（2026-11-11 实锤：用户早上喊不醒，就是中枢隔夜挂了）。本会话环境
# 无法注册 launchd 服务（bootstrap 报 EIO），cron 由系统 com.apple.cron
# 托管，跨会话、跨重启长期生效。
#
# 卸载方式：crontab -e 删掉 hub_watchdog 那一行即可。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/.data/logs/hub_cron.log"

code=$(/usr/bin/curl -s --noproxy '*' -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:18787/api/v1/health)
if [ "$code" = "200" ]; then
  exit 0
fi

# 中枢不在线：清代理（直连内网，代理会劫持）+ 指定中枢实际使用的 python3
unset http_proxy https_proxy all_proxy ftp_proxy
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY FTP_PROXY
export no_proxy="127.0.0.1,localhost,192.168.0.245,sub2api.vx123.xin"
export NO_PROXY="$no_proxy"
export PATH="/Users/jh-05/.workbuddy/binaries/python/versions/3.13.12/bin:$PATH"

echo "[$(date '+%F %T')] 中枢掉线（health=$code），cron 看门狗拉起" >> "$LOG"
/bin/bash "$ROOT/tools/start_pet_hub.sh" >> "$LOG" 2>&1
