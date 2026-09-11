#!/usr/bin/env bash
# 启动浏览器控制服务
# 用法: ./scripts/run.sh [--port /dev/cu.xxx] [--http-port 8787] [--open]
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 bridge/bridge.py "$@"
