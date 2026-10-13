#!/bin/zsh
set -u

API_LABEL="com.babycare.logistics-tower.api"
WATCHER_LABEL="com.babycare.logistics-tower.watcher"
DOMAIN="gui/$(id -u)"
API_PLIST="$HOME/Library/LaunchAgents/${API_LABEL}.plist"
WATCHER_PLIST="$HOME/Library/LaunchAgents/${WATCHER_LABEL}.plist"
HEALTH_URL="http://127.0.0.1:4174/api/v1/health"
DASHBOARD_URL="http://127.0.0.1:4174/"

is_loaded() {
  launchctl print "$DOMAIN/$1" >/dev/null 2>&1
}

ensure_loaded() {
  local label="$1"
  local plist="$2"
  if is_loaded "$label"; then
    return 0
  fi
  if [[ ! -f "$plist" ]]; then
    print -u2 "找不到服务启动配置：$plist"
    return 1
  fi
  launchctl bootstrap "$DOMAIN" "$plist" || return 1
}

print "正在检查 B2B 物流塔本地服务……"

if ! ensure_loaded "$WATCHER_LABEL" "$WATCHER_PLIST"; then
  print -u2 "文件监控服务无法启动，请检查 LaunchAgents 配置。"
  read -k 1 "?按任意键关闭……"
  exit 1
fi

if ! curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
  if ensure_loaded "$API_LABEL" "$API_PLIST"; then
    if is_loaded "$API_LABEL"; then
      launchctl kickstart -k "$DOMAIN/$API_LABEL" || true
    fi
  else
    print -u2 "API 服务启动配置不存在或无法加载。"
    read -k 1 "?按任意键关闭……"
    exit 1
  fi
fi

for _ in {1..30}; do
  if curl -fsS --max-time 2 "$HEALTH_URL" >/dev/null 2>&1; then
    print "服务已就绪，正在打开驾驶舱。"
    open "$DASHBOARD_URL"
    exit 0
  fi
  sleep 1
done

print -u2 "服务启动后仍未通过健康检查。请检查日志："
print -u2 "${HOME}/Documents/Codex/2026-10-08/qin-2/control-tower/runtime/api.err.log"
read -k 1 "?按任意键关闭……"
exit 1
