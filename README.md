# DVeah（ディーヴィア）

Icom の D-STAR 無線機を **ターミナルモード** で Mac につなぎ、インターネット上の D-STAR リフレクターへ接続するソフトウェアです。ホットスポットは不要です。

音声（AMBE）の符号化・復号は無線機が行い、DVeah は無線機とリフレクターの間でデータを中継するだけです。

> English summary: DVeah is a macOS tool that links an Icom D-STAR radio in Terminal Mode (USB serial) to D-STAR reflectors over DExtra (XRF/XLX) and DCS. No hotspot or AMBE dongle is needed. Written in Python, licensed under GPL-3.0-or-later.

## 主な機能

- DExtra（XRF / XLX）と DCS のリフレクターに接続
- ウィンドウ版（`dveah_gui.py`）とコマンドライン版（`dveah.py`）
- 実行中のモジュール切替・リフレクター切替
- 受信中の局のコールサイン表示と受信履歴（CSV に保存）
- ホスト一覧の自動取得（XLX 公式登録簿、Pi-Star 配布ファイルなど）
- 設定の保存（次回からは起動するだけ）

## 動作確認済みの環境

| 項目 | 内容 |
| --- | --- |
| OS | macOS |
| Python | Homebrew の Python 3（Tk 8.6 以降） |
| 無線機 | Icom ID-31PLUS |
| ケーブル | Icom OPC-2350LU |
| リフレクター | XLX095（DExtra / DCS）、XRF076（DExtra） |

ほかの無線機（ターミナルモード対応機）でも動く可能性はありますが、確認していません。動作報告を歓迎します。

## 必要なもの

- ターミナルモードに対応した Icom の D-STAR 無線機と、PC 接続用のデータケーブル
- Python 3 と Tk 8.6 以降
- pyserial

> **macOS 標準の Python（`/usr/bin/python3`）は使えません。** 付属の Tk 8.5 ではウィンドウが真っ暗になります。Homebrew か python.org の Python を使ってください。

## インストール（Homebrew の場合）

```sh
git clone https://github.com/Nickel2Plus/DVeah.git
cd DVeah
brew install python-tk
/opt/homebrew/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## アプリとして使う（DVeah.app）

上の「インストール」が済んだ状態で、次を実行すると `dist/DVeah.app` ができます。

```sh
./build_app.sh
```

できた `DVeah.app` を「アプリケーション」フォルダに移せば、ダブルクリックで起動できます。

- アプリとして起動した場合、設定・受信履歴・ホスト一覧は `~/Library/Application Support/DVeah` に保存されます（スクリプトとして起動した場合は、これまでどおり `dveah.py` と同じフォルダ）。
- ウィンドウの「データフォルダ」ボタンで、このフォルダを Finder で開けます。スクリプト版で使っていた `dveah.ini` や `My_Hosts.txt` を引き継ぐ場合は、ここにコピーしてください。
- 自分の Mac でビルドしたアプリはそのまま起動できます。ほかの人に配る場合、署名と公証（Apple Developer Program が必要）をしていないアプリは、相手の Mac で警告が出て開けないことがあります。

## 無線機の準備

1. 無線機の MY コールサインに自局のコールサインを設定します。
2. データケーブルで Mac と接続します。
3. 無線機をターミナルモードにします。

具体的な操作手順は、お使いの無線機の取扱説明書を参照してください。

## 使い方（ウィンドウ版）

```sh
.venv/bin/python dveah_gui.py
```

1. コールサインとシリアルポート（例: `/dev/cu.usbserial-XXXX`）を指定します。
2. 「ホスト一覧を更新」を押して、リフレクターの一覧を取得します（初回のみ。以後はときどき更新してください）。
3. リフレクターの種類（DCS / XLX / XRF）と番号、モジュールを選びます。
4. 「開始」を押します。

- 中央の表示が、待機中・受信中（緑、相手局のコールサイン）・送信中（赤）に変わります。
- 「接続先を切替」で、接続したまま別のモジュールやリフレクターに移れます。
- 一覧にないリフレクターは、「ホスト名」欄にアドレスを入れると接続できます。

## 使い方（コマンドライン版）

```sh
.venv/bin/python dveah.py --list-ports
.venv/bin/python dveah.py --call URCALL --reflector XRF095 --module E --save
```

`--save` を付けると設定が保存され、次回からは `.venv/bin/python dveah.py` だけで起動できます。主なオプションは `--help` で確認できます。

実行中は、次のコマンドを入力して Enter を押します。

| コマンド | 動作 |
| --- | --- |
| `B`（英字1文字） | 同じリフレクターの別モジュールへ切り替え |
| `m H` | H・Q・R・U モジュールへの切り替え（1文字だとコマンドと重なるため） |
| `link DCS095 E` | 別のリフレクターへ接続（ホスト名を続けて書くと一覧になくても接続可） |
| `find 09` | ホスト一覧を検索 |
| `update` | ホスト一覧を更新 |
| `h` | 受信履歴（直近10件） |
| `save` | 今の接続先を既定として保存 |
| `u` / `r` | 切断 / 再接続 |
| `q` | 終了 |

問題があるときは `--debug` を付けると、無線機とリフレクターとのやり取りを表示します。

## 接続方式の決まり方

| 名前 | 接続方式 | ポート |
| --- | --- | --- |
| `XRFxxx`、`XLXxxx` | DExtra | UDP 30001 |
| `DCSxxx` | DCS | UDP 30051 |

どちらもこちらから外向きに接続するので、通常はルーターのポート開放は不要です。

## 作られるファイル

いずれも `dveah.py` と同じフォルダ（アプリとして起動した場合は `~/Library/Application Support/DVeah`）に作られます。個人の設定やダウンロードしたデータなので、リポジトリには含めていません（`.gitignore` 参照）。

| ファイル | 内容 |
| --- | --- |
| `dveah.ini` | 設定（コールサイン、ポート、接続先など） |
| `heard.csv` | 受信履歴 |
| `XLX_Hosts.txt` | XLX 公式登録簿から取得したホスト一覧 |
| `DExtra_Hosts.txt`、`DCS_Hosts.txt` | Pi-Star の配布ファイルから取得したホスト一覧 |
| `My_Hosts.txt` | 自分で書くホスト一覧（任意。更新で上書きされず、同じ名前があれば最優先） |

`My_Hosts.txt` の形式は、1行に「名前 ホスト名」です。

```
XRF095 xrf095.xreflector-jp.org
```

ホスト一覧の取得元は、`dveah.ini` に `hosts_url_xlx`、`hosts_url_dextra`、`hosts_url_dcs` を書くと変更できます。

## 制限事項

- DPlus（REF リフレクター）には対応していません。
- エコー（Echo Test）では、返ってくる音声の頭の 0.7 秒ほどが欠けることがあります。リフレクター側の配信の仕方によるものと考えています。
- 途中から受信に入った場合、相手局のコールサインが分からないため、無線機にはリフレクター名が表示されます。
- Windows / Linux では確認していません。

## 運用上の注意

アマチュア無線局の運用は、電波法令と免許の範囲内で行ってください。

## 謝辞と参考にしたソフトウェア

- **[QnetGateway](https://github.com/n7tae/QnetGateway)**（N7TAE, GPL-2.0-or-later）: Icom ターミナルモードのシリアル手順を参考にしました。
- **[xlxd](https://github.com/LX3JL/xlxd)**（LX3JL, GPL-3.0-or-later）: DExtra / DCS のパケット形式を参考にしました。
- **[Doozy](https://www.pa7lim.nl/Doozy/)**（PA7LIM）: このソフトを作るきっかけになった Windows 用ソフトです。DVeah は Doozy とは無関係の、独立したソフトウェアです。
- ホスト一覧は、XLX リフレクターの登録簿（xlxapi.rlx.lu）と Pi-Star（pistar.uk）が公開しているものを、利用時に取得しています。

いずれのコードもコピーしておらず、Python で新しく書いています。

## ライセンス

Copyright (C) 2026 JQ7EAH (Nickel2Plus)

GNU General Public License v3.0 or later（GPL-3.0-or-later）で公開しています。全文は [LICENSE](LICENSE) を参照してください。

このソフトウェアは無保証です。ご利用は自己責任でお願いします。

D-STAR は日本アマチュア無線連盟（JARL）が策定した通信方式です。Icom、macOS、その他の会社名・製品名は、各社の商標または登録商標です。
