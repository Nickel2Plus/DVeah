#!/usr/bin/env python3
# DVeah - Icom D-STAR terminal mode <-> reflector (DExtra / DCS) relay
# Copyright (C) 2026 JQ7EAH (Nickel2Plus)
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
DVeah (ディーヴィア) - Icom D-STAR ターミナルモード <-> リフレクター (DExtra / DCS) 中継

無線機 (ターミナルモード, 38400bps) をシリアル接続し、
DExtra (UDP 30001) で XRF / XLX リフレクターに、DCS (UDP 30051) で DCS リフレクターに接続します。
音声 (AMBE) は無線機側で処理されるため、このプログラムはフレームを中継するだけです。

シリアル手順は QnetGateway (QnetITAP, N7TAE, GPL-2.0-or-later) の実装を、
DExtra / DCS のパケット形式は xlxd (LX3JL, GPL-3.0-or-later) の実装を参考にしています。

使い方の例:
  python3 dveah.py --list-ports
  python3 dveah.py --call JQ7EAH --port /dev/cu.usbserial-XXXX \
      --host <リフレクターのホスト名かIP> --reflector XRF095 --module E
"""

import argparse
import collections
import configparser
import csv
import glob
import os
import queue
import random
import re
import select
import socket
import struct
import sys
import threading
import time
import urllib.request

DEXTRA_PORT = 30001
DCS_PORT = 30051

# ---- Icom ターミナルモードのフレーム種別 ----
T_PONG = 0x03
T_HDR_FROM_RADIO = 0x10
T_HDR_ACK_TO_RADIO = 0x11
T_DATA_FROM_RADIO = 0x12
T_DATA_ACK_TO_RADIO = 0x13
T_HDR_TO_RADIO = 0x20
T_HDR_ACK_FROM_RADIO = 0x21
T_DATA_TO_RADIO = 0x22
T_DATA_ACK_FROM_RADIO = 0x23


LOG_HOOK = None      # ウィンドウ版がログを受け取るための差し込み口


def log(*a):
    line = time.strftime("%H:%M:%S") + " " + " ".join(str(x) for x in a)
    print(line, flush=True)
    if LOG_HOOK:
        LOG_HOOK(line)


def pfcs(data: bytes) -> bytes:
    """D-STAR ヘッダーのチェックサム (CRC-CCITT, 反転多項式 0x8408)"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    crc = ~crc & 0xFFFF
    return bytes((crc & 0xFF, crc >> 8))


def pad8(s: str) -> bytes:
    return s.upper().encode("ascii")[:8].ljust(8, b" ")


def cs_mod(call: str, mod: str) -> bytes:
    """コールサイン7文字 + モジュール1文字"""
    return call.upper().encode("ascii")[:7].ljust(7, b" ") + mod.upper().encode("ascii")


def load_hosts(path):
    """DExtra_Hosts.txt 形式 (名前 ホスト [L]、# はコメント) を読み込む"""
    hosts = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                w = line.split("#", 1)[0].split()
                # REFxxx (DPlus) は未対応なので読み込まない
                if len(w) >= 2 and not w[0].upper().startswith("REF"):
                    hosts[w[0].upper()] = w[1]
    except OSError as e:
        log(f"ホスト一覧 {path} を読めません: {e}")
    return hosts


def find_host(hosts, name):
    """名前からホストを探す。XLX095 と XRF095 は同じものとして扱う"""
    name = name.upper()
    for n in (name, "XRF" + name[3:] if name.startswith("XLX") else name):
        if n in hosts:
            return n, hosts[n]
    return None


class FrameParser:
    """無線機からのバイト列を [長さ, 種別, ...] のフレームに切り出す"""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf += data
        out = []
        while self.buf:
            n = self.buf[0]
            if n == 0xFF:              # 区切り / 空きバイト
                del self.buf[0]
                continue
            if n < 2 or n >= 100:      # 同期はずれ: 1バイト捨てて再同期
                del self.buf[0]
                continue
            if len(self.buf) < n:
                break
            out.append(bytes(self.buf[:n]))
            del self.buf[:n]
        return out


class Radio:
    def __init__(self, ser, debug=False):
        self.ser = ser
        self.debug = debug
        self.parser = FrameParser()
        self.reset()

    def reset(self):
        self.poll_count = 0
        self.next_ping = 0.0
        self.connected = False
        self.last_rx = time.monotonic()
        self.queue = collections.deque()
        self.waiting_ack = False
        self.ack_deadline = 0.0
        self.tx_counter = 0

    def write(self, frame: bytes):
        if self.debug:
            log("PC->RIG", frame.hex())
        self.ser.write(frame + b"\xff")

    def tick(self, now):
        # 起動時は 0xFF 0xFF を18回、その後は1秒ごとに ping
        if now >= self.next_ping:
            if self.poll_count < 18:
                self.ser.write(b"\xff\xff\xff")
                self.poll_count += 1
                self.next_ping = now + (0.001 if self.poll_count < 18 else 1.0)
            else:
                self.write(b"\x02\x02")
                self.next_ping = now + 1.0
        # 送信待ちフレームを ACK を待ちながら1つずつ送る
        if self.waiting_ack and now >= self.ack_deadline:
            if self.debug:
                log("ACK待ちタイムアウト")
            self.waiting_ack = False
        if self.connected and not self.waiting_ack and self.queue:
            self.write(self.queue.popleft())
            self.waiting_ack = True
            self.ack_deadline = now + 0.06
        if self.connected and now - self.last_rx > 10:
            log("無線機から10秒間応答がありません。再接続を試みます。")
            self.reset()

    def read(self):
        data = self.ser.read(self.ser.in_waiting or 1)
        frames = self.parser.feed(data)
        if frames:
            self.last_rx = time.monotonic()
        for f in frames:
            if self.debug and f[1] != T_PONG:
                log("RIG->PC", f.hex())
            t = f[1]
            if t == T_PONG:
                if not self.connected:
                    log("無線機と接続しました (ターミナルモード)")
                    self.connected = True
            elif t == T_HDR_FROM_RADIO:
                self.write(bytes((0x03, T_HDR_ACK_TO_RADIO, 0x00)))
            elif t == T_DATA_FROM_RADIO and len(f) >= 3:
                self.write(bytes((0x04, T_DATA_ACK_TO_RADIO, f[2], 0x00)))
            elif t in (T_HDR_ACK_FROM_RADIO, T_DATA_ACK_FROM_RADIO):
                self.waiting_ack = False
        return frames

    def send_header(self, flags, r2, r1, ur, my, nm):
        self.tx_counter = 0
        self.queue.append(bytes((41, T_HDR_TO_RADIO)) + flags + r2 + r1 + ur + my + nm)

    def send_voice(self, seq, ambe12):
        self.queue.append(bytes((16, T_DATA_TO_RADIO, self.tx_counter & 0xFF, seq)) + ambe12)
        self.tx_counter += 1


class Link:
    """リフレクター接続の共通部分。read() は次のいずれかを返す:
         None                              受信なし
         ()                                制御パケットなど (処理済み)
         ("hdr", sid, flags, ur, my, nm)   ストリームの先頭
         ("voice", sid, seq, ambe12)       音声フレーム
    """
    PROTO = ""
    PORT = 0

    def __init__(self, host, port, call, local_mod, refl, refl_mod, debug=False):
        self.debug = debug
        self.host = host
        self.addr = (socket.gethostbyname(host), port or self.PORT)
        self.call = call
        self.local_mod = local_mod
        self.refl = refl
        self.refl_mod = refl_mod
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.linked = False
        self.next_try = 0.0
        self.next_keepalive = 0.0
        self.last_rx = time.monotonic()
        self.pending = collections.deque()

    def _send(self, data):
        try:
            self.sock.sendto(data, self.addr)
        except OSError as e:
            log("UDP送信エラー:", e)

    def tick(self, now):
        if not self.linked:
            if self.next_try is None:      # 切断したまま待機
                return
            if now >= self.next_try:
                log(f"{self.refl} {self.refl_mod} に接続要求中 ({self.addr[0]}, {self.PROTO})")
                self._send(self.connect_packet())
                self.next_try = now + 5
            return
        if now >= self.next_keepalive:
            self._send(self.keepalive_packet())
            self.next_keepalive = now + 3
        if now - self.last_rx > 30:
            log("リフレクターから30秒間応答がありません。再接続します。")
            self.linked = False

    def relink(self, refl_mod, refl=None, host=None):
        """接続先を切り替える (host 省略時は同じリフレクターの別モジュール)"""
        if host:
            try:
                addr = (socket.gethostbyname(host), self.addr[1])
            except OSError as e:
                log(f"ホスト名 {host} を解決できません: {e}")
                return False
        else:
            addr = self.addr
        self.unlink()
        self.addr = addr
        if host:
            self.host = host
        if refl:
            self.refl = refl.upper()
        self.refl_mod = refl_mod.upper()
        self.linked = False
        self.next_try = 0.0
        self.last_rx = time.monotonic()
        return True

    def unlink(self):
        if self.linked:
            for _ in range(2):
                self._send(self.disconnect_packet())
            log("リフレクターから切断しました")
        self.linked = False

    def close(self):
        self.unlink()
        self.sock.close()

    def read(self):
        if self.pending:
            return self.pending.popleft()
        try:
            data, src = self.sock.recvfrom(1024)
        except OSError:
            return None
        if src[0] != self.addr[0]:
            if self.debug:
                log(f"NET<- 別アドレス {src} からの {len(data)} バイトを無視")
            return ()
        self.last_rx = time.monotonic()
        if len(data) == 14 and data[10:13] in (b"ACK", b"NAK"):
            if self.debug:
                log(f"NET<- {data.hex()}")
            if data[10:13] == b"ACK":
                if not self.linked:
                    log(f"{self.refl} {self.refl_mod} に接続しました")
                self.linked = True
                self.next_keepalive = 0.0
            else:
                log("リフレクターに接続を拒否されました (NAK)")
                self.linked = False
            return ()
        return self.parse(data) or ()


class DExtra(Link):
    PROTO = "DExtra"
    PORT = DEXTRA_PORT

    def connect_packet(self):
        return pad8(self.call) + self.local_mod.encode() + self.refl_mod.encode() + b"\x00"

    def keepalive_packet(self):
        return pad8(self.call) + b"\x00"

    def disconnect_packet(self):
        return pad8(self.call) + self.local_mod.encode() + b" \x00"

    def parse(self, data):
        if data[:4] != b"DSVT" or len(data) not in (27, 56):
            if self.debug and len(data) != 9:
                log(f"NET<- {len(data)}バイト {data[:60].hex()}")
            return None
        sid = struct.unpack(">H", data[12:14])[0]
        if len(data) == 56:
            if self.debug:
                log(f"NET<- ヘッダー sid={sid:04x}")
            return ("hdr", sid, data[15:18], data[34:42], data[42:50], data[50:54])
        if self.debug and (data[14] == 0 or data[14] & 0x40):
            log(f"NET<- 音声 sid={sid:04x} seq={data[14]:02x}")
        return ("voice", sid, data[14], data[15:27])

    def send_header(self, sid, flags, ur, my, nm):
        body = flags + cs_mod(self.refl, self.refl_mod) + cs_mod(self.call, self.local_mod) + ur + my + nm
        pkt = (b"DSVT\x10\x00\x00\x00\x20\x00\x01" + self.local_mod.encode()
               + struct.pack(">H", sid) + b"\x80" + body + pfcs(body))
        for _ in range(5):
            self._send(pkt)

    def send_voice(self, sid, seq, ambe12):
        self._send(b"DSVT\x20\x00\x00\x00\x20\x00\x01" + self.local_mod.encode()
                   + struct.pack(">H", sid) + bytes((seq,)) + ambe12)


class DCS(Link):
    """DCS: 音声パケット (100バイト) の1つ1つにヘッダー情報が入っている"""
    PROTO = "DCS"
    PORT = DCS_PORT
    HTML = b"<table border=\"0\" width=\"95%\"><tr><td><b>TERMINAL</b> DVeah</td></tr></table>"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.tx_hdr = None
        self.tx_count = 0
        self.rx_sid = None

    def connect_packet(self):
        pkt = (pad8(self.call) + self.local_mod.encode() + self.refl_mod.encode() + b"\x00"
               + pad8(self.refl) + self.HTML)
        return pkt.ljust(519, b"\x00")

    def keepalive_packet(self):
        return cs_mod(self.call, self.local_mod) + b"\x00" + cs_mod(self.refl, self.refl_mod)

    def disconnect_packet(self):
        return pad8(self.call) + self.local_mod.encode() + b" \x00" + pad8(self.refl)

    def relink(self, *a, **kw):
        self.rx_sid = None
        return super().relink(*a, **kw)

    def parse(self, data):
        if len(data) >= 100 and data[:4] == b"0001":
            sid = struct.unpack(">H", data[43:45])[0]
            seq = data[45]
            voice = ("voice", sid, seq, data[46:58])
            if self.debug and (seq == 0 or seq & 0x40):
                log(f"NET<- 音声 sid={sid:04x} seq={seq:02x}")
            if sid != self.rx_sid and not seq & 0x40:
                self.rx_sid = sid
                self.pending.append(voice)
                return ("hdr", sid, data[4:7], data[23:31], data[31:39], data[39:43])
            if seq & 0x40:
                self.rx_sid = None
            return voice
        if len(data) in (9, 22):           # リフレクターからの生存確認には応答を返す
            self._send(self.keepalive_packet())
        elif self.debug:
            log(f"NET<- {len(data)}バイト {data[:60].hex()}")
        return None

    def send_header(self, sid, flags, ur, my, nm):
        self.tx_hdr = (flags + cs_mod(self.refl, self.refl_mod) + cs_mod(self.call, self.local_mod)
                       + ur + my + nm)
        self.tx_count = 0

    def send_voice(self, sid, seq, ambe12):
        if not self.tx_hdr:
            return
        n = self.tx_count
        self.tx_count += 1
        pkt = (b"0001" + self.tx_hdr + struct.pack(">H", sid) + bytes((seq,)) + ambe12
               + bytes((n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF)) + b"\x01\x00")
        self._send(pkt.ljust(100, b"\x00"))


def link_class(name):
    """リフレクター名から接続方式を決める (DCSxxx は DCS、それ以外は DExtra)"""
    return DCS if name.upper().startswith("DCS") else DExtra


HELP = ("コマンド: A～Z = モジュール切替 (H,Q,R,U は m H の形で) / link 名前 [ホスト] [モジュール] = 別のリフレクターへ / "
        "find 文字列 = 一覧を検索 / update = ホスト一覧を更新 / h = 受信履歴 / save = 今の接続先を既定として保存 / "
        "u = 切断 / r = 再接続 / q = 終了 (入力後 Enter)")


class Engine:
    """無線機とリフレクターの中継本体。コマンドライン版とウィンドウ版の両方から使う"""

    def __init__(self, args):
        import serial

        self.args = args
        self.ser = serial.Serial(args.port, 38400, bytesize=8, parity="N", stopbits=1, timeout=0)
        try:
            self.net = self.make_link(args.reflector, args.host, args.module)
        except OSError:
            self.ser.close()
            raise
        self.radio = Radio(self.ser, args.debug)
        self.tx_sid = 0          # 無線機 -> リフレクター のストリームID (0 = 送信していない)
        self.rx_sid = None       # リフレクター -> 無線機 のストリームID
        self.rx_last = 0.0
        self.rx_info = None      # 受信中の局 (コールサイン, 開始時刻)
        self.heard = []          # 受信履歴 [日時, リフレクター, モジュール, コールサイン, 秒]
        self.cmdq = queue.Queue()
        self.running = True

    # ---- 別スレッドから安全に操作するための入口 ----
    def post(self, fn, *a):
        self.cmdq.put((fn, a))

    def make_link(self, name, host, mod):
        a = self.args
        return link_class(name)(host, a.udp_port, a.call, a.local_module, name.upper(), mod.upper(), a.debug)

    # ---- 接続先の操作 ----
    def switch_module(self, mod):
        self.net.relink(mod)
        self.rx_end()

    def link(self, name, host=None, mod="A"):
        name = name.upper()
        if host is None:
            found = find_host(self.args.hosts, name)
            if not found:
                log(f"{name} はホスト一覧にありません。ホスト名も指定してください。")
                return False
            name, host = found
        if link_class(name) is not type(self.net):
            # 接続方式が変わるときは、接続を作り直す
            try:
                new = self.make_link(name, host, mod)
            except OSError as e:
                log(f"ホスト名 {host} を解決できません: {e}")
                return False
            self.rx_end()
            self.net.close()
            self.net = new
            return True
        if self.net.relink(mod, name, host):
            self.rx_end()
            return True
        return False

    def unlink(self):
        self.net.unlink()
        self.net.next_try = None
        self.rx_end()

    def reconnect(self):
        self.net.relink(self.net.refl_mod)
        self.rx_end()

    def save(self):
        self.args.reflector, self.args.module, self.args.host = self.net.refl, self.net.refl_mod, self.net.host
        save_config(self.args)

    # ---- 受信履歴 ----
    def rx_begin(self, my, nm):
        call = my.decode("ascii", "replace").strip()
        sfx = nm.decode("ascii", "replace").strip()
        self.rx_info = (call + ("/" + sfx if sfx else ""), time.time())

    def rx_end(self):
        self.rx_sid = None
        if not self.rx_info:
            return
        who, start = self.rx_info
        self.rx_info = None
        row = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start)), self.net.refl, self.net.refl_mod,
               who, f"{time.time() - start:.1f}"]
        self.heard.append(row)
        if self.args.heard_log:
            try:
                new = not os.path.exists(self.args.heard_log)
                with open(self.args.heard_log, "a", newline="", encoding="utf-8") as f:
                    wr = csv.writer(f)
                    if new:
                        wr.writerow(["日時", "リフレクター", "モジュール", "コールサイン", "秒"])
                    wr.writerow(row)
            except OSError as e:
                log("受信履歴を書き込めません:", e)
                self.args.heard_log = None

    # ---- 中継処理 (5ms ごとに1回) ----
    def step(self, extra=()):
        args, radio, net, ser = self.args, self.radio, self.net, self.ser
        r, _, _ = select.select([ser.fileno(), net.sock, *extra], [], [], 0.005)
        now = time.monotonic()

        while True:
            try:
                fn, a = self.cmdq.get_nowait()
            except queue.Empty:
                break
            fn(*a)
        net = self.net               # コマンドで接続が作り直された場合に備える

        if ser.fileno() in r:
            for f in radio.read():
                t = f[1]
                if t == T_HDR_FROM_RADIO and len(f) >= 41:
                    flags, ur, my, nm = f[2:5], f[21:29], f[29:37], f[37:41]
                    self.tx_sid = random.randint(1, 0xFFFF)
                    self.rx_end()
                    log(f"送信開始 MY={my.decode('ascii', 'replace')}/{nm.decode('ascii', 'replace')}"
                        f" UR={ur.decode('ascii', 'replace')}")
                    if net.linked:
                        # リフレクター宛はすべて CQCQCQ として送る
                        net.send_header(self.tx_sid, bytes((flags[0] & ~0x40 & 0xFF, flags[1], flags[2])),
                                        b"CQCQCQ  ", my, nm)
                elif t == T_DATA_FROM_RADIO and len(f) >= 16 and self.tx_sid:
                    seq = f[3]
                    if net.linked:
                        net.send_voice(self.tx_sid, seq, f[4:16])
                    if seq & 0x40:
                        log("送信終了")
                        self.tx_sid = 0

        if net.sock in r:
            while True:
                ev = net.read()
                if ev is None:       # 受信バッファが空
                    break
                if not ev:           # 制御パケット
                    continue
                if self.tx_sid:      # 自局送信中は受信を捨てる
                    continue
                sid = ev[1]
                if ev[0] == "hdr":
                    if sid == self.rx_sid:    # ヘッダーの重複
                        continue
                    self.rx_end()
                    self.rx_sid, self.rx_last = sid, now
                    flags, ur, my, nm = ev[2:6]
                    self.rx_begin(my, nm)
                    log(f"受信開始 MY={my.decode('ascii', 'replace')}/{nm.decode('ascii', 'replace')}"
                        f" UR={ur.decode('ascii', 'replace')}")
                    radio.send_header(bytes((flags[0] & ~0x40 & 0xFF, flags[1], flags[2])),
                                      cs_mod(args.call, "G"), cs_mod(args.call, args.local_module),
                                      b"CQCQCQ  ", my, nm)
                else:
                    seq, ambe = ev[2], ev[3]
                    if sid != self.rx_sid:
                        if self.rx_sid is not None or seq & 0x40:
                            continue
                        # ヘッダーを受け取れなかったストリーム: 仮のヘッダーを作って受信する
                        self.rx_sid = sid
                        self.rx_begin(b"(unknown)", b"")
                        log("受信開始 (ヘッダーなし)")
                        radio.send_header(b"\x00\x00\x00", cs_mod(args.call, "G"),
                                          cs_mod(args.call, args.local_module),
                                          b"CQCQCQ  ", pad8(net.refl), b"    ")
                    self.rx_last = now
                    radio.send_voice(seq, ambe)
                    if seq & 0x40:
                        log("受信終了")
                        self.rx_end()

        if self.rx_sid is not None and now - self.rx_last > 2:
            log("受信がタイムアウトしました")
            self.rx_end()

        radio.tick(now)
        net.tick(now)
        return r

    def close(self):
        self.running = False
        try:
            self.rx_end()
            self.net.close()
        finally:
            self.ser.close()


def run(args):
    eng = Engine(args)
    log(HELP)
    use_stdin = sys.stdin.isatty()
    try:
        while True:
            r = eng.step([sys.stdin] if use_stdin else [])
            if not (use_stdin and sys.stdin in r):
                continue
            line = sys.stdin.readline()
            if not line:
                use_stdin = False
            w = line.split()
            cmd = w[0].lower() if w else ""
            if cmd == "q":
                break
            elif cmd == "u":
                eng.unlink()
            elif cmd == "r":
                eng.reconnect()
            elif cmd == "h":
                if not eng.heard:
                    log("受信履歴はまだありません")
                for row in eng.heard[-10:]:
                    print("   ", row[0][11:], f"{row[1]} {row[2]}", row[3], row[4] + "秒", flush=True)
            elif cmd == "save":
                eng.save()
            elif cmd == "update":
                # 音声を止めないよう、ダウンロードは別スレッドで行う
                def job():
                    if update_hosts(args.config):
                        eng.post(lambda: args.hosts.update(load_default_hosts(args.hosts_file)))
                threading.Thread(target=job, daemon=True).start()
            elif cmd == "m" and len(w) == 2 and len(w[1]) == 1 and w[1].isalpha():
                eng.switch_module(w[1])
            elif len(cmd) == 1 and cmd.isalpha():
                eng.switch_module(cmd)
            elif cmd == "link" and len(w) in (2, 3, 4):
                # link 名前 / link 名前 モジュール / link 名前 ホスト / link 名前 ホスト モジュール
                rest = w[2:]
                mod = rest.pop() if rest and len(rest[-1]) == 1 and rest[-1].isalpha() else "A"
                if len(rest) > 1:
                    log(HELP)
                else:
                    eng.link(w[1], rest[0] if rest else None, mod)
            elif cmd == "find" and len(w) == 2:
                hits = sorted(n for n in args.hosts if w[1].upper() in n)
                log(f"{len(hits)}件: " + " ".join(hits[:60]) + (" ..." if len(hits) > 60 else ""))
            elif cmd:
                log(HELP)
    except KeyboardInterrupt:
        pass
    finally:
        eng.close()


def _data_dir():
    """設定・履歴・ホスト一覧の置き場所。
    アプリ (DVeah.app) として動いているときは ~/Library/Application Support/DVeah、
    スクリプトとして動いているときは dveah.py と同じフォルダ"""
    if getattr(sys, "frozen", False):
        d = os.path.expanduser("~/Library/Application Support/DVeah")
        os.makedirs(d, exist_ok=True)
        return d
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _data_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "dveah.ini")
OLD_CONFIG_PATH = os.path.join(BASE_DIR, "dterm.ini")     # 改名前の設定ファイル


def load_config_with_migration(path):
    """設定を読む。既定の dveah.ini がまだなく、改名前の dterm.ini があればそちらを読む"""
    if path == CONFIG_PATH and not os.path.exists(path) and os.path.exists(OLD_CONFIG_PATH):
        log(f"改名前の設定ファイルを読み込みます: {OLD_CONFIG_PATH} (次回の保存から dveah.ini に書きます)")
        return load_config(OLD_CONFIG_PATH)
    return load_config(path)


CONFIG_KEYS = ("call", "port", "reflector", "host", "module", "local_module")


def load_config(path):
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    for sec in ("dveah", "dterm"):       # dterm は改名前の設定ファイルの見出し
        if cp.has_section(sec):
            return dict(cp[sec])
    return {}


def save_config(args):
    cp = configparser.ConfigParser()
    keep = {k: v for k, v in load_config_with_migration(args.config).items() if k not in CONFIG_KEYS}
    cp["dveah"] = {**keep, **{k: str(getattr(args, k)) for k in CONFIG_KEYS if getattr(args, k)}}
    try:
        with open(args.config, "w", encoding="utf-8") as f:
            cp.write(f)
        log(f"設定を保存しました: {args.config}")
    except OSError as e:
        log("設定を保存できません:", e)


# ホスト一覧の取得元。ファイルごとに上から順に試し、最初に取得できたものを使う。
# 読み込みはこの並び順で、同じ名前は後のファイルが優先 (XLX 公式の登録簿を優先)。
# dveah.ini の hosts_url_xlx / hosts_url_dextra / hosts_url_dcs に URL を書くと、それを最優先で試す
HOSTS_URLS = {
    "DExtra_Hosts.txt": ("hosts_url_dextra", ["http://www.pistar.uk/downloads/DExtra_Hosts.txt",
                                              "http://www.arrg.us/HF/DExtra_Hosts.txt"]),
    "DCS_Hosts.txt": ("hosts_url_dcs", ["http://www.pistar.uk/downloads/DCS_Hosts.txt",
                                        "http://www.arrg.us/HF/DCS_Hosts.txt"]),
    # XLX リフレクターの公式登録簿 (DCSxxx / XRFxxx / REFxxx が1つのファイルに入っている)
    "XLX_Hosts.txt": ("hosts_url_xlx", ["http://xlxapi.rlx.lu/api.php?do=GetReflectorHostname"]),
}
MY_HOSTS = "My_Hosts.txt"    # 自分で書く一覧。自動更新で上書きされず、同じ名前があればこちらを優先


def load_default_hosts(path=None):
    """--hosts-file の指定がなければ、同じフォルダの DExtra_Hosts.txt、DCS_Hosts.txt、XLX_Hosts.txt、My_Hosts.txt を読む"""
    names = list(HOSTS_URLS) + [MY_HOSTS]
    paths = [path] if path else [p for p in (os.path.join(BASE_DIR, n) for n in names) if os.path.exists(p)]
    hosts = {}
    for p in paths:
        h = load_hosts(p)
        log(f"ホスト一覧: {len(h)}件 ({p})")
        hosts.update(h)
    return hosts


def fetch_hosts(url):
    """URL から一覧を取得し、(本文, 有効な行数) を返す。形式が違えば例外"""
    req = urllib.request.Request(url, headers={"User-Agent": "DVeah"})
    with urllib.request.urlopen(req, timeout=15) as res:
        data = res.read(5_000_000)
    text = data.decode("utf-8", "replace")
    # 中身の確認: 「名前 ホスト名」の形の行が十分にあること
    good = 0
    for line in text.splitlines():
        w = line.split("#", 1)[0].split()
        if len(w) >= 2 and re.fullmatch(r"[A-Za-z0-9]{3,8}", w[0]) and re.fullmatch(r"[A-Za-z0-9._:-]+", w[1]):
            good += 1
    if good < 5:
        head = " ".join(text[:100].split())
        raise ValueError(f"ホスト一覧の形式ではありません ({len(data)}バイト、先頭: {head!r})")
    return text, good


def update_hosts(config_path=None):
    """ホスト一覧をダウンロードして置き換える。成功したファイル数を返す"""
    cfg = load_config_with_migration(config_path) if config_path else {}
    ok = 0
    for fname, (key, urls) in HOSTS_URLS.items():
        path = os.path.join(BASE_DIR, fname)
        for url in ([cfg[key]] if cfg.get(key) else []) + urls:
            try:
                text, good = fetch_hosts(url)
            except Exception as e:
                log(f"{fname}: 取得できません ({url}): {e}")
                continue
            try:
                if os.path.exists(path):
                    os.replace(path, path + ".bak")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
            except OSError as e:
                log(f"{fname}: 保存できません: {e}")
                break
            log(f"{fname}: {good}件に更新しました ({url})")
            ok += 1
            break
    return ok


def main():
    p = argparse.ArgumentParser(description="DVeah - Icom ターミナルモード <-> リフレクター (DExtra / DCS) 中継")
    p.add_argument("--list-ports", action="store_true", help="シリアルポートの一覧を表示")
    p.add_argument("--update-hosts", action="store_true", help="ホスト一覧をダウンロードして更新し、終了")
    p.add_argument("--call", help="自局コールサイン (例: JQ7EAH)")
    p.add_argument("--port", help="シリアルポート (例: /dev/cu.usbserial-XXXX)")
    p.add_argument("--host", help="リフレクターのホスト名またはIPアドレス (ホスト一覧にあれば省略可)")
    p.add_argument("--hosts-file", help="ホスト一覧ファイル (既定: dveah.py と同じフォルダの *_Hosts.txt)")
    p.add_argument("--reflector", help="リフレクター名 (例: XRF095、DCS095。DCSで始まる名前は DCS 接続)")
    p.add_argument("--module", help="接続先モジュール (既定: A)")
    p.add_argument("--local-module", help="自局側モジュール (既定: C)")
    p.add_argument("--config", default=CONFIG_PATH,
                   help="設定ファイル (既定: dveah.py と同じフォルダの dveah.ini)")
    p.add_argument("--save", action="store_true", help="今回の指定を設定ファイルに保存して起動")
    p.add_argument("--heard-log", default=os.path.join(BASE_DIR, "heard.csv"),
                   help="受信履歴のCSV (既定: dveah.py と同じフォルダの heard.csv)")
    p.add_argument("--no-heard-log", action="store_true", help="受信履歴をファイルに残さない")
    p.add_argument("--udp-port", type=int, help="UDPポート (既定: DExtra 30001 / DCS 30051)")
    p.add_argument("--debug", action="store_true", help="シリアルとネットワークのやり取りを表示")
    args = p.parse_args()

    if args.list_ports:
        from serial.tools import list_ports
        for port in list_ports.comports():
            print(port.device, "-", port.description)
        return
    if args.update_hosts:
        update_hosts(args.config)
        return
    # 優先順位: コマンドラインの指定 > 設定ファイル > 既定値
    cfg = load_config_with_migration(args.config)
    cli_reflector = args.reflector
    for k in CONFIG_KEYS:
        if getattr(args, k) is None and cfg.get(k):
            # リフレクターを指定し直したときは、保存済みのホストは使わない
            if k == "host" and cli_reflector:
                continue
            setattr(args, k, cfg[k])
    args.module = args.module or "A"
    args.local_module = args.local_module or "C"
    if args.no_heard_log:
        args.heard_log = None
    if not args.port:
        ports = sorted(glob.glob("/dev/cu.usbserial*"))
        if len(ports) == 1:
            args.port = ports[0]
            log(f"シリアルポートを自動選択: {args.port}")
    missing = [n for n in ("call", "port", "reflector") if not getattr(args, n)]
    if missing:
        p.error("次の指定が必要です: " + ", ".join("--" + m for m in missing))
    args.hosts = load_default_hosts(args.hosts_file)
    args.reflector = args.reflector.upper()
    if not args.host:
        found = find_host(args.hosts, args.reflector)
        if not found:
            p.error(f"{args.reflector} がホスト一覧にありません。--host で指定してください。")
        args.reflector, args.host = found
    args.module = args.module.upper()
    args.local_module = args.local_module.upper()
    args.call = args.call.upper()
    if args.save:
        save_config(args)
    run(args)


if __name__ == "__main__":
    main()
