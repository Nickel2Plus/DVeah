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
dveah_gui.py - DVeah (ディーヴィア) のウィンドウ版

dveah.py と同じフォルダに置いて起動します:
  python3 dveah_gui.py
設定 (dveah.ini)、ホスト一覧 (DExtra_Hosts.txt)、受信履歴 (heard.csv) は
コマンドライン版と共通です。
"""

import argparse
import os
import queue
import string
import subprocess
import sys
import threading
import webbrowser
import tkinter as tk
from tkinter import messagebox, ttk

import dveah

CONFIG = dveah.CONFIG_PATH
HEARD = os.path.join(dveah.BASE_DIR, "heard.csv")


def list_serial_ports():
    try:
        from serial.tools import list_ports
        ports = [p.device for p in list_ports.comports()]
    except Exception:
        ports = []
    # macOS では cu.usbserial を先頭に
    return sorted(ports, key=lambda p: ("usbserial" not in p, p))


class App:
    def __init__(self, root):
        self.root = root
        self.eng = None
        self.thread = None
        self.logq = queue.Queue()
        self.heard_shown = 0
        dveah.LOG_HOOK = self.logq.put

        cfg = dveah.load_config_with_migration(CONFIG)
        self.hosts = dveah.load_default_hosts()

        root.title(f"DVeah {dveah.__version__}")
        self.build_menu()
        root.minsize(520, 560)
        pad = {"padx": 6, "pady": 3}

        # ---- 無線機 ----
        f1 = ttk.LabelFrame(root, text="無線機")
        f1.pack(fill="x", padx=8, pady=(8, 4))
        ttk.Label(f1, text="コールサイン").grid(row=0, column=0, sticky="e", **pad)
        self.v_call = tk.StringVar(value=cfg.get("call", ""))
        ttk.Entry(f1, textvariable=self.v_call, width=10).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f1, text="自局モジュール").grid(row=0, column=2, sticky="e", **pad)
        self.v_lmod = tk.StringVar(value=cfg.get("local_module", "C").upper())
        ttk.Combobox(f1, textvariable=self.v_lmod, values=list("ABCD"), width=3).grid(
            row=0, column=3, sticky="w", **pad)
        ttk.Label(f1, text="シリアルポート").grid(row=1, column=0, sticky="e", **pad)
        self.v_port = tk.StringVar(value=cfg.get("port", ""))
        self.cb_port = ttk.Combobox(f1, textvariable=self.v_port, width=32)
        self.cb_port.grid(row=1, column=1, columnspan=3, sticky="we", **pad)
        ttk.Button(f1, text="再検索", command=self.refresh_ports).grid(row=1, column=4, **pad)
        f1.columnconfigure(1, weight=1)

        # ---- リフレクター ----
        f2 = ttk.LabelFrame(root, text="リフレクター (XRF/XLX は DExtra、DCS は DCS で接続)")
        f2.pack(fill="x", padx=8, pady=4)
        # 名前は「種類 (DCS/XLX/XRF)」と「番号」に分けて選ぶ
        ttk.Label(f2, text="名前").grid(row=0, column=0, sticky="e", **pad)
        saved = cfg.get("reflector", "").upper()
        fn = ttk.Frame(f2)
        fn.grid(row=0, column=1, sticky="w", **pad)
        self.v_kind = tk.StringVar(value=saved[:3] or "XRF")
        self.cb_kind = ttk.Combobox(fn, textvariable=self.v_kind, width=5, state="readonly")
        self.cb_kind.pack(side="left")
        self.v_num = tk.StringVar(value=saved[3:])
        self.cb_num = ttk.Combobox(fn, textvariable=self.v_num, width=6)
        self.cb_num.pack(side="left", padx=(4, 0))
        self.refresh_names()
        self.v_kind.trace_add("write", lambda *a: self.refresh_names())
        ttk.Label(f2, text="モジュール").grid(row=0, column=2, sticky="e", **pad)
        self.v_mod = tk.StringVar(value=cfg.get("module", "A").upper())
        ttk.Combobox(f2, textvariable=self.v_mod, values=list(string.ascii_uppercase), width=3).grid(
            row=0, column=3, sticky="w", **pad)
        ttk.Label(f2, text="ホスト名").grid(row=1, column=0, sticky="e", **pad)
        self.v_host = tk.StringVar(value=cfg.get("host", ""))
        ttk.Entry(f2, textvariable=self.v_host).grid(row=1, column=1, columnspan=3, sticky="we", **pad)
        ttk.Label(f2, text="(一覧にあれば空欄で可)").grid(row=1, column=4, sticky="w", **pad)
        # 名前を変えたら、前のリフレクターのホスト名は消す (別の名前で古いホストにつながないように)
        self.v_kind.trace_add("write", lambda *a: self.v_host.set(""))
        self.v_num.trace_add("write", lambda *a: self.v_host.set(""))
        f2.columnconfigure(3, weight=1)

        # ---- 操作ボタン ----
        f3 = ttk.Frame(root)
        f3.pack(fill="x", padx=8, pady=4)
        self.b_start = ttk.Button(f3, text="開始", command=self.start)
        self.b_start.pack(side="left", padx=3)
        self.b_link = ttk.Button(f3, text="接続先を切替", command=self.relink, state="disabled")
        self.b_link.pack(side="left", padx=3)
        self.b_unlink = ttk.Button(f3, text="切断", command=self.unlink, state="disabled")
        self.b_unlink.pack(side="left", padx=3)
        self.b_stop = ttk.Button(f3, text="停止", command=self.stop, state="disabled")
        self.b_stop.pack(side="left", padx=3)
        self.b_hosts = ttk.Button(f3, text="ホスト一覧を更新", command=self.update_hosts)
        self.b_hosts.pack(side="right", padx=3)
        ttk.Button(f3, text="データフォルダ", command=self.open_data_dir).pack(side="right", padx=3)

        # ---- 状態表示 ----
        f4 = ttk.Frame(root)
        f4.pack(fill="x", padx=8, pady=4)
        self.l_radio = ttk.Label(f4, text="無線機: 停止中")
        self.l_radio.pack(anchor="w")
        self.l_net = ttk.Label(f4, text="リフレクター: 停止中")
        self.l_net.pack(anchor="w")
        self.l_act = tk.Label(root, text="停止中", font=("Helvetica", 24, "bold"), pady=8)
        self.l_act.pack(fill="x", padx=8)
        self.act_bg = self.l_act.cget("bg")
        self.act_fg = self.l_act.cget("fg")

        # ---- 受信履歴 ----
        f5 = ttk.LabelFrame(root, text="受信履歴")
        f5.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("time", "refl", "call", "sec")
        self.tree = ttk.Treeview(f5, columns=cols, show="headings", height=8)
        for c, t, w in zip(cols, ("時刻", "接続先", "コールサイン", "秒"), (80, 90, 180, 60)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(f5, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # ---- ログ ----
        f6 = ttk.LabelFrame(root, text="ログ")
        f6.pack(fill="both", padx=8, pady=(4, 8))
        self.t_log = tk.Text(f6, height=6, state="disabled", wrap="none")
        self.t_log.pack(fill="both", expand=True)

        self.refresh_ports()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        if sys.platform == "darwin":
            # Cmd+Q やメニューの「終了」でも、リフレクターから切断してから終わる
            root.createcommand("::tk::mac::Quit", self.quit)
        root.after(200, self.poll)

    # ---- 入力値 ----
    def refresh_ports(self):
        ports = list_serial_ports()
        self.cb_port["values"] = ports
        if not self.v_port.get() or (self.v_port.get() not in ports and ports):
            usb = [p for p in ports if "usbserial" in p]
            if len(usb) == 1:
                self.v_port.set(usb[0])

    KINDS = ("DCS", "XLX", "XRF")

    def refresh_names(self):
        """種類の選択肢と、選んだ種類に合う番号の選択肢を作り直す"""
        names = [n for n in self.hosts if len(n) > 3]
        extra = sorted({n[:3] for n in names} - set(self.KINDS))     # My_Hosts.txt などの独自の名前
        kinds = list(self.KINDS) + extra
        if self.v_kind.get() not in kinds:
            kinds.append(self.v_kind.get())
        self.cb_kind["values"] = kinds
        kind = self.v_kind.get()
        # XLX は XRF の一覧からも探すので、番号の候補も両方から集める
        prefixes = ("XLX", "XRF") if kind == "XLX" else (kind,)
        self.cb_num["values"] = sorted({n[3:] for n in names if n[:3] in prefixes})

    def target(self):
        """入力欄から (名前, ホスト, モジュール) を取り出す。問題があれば None"""
        num = self.v_num.get().strip().upper()
        name = (self.v_kind.get().strip().upper() + num) if num else ""
        host = self.v_host.get().strip() or None
        mod = self.v_mod.get().strip().upper()
        if not name:
            messagebox.showerror("DVeah", "リフレクターの番号を入力してください。")
            return None
        if len(mod) != 1 or not mod.isalpha():
            messagebox.showerror("DVeah", "モジュールは A～Z の1文字で指定してください。")
            return None
        if host is None:
            found = dveah.find_host(self.hosts, name)
            if not found:
                messagebox.showerror("DVeah", f"{name} はホスト一覧にありません。ホスト名を入力してください。")
                return None
            name, host = found
        return name, host, mod

    # ---- 開始 / 停止 ----
    def start(self):
        call = self.v_call.get().strip().upper()
        port = self.v_port.get().strip()
        lmod = self.v_lmod.get().strip().upper()
        if not call or not port:
            messagebox.showerror("DVeah", "コールサインとシリアルポートを指定してください。")
            return
        if len(lmod) != 1 or not lmod.isalpha():
            messagebox.showerror("DVeah", "自局モジュールは1文字で指定してください。")
            return
        tgt = self.target()
        if not tgt:
            return
        name, host, mod = tgt
        args = argparse.Namespace(call=call, port=port, reflector=name, host=host, module=mod,
                                  local_module=lmod, udp_port=None, debug=False,
                                  hosts=self.hosts, heard_log=HEARD, config=CONFIG)
        try:
            self.eng = dveah.Engine(args)
        except Exception as e:
            messagebox.showerror("DVeah", f"開始できません:\n{e}")
            self.eng = None
            return
        dveah.save_config(args)
        self.heard_shown = 0
        self.thread = threading.Thread(target=self.worker, args=(self.eng,), daemon=True)
        self.thread.start()
        self.set_running(True)

    def worker(self, eng):
        try:
            while eng.running:
                eng.step()
        except Exception as e:
            dveah.log(f"エラーで停止しました: {e}")
        finally:
            try:
                eng.close()
            except Exception:
                pass

    def stop(self):
        if self.eng:
            self.eng.running = False
            if self.thread:
                self.thread.join(2)
        self.eng = None
        self.thread = None
        self.set_running(False)

    def set_running(self, on):
        self.b_start["state"] = "disabled" if on else "normal"
        for b in (self.b_link, self.b_unlink, self.b_stop):
            b["state"] = "normal" if on else "disabled"

    def relink(self):
        tgt = self.target()
        if tgt and self.eng:
            name, host, mod = tgt
            eng = self.eng
            eng.post(eng.link, name, host, mod)
            eng.post(eng.save)

    def unlink(self):
        if self.eng:
            self.eng.post(self.eng.unlink)

    def update_hosts(self):
        """ホスト一覧をダウンロードして読み直す (画面が固まらないよう別スレッドで)"""
        self.b_hosts["state"] = "disabled"
        self.hosts_job = None

        def job():
            ok = dveah.update_hosts(CONFIG)
            self.hosts_job = dveah.load_default_hosts() if ok else {}

        threading.Thread(target=job, daemon=True).start()

    # ---- メニュー ----
    def build_menu(self):
        """自前のメニューを付ける。
        macOS では、メニューを付けないと Tk の既定のメニュー (File の Run Widget Demo など) が出てしまう"""
        root = self.root
        menubar = tk.Menu(root)
        if sys.platform == "darwin":
            # アプリケーションメニュー (「DVeah」の下)。Services、Hide、Quit などは Tk が後ろに足す
            app_menu = tk.Menu(menubar, name="apple")
            menubar.add_cascade(menu=app_menu)
            app_menu.add_command(label="About DVeah", command=self.show_about)
            app_menu.add_separator()
            root.createcommand("tkAboutDialog", self.show_about)
            root.createcommand("::tk::mac::ShowHelp", self.open_help)

        file_menu = tk.Menu(menubar, tearoff=False)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Open Data Folder", command=self.open_data_dir)
        file_menu.add_command(label="Update Host Lists", command=self.update_hosts)

        if sys.platform == "darwin":
            window_menu = tk.Menu(menubar, name="window")     # 最小化などは macOS が入れる
            menubar.add_cascade(label="Window", menu=window_menu)

        help_menu = tk.Menu(menubar, name="help", tearoff=False)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="DVeah Help (GitHub)", command=self.open_help)
        root.config(menu=menubar)

    def show_about(self):
        messagebox.showinfo(
            "About DVeah",
            f"DVeah {dveah.__version__}\n\n"
            "Icom D-STAR ターミナルモード ⇔ リフレクター (DExtra / DCS) 中継ソフト\n\n"
            "Copyright (C) 2026 JQ7EAH (Nickel2Plus)\n"
            "License: GPL-3.0-or-later\n\n"
            f"{dveah.REPO_URL}")

    def open_help(self):
        webbrowser.open(dveah.REPO_URL)

    def open_data_dir(self):
        """設定・履歴・ホスト一覧のあるフォルダを Finder で開く"""
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.Popen([opener, dveah.BASE_DIR])
        except OSError:
            messagebox.showinfo("DVeah", f"データフォルダ:\n{dveah.BASE_DIR}")

    def quit(self):
        self.stop()
        self.root.destroy()

    # ---- 画面の更新 (0.2秒ごと) ----
    def poll(self):
        # ログ
        lines = []
        while True:
            try:
                lines.append(self.logq.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.t_log["state"] = "normal"
            self.t_log.insert("end", "\n".join(lines) + "\n")
            if int(self.t_log.index("end-1c").split(".")[0]) > 500:
                self.t_log.delete("1.0", "200.0")
            self.t_log.see("end")
            self.t_log["state"] = "disabled"

        if getattr(self, "hosts_job", None) is not None:     # ホスト一覧の更新が終わった
            new, self.hosts_job = self.hosts_job, None
            self.hosts.update(new)                           # 中継中のエンジンも同じ辞書を見ている
            self.refresh_names()
            self.b_hosts["state"] = "normal"

        eng = self.eng
        if eng and not eng.running:        # エラーなどで中継が止まった
            self.stop()
            eng = None

        if not eng:
            self.l_radio["text"] = "無線機: 停止中"
            self.l_net["text"] = "リフレクター: 停止中"
            self.show_activity("停止中", None)
        else:
            self.l_radio["text"] = "無線機: " + ("接続中" if eng.radio.connected else "応答待ち (ターミナルモードにしてください)")
            net = eng.net
            if net.linked:
                st = "接続中"
            elif net.next_try is None:
                st = "切断"
            else:
                st = "接続要求中"
            self.l_net["text"] = f"リフレクター: {net.refl} {net.refl_mod}  {st}"
            info = eng.rx_info
            if eng.tx_sid:
                self.show_activity("送信中", "#c0392b")
            elif info:
                self.show_activity("受信中  " + info[0], "#1e8449")
            else:
                self.show_activity("待機中", None)
            heard = eng.heard
            while self.heard_shown < len(heard):
                row = heard[self.heard_shown]
                self.heard_shown += 1
                self.tree.insert("", 0, values=(row[0][11:], f"{row[1]} {row[2]}", row[3], row[4]))

        self.root.after(200, self.poll)

    def show_activity(self, text, color):
        if self.l_act.cget("text") != text:
            self.l_act.configure(text=text, bg=color or self.act_bg, fg="white" if color else self.act_fg)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
