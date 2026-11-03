#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
printf '\n安装完成。请确保已安装 Google Chrome，然后运行 ./run.sh\n'
