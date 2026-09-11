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
import subprocess
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
FONTS = ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont")


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


def doc_labels(paths):
    """一覧に出す .aup2 の名前。ファイル名が重なるものだけ親フォルダ名を付ける。"""
    names = [ntpath.basename(p) for p in paths]
    return [n if names.count(n) == 1 else ntpath.join(ntpath.basename(ntpath.dirname(p)), n)
            for p, n in zip(paths, names)]


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
MONITORENUMPROC = ctypes.WINFUNCTYPE(W.BOOL, W.HMONITOR, W.HDC, ctypes.POINTER(W.RECT), W.LPARAM)
user32.EnumDisplayMonitors.argtypes = [W.HDC, ctypes.c_void_p, MONITORENUMPROC, W.LPARAM]
user32.EnumDisplayMonitors.restype = W.BOOL


def window_scale(root):
    """窓が今いるモニタの拡大率（100% = 1.0）。"""
    return user32.GetDpiForWindow(int(root.wm_frame(), 16)) / 96 or 1


def min_scale():
    """全モニタのうち一番小さい拡大率。"""
    shcore = ctypes.windll.shcore
    shcore.GetDpiForMonitor.argtypes = [W.HMONITOR, ctypes.c_int, ctypes.POINTER(W.UINT), ctypes.POINTER(W.UINT)]
    shcore.GetDpiForMonitor.restype = ctypes.c_long
    dpis = []

    def cb(h, dc, r, _):
        x, y = W.UINT(), W.UINT()
        if shcore.GetDpiForMonitor(h, 0, ctypes.byref(x), ctypes.byref(y)) == 0:
            dpis.append(x.value)
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    return min(dpis, default=96) / 96


def load_dpihook():
    """WM_DPICHANGED を処理する小さな DLL（dpihook.c）。読めなければ None。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    try:
        dll = ctypes.CDLL(os.path.join(base, "dpihook.dll"))
    except OSError:
        return None
    dll.hook_dpi_changed.argtypes = [W.HWND]
    dll.hook_dpi_changed.restype = ctypes.c_int
    return dll


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
    # 幅の合計は窓の初期幅（1040）に収める。「プロジェクト」列を隠したぶんは old / new が伸びて埋める
    COLS = (("state", "状態", 80), ("name", "ファイル名", 150), ("proj", "プロジェクト", 160),
            ("old", "元の場所（見つからない）", 260), ("new", "新しい場所", 260), ("count", "使用数", 60))

    def __init__(self, root, S):
        self.root, self.S = root, S
        self.docs = []
        self.rows = {}        # 旧パス -> {"count", "new", "note", "proj"}
        self.total = 0
        self.job = None       # (thread, cancel, state)
        self.hooked = False   # WM_DPICHANGED を DLL で受けているか
        root.title(APP)
        root.geometry(f"{int(1040 * S)}x{int(540 * S)}")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.top = ttk.Frame(root)
        self.top.pack(fill="x")
        ttk.Button(self.top, text="aup2 を開く…", command=self.ask_open).pack(side="left")
        self.info = ttk.Label(self.top, text="ここに .aup2（またはフォルダ）をドロップ")
        self.info.pack(side="left")

        self.mid = ttk.Frame(root)
        self.mid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(self.mid, columns=[c[0] for c in self.COLS], show="headings", selectmode="extended")
        for key, label, w in self.COLS:
            self.tree.heading(key, text=label, anchor="w")
            self.tree.column(key, width=int(w * S), anchor="e" if key == "count" else "w",
                             stretch=key in ("old", "new"))
        self.tree.tag_configure("none", foreground="#b00020")
        self.tree.tag_configure("tie", foreground="#b35c00")
        self.tree.tag_configure("ok", foreground="#1b7f2a")
        ys = ttk.Scrollbar(self.mid, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(self.mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        self.mid.rowconfigure(0, weight=1)
        self.mid.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda e: self.pick_manual())

        self.bot = ttk.Frame(root)
        self.bot.pack(fill="x")
        self.btn_search = ttk.Button(self.bot, text="フォルダから探す…", command=self.ask_search)
        self.btn_search.pack(side="left")
        self.btn_manual = ttk.Button(self.bot, text="選んだ行を手で指定…", command=self.pick_manual)
        self.btn_manual.pack(side="left")
        ttk.Button(self.bot, text="選んだ行の指定を外す", command=self.clear_selected).pack(side="left")
        self.btn_save = ttk.Button(self.bot, text="保存（バックアップを残す）", command=self.save)
        self.btn_save.pack(side="right")
        self.status = ttk.Label(root, text="")
        self.status.pack(fill="x")
        self.show_proj_column()
        self.apply_scale()
        # 最小サイズは一番小さい拡大率で決める。今のモニタの値だと、縮小側のモニタへ移ったとき
        # Windows が示す大きさが最小サイズで止められ、窓の大半が元のモニタに残って行き来する
        S0 = min_scale()
        root.minsize(int(640 * S0), int(320 * S0))
        root.bind("<Configure>", self.on_configure)
        self.hooked = self.hook_dpi_changed()

    # ---- モニタの拡大率

    def hook_dpi_changed(self):
        """拡大率の違うモニタへ移ったとき、Windows が示す位置と大きさ（WM_DPICHANGED）へ窓を合わせる。
        Tk はこの通知を処理しない。自前で左上を固定して大きさだけ変えると、ドラッグ中に窓の大半が載る
        モニタが入れ替わって拡大と縮小を繰り返し、窓が境目で止まる（2026-09-12 実測）。
        窓プロシージャの差し替えは C の DLL で行う。ctypes のコールバックで差し替えると、
        tkinter の GIL の受け渡しと衝突して起動直後に落ちる（同日実測）。"""
        self._dpihook = load_dpihook()  # DLL を解放させない
        return bool(self._dpihook and self._dpihook.hook_dpi_changed(int(self.root.wm_frame(), 16)))

    def apply_scale(self):
        """self.S に合わせて余白・行の高さ・最小サイズを決め直す。"""
        S, p = self.S, int(6 * self.S)
        self.top.configure(padding=p)
        self.mid.configure(padding=(p, 0))
        self.bot.configure(padding=p)
        self.status.configure(padding=(p, 0, p, p))
        self.info.pack_configure(padx=p)
        self.btn_manual.pack_configure(padx=p)
        ttk.Style(self.root).configure("Treeview", rowheight=int(24 * S))

    def on_configure(self, event):
        """拡大率の違うモニタへ移ったら、文字・列幅・余白を掛け直す（窓の大きさは hook_dpi_changed が合わせる）。"""
        if event.widget is not self.root:
            return
        S = window_scale(self.root)
        if abs(S - self.S) < 0.01:
            return
        r, self.S = S / self.S, S
        self.root.tk.call("tk", "scaling", S * 96 / 72)
        for name in FONTS:  # 同じ pt で設定し直すと、新しい scaling で px が計算し直される
            f = tkfont.nametofont(name)
            f.configure(size=f.cget("size"))
        for key, *_ in self.COLS:
            self.tree.column(key, width=int(self.tree.column(key, "width") * r))
        self.apply_scale()
        if not self.hooked:  # DLL が無いときは、せめて大きさだけ合わせる（ドラッグ中は行き来することがある）
            self.root.geometry(f"{int(self.root.winfo_width() * r)}x{int(self.root.winfo_height() * r)}")

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
        counts, projs, self.total = {}, {}, 0
        for d, label in zip(self.docs, doc_labels([d.path for d in self.docs])):
            for _, _, v in d.refs:
                self.total += 1
                counts[v] = counts.get(v, 0) + 1
                if label not in projs.setdefault(v, []):
                    projs[v].append(label)
        self.rows = {v: {"count": c, "new": None, "note": "未解決", "proj": "、".join(projs[v])}
                     for v, c in counts.items() if not os.path.exists(v)}
        self.show_proj_column()
        self.refresh()
        if errors:
            messagebox.showwarning(APP, "読めなかったファイル:\n\n" + "\n".join(errors), parent=self.root)
        if self.docs and not self.rows:
            self.status.config(text="リンク切れはありません。")
        elif self.rows:
            self.status.config(text="「フォルダから探す」で移動先のフォルダを選ぶと、同じ名前のファイルを探します。")

    def show_proj_column(self):
        """「プロジェクト」列は .aup2 を 2 本以上開いたときだけ出す。"""
        many = len(self.docs) > 1
        self.tree.configure(displaycolumns=[c[0] for c in self.COLS if many or c[0] != "proj"])

    def refresh(self):
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for old, r in sorted(self.rows.items(), key=lambda kv: (kv[1]["proj"].lower(), kv[0].lower())):
            new = r["new"]
            tag = "none" if not new else "tie" if r["note"].startswith("候補") else "ok"
            self.tree.insert("", "end", iid=old, tags=(tag,), values=(
                r["note"], ntpath.basename(old), r["proj"], ntpath.dirname(old),
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


def shortcut(remove=False):
    """スタートメニューのショートカットを作る / 消す。AviUtl2 カタログから入れたときの起動口
    （カタログは exe を隠れたフォルダに置くだけなので、インストール手順から --install で呼ぶ）。"""
    lnk = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", APP + ".lnk")
    if remove:
        if os.path.exists(lnk):
            os.remove(lnk)
        return 0
    exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
    # パスと説明は環境変数で渡す（日本語や空白を PowerShell の引用符に通さない）
    env = dict(os.environ, A2R_LNK=lnk, A2R_EXE=exe, A2R_DESC="AviUtl2 の .aup2 で切れた素材のリンクを直す")
    ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:A2R_LNK); "
          "$s.TargetPath = $env:A2R_EXE; $s.WorkingDirectory = Split-Path $env:A2R_EXE; "
          "$s.Description = $env:A2R_DESC; $s.Save()")
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                          env=env, creationflags=0x08000000).returncode  # CREATE_NO_WINDOW


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--install", "--uninstall"):
        sys.exit(shortcut(remove=sys.argv[1] == "--uninstall"))
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
    S = window_scale(root)
    root.tk.call("tk", "scaling", S * 96 / 72)
    for name in FONTS:
        tkfont.nametofont(name).configure(family="Yu Gothic UI", size=10)
    app = App(root, S)
    if DND_FILES:
        root.drop_target_register(DND_FILES)
        root.dnd_bind("<<Drop>>", app.on_drop)
    if len(sys.argv) > 1:
        root.after(50, lambda: app.load(collect_aup2(sys.argv[1:])))
    root.mainloop()


if __name__ == "__main__":
    main()
