#!/usr/bin/env bash
# AI 工作台 · macOS / Linux 启动脚本
# 用法：
#   chmod +x start-workbench.sh
#   ./start-workbench.sh
# 服务仅绑定 127.0.0.1，自动打开浏览器，关闭浏览器标签后 Ctrl+C 退出。

cd "$(dirname "$0")" || exit 1

PYBIN="$(command -v python3 || command -v python)"
if [ -z "$PYBIN" ]; then
  echo "未找到 python3，请先安装 Python 3.11+ 后再运行。"
  exit 1
fi

echo "正在启动 AI 工作台（仅本机访问）..."
exec "$PYBIN" server.py
