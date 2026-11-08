"""
Chat Controls - let YouTube live chat control a VirtualBox OR VMware VM.

Setup (Windows 11):
    1. Install Python 3.10+ from python.org (tick "Add to PATH")
    2. pip install pytchat
    3. python chat_controls.py

Only run this against a VM you are happy to have wrecked. Take a snapshot first,
and set the VM network adapter to "Not attached" or "Host-only".
"""
import ctypes
import glob
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk, messagebox, filedialog

try:
    import pytchat
except ImportError:
    pytchat = None

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".chat_controls.json")
NOWIN = 0x08000000  # CREATE_NO_WINDOW
DEFAULT_EXE = {
    "VirtualBox": r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe",
    "VMware": r"C:\Program Files\VMware\VMware Workstation\vmrun.exe",
}

# ---------------------------------------------------------------- scancodes
# value = (extended?, make code)
KEYS = {"esc": (0, 0x01), "backspace": (0, 0x0E), "tab": (0, 0x0F),
        "enter": (0, 0x1C), "ctrl": (0, 0x1D), "shift": (0, 0x2A),
        "alt": (0, 0x38), "space": (0, 0x39), "capslock": (0, 0x3A),
        "f11": (0, 0x57), "f12": (0, 0x58), "home": (1, 0x47), "up": (1, 0x48),
        "pgup": (1, 0x49), "left": (1, 0x4B), "right": (1, 0x4D),
        "end": (1, 0x4F), "down": (1, 0x50), "pgdn": (1, 0x51),
        "insert": (1, 0x52), "del": (1, 0x53), "win": (1, 0x5B),
        "menu": (1, 0x5D), "-": (0, 0x0C), "=": (0, 0x0D), "[": (0, 0x1A),
        "]": (0, 0x1B), "\\": (0, 0x2B), ";": (0, 0x27), "'": (0, 0x28),
        ",": (0, 0x33), ".": (0, 0x34), "/": (0, 0x35)}
for i, ch in enumerate("1234567890"):
    KEYS[ch] = (0, 0x02 + i)
for row, start in (("qwertyuiop", 0x10), ("asdfghjkl", 0x1E), ("zxcvbnm", 0x2C)):
    for i, ch in enumerate(row):
        KEYS[ch] = (0, start + i)
for n in range(1, 11):
    KEYS[f"f{n}"] = (0, 0x3A + n)
ALIASES = {"return": "enter", "escape": "esc", "delete": "del", "control": "ctrl",
           "windows": "win", "super": "win", "pageup": "pgup", "pagedown": "pgdn",
           "bksp": "backspace", "spacebar": "space", "minus": "-", "equals": "="}


def resolve_keys(names):
    out = []
    for n in names:
        n = ALIASES.get(n.lower(), n.lower())
        if n not in KEYS:
            raise ValueError(f"unsupported key '{n}'")
        out.append(KEYS[n])
    return out


# ---------------------------------------------------------------- host input (VMware + mouse)
user32 = ctypes.windll.user32
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def send_scan(ext, code, up):
    flags = 0x0008 | (0x0001 if ext else 0) | (0x0002 if up else 0)
    inp = INPUT(1, _U(ki=KEYBDINPUT(0, code, flags, 0, 0)))
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
    time.sleep(0.02)


def host_press(names):
    keys = resolve_keys(names)
    for ext, code in keys:
        send_scan(ext, code, False)
    for ext, code in reversed(keys):
        send_scan(ext, code, True)


def host_type(text):
    for ch in text:
        if ch == "\n":
            host_press(["enter"]); continue
        r = user32.VkKeyScanW(ord(ch))
        if r == -1:
            continue
        vk, shift = r & 0xFF, bool(r & 0x100)
        sc = user32.MapVirtualKeyW(vk, 0)
        if shift:
            send_scan(0, 0x2A, False)
        send_scan(0, sc, False); send_scan(0, sc, True)
        if shift:
            send_scan(0, 0x2A, True)


DEBUG = [True]
LOGFN = [print]
AUTOFOCUS = [True]


def focus_window(needles):
    """Finds the VMware window for this VM and brings it to front only if it is not
    already in front. Never moves/resizes it; restores it only if minimized."""
    if isinstance(needles, str):
        needles = [needles]
    wins = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if "vmware" in buf.value.lower():
                wins.append((hwnd, buf.value))
        return True
    user32.EnumWindows(proto(cb), 0)
    match = [w for w in wins if any(n.lower() in w[1].lower() for n in needles if n)]
    if not match:
        seen = ", ".join(f"'{t}'" for _, t in wins) or "none"
        raise RuntimeError(f"no VMware window titled like {needles}. VMware windows open: {seen}")
    hwnd, title = match[0]
    if not AUTOFOCUS[0]:
        return
    if user32.GetForegroundWindow() == hwnd:
        LOGFN[0](f"  VMware window '{title}' already in front")
        return
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    user32.keybd_event(0x12, 0, 0, 0); user32.keybd_event(0x12, 0, 2, 0)  # ALT trick
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    ok = user32.GetForegroundWindow() == hwnd
    LOGFN[0](f"  focused VMware window '{title}'" + ("" if ok else "  (WINDOWS REFUSED to focus it - click it yourself)"))


def move_mouse(dx, dy):
    """Moves the HOST cursor. Works while it is over the VM window with mouse integration."""
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.SetCursorPos(pt.x + dx, pt.y + dy)


def sh(args, timeout=60):
    if DEBUG[0]:
        a = list(args)
        for i in range(1, len(a)):
            if a[i - 1] in ("--password", "-gp"):
                a[i] = "****"
        LOGFN[0]("  $ " + " ".join(a)[:170])
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                       creationflags=NOWIN)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200] or f"exit {r.returncode}")
    return r.stdout


def kill_procs(names):
    for n in names:
        subprocess.run(["taskkill", "/F", "/IM", n], capture_output=True, creationflags=NOWIN)


# ---------------------------------------------------------------- backends
class VBoxBackend:
    name = "VirtualBox"

    def __init__(self, exe, vm, gu="", gp="", snap=""):
        self.exe, self.vm, self.gu, self.gp, self.snap = exe, vm, gu, gp, snap

    def run(self, *a, timeout=60):
        return sh([self.exe, *a], timeout)

    @staticmethod
    def list_vms(exe):
        return re.findall(r'^"(.+?)" \{', sh([exe, "list", "vms"]), re.M)

    def press(self, names):
        codes = resolve_keys(names)
        out = []
        for e, c in codes:
            out += ([0xE0] if e else []) + [c]
        for e, c in reversed(codes):
            out += ([0xE0] if e else []) + [c | 0x80]
        self.run("controlvm", self.vm, "keyboardputscancode", *[f"{b:02x}" for b in out])

    def type_text(self, text, enter=False):
        if text:
            self.run("controlvm", self.vm, "keyboardputstring", text)
        if enter:
            self.press(["enter"])

    def start(self):
        subprocess.Popen([self.exe, "startvm", self.vm, "--type", "gui"], creationflags=NOWIN)

    def reset(self):
        self.run("controlvm", self.vm, "reset")

    def poweroff(self):
        self.run("controlvm", self.vm, "poweroff")

    def restore(self):
        try:
            self.poweroff()
        except RuntimeError:
            pass
        time.sleep(3)
        if self.snap:
            self.run("snapshot", self.vm, "restore", self.snap, timeout=300)
        else:
            self.run("snapshot", self.vm, "restorecurrent", timeout=300)
        self.start()

    def run_exe(self, path):
        self.run("guestcontrol", self.vm, "run", "--exe", path,
                 "--username", self.gu, "--password", self.gp, timeout=30)

    def kill(self):
        kill_procs(["VirtualBoxVM.exe", "VBoxHeadless.exe", "VirtualBox.exe"])


class VMwareBackend:
    """Uses vmrun for power/snapshot/guest-exe, and host keyboard injection into the
    VMware window for keys and typing (vmrun cannot send arbitrary keys)."""
    name = "VMware"

    def __init__(self, exe, vm, gu="", gp="", snap=""):
        self.exe, self.vm, self.gu, self.gp, self.snap = exe, vm, gu, gp, snap
        self.titles = [os.path.splitext(os.path.basename(vm))[0]]
        try:
            for line in open(vm, encoding="utf-8", errors="ignore"):
                m = re.match(r'\s*displayName\s*=\s*"(.*)"', line, re.I)
                if m:
                    self.titles.insert(0, m.group(1))
                    break
        except OSError:
            pass

    def run(self, *a, timeout=60, auth=False):
        base = [self.exe, "-T", "ws"]
        if auth:
            base += ["-gu", self.gu, "-gp", self.gp]
        return sh(base + list(a), timeout)

    @staticmethod
    def list_vms(exe):
        vms = set()
        try:
            for line in sh([exe, "-T", "ws", "list"]).splitlines()[1:]:
                if line.strip().lower().endswith(".vmx"):
                    vms.add(line.strip())
        except Exception:
            pass
        docs = os.path.join(os.path.expanduser("~"), "Documents", "Virtual Machines")
        vms.update(glob.glob(os.path.join(docs, "**", "*.vmx"), recursive=True))
        return sorted(vms)

    def press(self, names):
        names = [n.lower() for n in names]
        # Ctrl+Alt+Del is intercepted by the host; VMware's guest equivalent is Ctrl+Alt+Insert
        if {"ctrl", "alt"} <= set(names) and ("del" in names or "delete" in names):
            names = ["insert" if n in ("del", "delete") else n for n in names]
        focus_window(self.titles)
        LOGFN[0]("  host keys: " + "+".join(names))
        host_press(names)

    def type_text(self, text, enter=False):
        focus_window(self.titles)
        LOGFN[0](f"  host typing {len(text)} chars")
        host_type(text)
        if enter:
            host_press(["enter"])

    def start(self):
        subprocess.Popen([self.exe, "-T", "ws", "start", self.vm, "gui"], creationflags=NOWIN)

    def reset(self):
        self.run("reset", self.vm, "hard")

    def poweroff(self):
        self.run("stop", self.vm, "hard")

    def restore(self):
        try:
            self.poweroff()
        except RuntimeError:
            pass
        time.sleep(3)
        snap = self.snap
        if not snap:
            lines = self.run("listSnapshots", self.vm).splitlines()[1:]
            if not lines:
                raise RuntimeError("VM has no snapshots")
            snap = lines[0].strip()
        self.run("revertToSnapshot", self.vm, snap, timeout=300)
        self.start()

    def run_exe(self, path):
        self.run("runProgramInGuest", self.vm, "-noWait", "-activeWindow",
                 "-interactive", path, timeout=30, auth=True)

    def run_shell(self, arg):
        """Like Win+R: `cmd /c start "" <arg>` inside the guest (needs Tools + guest login)."""
        args = [arg.strip('"')] if arg.startswith('"') else arg.split()
        self.run("runProgramInGuest", self.vm, "-noWait", "-activeWindow", "-interactive",
                 r"C:\Windows\System32\cmd.exe", "/c", "start", "", *args,
                 timeout=30, auth=True)

    def kill(self):
        kill_procs(["vmware-vmx.exe", "vmware.exe", "vmplayer.exe"])


BACKENDS = {"VirtualBox": VBoxBackend, "VMware": VMwareBackend}


# ---------------------------------------------------------------- app
class App(tk.Tk):
    COMMANDS = ["run", "send", "key", "wait", "combo", "restore", "startvm",
                "restart", "shutdown", "killvm", "vrun", "up", "down", "left", "right"]

    def __init__(self):
        super().__init__()
        self.title("Chat Controls")
        self.geometry("840x760")
        self.logq, self.cmdq = queue.Queue(), queue.Queue()
        self.stop_evt = threading.Event()
        self.votes = {"restart": {}, "shutdown": {}, "restore": {}}
        self.last_cmd = {}
        self.be = None
        self.exes = dict(DEFAULT_EXE)
        self.vms = {"VirtualBox": "", "VMware": ""}
        self.cur_hv = "VirtualBox"
        LOGFN[0] = self.log
        self.build_ui()
        self.load_cfg()
        self.after(100, self.pump_log)
        threading.Thread(target=self.worker, daemon=True).start()
        if pytchat is None:
            self.log("pytchat not installed -> run: pip install pytchat")

    # ---- UI
    def build_ui(self):
        pad = {"padx": 6, "pady": 3}
        f1 = ttk.LabelFrame(self, text="1. Hypervisor and virtual machine")
        f1.pack(fill="x", padx=8, pady=4)
        self.hv, self.exe, self.vm = tk.StringVar(value="VirtualBox"), tk.StringVar(), tk.StringVar()
        ttk.Label(f1, text="Hypervisor").grid(row=0, column=0, sticky="w", **pad)
        hvbox = ttk.Combobox(f1, textvariable=self.hv, values=list(BACKENDS),
                             state="readonly", width=14)
        hvbox.grid(row=0, column=1, sticky="w", **pad)
        hvbox.bind("<<ComboboxSelected>>", lambda _e: self.switch_hv())
        self.exe_lbl = ttk.Label(f1, text="VBoxManage.exe")
        self.exe_lbl.grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(f1, textvariable=self.exe, width=62).grid(row=1, column=1, columnspan=2, **pad)
        ttk.Label(f1, text="VM").grid(row=2, column=0, sticky="w", **pad)
        self.vmbox = ttk.Combobox(f1, textvariable=self.vm, width=59)
        self.vmbox.grid(row=2, column=1, sticky="w", **pad)
        bf = ttk.Frame(f1)
        bf.grid(row=2, column=2, **pad)
        ttk.Button(bf, text="Refresh", command=self.refresh_vms).pack(side="left")
        self.browse_btn = ttk.Button(bf, text="Browse .vmx", command=self.browse_vmx)
        self.browse_btn.pack(side="left", padx=4)
        self.hint = ttk.Label(f1, foreground="#888", wraplength=780)
        self.hint.grid(row=3, column=0, columnspan=3, sticky="w", **pad)

        f2 = ttk.LabelFrame(self, text="2. YouTube chat")
        f2.pack(fill="x", padx=8, pady=4)
        self.video = tk.StringVar()
        ttk.Label(f2, text="Live stream URL or video ID").grid(row=0, column=0, **pad)
        ttk.Entry(f2, textvariable=self.video, width=50).grid(row=0, column=1, **pad)
        self.modok = tk.BooleanVar(value=True)
        ttk.Checkbutton(f2, text="Moderators may use !vrun",
                        variable=self.modok).grid(row=1, column=1, sticky="w", **pad)
        self.autofocus = tk.BooleanVar(value=True)
        ttk.Checkbutton(f2, text="VMware: bring VM window to front if it isn't already "
                        "(turn off to never touch windows)", variable=self.autofocus,
                        command=self.sync_focus).grid(row=2, column=1, sticky="w", **pad)

        f3 = ttk.LabelFrame(self, text="3. Settings")
        f3.pack(fill="x", padx=8, pady=4)
        self.guser = tk.StringVar(value="Administrator")
        self.gpass = tk.StringVar()
        self.snap = tk.StringVar(value="chatvmrestore")
        self.votes_needed, self.vote_window = tk.IntVar(value=2), tk.IntVar(value=60)
        self.cooldown, self.maxpx = tk.DoubleVar(value=1.0), tk.IntVar(value=1000)
        rows = [("Guest username (for !vrun)", self.guser, {}),
                ("Guest password", self.gpass, {"show": "*"}),
                ("Snapshot for !restore", self.snap, {}),
                ("Votes needed (restart/shutdown/restore)", self.votes_needed, {}),
                ("Vote window (seconds)", self.vote_window, {}),
                ("Per-user cooldown (seconds)", self.cooldown, {}),
                ("Max mouse move (pixels)", self.maxpx, {})]
        for i, (lbl, var, kw) in enumerate(rows):
            r, c = divmod(i, 2)
            ttk.Label(f3, text=lbl).grid(row=r, column=c * 2, sticky="w", **pad)
            ttk.Entry(f3, textvariable=var, width=18, **kw).grid(row=r, column=c * 2 + 1, **pad)

        f4 = ttk.LabelFrame(self, text="Enabled commands")
        f4.pack(fill="x", padx=8, pady=4)
        self.enabled = {}
        for i, c in enumerate(self.COMMANDS):
            v = tk.BooleanVar(value=True)
            self.enabled[c] = v
            ttk.Checkbutton(f4, text="!" + c, variable=v).grid(row=i // 8, column=i % 8,
                                                             sticky="w", padx=4)

        f5 = ttk.Frame(self)
        f5.pack(fill="x", padx=8, pady=4)
        self.btn = ttk.Button(f5, text="Start listening", command=self.toggle)
        self.btn.pack(side="left")
        ttk.Button(f5, text="Save settings", command=self.save_cfg).pack(side="left", padx=6)
        ttk.Button(f5, text="Clear queue", command=self.clear_queue).pack(side="left")
        ttk.Button(f5, text="Check VM", command=self.check_vm).pack(side="left", padx=6)
        self.test = tk.StringVar()
        e = ttk.Entry(f5, textvariable=self.test, width=36)
        e.pack(side="right")
        e.bind("<Return>", lambda _e: self.run_test())
        ttk.Label(f5, text="Test as owner:").pack(side="right", padx=4)

        self.logbox = tk.Text(self, height=12, state="disabled", bg="#111", fg="#ddd")
        self.logbox.pack(fill="both", expand=True, padx=8, pady=4)
        self.refresh_hv_labels()

    def sync_focus(self):
        AUTOFOCUS[0] = self.autofocus.get()

    def refresh_hv_labels(self):
        vbox = self.hv.get() == "VirtualBox"
        self.exe_lbl.configure(text="VBoxManage.exe" if vbox else "vmrun.exe")
        self.browse_btn.state(["disabled"] if vbox else ["!disabled"])
        self.hint.configure(text=(
            "VirtualBox: keys/typing are sent straight to the VM through VBoxManage (works even "
            "if the window is minimized)." if vbox else
            "VMware: keys/typing are injected into the VMware window on the host, so the VM window "
            "must be open and the guest should already have keyboard focus (click inside it once). "
            "Power, snapshots and !vrun use vmrun (!vrun needs VMware Tools + guest login)."))

    def switch_hv(self):
        self.exes[self.cur_hv] = self.exe.get()
        self.vms[self.cur_hv] = self.vm.get()
        self.cur_hv = self.hv.get()
        self.exe.set(self.exes[self.cur_hv])
        self.vm.set(self.vms[self.cur_hv])
        self.vmbox["values"] = []
        self.refresh_hv_labels()
        self.refresh_vms(quiet=True)

    # ---- config
    def save_cfg(self):
        self.exes[self.cur_hv] = self.exe.get()
        self.vms[self.cur_hv] = self.vm.get()
        d = {"hv": self.hv.get(), "exes": self.exes, "vms": self.vms,
             "video": self.video.get(), "guser": self.guser.get(), "snap": self.snap.get(),
             "votes": self.votes_needed.get(), "window": self.vote_window.get(),
             "cooldown": self.cooldown.get(), "maxpx": self.maxpx.get(),
             "modok": self.modok.get(), "autofocus": self.autofocus.get(),
             "enabled": {k: v.get() for k, v in self.enabled.items()}}
        with open(CONFIG_FILE, "w") as f:
            json.dump(d, f)  # password intentionally not saved
        self.log("Settings saved.")

    def load_cfg(self):
        try:
            d = json.load(open(CONFIG_FILE))
        except Exception:
            d = {}
        self.exes.update(d.get("exes", {}))
        if self.exes.get("VMware", "").startswith(r"C:\Program Files (x86)\VMware"):
            self.exes["VMware"] = DEFAULT_EXE["VMware"]  # replace the old saved default
        self.vms.update(d.get("vms", {}))
        self.cur_hv = d.get("hv", "VirtualBox")
        self.hv.set(self.cur_hv)
        self.exe.set(self.exes[self.cur_hv]); self.vm.set(self.vms[self.cur_hv])
        self.video.set(d.get("video", "")); self.guser.set(d.get("guser") or "Administrator")
        self.snap.set(d.get("snap") or "chatvmrestore"); self.votes_needed.set(d.get("votes", 2))
        self.vote_window.set(d.get("window", 60)); self.cooldown.set(d.get("cooldown", 1.0))
        self.maxpx.set(d.get("maxpx", 1000)); self.modok.set(d.get("modok", True))
        self.autofocus.set(d.get("autofocus", True)); self.sync_focus()
        for k, v in d.get("enabled", {}).items():
            if k in self.enabled:
                self.enabled[k].set(v)
        self.refresh_hv_labels()

    # ---- logging
    def log(self, msg):
        self.logq.put(time.strftime("[%H:%M:%S] ") + msg)

    def pump_log(self):
        try:
            while True:
                m = self.logq.get_nowait()
                self.logbox.configure(state="normal")
                self.logbox.insert("end", m + "\n")
                self.logbox.see("end")
                self.logbox.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(100, self.pump_log)

    # ---- VM list
    def refresh_vms(self, quiet=False):
        try:
            vms = BACKENDS[self.hv.get()].list_vms(self.exe.get())
            self.vmbox["values"] = vms
            if vms and not self.vm.get():
                self.vm.set(vms[0])
            self.log(f"Found {len(vms)} VM(s).")
        except Exception as e:
            if not quiet:
                messagebox.showerror(self.hv.get(), str(e))

    def browse_vmx(self):
        p = filedialog.askopenfilename(filetypes=[("VMware VM", "*.vmx")])
        if p:
            self.vm.set(os.path.normpath(p))

    def make_backend(self):
        return BACKENDS[self.hv.get()](self.exe.get(), self.vm.get(), self.guser.get(),
                                       self.gpass.get(), self.snap.get())

    # ---- listening
    def toggle(self):
        if self.btn["text"].startswith("Start"):
            if not self.vm.get():
                return messagebox.showwarning("Chat Controls", "Select a VM first.")
            global pytchat
            if pytchat is None:
                try:
                    import pytchat as _p
                    pytchat = _p
                except ImportError:
                    return messagebox.showerror(
                        "Chat Controls",
                        "pytchat is not installed for the Python running this app.\n\n"
                        "Run this in Command Prompt, then press Start again:\n\n"
                        f'"{sys.executable}" -m pip install pytchat')
            vid = self.video.get().strip()
            m = re.search(r"(?:v=|youtu\.be/|live/)([\w-]{11})", vid)
            vid = m.group(1) if m else vid
            if not vid:
                return messagebox.showwarning("Chat Controls", "Enter a stream URL / ID.")
            self.be = self.make_backend()
            self.stop_evt.clear()
            threading.Thread(target=self.chat_loop, args=(vid,), daemon=True).start()
            self.btn.configure(text="Stop listening")
        else:
            self.stop_evt.set()
            self.btn.configure(text="Start listening")
            self.log("Stopped.")

    def chat_loop(self, vid):
        try:
            chat = pytchat.create(video_id=vid, interruptable=False)
        except Exception as e:
            return self.log(f"Could not open chat: {e}")
        self.log(f"Connected to chat {vid}, controlling {self.be.name} VM '{self.be.vm}'.")
        first = True
        while chat.is_alive() and not self.stop_evt.is_set():
            items = chat.get().sync_items()
            if first:
                first = False
                self.log(f"Ignoring {len(items)} old chat message(s); listening for new ones...")
                continue
            for c in items:
                self.handle(c.author.name, c.message,
                            bool(c.author.isChatOwner), bool(c.author.isChatModerator))
            time.sleep(0.5)
        if not self.stop_evt.is_set():
            self.log("Chat connection ended (stream offline or YouTube blocked it). Press Start again.")

    def check_vm(self):
        try:
            be = self.make_backend()
            if be.name == "VirtualBox":
                out = be.run("showvminfo", be.vm, "--machinereadable")
                st = re.search(r'VMState="(\w+)"', out)
                state = st.group(1) if st else "unknown"
                ok = state == "running"
            else:
                state = "running" if be.vm.lower() in be.run("list").lower() else "not running"
                ok = state == "running"
            self.log(f"{be.name} VM '{be.vm}': {state}" +
                     ("" if ok else "  -> start the VM first; commands only work while it is running"))
        except Exception as e:
            self.log(f"VM check failed: {e}")

    def run_test(self):
        if not self.vm.get():
            return messagebox.showwarning("Chat Controls", "Select a VM first.")
        self.be = self.make_backend()  # always reflects current GUI settings
        self.log(f"Using {self.be.name} backend, VM '{self.be.vm}'")
        self.handle("OWNER(test)", self.test.get(), True, True)
        self.test.set("")

    def clear_queue(self):
        with self.cmdq.mutex:
            self.cmdq.queue.clear()
        self.log("Queue cleared.")

    # ---- parsing
    # Syntax: "!action" alone, or "!action <argument>" with a space after the action.
    # kind: none = no argument, text = any text, key = one key, keys = one or more keys,
    #       secs = number of seconds, px = whole number of pixels
    SPEC = {"run": ("text", "!run <command or path>   e.g. !run cmd"),
            "send": ("text", "!send <text>   e.g. !send ipconfig"),
            "key": ("key", "!key <key>   e.g. !key f11"),
            "combo": ("keys", "!combo <key> <key> ...   e.g. !combo ctrl alt del"),
            "wait": ("secs", "!wait <seconds>   e.g. !wait 3"),
            "vrun": ("text", "!vrun <full path>   e.g. !vrun C:\\Windows\\System32\\taskmgr.exe"),
            "up": ("px", "!up <pixels>   e.g. !up 421"),
            "down": ("px", "!down <pixels>   e.g. !down 400"),
            "left": ("px", "!left <pixels>   e.g. !left 671"),
            "right": ("px", "!right <pixels>   e.g. !right 453"),
            "startvm": ("none", "!startvm"), "restart": ("none", "!restart"),
            "shutdown": ("none", "!shutdown"), "restore": ("none", "!restore"),
            "killvm": ("none", "!killvm")}

    def parse(self, user, msg):
        """Returns a list of (name, arg) or None. A new command in a chain starts at a
        space followed by !<known command>, so other '!' characters in text are kept."""
        names = "|".join(sorted(self.SPEC, key=len, reverse=True))
        chain = []
        for part in re.split(rf"\s+(?=!(?:{names})(?:\s|$))", msg, flags=re.I):
            m = re.fullmatch(r"!(\w+)(?:\s+(.*))?", part.strip(), flags=re.S)
            if not m or m.group(1).lower() not in self.SPEC:
                self.log(f"{user}: not a valid command: {part.strip()[:60]}")
                return None
            name, arg = m.group(1).lower(), (m.group(2) or "").strip()
            kind, usage = self.SPEC[name]
            ok = True
            if kind == "text":
                ok = bool(arg)
            elif kind == "key":
                try:
                    ok = len(arg.split()) == 1 and bool(resolve_keys([arg]))
                except ValueError:
                    ok = False
            elif kind == "keys":
                try:
                    ok = bool(arg) and bool(resolve_keys(arg.split()))
                except ValueError:
                    ok = False
            elif kind == "secs":
                ok = bool(re.fullmatch(r"\d+(\.\d+)?", arg)) and float(arg) > 0
            elif kind == "px":
                ok = bool(re.fullmatch(r"\d+", arg)) and int(arg) > 0
            elif kind == "none":
                arg = ""  # anything after a no-argument command is ignored
            if not ok:
                self.log(f"{user}: bad syntax. Usage: {usage}")
                return None
            chain.append((name, arg))
        return chain

    def handle(self, user, msg, owner, mod):
        msg = msg.strip()
        if not msg.startswith("!"):
            return
        now = time.time()
        if now - self.last_cmd.get(user, 0) < self.cooldown.get():
            return self.log(f"{user}: ignored (cooldown): {msg[:60]}")
        self.last_cmd[user] = now
        chain = self.parse(user, msg)
        if not chain:
            return
        if any(n == "wait" for n, _ in chain):
            if chain[0][0] == "wait" or chain[-1][0] == "wait":
                return self.log(f"{user}: !wait cannot start or end a chain")
            total = 0.0
            for n, a in chain:
                if n == "wait":
                    try:
                        s = float(a)
                    except ValueError:
                        return self.log(f"{user}: bad !wait value")
                    if s > 15:
                        return self.log(f"{user}: !wait max is 15s")
                    total += s
            if total > 30:
                return self.log(f"{user}: chain wait over 30s")
        self.cmdq.put((user, chain, owner, mod))

    # ---- execution
    def worker(self):
        while True:
            user, chain, owner, mod = self.cmdq.get()
            for name, arg in chain:
                if name not in self.enabled:
                    continue
                if not self.enabled[name].get():
                    self.log(f"!{name} is disabled")
                    continue
                try:
                    self.execute(user, name, arg, owner, mod)
                except Exception as e:
                    self.log(f"!{name} failed: {e}")

    def vote(self, kind, user):
        now = time.time()
        v = self.votes[kind]
        for u in [u for u, t in v.items() if now - t > self.vote_window.get()]:
            del v[u]
        v[user] = now
        need = self.votes_needed.get()
        self.log(f"!{kind} vote {len(v)}/{need} ({user})")
        if len(v) >= need:
            v.clear()
            return True
        return False

    def execute(self, user, name, arg, owner, mod):
        be = self.be
        self.log(f"{user}: !{name} {arg}".strip())
        if name == "run":
            if not arg:
                return
            done = False
            if be.name == "VMware" and be.gu:
                try:
                    be.run_shell(arg)  # guest login set: run inside the guest, no keystrokes
                    done = True
                except RuntimeError as e:
                    self.log(f"  vmrun guest login failed ({e}); falling back to keystrokes")
            if not done:
                be.press(["win", "r"]); time.sleep(0.8)
                be.type_text(arg, enter=True)
        elif name == "send":
            be.type_text(arg, enter=True)
        elif name == "key":
            be.press([arg.split()[0]])
        elif name == "combo":
            be.press(arg.split())
        elif name == "wait":
            time.sleep(float(arg))
        elif name == "startvm":
            be.start()
        elif name == "restart":
            if self.vote("restart", user):
                be.reset()
        elif name == "shutdown":
            if self.vote("shutdown", user):
                be.poweroff()
        elif name == "restore":
            if self.vote("restore", user):
                be.restore()
        elif name == "killvm":
            if not owner:
                return self.log("!killvm: owner only")
            be.kill()
        elif name == "vrun":
            if not (owner or (mod and self.modok.get())):
                return self.log("!vrun: owner/moderator only")
            if not arg or not self.guser.get():
                return self.log("!vrun needs a path and guest credentials in settings")
            be.run_exe(arg)
        elif name in ("up", "down", "left", "right"):
            try:
                px = min(abs(int(arg)), self.maxpx.get())
            except ValueError:
                return self.log(f"!{name} needs a number")
            move_mouse({"left": -px, "right": px}.get(name, 0),
                       {"up": -px, "down": px}.get(name, 0))


if __name__ == "__main__":
    App().mainloop()
