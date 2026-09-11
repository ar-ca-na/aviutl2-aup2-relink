"""aup2 リンク修復 — .aup2 に書かれた素材のパスを、移動先に書き換える。

AviUtl2 本体は「.aup2 と素材をフォルダごと移動した」場合だけ、開くときに追従する
（「プロジェクトファイルのパスが変更されています」の確認）。
素材だけを別の場所へ移した場合は追従しないので、このツールで直す。
"""
import ctypes
import ctypes.wintypes as W
import datetime
import ntpath
import os
import re
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

APP = "aup2 リンク修復"
SKIP_SECTIONS = {"project"}          # [project] の file= は .aup2 自身の保存場所
SKIP_KEYS = {"テキスト", "output.file"}
ABS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
SEP = re.compile(r"[\\/]+")


# ---------------------------------------------------------------- .aup2 の読み書き

class Aup2:
    """1 本の .aup2。素材パスの行だけを差し替えて書き戻す（他の行はバイト単位でそのまま）。"""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.raw = f.read()
        self.bom = self.raw.startswith(b"\xef\xbb\xbf")
        self.lines = self.raw[3 if self.bom else 0:].decode("utf-8").splitlines(keepends=True)
        self.refs = []  # (行番号, キー, パス)
        section = None
        for i, line in enumerate(self.lines):
            s = line.rstrip("\r\n")
            if s.startswith("[") and s.endswith("]"):
                section = s[1:-1]
                continue
            key, eq, value = s.partition("=")
            if eq and section not in SKIP_SECTIONS and key not in SKIP_KEYS and ABS_PATH.match(value):
                self.refs.append((i, key, value))

    def save(self, mapping):
        """mapping（旧パス→新パス）で書き換える。(書き換えた行数, バックアップのパス) を返す。"""
        lines = list(self.lines)
        n = 0
        for i, key, value in self.refs:
            if value in mapping:
                eol = lines[i][len(lines[i].rstrip("\r\n")):]
                lines[i] = f"{key}={mapping[value]}{eol}"
                n += 1
        if not n:
            return 0, None
        bak = f"{self.path}.bak-{datetime.datetime.now():%Y%m%d-%H%M%S}"
        with open(bak, "wb") as f:
            f.write(self.raw)
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            f.write((b"\xef\xbb\xbf" if self.bom else b"") + "".join(lines).encode("utf-8"))
        os.replace(tmp, self.path)
        return n, bak


def collect_aup2(paths):
    """ドロップされたファイル・フォルダから .aup2 を集める（フォルダは中を再帰的に）。"""
    out = []
    for p in paths:
        if os.path.isdir(p):
            for dirpath, _, names in os.walk(p):
                out += [os.path.join(dirpath, n) for n in names if n.lower().endswith(".aup2")]
        elif p.lower().endswith(".aup2") and os.path.isfile(p):
            out.append(p)
    return sorted(set(os.path.normpath(p) for p in out))


# ---------------------------------------------------------------- 移動先の推定

def common_tail(a, b):
    """末尾から一致するパス要素の数（ファイル名を含む。大文字小文字は区別しない）。"""
    n = 0
    for x, y in zip(reversed(SEP.split(a.lower().rstrip("\\/"))), reversed(SEP.split(b.lower().rstrip("\\/")))):
        if x != y:
            break
        n += 1
    return n


def choose(old, candidates):
    """同名の候補から、元のパスと末尾のフォルダ名が長く一致するものを選ぶ。(選んだパス, 同点の数)"""
    ranked = sorted(candidates, key=lambda c: (-common_tail(old, c), len(c), c))
    best = common_tail(old, ranked[0])
    return ranked[0], sum(1 for c in candidates if common_tail(old, c) == best)


def search(root_dir, olds, cancel, state):
    """root_dir 以下を 1 回だけ走査し、olds それぞれの同名ファイルを探す。state に結果を入れる。"""
    index = {}
    for dirpath, _, names in os.walk(root_dir):
        if cancel.is_set():
            return
        state["progress"] = dirpath
        for n in names:
            index.setdefault(n.lower(), []).append(os.path.join(dirpath, n))
    state["found"] = {old: choose(old, index[ntpath.basename(old).lower()])
                      for old in olds if ntpath.basename(old).lower() in index}


def infer_moves(old, new, others):
    """old→new の指定から、同じフォルダ（とその下）にあった他のファイルの移動先を推定する。"""
    n = common_tail(old, new) - 1  # ファイル名を除いた、一致するフォルダの段数
    old_dir, new_dir = ntpath.dirname(old), ntpath.dirname(new)
    for _ in range(max(n, 0)):     # 「素材\bg\a.png → 新\素材\bg\a.png」なら 素材 → 新\素材 と見る
        old_dir, new_dir = ntpath.dirname(old_dir), ntpath.dirname(new_dir)
    head = old_dir.rstrip("\\/").lower() + "\\"
    out = {}
    for q in others:
        if q.lower().startswith(head):
            cand = ntpath.normpath(new_dir.rstrip("\\/") + "\\" + q[len(head):])
            if os.path.isfile(cand):
                out[q] = cand
    return out


# ---------------------------------------------------------------- Win32

user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, W.LPARAM]
user32.EnumWindows.restype = W.BOOL
user32.GetClassNameW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [W.HWND]
user32.GetDpiForWindow.argtypes = [W.HWND]
user32.GetDpiForWindow.restype = W.UINT


def aviutl2_titles():
    """起動中の AviUtl2 のタイトル（= 開いている .aup2 のファイル名）。"""
    titles = []

    def cb(h, _):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetClassNameW(h, buf, 512)
        if buf.value == "aviutl2Manager" and user32.IsWindowVisible(h):
            user32.GetWindowTextW(h, buf, 512)
            titles.append(buf.value)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return titles


# ---------------------------------------------------------------- 画面

class App:
    COLS = (("state", "状態", 90), ("name", "ファイル名", 170), ("old", "元の場所（見つからない）", 330),
            ("new", "新しい場所", 330), ("count", "使用数", 50))

    def __init__(self, root, S):
        self.root, self.S = root, S
        self.docs = []
        self.rows = {}        # 旧パス -> {"count", "new", "note"}
        self.total = 0
        self.job = None       # (thread, cancel, state)
        root.title(APP)
        root.geometry(f"{int(1040 * S)}x{int(540 * S)}")
        root.minsize(int(640 * S), int(320 * S))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        p = int(6 * S)

        top = ttk.Frame(root, padding=p)
        top.pack(fill="x")
        ttk.Button(top, text="aup2 を開く…", command=self.ask_open).pack(side="left")
        self.info = ttk.Label(top, text="ここに .aup2（またはフォルダ）をドロップ")
        self.info.pack(side="left", padx=p)

        mid = ttk.Frame(root, padding=(p, 0))
        mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(mid, columns=[c[0] for c in self.COLS], show="headings", selectmode="extended")
        for key, label, w in self.COLS:
            self.tree.heading(key, text=label, anchor="w")
            self.tree.column(key, width=int(w * S), anchor="e" if key == "count" else "w",
                             stretch=key in ("old", "new"))
        self.tree.tag_configure("none", foreground="#b00020")
        self.tree.tag_configure("tie", foreground="#b35c00")
        self.tree.tag_configure("ok", foreground="#1b7f2a")
        ys = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        mid.rowconfigure(0, weight=1)
        mid.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda e: self.pick_manual())

        bot = ttk.Frame(root, padding=p)
        bot.pack(fill="x")
        self.btn_search = ttk.Button(bot, text="フォルダから探す…", command=self.ask_search)
        self.btn_search.pack(side="left")
        ttk.Button(bot, text="選んだ行を手で指定…", command=self.pick_manual).pack(side="left", padx=p)
        ttk.Button(bot, text="選んだ行の指定を外す", command=self.clear_selected).pack(side="left")
        self.btn_save = ttk.Button(bot, text="保存（バックアップを残す）", command=self.save)
        self.btn_save.pack(side="right")
        self.status = ttk.Label(root, padding=(p, 0, p, p), text="")
        self.status.pack(fill="x")

    # ---- 読み込み

    def ask_open(self):
        if not self.confirm_discard():
            return
        paths = filedialog.askopenfilenames(parent=self.root, title="aup2 を開く",
                                            filetypes=[("AviUtl2 プロジェクト", "*.aup2"), ("すべて", "*.*")])
        if paths:
            self.load(collect_aup2(paths))

    def on_drop(self, event):
        paths = collect_aup2(self.root.tk.splitlist(event.data))
        if not paths:
            messagebox.showinfo(APP, ".aup2 が見つかりませんでした。", parent=self.root)
        elif self.confirm_discard():
            self.load(paths)

    def load(self, paths):
        self.cancel_job()
        self.docs, errors = [], []
        for p in paths:
            try:
                self.docs.append(Aup2(p))
            except (OSError, UnicodeDecodeError) as e:
                errors.append(f"{p}\n  {e}")
        counts, self.total = {}, 0
        for d in self.docs:
            for _, _, v in d.refs:
                self.total += 1
                counts[v] = counts.get(v, 0) + 1
        self.rows = {v: {"count": c, "new": None, "note": "未解決"} for v, c in counts.items() if not os.path.exists(v)}
        self.refresh()
        if errors:
            messagebox.showwarning(APP, "読めなかったファイル:\n\n" + "\n".join(errors), parent=self.root)
        if self.docs and not self.rows:
            self.status.config(text="リンク切れはありません。")
        elif self.rows:
            self.status.config(text="「フォルダから探す」で移動先のフォルダを選ぶと、同じ名前のファイルを探します。")

    def refresh(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for old, r in sorted(self.rows.items(), key=lambda kv: kv[0].lower()):
            new = r["new"]
            tag = "none" if not new else "tie" if r["note"].startswith("候補") else "ok"
            self.tree.insert("", "end", iid=old, tags=(tag,), values=(
                r["note"], ntpath.basename(old), ntpath.dirname(old),
                ntpath.dirname(new) if new else "", r["count"]))
        keep = [s for s in sel if self.tree.exists(s)]
        if keep:
            self.tree.selection_set(keep)
        fixed = sum(1 for r in self.rows.values() if r["new"])
        if not self.docs:
            self.info.config(text="ここに .aup2（またはフォルダ）をドロップ")
        else:
            name = ntpath.basename(self.docs[0].path) if len(self.docs) == 1 else f"aup2 {len(self.docs)} 本"
            self.info.config(text=f"{name}　素材の参照 {self.total} 件 / 見つからない {len(self.rows)} 件"
                                  f"（うち移動先が決まった {fixed} 件）")

    # ---- 探す

    def ask_search(self):
        if self.job:
            self.cancel_job()
            self.status.config(text="中止しました。")
            return
        targets = [o for o, r in self.rows.items() if r["note"] in ("未解決",) or r["note"].startswith("候補")]
        if not targets:
            messagebox.showinfo(APP, "探す対象（移動先が決まっていないファイル）がありません。", parent=self.root)
            return
        start = ntpath.dirname(self.docs[0].path) if self.docs else None
        d = filedialog.askdirectory(parent=self.root, title="移動先を含むフォルダ（この中を全部探します）",
                                    initialdir=start, mustexist=True)
        if not d:
            return
        cancel, state = threading.Event(), {"progress": d}
        th = threading.Thread(target=search, args=(os.path.normpath(d), targets, cancel, state), daemon=True)
        self.job = (th, cancel, state)
        self.btn_search.config(text="探すのを中止")
        th.start()
        self.poll()

    def poll(self):
        if not self.job:
            return
        th, cancel, state = self.job
        if th.is_alive():
            self.status.config(text="探しています: " + state.get("progress", ""))
            self.root.after(100, self.poll)
            return
        self.job = None
        self.btn_search.config(text="フォルダから探す…")
        found = state.get("found", {})
        ties = 0
        for old, (new, tie) in found.items():
            self.rows[old].update(new=new, note=f"候補 {tie} 件" if tie > 1 else "見つかった")
            ties += tie > 1
        self.refresh()
        left = sum(1 for r in self.rows.values() if not r["new"])
        msg = f"{len(found)} 件見つかりました。まだ見つからないもの {left} 件。"
        if ties:
            msg += f"　同じ名前が複数あったもの {ties} 件は、フォルダ名が近いものを仮に選んでいます（橙色）。確かめてください。"
        self.status.config(text=msg)

    def cancel_job(self):
        if self.job:
            self.job[1].set()
            self.job = None
            self.btn_search.config(text="フォルダから探す…")

    # ---- 手で指定

    def pick_manual(self):
        sel = self.tree.selection()
        if len(sel) != 1:
            messagebox.showinfo(APP, "行を 1 つ選んでください。", parent=self.root)
            return
        old = sel[0]
        r = self.rows[old]
        start = ntpath.dirname(r["new"]) if r["new"] else ntpath.dirname(self.docs[0].path)
        ext = ntpath.splitext(old)[1]
        new = filedialog.askopenfilename(parent=self.root, title=f"{ntpath.basename(old)} の移動先",
                                         initialdir=start, initialfile=ntpath.basename(old),
                                         filetypes=[(f"{ext} ファイル", f"*{ext}"), ("すべて", "*.*")] if ext else None)
        if not new:
            return
        new = ntpath.normpath(new)
        r.update(new=new, note="手で指定")
        others = [o for o, x in self.rows.items() if o != old and not x["new"]]
        guessed = infer_moves(old, new, others)
        for o, n in guessed.items():
            self.rows[o].update(new=n, note="フォルダ推定")
        self.refresh()
        self.status.config(text=f"同じフォルダにあった他のファイル {len(guessed)} 件も、同じ移動先で見つかりました。"
                           if guessed else "指定しました。")

    def clear_selected(self):
        for old in self.tree.selection():
            self.rows[old].update(new=None, note="未解決")
        self.refresh()

    # ---- 保存

    def mapping(self):
        return {o: r["new"] for o, r in self.rows.items() if r["new"]}

    def save(self):
        m = self.mapping()
        if not m:
            messagebox.showinfo(APP, "書き換えるものがありません。", parent=self.root)
            return
        opened = [d.path for d in self.docs if any(ntpath.basename(d.path) in t for t in aviutl2_titles())]
        if opened and not messagebox.askyesno(APP, "AviUtl2 で開いているかもしれません:\n\n" + "\n".join(
                ntpath.basename(p) for p in opened) + "\n\nこのまま AviUtl2 で上書き保存すると、ここで直した内容が"
                "消えます。書き換えたら AviUtl2 側は保存せずに開き直してください。\n\n書き換えますか？",
                icon="warning", parent=self.root):
            return
        done, errors = [], []
        for d in self.docs:
            try:
                n, bak = d.save(m)
                if n:
                    done.append(f"{ntpath.basename(d.path)}: {n} 行（元のファイル → {ntpath.basename(bak)}）")
            except OSError as e:
                errors.append(f"{d.path}\n  {e}")
        msg = "書き換えました。\n\n" + "\n".join(done) if done else "書き換えた行はありません。"
        if errors:
            msg += "\n\n失敗:\n" + "\n".join(errors)
        (messagebox.showwarning if errors else messagebox.showinfo)(APP, msg, parent=self.root)
        self.load([d.path for d in self.docs])

    def confirm_discard(self):
        return not self.mapping() or messagebox.askyesno(
            APP, "まだ保存していない指定があります。捨てますか？", parent=self.root)

    def on_close(self):
        if self.confirm_discard():
            self.cancel_job()
            self.root.destroy()


def main():
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        pass
    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        root = TkinterDnD.Tk()
    except Exception:  # D&D が使えなくても「開く」ボタンで使える
        DND_FILES, root = None, tk.Tk()
    root.update_idletasks()
    S = user32.GetDpiForWindow(int(root.wm_frame(), 16)) / 96 or 1
    root.tk.call("tk", "scaling", S * 96 / 72)
    for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
        tkfont.nametofont(name).configure(family="Yu Gothic UI", size=10)
    ttk.Style(root).configure("Treeview", rowheight=int(24 * S))
    app = App(root, S)
    if DND_FILES:
        root.drop_target_register(DND_FILES)
        root.dnd_bind("<<Drop>>", app.on_drop)
    if len(sys.argv) > 1:
        root.after(50, lambda: app.load(collect_aup2(sys.argv[1:])))
    root.mainloop()


if __name__ == "__main__":
    main()
