#!/usr/bin/env bash
# 首次准备：把 AVR 工具链和 Servo 库装到项目本地目录（不会污染 ~/Library/Arduino15）
set -euo pipefail
cd "$(dirname "$0")/.."

CFG="arduino-cli.yaml"

echo ">> 更新索引"
arduino-cli --config-file "$CFG" core update-index
arduino-cli --config-file "$CFG" lib update-index

echo ">> 安装 arduino:avr 核心（含 avr-gcc / avrdude，约 250MB）"
arduino-cli --config-file "$CFG" core install arduino:avr

echo ">> 安装 Servo 库（AVR 1.8.8 起不再随核心捆绑）"
arduino-cli --config-file "$CFG" lib install Servo

echo ">> 完成。当前串口："
arduino-cli --config-file "$CFG" board list
