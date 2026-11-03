#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ ! -x .venv/bin/python ]; then
  echo '请先运行 ./setup.sh 安装依赖。' >&2
  exit 1
fi
exec .venv/bin/python config_server.py "$@"
