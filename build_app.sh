#!/bin/bash
# DVeah.app を作るスクリプト (macOS 専用)
#
# 使い方: このフォルダで
#   ./build_app.sh
# を実行すると dist/DVeah.app ができます。
#
# 使う Python は .venv/bin/python です。別の Python を使う場合は
#   PYTHON=/path/to/python3 ./build_app.sh
# のように指定してください。
#
# Copyright (C) 2026 JQ7EAH (Nickel2Plus)
# SPDX-License-Identifier: GPL-3.0-or-later

set -euo pipefail
cd "$(dirname "$0")"

if [ "$(uname)" != "Darwin" ]; then
    echo "このスクリプトは macOS で実行してください。" >&2
    exit 1
fi

PY="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PY" ]; then
    echo "$PY が見つかりません。README の「インストール」の手順で .venv を作ってください。" >&2
    exit 1
fi

# Tk 8.6 以上でないと、アプリのウィンドウが真っ暗になる
if ! "$PY" -c 'import sys, tkinter; sys.exit(0 if tkinter.TkVersion >= 8.6 else 1)'; then
    echo "この Python の Tk は古すぎます (8.6 以上が必要)。Homebrew の Python と python-tk を使ってください。" >&2
    exit 1
fi

echo "== ビルドに必要なライブラリを入れています"
"$PY" -m pip install --quiet --upgrade -r requirements.txt pyinstaller pillow

echo "== DVeah.app を作っています (1〜2分かかります)"
"$PY" -m PyInstaller \
    --noconfirm --clean \
    --windowed \
    --name DVeah \
    --icon assets/icon.png \
    --osx-bundle-identifier io.github.nickel2plus.dveah \
    dveah_gui.py

echo
echo "できました: dist/DVeah.app"
echo "Finder で dist フォルダを開き、DVeah.app を「アプリケーション」フォルダに移してください。"
