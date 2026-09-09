"""
setup_wizard.py — AI Cowork Setup Wizard

GUI tkinter cho phép user:
  1. Nhập API keys (Gemini, Anthropic, Groq, OpenAI, ...)
  2. Validate key còn hạn hay không
  3. Ghi .env
  4. Launch app (run-web.bat hoặc launch.py trực tiếp)

Đóng gói thành .exe bằng build_setup.bat (PyInstaller --onefile --windowed).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

def _get_project_root() -> Path:
    """Xác định thư mục gốc của project AI-COWORK.

    Khi chạy dưới PyInstaller (--onefile), sys.frozen = True và __file__
    nằm trong thư mục tạm _MEIPASS. Do đó cần tìm theo vị trí file .exe
    hoặc current working directory nơi có run-web.bat / launch.py.
    """
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir)
        candidates.append(exe_dir.parent)
    else:
        candidates.append(Path(__file__).resolve().parent)

    candidates.append(Path.cwd())

    for c in candidates:
        if (c / "run-web.bat").exists() or (c / "launch.py").exists():
            return c

    return candidates[0]


ROOT = _get_project_root()

# ── Màu sắc ───────────────────────────────────────────────────────────────────
BG = "#0f1117"
BG2 = "#1a1d27"
ACCENT = "#6c8ef7"
GREEN = "#4ade80"
RED = "#f87171"
YELLOW = "#fbbf24"
TEXT = "#e2e8f0"
TEXT_MUTED = "#94a3b8"
BORDER = "#2d3149"

# ── API key fields định nghĩa ──────────────────────────────────────────────────
FIELDS = [
    {
        "env_key": "GEMINI_API_KEY",
        "label": "Gemini API Key",
        "required": True,
        "hint": "aistudio.google.com/apikey  (miễn phí)",
        "link": "https://aistudio.google.com/apikey",
        "validate_url": "https://generativelanguage.googleapis.com/v1beta/models?key={key}",
        "prefix": "AIza",
    },
    {
        "env_key": "ANTHROPIC_API_KEY",
        "label": "Anthropic API Key",
        "required": False,
        "hint": "console.anthropic.com  (khuyến nghị — Claude)",
        "link": "https://console.anthropic.com/settings/keys",
        "prefix": "sk-ant-",
    },
    {
        "env_key": "GROQ_API_KEY",
        "label": "Groq API Key",
        "required": False,
        "hint": "console.groq.com/keys  (miễn phí, nhanh)",
        "link": "https://console.groq.com/keys",
        "prefix": "gsk_",
    },
    {
        "env_key": "OPENAI_API_KEY",
        "label": "OpenAI API Key",
        "required": False,
        "hint": "platform.openai.com/api-keys  (có phí)",
        "link": "https://platform.openai.com/api-keys",
        "prefix": "sk-",
    },
    {
        "env_key": "AION_API_KEY",
        "label": "AionLabs API Key",
        "required": False,
        "hint": "api.aionlabs.ai  (aion-2.0, aion-3.0, aion-3.0-mini, aion-rp-llama-3.1-8b)",
        "link": "https://api.aionlabs.ai/",
    },
    {
        "env_key": "CEREBRAS_API_KEY",
        "label": "Cerebras API Key",
        "required": False,
        "hint": "cloud.cerebras.ai  (Qwen 3.8 27B, Gemma 4 31B, GPT-OSS 120B)",
        "link": "https://cloud.cerebras.ai/",
    },
    {
        "env_key": "CLOUDFLARE_API_TOKEN",
        "label": "Cloudflare API Token",
        "required": False,
        "hint": "Workers AI — miễn phí (cần cả Account ID bên dưới)",
        "link": "https://dash.cloudflare.com/profile/api-tokens",
    },
    {
        "env_key": "CLOUDFLARE_ACCOUNT_ID",
        "label": "Cloudflare Account ID",
        "required": False,
        "hint": "Tìm trong URL dashboard.cloudflare.com/...",
        "link": "https://dash.cloudflare.com/",
    },
]


def _read_existing_env() -> dict[str, str]:
    """Đọc .env hiện tại nếu có."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return {}
    result = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip().strip('"').strip("'")
    return result


def _write_env(values: dict[str, str]) -> None:
    """Ghi .env, merge với nội dung cũ (giữ các key khác không bị mất)."""
    env_path = ROOT / ".env"
    existing = _read_existing_env()
    merged = {**existing, **{k: v for k, v in values.items() if v}}

    # Đọc template từ .env.example nếu có, để giữ format và comment
    example = ROOT / ".env.example"
    if not example.exists() and getattr(sys, "_MEIPASS", None):
        bundled = Path(sys._MEIPASS) / ".env.example"
        if bundled.exists():
            example = bundled
    if example.exists():
        lines = example.read_text(encoding="utf-8").splitlines()
        written_keys: set[str] = set()
        out_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                out_lines.append(line)
                continue
            if "=" in stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0].strip()
                val = merged.get(key, "")
                out_lines.append(f"{key}={val}")
                written_keys.add(key)
            else:
                out_lines.append(line)
        # Append bất kỳ key nào trong merged mà không có trong template
        for k, v in merged.items():
            if k not in written_keys:
                out_lines.append(f"{k}={v}")
        env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    else:
        lines = [f"{k}={v}" for k, v in merged.items() if v]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _validate_gemini(key: str) -> tuple[bool, str]:
    """Validate Gemini key bằng cách gọi API list models."""
    if not key.strip():
        return False, "Trống"
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key.strip()}"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            if r.status == 200:
                return True, "✓ Hợp lệ"
            return False, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code == 400:
            return False, "Key không hợp lệ (400)"
        if e.code == 403:
            return False, "Key bị từ chối (403)"
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, f"Lỗi: {str(e)[:40]}"


def _find_launcher() -> str | None:
    """Tìm run-web.bat hoặc launch.py."""
    for name in ("run-web.bat", "launch.py"):
        p = ROOT / name
        if p.exists():
            return str(p)
    return None


class SetupWizard(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("AI Cowork — Setup")
        self.configure(bg=BG)
        self.resizable(False, False)
        # Căn giữa màn hình
        w, h = 620, 620
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

        self._existing = _read_existing_env()
        self._entries: list[dict] = []  # [{entry, status_label, field}, ...]
        self._build_ui()

    # ── UI builder ────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        # Header
        header = tk.Frame(self, bg=ACCENT, height=6)
        header.pack(fill="x")

        title_frame = tk.Frame(self, bg=BG, pady=20)
        title_frame.pack(fill="x", padx=30)
        tk.Label(
            title_frame, text="🤖  AI Cowork Setup", font=("Segoe UI", 18, "bold"),
            bg=BG, fg=TEXT,
        ).pack(anchor="w")
        tk.Label(
            title_frame,
            text="Nhập ít nhất Gemini API Key để bắt đầu. Các key khác tùy chọn — thêm sau trong .env.",
            font=("Segoe UI", 9), bg=BG, fg=TEXT_MUTED, wraplength=560, justify="left",
        ).pack(anchor="w", pady=(4, 0))

        # Divider
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=30, pady=(0, 10))

        # Scrollable area cho fields
        canvas = tk.Canvas(self, bg=BG, highlightthickness=0, height=370)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self._scroll_frame = tk.Frame(canvas, bg=BG)
        self._scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self._scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True, padx=(30, 0))
        scrollbar.pack(side="right", fill="y", padx=(0, 30))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))

        for field in FIELDS:
            self._add_field(self._scroll_frame, field)

        # Bottom section
        bottom = tk.Frame(self, bg=BG, pady=15)
        bottom.pack(fill="x", padx=30, pady=(10, 0))

        tk.Frame(bottom, bg=BORDER, height=1).pack(fill="x", pady=(0, 15))

        self._status_var = tk.StringVar(value="Sẵn sàng — nhập key và nhấn Start.")
        tk.Label(
            bottom, textvariable=self._status_var,
            font=("Segoe UI", 9), bg=BG, fg=TEXT_MUTED,
        ).pack(anchor="w", pady=(0, 12))

        btn_frame = tk.Frame(bottom, bg=BG)
        btn_frame.pack(fill="x")

        # Validate button
        tk.Button(
            btn_frame, text="🔍  Kiểm tra key",
            font=("Segoe UI", 10), bg=BG2, fg=TEXT, relief="flat",
            activebackground=BORDER, activeforeground=TEXT,
            padx=16, pady=8, cursor="hand2",
            command=self._on_validate,
        ).pack(side="left", padx=(0, 10))

        # Open .env button
        tk.Button(
            btn_frame, text="📄  Mở .env",
            font=("Segoe UI", 10), bg=BG2, fg=TEXT_MUTED, relief="flat",
            activebackground=BORDER, activeforeground=TEXT,
            padx=12, pady=8, cursor="hand2",
            command=lambda: os.startfile(str(ROOT / ".env")) if (ROOT / ".env").exists() else None,
        ).pack(side="left", padx=(0, 10))

        # Start button (primary)
        self._start_btn = tk.Button(
            btn_frame, text="▶  Start App",
            font=("Segoe UI", 11, "bold"), bg=ACCENT, fg="white", relief="flat",
            activebackground="#4f6ee0", activeforeground="white",
            padx=24, pady=8, cursor="hand2",
            command=self._on_start,
        )
        self._start_btn.pack(side="right")

        # Version / footer
        tk.Label(
            bottom, text=f"Project: {ROOT}",
            font=("Segoe UI", 8), bg=BG, fg=TEXT_MUTED,
        ).pack(anchor="w", pady=(12, 0))

    def _add_field(self, parent: tk.Frame, field: dict) -> None:
        """Tạo một row field: label + entry + status + hint."""
        row = tk.Frame(parent, bg=BG, pady=8)
        row.pack(fill="x", padx=0, pady=(0, 2))

        label_row = tk.Frame(row, bg=BG)
        label_row.pack(fill="x")

        required = "  *" if field.get("required") else "  (tùy chọn)"
        req_color = RED if field.get("required") else TEXT_MUTED
        tk.Label(
            label_row, text=field["label"], font=("Segoe UI", 9, "bold"),
            bg=BG, fg=TEXT,
        ).pack(side="left")
        tk.Label(
            label_row, text=required, font=("Segoe UI", 8),
            bg=BG, fg=req_color,
        ).pack(side="left")

        entry_row = tk.Frame(row, bg=BG)
        entry_row.pack(fill="x", pady=(4, 0))

        existing_val = self._existing.get(field["env_key"], "")
        var = tk.StringVar(value=existing_val)
        entry = tk.Entry(
            entry_row, textvariable=var,
            font=("Consolas", 10), bg=BG2, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
            show="•" if "KEY" in field["env_key"] or "TOKEN" in field["env_key"] else "",
            width=52,
        )
        entry.pack(side="left", ipady=6, padx=(0, 8))

        # Toggle show/hide password
        if "KEY" in field["env_key"] or "TOKEN" in field["env_key"]:
            show_var = tk.BooleanVar(value=False)

            def _toggle(e=entry, sv=show_var):
                sv.set(not sv.get())
                e.config(show="" if sv.get() else "•")

            tk.Button(
                entry_row, text="👁", font=("Segoe UI", 9), bg=BG2, fg=TEXT_MUTED,
                relief="flat", cursor="hand2", padx=4, pady=4, command=_toggle,
            ).pack(side="left", padx=(0, 8))

        # Link button
        if field.get("link"):
            tk.Button(
                entry_row, text="?", font=("Segoe UI", 8), bg=BG2, fg=ACCENT,
                relief="flat", cursor="hand2", padx=4, pady=4,
                command=lambda url=field["link"]: webbrowser.open(url),
            ).pack(side="left")

        # Status label (✓/✗/...)
        status_lbl = tk.Label(row, text="", font=("Segoe UI", 8), bg=BG, fg=TEXT_MUTED)
        status_lbl.pack(anchor="w", pady=(2, 0))

        # Hint
        tk.Label(
            row, text=field.get("hint", ""),
            font=("Segoe UI", 8), bg=BG, fg=TEXT_MUTED,
        ).pack(anchor="w")

        self._entries.append({"var": var, "entry": entry, "status": status_lbl, "field": field})

    # ── Handlers ──────────────────────────────────────────────────────────────
    def _collect_values(self) -> dict[str, str]:
        return {e["field"]["env_key"]: e["var"].get().strip() for e in self._entries}

    def _on_validate(self) -> None:
        self._status_var.set("Đang kiểm tra keys...")
        self.update()

        def run_validate() -> None:
            for item in self._entries:
                key = item["var"].get().strip()
                field = item["field"]
                lbl = item["status"]
                if not key:
                    lbl.config(text="(trống)", fg=TEXT_MUTED)
                    continue
                # Check prefix
                prefix = field.get("prefix")
                if prefix and not key.startswith(prefix):
                    lbl.config(text=f"⚠ Key thường bắt đầu bằng '{prefix}'", fg=YELLOW)
                    continue
                # Validate Gemini online
                if field["env_key"] == "GEMINI_API_KEY":
                    ok, msg = _validate_gemini(key)
                    lbl.config(text=msg, fg=GREEN if ok else RED)
                else:
                    lbl.config(text="✓ Đã nhập", fg=GREEN)
            self._status_var.set("Kiểm tra xong.")

        threading.Thread(target=run_validate, daemon=True).start()

    def _on_start(self) -> None:
        values = self._collect_values()

        # Validate bắt buộc
        gemini = values.get("GEMINI_API_KEY", "")
        if not gemini:
            messagebox.showerror(
                "Thiếu key bắt buộc",
                "Gemini API Key là bắt buộc.\n\nLấy miễn phí tại: https://aistudio.google.com/apikey",
            )
            return

        # Ghi .env
        try:
            _write_env(values)
        except OSError as e:
            messagebox.showerror("Lỗi ghi file", f"Không ghi được .env:\n{e}")
            return

        self._status_var.set("Đã ghi .env — đang khởi động app...")
        self._start_btn.config(state="disabled", text="Đang chạy...")
        self.update()

        launcher = _find_launcher()
        if launcher is None:
            messagebox.showerror(
                "Không tìm thấy launcher",
                f"Không tìm thấy run-web.bat hoặc launch.py trong:\n{ROOT}",
            )
            self._start_btn.config(state="normal", text="▶  Start App")
            return

        def _launch() -> None:
            try:
                if launcher.endswith(".bat"):
                    subprocess.Popen(
                        ["cmd", "/c", launcher],
                        cwd=str(ROOT),
                        creationflags=subprocess.CREATE_NEW_CONSOLE,
                    )
                else:
                    py_exe = str(ROOT / ".venv" / "Scripts" / "python.exe")
                    if not Path(py_exe).exists():
                        py_exe = sys.executable if not getattr(sys, "frozen", False) else "python"
                    subprocess.Popen(
                        [py_exe, launcher],
                        cwd=str(ROOT),
                        creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
                    )
                # Mở browser sau 5 giây
                import time
                time.sleep(5)
                webbrowser.open("http://localhost:1420")
            except OSError as e:
                self.after(0, lambda: messagebox.showerror("Lỗi launch", str(e)))
                self.after(0, lambda: self._start_btn.config(state="normal", text="▶  Start App"))
                return
            # Đóng wizard
            self.after(500, self.destroy)

        threading.Thread(target=_launch, daemon=True).start()


def main() -> None:
    app = SetupWizard()
    app.mainloop()


if __name__ == "__main__":
    main()
