#!/bin/bash
# qq_watchdog.sh — 塔菲 QQ 掉线自动恢复看护（root 执行，由 systemd timer 每 5 分钟调用）
#
# 掉线判定（任一成立即视为掉线）：
#   1) bot ready 非 ready（服务不可用/连接失败）
#   2) NapCat 最近 10 分钟日志出现 KickedOffLine / 账号离线（QQ 被踢但 bot 仍 ready）
# 处置：冷却 10 分钟内不重复重启；若 NapCat 已在等人工扫码（有新鲜二维码），
# 不折腾重启，写入 /tmp/napcat-need-scan 标记等待人工。

LOG=/var/log/qq-watchdog.log
COOLDOWN_FILE=/tmp/napcat-last-restart
NEED_SCAN_FILE=/tmp/napcat-need-scan
COOLDOWN_SECONDS=600

ready=$(curl -s -m 5 http://127.0.0.1:8080/health/ready | grep -c '"status":"ready"')
kicked=$(docker logs napcat --since 10m 2>/dev/null | grep -cE 'KickedOffLine|状态变更为离线')

# 正常：直接退出
if [ "$ready" -eq 1 ] && [ "$kicked" -eq 0 ]; then
    rm -f "$NEED_SCAN_FILE"
    exit 0
fi

# 已在等人工扫码（二维码 5 分钟内刷新过）→ 标记待扫码，不重复重启
if docker exec napcat sh -c 'find /app/napcat/cache/qrcode.png -mmin -5 2>/dev/null | grep -q .' 2>/dev/null; then
    touch "$NEED_SCAN_FILE"
    exit 0
fi

# 冷却
last=$(stat -c %Y "$COOLDOWN_FILE" 2>/dev/null || echo 0)
now=$(date +%s)
if [ $((now - last)) -lt $COOLDOWN_SECONDS ]; then
    exit 0
fi

touch "$COOLDOWN_FILE"
rm -f "$NEED_SCAN_FILE"
echo "$(date '+%F %T') down(ready=$ready kicked=$kicked) -> restarting napcat" >> "$LOG"
docker restart napcat >> "$LOG" 2>&1