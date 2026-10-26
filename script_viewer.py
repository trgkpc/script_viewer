#!/usr/bin/env python3
"""Markdown presentation notes + rehearsal timer. Python 3.10+, tkinter."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import re
import sys
import time
import unicodedata


@dataclass(frozen=True)
class Slide:
    number: int
    text: str
    duration: int | None
    cumulative: int | None
    chapter: str = ""


def parse_duration(value: str) -> int:
    value = unicodedata.normalize("NFKC", value).replace("*", "").strip()
    value = re.sub(r"\s+", "", value)
    if re.fullmatch(r"\d+(?::\d{1,2}){1,2}", value):
        parts = [int(x) for x in value.split(":")]
        if any(x >= 60 for x in parts[1:]):
            raise ValueError(f"時間の形式を確認してください: {value}")
        total = 0
        for part in parts:
            total = total * 60 + part
        return total
    match = re.fullmatch(r"(?:(\d+)時間)?(?:(\d+)分)?(?:(\d+)秒)?", value)
    if match and any(part is not None for part in match.groups()):
        h, m, s = (int(part or 0) for part in match.groups())
        return h * 3600 + m * 60 + s
    if value.isdecimal():
        return int(value)
    raise ValueError(f"時間の形式を確認してください: {value!r}（例: 1分15秒 / 01:15）")


PAGE = re.compile(r"^\s*#{1,6}\s+\**p\s*(\d+)\s*[.:]?\s*(.*?)\s*$", re.I)


def parse_markdown(source: str) -> tuple[list[Slide], list[str]]:
    """Parse page headings; chapter text and code-fenced headings are not pages."""
    slides: list[Slide] = []
    warnings: list[str] = []
    numbers: set[int] = set()
    current: tuple[int, int | None, int | None, str] | None = None
    body: list[str] = []
    chapter = ""
    fence: str | None = None

    def flush() -> None:
        nonlocal current, body
        if current is not None:
            number, duration, cumulative, slide_chapter = current
            slides.append(Slide(number, "\n".join(body).strip(), duration, cumulative, slide_chapter))
        current, body = None, []

    for line_no, original in enumerate(source.lstrip("\ufeff").splitlines(), 1):
        line = original.rstrip()
        normalized = unicodedata.normalize("NFKC", line)
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            if current:
                body.append(line)
            continue
        if fence is not None:
            if current:
                body.append(line)
            continue
        match = PAGE.match(normalized)
        if match:
            flush()
            number = int(match.group(1))
            if number in numbers:
                raise ValueError(f"{line_no}行目: p{number} が重複しています。")
            if slides and number <= slides[-1].number:
                raise ValueError(f"{line_no}行目: ページ番号は昇順にしてください。")
            numbers.add(number)
            timing = match.group(2).replace("*", "").strip().rstrip("#").strip()
            duration = cumulative = None
            if timing:
                parts = timing.split("/")
                if len(parts) > 2:
                    raise ValueError(f"{line_no}行目: 時間は「このページ／ここまで」の2つまでです。")
                try:
                    duration = parse_duration(parts[0])
                    if len(parts) == 2:
                        cumulative = parse_duration(parts[1])
                except ValueError as exc:
                    raise ValueError(f"{line_no}行目: {exc}") from exc
            previous = slides[-1].cumulative if slides else 0
            expected = previous + duration if previous is not None and duration is not None else None
            if cumulative is None:
                cumulative = expected
            elif duration is not None and cumulative < duration:
                raise ValueError(f"{line_no}行目: 累計時間がこのページの時間より短くなっています。")
            elif slides and previous is not None and cumulative < previous:
                raise ValueError(f"{line_no}行目: 累計時間が前のページより短くなっています。")
            elif slides and number == slides[-1].number + 1 and expected is not None and cumulative != expected:
                warnings.append(f"p{number}: 持ち時間の合計と累計が一致しません。見出しの累計を使用します。")
            current = (number, duration, cumulative, chapter)
            continue
        # Chapter headings and their summary must not leak into the previous page.
        if re.match(r"^\s*#{1,2}\s+", normalized):
            flush()
            chapter = re.sub(r"^\s*#{1,2}\s+", "", line).strip().strip("*")
            continue
        if line.strip().startswith(":::"):
            continue
        if current:
            body.append(line)
    flush()
    if not slides:
        raise ValueError("ページが見つかりません。\n見出しを「### p1. 30秒／30秒」の形式で記載してください。")
    return slides, warnings


def read_slides(path: Path) -> tuple[list[Slide], list[str]]:
    try:
        return parse_markdown(path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise ValueError("原稿を UTF-8 形式の .md ファイルで保存してください。") from exc


def find_scripts(directory: Path) -> list[Path]:
    """Direct children only. Prefer actual manuscripts over the bundled demo."""
    candidates = sorted((p for p in directory.iterdir()
                         if p.is_file() and p.suffix.lower() == ".md"
                         and p.name.lower() not in {"readme.md", "agents.md"}),
                        key=lambda p: p.name.casefold())
    actual = [p for p in candidates if p.name != "sample.md"]
    return actual or candidates


def format_time(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    total = max(0, int(seconds))
    minutes, sec = divmod(total, 60)
    return f"{minutes:02d}:{sec:02d}"


class PresentationClock:
    """Monotonic clock, separate from GUI, with accumulated time for each page."""
    def __init__(self, count: int, now=time.monotonic):
        self.now = now
        self.elapsed = [0.0] * count
        self.index = 0
        self.start_index = 0
        self.started = False
        self.running = False
        self.finished = False
        self.anchor = self.now()

    def _commit(self) -> None:
        instant = self.now()
        if self.running:
            self.elapsed[self.index] += max(0.0, instant - self.anchor)
        self.anchor = instant

    def toggle(self) -> None:
        if self.finished:
            return
        self._commit()
        if not self.started:
            self.start_index = self.index
            self.started = True
        self.running = not self.running

    def go(self, index: int) -> None:
        if not 0 <= index < len(self.elapsed) or index == self.index:
            return
        self._commit()
        self.index = index

    def finish(self) -> None:
        if self.started:
            self._commit()
            self.running = False
            self.finished = True

    def snapshot(self) -> tuple[float, float]:
        extra = max(0.0, self.now() - self.anchor) if self.running else 0.0
        return self.elapsed[self.index] + extra, sum(self.elapsed) + extra


def session_target(slides: list[Slide], clock: PresentationClock) -> int | None:
    start = slides[clock.start_index if clock.started else clock.index]
    end = slides[clock.index].cumulative
    if start.cumulative is None or start.duration is None or end is None:
        return None
    target = end - (start.cumulative - start.duration)
    return target if target >= 0 else None


GLOBAL_KEYS = {"right": "next", "left": "previous", "page_down": "next", "page_up": "previous"}
LOCAL_KEYS = {"Right": "next", "Left": "previous", "Next": "next", "Prior": "previous"}


def local_navigation(keysym: str, global_active: bool) -> tuple[bool, str | None]:
    """When the listener owns navigation, consume the local duplicate only."""
    action = LOCAL_KEYS.get(keysym)
    return action is not None, action if not global_active else None


class GlobalNavigation:
    """Pass-through navigation listener. Callbacks enqueue actions, never touch Tk."""
    def __init__(self):
        self.events = SimpleQueue()
        self.listener = None
        self.enabled = False
        self.suspended = False
        self.error = ""
        self._epoch = 0
        self._token = None

    def start(self, keyboard_module=None) -> bool:
        self.stop()
        self.error = ""
        token = self._token = object()
        try:
            keyboard = keyboard_module or importlib.import_module("pynput.keyboard")

            def on_press(key, *_extra):
                # Only these four navigation keys become actions. No text is retained.
                epoch = self._epoch
                action = GLOBAL_KEYS.get(getattr(key, "name", None))
                if action and token is self._token and self.enabled and not self.suspended:
                    self.events.put((epoch, action))

            listener = keyboard.Listener(on_press=on_press, suppress=False)
            if not getattr(listener, "IS_TRUSTED", True):
                raise PermissionError("キーボード監視の許可がありません。OS の設定で起動元の Terminal / Python に入力監視・アクセシビリティの許可を設定し、アプリを再起動してください。")
            self.listener = listener
            self.enabled = True
            listener.start()
            return True
        except Exception as exc:
            self.stop()
            if isinstance(exc, ModuleNotFoundError) and exc.name == "pynput":
                self.error = "pynput が未インストールです。同じ Python で「python -m pip install -r requirements.txt」を実行して再起動してください。"
            else:
                self.error = str(exc) or type(exc).__name__
            return False

    def stop(self) -> None:
        self.enabled = False
        self._epoch += 1
        self._token = None
        listener, self.listener = self.listener, None
        if listener is not None:
            listener.stop()
        self.discard()

    def suspend(self, value: bool) -> None:
        self.suspended = value
        self._epoch += 1
        self.discard()

    def discard(self) -> None:
        while True:
            try:
                self.events.get_nowait()
            except Empty:
                break

    def poll(self) -> list[str]:
        if self.enabled and self.listener is not None and not self.listener.is_alive():
            try:
                self.listener.join(timeout=0)
            except Exception as exc:
                self.error = str(exc) or type(exc).__name__
            else:
                self.error = "キーボード監視が停止しました。OS の権限を確認し、監視をオンにし直してください。"
            self.stop()
        actions = []
        while True:
            try:
                epoch, action = self.events.get_nowait()
            except Empty:
                break
            if self.enabled and not self.suspended and epoch == self._epoch:
                actions.append(action)
        return actions


def launch(initial_path: Path | None = None) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, font, messagebox, ttk
    except ImportError:
        print("tkinter が必要です。Python の Tk 対応版を使用してください。", file=sys.stderr)
        return 1

    BG, CARD, TEXT, MUTED, ACCENT, RED = "#101723", "#1c2738", "#f1f5f9", "#a4b3c7", "#58dfc1", "#ff8585"

    class App:
        def __init__(self, root):
            self.root = root
            self.slides: list[Slide] = []
            self.path: Path | None = None
            self.clock: PresentationClock | None = None
            self.font_size = 24
            self.fullscreen = False
            self.navigation = GlobalNavigation()
            self.monitor_enabled = tk.BooleanVar(value=True)
            self.monitor_problem = ""
            self.dialog_depth = 0
            root.title("発表原稿ビューア")
            root.geometry("1120x880")
            root.minsize(850, 680)
            root.configure(bg=BG)
            root.protocol("WM_DELETE_WINDOW", self.close)
            available = set(font.families(root))
            self.family = next((f for f in ("Yu Gothic UI", "Yu Gothic", "Hiragino Sans", "Noto Sans CJK JP", "Meiryo") if f in available), "TkDefaultFont")
            self.script_font = font.Font(root=root, family=self.family, size=self.font_size)
            self.normal_font = (self.family, 11)
            style = ttk.Style(root)
            style.theme_use("clam")
            style.configure("TButton", font=self.normal_font, padding=(12, 8), background=CARD, foreground=TEXT, borderwidth=0)
            style.map("TButton", background=[("active", "#30425b")], foreground=[("disabled", "#64748b")])
            style.configure("Start.TButton", background=ACCENT, foreground=BG, font=(self.family, 12, "bold"))
            style.map("Start.TButton", background=[("active", "#8cebd6")], foreground=[("disabled", "#64748b")])
            style.configure("TCombobox", font=self.normal_font, padding=5)
            style.configure("Monitor.TCheckbutton", font=self.normal_font, background=BG, foreground=TEXT)
            style.map("Monitor.TCheckbutton", background=[("active", BG)], foreground=[("active", TEXT)])
            style.configure("Time.Horizontal.TProgressbar", troughcolor=BG, background=ACCENT, borderwidth=0)

            top = tk.Frame(root, bg=BG)
            top.pack(fill="x", padx=24, pady=(20, 12))
            tk.Label(top, text="発表原稿", bg=BG, fg=TEXT, font=(self.family, 18, "bold")).pack(side="left")
            ttk.Button(top, text="原稿を開く", command=self.choose).pack(side="right")
            self.filename = tk.Label(top, text="同じフォルダの .md を読み込みます", bg=BG, fg=MUTED, font=self.normal_font)
            self.filename.pack(side="left", padx=18)

            monitor_bar = tk.Frame(root, bg=BG)
            monitor_bar.pack(fill="x", padx=24, pady=(0, 10))
            ttk.Checkbutton(monitor_bar, text="キーボード監視", style="Monitor.TCheckbutton",
                            variable=self.monitor_enabled, command=self.toggle_monitor, takefocus=False).pack(side="left")
            self.monitor_status = tk.Label(monitor_bar, text="準備中…", bg=BG, fg=MUTED, font=self.normal_font)
            self.monitor_status.pack(side="left", padx=12)
            ttk.Button(monitor_bar, text="監視の設定", command=self.monitor_help, takefocus=False).pack(side="right")

            self.chapter = tk.Label(root, text="", bg=BG, fg=MUTED, anchor="w", font=self.normal_font)
            self.chapter.pack(fill="x", padx=26)
            bar = tk.Frame(root, bg=BG)
            bar.pack(fill="x", padx=24, pady=(8, 12))
            self.page_label = tk.Label(bar, text="原稿を選択してください", bg=BG, fg=TEXT, font=(self.family, 20, "bold"))
            self.page_label.pack(side="left")
            self.combo = ttk.Combobox(bar, state="readonly", width=12, takefocus=False)
            self.combo.pack(side="right")
            self.combo.bind("<<ComboboxSelected>>", self.select_page)
            ttk.Button(bar, text="A＋", width=4, command=lambda: self.resize(2), takefocus=False).pack(side="right", padx=3)
            ttk.Button(bar, text="A−", width=4, command=lambda: self.resize(-2), takefocus=False).pack(side="right", padx=3)

            timers = tk.Frame(root, bg=BG)
            timers.pack(fill="x", padx=24, pady=(0, 12))
            timers.columnconfigure(0, weight=1, uniform="cards")
            timers.columnconfigure(1, weight=1, uniform="cards")
            self.page_card = self.make_card(timers, "このページ（このスライド）", 0)
            self.total_card = self.make_card(timers, "ここまで（トータル）", 1)
            self.context = tk.Label(root, text="", bg=BG, fg=MUTED, anchor="w", font=(self.family, 10))
            self.context.pack(fill="x", padx=26, pady=(0, 8))

            paper = tk.Frame(root, bg=CARD)
            paper.pack(fill="both", expand=True, padx=24)
            scrollbar = ttk.Scrollbar(paper)
            scrollbar.pack(side="right", fill="y")
            self.script = tk.Text(paper, wrap="char", width=1, height=1, font=self.script_font, bg=CARD, fg=TEXT,
                                  borderwidth=0, highlightthickness=0, padx=24, pady=20,
                                  spacing1=4, spacing3=10, insertwidth=0, cursor="arrow",
                                  selectbackground="#385471", yscrollcommand=scrollbar.set)
            self.script.pack(fill="both", expand=True)
            scrollbar.configure(command=self.script.yview)
            self.set_text("原稿の .md ファイルを、このアプリと同じフォルダに置いて起動してください。\n\n「原稿を開く」から選択することもできます。")

            bottom = tk.Frame(root, bg=BG)
            bottom.pack(fill="x", padx=24, pady=16)
            self.start = ttk.Button(bottom, text="スタート", style="Start.TButton", command=self.toggle, state="disabled", takefocus=False)
            self.start.pack(side="left")
            ttk.Button(bottom, text="リセット", command=self.reset, takefocus=False).pack(side="left", padx=8)
            self.next_button = ttk.Button(bottom, text="次へ →", command=self.next_page, state="disabled", takefocus=False)
            self.next_button.pack(side="right")
            self.previous_button = ttk.Button(bottom, text="← 前へ", command=self.previous_page, state="disabled", takefocus=False)
            self.previous_button.pack(side="right", padx=8)
            self.state = tk.Label(bottom, text="待機中", bg=BG, fg=ACCENT, font=(self.family, 11, "bold"))
            self.state.pack(side="left", padx=10)
            tk.Label(root, text="監視オン：← / → / PageUp / PageDown で他アプリと連動 ｜ このアプリ内：Space 一時停止・再開 / F11 全画面",
                     bg=BG, fg=MUTED, font=(self.family, 10)).pack(pady=(0, 14))

            # Binding before widget/class handlers also works when the text has focus.
            root.bind_class("PresenterKeys", "<KeyPress>", self.keypress)
            self.install_keys(root)
            root.after(100, self.tick)
            root.after(0, lambda: self.initial_load(initial_path))
            root.after(0, self.toggle_monitor)
            root.after(30, self.poll_navigation)

        def dialog(self, function, *args, **kwargs):
            # Dialog key events must not turn pages, even after a dialog closes.
            self.dialog_depth += 1
            self.navigation.suspend(True)
            try:
                return function(*args, **kwargs)
            finally:
                self.dialog_depth -= 1
                self.navigation.suspend(self.dialog_depth > 0)

        def toggle_monitor(self):
            self.monitor_problem = ""
            if self.monitor_enabled.get():
                if not self.navigation.start():
                    self.monitor_problem = self.navigation.error
                    self.monitor_enabled.set(False)
            else:
                self.navigation.stop()
            self.update_monitor_status()

        def update_monitor_status(self):
            if self.navigation.enabled:
                text = "オン：プレゼンアプリ操作中も ← / → で連動"
                color = ACCENT
                if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
                    text = "オン（Wayland：Xwayland アプリのみ対応）"
            elif self.monitor_problem:
                text, color = "監視できません：「監視の設定」を確認", RED
            else:
                text, color = "オフ：このアプリ内のキーだけ有効", MUTED
            self.monitor_status.configure(text=text, fg=color)

        def monitor_help(self):
            details = (self.monitor_problem + "\n\n") if self.monitor_problem else ""
            self.dialog(messagebox.showinfo, "キーボード監視の設定", details +
                        "初回の準備：\npython -m pip install -r requirements.txt\n\n"
                        "macOS：システム設定 → プライバシーとセキュリティで、起動に使う Terminal / Python の「入力監視」「アクセシビリティ」を確認し、再起動してください。\n\n"
                        "Linux：X11 を使用してください。Wayland では Xwayland 上のアプリのキーだけを受け取れる場合があります。\n\n"
                        "監視中は左右キーと PageUp / PageDown がプレゼンアプリにそのまま届き、原稿も切り替わります。原稿の開始ページとスライドを合わせてからスタートしてください。\n\n"
                        "Space は原稿アプリにフォーカスがあるときだけ、タイマーの一時停止・再開に使います。",
                        parent=self.root)

        def poll_navigation(self):
            for action in self.navigation.poll():
                if self.dialog_depth == 0:
                    self.apply_navigation(action)
            if self.navigation.error and self.monitor_enabled.get():
                self.monitor_problem = self.navigation.error
                self.monitor_enabled.set(False)
                self.update_monitor_status()
            self.root.after(30, self.poll_navigation)

        def apply_navigation(self, action):
            if action == "next":
                self.next_page()
            elif action == "previous":
                self.previous_page()

        def install_keys(self, widget):
            widget.bindtags(("PresenterKeys",) + widget.bindtags())
            for child in widget.winfo_children():
                self.install_keys(child)

        def make_card(self, parent, title, column):
            frame = tk.Frame(parent, bg=CARD, padx=18, pady=12)
            frame.grid(row=0, column=column, sticky="ew", padx=(0, 6) if column == 0 else (6, 0))
            tk.Label(frame, text=title, bg=CARD, fg=MUTED, font=(self.family, 12, "bold")).pack(anchor="w")
            elapsed = tk.Label(frame, text="00:00", bg=CARD, fg=TEXT, font=(self.family, 29, "bold"))
            elapsed.pack(anchor="w")
            budget = tk.Label(frame, text="目安 —", bg=CARD, fg=ACCENT, font=(self.family, 29, "bold"))
            budget.pack(anchor="w")
            seconds = tk.Label(frame, text="", bg=CARD, fg=MUTED, font=self.normal_font)
            seconds.pack(anchor="w")
            remaining = tk.Label(frame, text="残り —", bg=CARD, fg=ACCENT, font=self.normal_font)
            remaining.pack(anchor="w", pady=(6, 8))
            progress = ttk.Progressbar(frame, style="Time.Horizontal.TProgressbar", maximum=100, value=0)
            progress.pack(fill="x")
            return elapsed, budget, seconds, remaining, progress

        def set_text(self, text):
            self.script.configure(state="normal")
            self.script.delete("1.0", "end")
            self.script.insert("1.0", text or "（このページの原稿は空です）")
            self.script.configure(state="disabled")
            self.script.yview_moveto(0)

        def initial_load(self, explicit):
            if explicit:
                self.load(explicit)
                return
            try:
                files = find_scripts(Path(__file__).resolve().parent)
            except OSError as exc:
                self.dialog(messagebox.showerror, "フォルダを開けません", str(exc), parent=self.root)
                return
            if len(files) == 1:
                self.load(files[0])
            elif len(files) > 1:
                self.choose()

        def choose(self):
            path = self.dialog(filedialog.askopenfilename, parent=self.root, title="発表原稿の Markdown ファイルを選択",
                                             initialdir=str(Path(__file__).resolve().parent),
                                             filetypes=[("Markdown", "*.md *.MD"), ("All files", "*")])
            if path:
                self.load(Path(path))

        def load(self, path):
            try:
                slides, warnings = read_slides(path)
            except (OSError, ValueError) as exc:
                self.dialog(messagebox.showerror, "原稿を読み込めません", str(exc), parent=self.root)
                return
            if self.clock and self.clock.started and not self.dialog(messagebox.askyesno, "原稿を変更", "計測をリセットして原稿を変更しますか？", parent=self.root):
                return
            self.path, self.slides = path, slides
            self.clock = PresentationClock(len(slides))
            self.filename.configure(text=path.name + ("（動作確認用）" if path.name == "sample.md" else ""))
            self.combo.configure(values=[f"p{s.number}" for s in slides])
            self.root.title(f"発表原稿ビューア — {path.name}")
            self.render()
            if warnings:
                self.dialog(messagebox.showwarning, "時間設定の確認", "\n".join(warnings), parent=self.root)

        def render(self):
            if not self.clock:
                return
            index = self.clock.index
            slide = self.slides[index]
            self.page_label.configure(text=f"p{slide.number}   ·   {index + 1} / {len(self.slides)}")
            self.chapter.configure(text=slide.chapter)
            self.combo.current(index)
            self.set_text(slide.text)
            self.previous_button.configure(state="normal" if index > 0 else "disabled")
            self.refresh()

        def update_card(self, card, elapsed, target):
            value, budget, seconds, remaining, progress = card
            over = target is not None and elapsed > target
            value.configure(text=f"経過 {format_time(elapsed)}", fg=RED if over else TEXT)
            budget.configure(text=f"目安 {format_time(target)}")
            seconds.configure(text=f"持ち時間 {target:,}秒" if target is not None else "目安は未設定")
            if target is None:
                remaining.configure(text="残り —", fg=MUTED)
            elif over:
                remaining.configure(text=f"超過 ＋{format_time(elapsed - target)}", fg=RED)
            else:
                remaining.configure(text=f"残り {format_time(target - int(elapsed))}", fg=ACCENT)
            progress.configure(value=min(100, elapsed / target * 100) if target else (100 if over else 0))

        def refresh(self):
            if not self.clock:
                return
            clock = self.clock
            page_elapsed, total_elapsed = clock.snapshot()
            slide = self.slides[clock.index]
            self.update_card(self.page_card, page_elapsed, slide.duration)
            self.update_card(self.total_card, total_elapsed, session_target(self.slides, clock))
            start_index = clock.start_index if clock.started else clock.index
            self.context.configure(text=f"p{self.slides[start_index].number}から計測 ｜ 「ここまで」の目安は、このページの終了時点 ｜ 原稿の累計 {format_time(slide.cumulative)}")
            label = "終了" if clock.finished else "一時停止" if clock.running else "再開" if clock.started else "スタート"
            self.start.configure(text=label, state="disabled" if clock.finished else "normal")
            state = "発表終了" if clock.finished else "計測中" if clock.running else "一時停止中" if clock.started else "開始前"
            self.state.configure(text=state)
            last = clock.index == len(self.slides) - 1
            self.next_button.configure(text="終了 ■" if last else "次へ →",
                                       state="disabled" if last and (not clock.started or clock.finished) else "normal")

        def tick(self):
            self.refresh()
            self.root.after(100, self.tick)

        def toggle(self):
            if self.clock:
                self.clock.toggle()
                self.refresh()

        def next_page(self):
            if not self.clock:
                return
            if self.clock.index == len(self.slides) - 1:
                self.clock.finish()
            else:
                self.clock.go(self.clock.index + 1)
            self.render()

        def previous_page(self):
            if self.clock:
                self.clock.go(self.clock.index - 1)
                self.render()

        def select_page(self, _event=None):
            if self.clock:
                self.clock.go(self.combo.current())
                self.render()
                self.root.focus_set()

        def reset(self):
            if not self.clock:
                return
            if self.clock.started and not self.dialog(messagebox.askyesno, "リセット", "計測をゼロに戻し、最初のページに戻りますか？", parent=self.root):
                return
            self.clock = PresentationClock(len(self.slides))
            self.render()

        def resize(self, delta):
            self.font_size = min(48, max(14, self.font_size + delta))
            self.script_font.configure(size=self.font_size)

        def keypress(self, event):
            # Open dialogs retain their normal keyboard behavior.
            if event.widget.winfo_toplevel() != self.root:
                return None
            handled, action = local_navigation(event.keysym, self.navigation.enabled)
            if handled:
                if action:
                    self.apply_navigation(action)
                return "break"
            actions = {"space": self.toggle, "Up": lambda: self.script.yview_scroll(-2, "units"),
                       "Down": lambda: self.script.yview_scroll(2, "units"),
                       "plus": lambda: self.resize(2), "equal": lambda: self.resize(2),
                       "minus": lambda: self.resize(-2)}
            if event.keysym in actions:
                actions[event.keysym]()
                return "break"
            if event.keysym in {"F11", "Escape"}:
                self.fullscreen = not self.fullscreen if event.keysym == "F11" else False
                self.root.attributes("-fullscreen", self.fullscreen)
                return "break"
            return None

        def close(self):
            if self.clock and self.clock.running and not self.dialog(messagebox.askyesno, "終了", "計測中です。アプリを終了しますか？", parent=self.root):
                return
            self.navigation.stop()
            self.root.destroy()

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"画面を開けません。デスクトップ環境で起動してください。\n{exc}", file=sys.stderr)
        return 1
    App(root)
    root.mainloop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Markdown の発表原稿をページ単位で表示し、時間を計測します。")
    parser.add_argument("markdown", nargs="?", type=Path, help="原稿 .md（省略するとアプリと同じフォルダから選択）")
    parser.add_argument("--check", action="store_true", help="画面を開かずに原稿の形式を確認")
    args = parser.parse_args()
    if args.check:
        if args.markdown is None:
            parser.error("--check には原稿のファイル名が必要です。")
        try:
            slides, warnings = read_slides(args.markdown)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        for slide in slides:
            print(f"p{slide.number}: このページ {format_time(slide.duration)} / ここまで {format_time(slide.cumulative)}")
        for warning in warnings:
            print(f"注意: {warning}", file=sys.stderr)
        print(f"{len(slides)}ページを読み込みました。")
        return 0
    return launch(args.markdown)


if __name__ == "__main__":
    raise SystemExit(main())
