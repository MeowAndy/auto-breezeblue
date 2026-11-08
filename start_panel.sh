#!/usr/bin/env bash
# BreezeBlue 签到面板启动脚本 (Linux / macOS)
# 用法:
#   ./start_panel.sh                                    # 本机访问(127.0.0.1:8765)
#   ./start_panel.sh --host 0.0.0.0 --token mysecret    # 服务器远程访问(带令牌)
cd "$(dirname "$0")"
exec python3 bb_web.py "$@"
