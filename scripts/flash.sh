#!/usr/bin/env bash
# 一键编译并上传固件
# 用法: ./scripts/flash.sh [串口设备]   默认 /dev/cu.wchusbserial1110
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${1:-/dev/cu.wchusbserial1110}"
CFG="arduino-cli.yaml"

echo ">> 编译固件 (arduino:avr:uno)"
arduino-cli --config-file "$CFG" compile --fqbn arduino:avr:uno firmware/sucker

echo ">> 上传到 $PORT"
arduino-cli --config-file "$CFG" upload -p "$PORT" --fqbn arduino:avr:uno firmware/sucker

echo ">> 完成。可执行下面的命令验证固件："
echo "   python3 tools/serial_test.py --seq"
