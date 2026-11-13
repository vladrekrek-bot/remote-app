# -*- coding: utf-8 -*-
"""
SHKWIZI Remote — управление компьютером с телефона по Wi-Fi.
Запуск: START.bat (сам поставит библиотеки). Телефон и ПК — в одной Wi-Fi сети.
"""
import asyncio, ctypes, io, json, os, random, socket, sys, threading, time, zlib
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
import tkinter as tk

try:
    import mss
    from PIL import Image, ImageTk, ImageDraw, ImageFont
    from aiohttp import web, WSMsgType
except ImportError:
    print("Не хватает библиотек. Запусти START.bat — он всё установит.")
    input("Enter для выхода...")
    sys.exit(1)
try:
    import qrcode
except ImportError:
    qrcode = None

PORT = 8765
BG, CARD, ACC, PINK, TXT, DIM = "#0b0614", "#170d26", "#b44cff", "#ff4fa3", "#f3eaff", "#9b8bb5"

# ---------------- Windows: DPI + ввод ----------------
user32 = ctypes.windll.user32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send(*inputs):
    arr = (INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def mouse(flags, data=0):
    i = INPUT(type=0)
    i.u.mi = MOUSEINPUT(0, 0, data, flags, 0, 0)
    _send(i)


EXT = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}


def key(vk, up=False):
    flags = (2 if up else 0) | (1 if vk in EXT else 0)
    i = INPUT(type=1)
    i.u.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    _send(i)


def type_text(s):
    for ch in s:
        if ch == "\n":
            combo("enter")
            continue
        data = ch.encode("utf-16-le")
        for k in range(0, len(data), 2):
            unit = int.from_bytes(data[k:k + 2], "little")
            a, b = INPUT(type=1), INPUT(type=1)
            a.u.ki = KEYBDINPUT(0, unit, 4, 0, 0)
            b.u.ki = KEYBDINPUT(0, unit, 4 | 2, 0, 0)
            _send(a, b)


VK = {"enter": 0x0D, "backspace": 0x08, "esc": 0x1B, "tab": 0x09, "win": 0x5B, "alt": 0x12,
      "ctrl": 0x11, "shift": 0x10, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
      "delete": 0x2E, "space": 0x20, "home": 0x24, "end": 0x23, "pgup": 0x21, "pgdn": 0x22,
      "f4": 0x73, "f5": 0x74, "f11": 0x7A}


def combo(s):
    vks = []
    for p in s.lower().split("+"):
        if p in VK:
            vks.append(VK[p])
        elif len(p) == 1:
            vks.append(ord(p.upper()))
    for v in vks:
        key(v)
    for v in reversed(vks):
        key(v, True)


BTN = {"left": (0x02, 0x04), "right": (0x08, 0x10), "middle": (0x20, 0x40)}


# ---------------- захват экрана ----------------
class Grabber:
    def __init__(self):
        self.sct = None
        self.rect = (0, 0, 1920, 1080)
        self.count = 1

    def grab(self, idx, maxw, q, last):
        try:
            if self.sct is None:
                self.sct = mss.mss()
            mons = self.sct.monitors
            self.count = max(1, len(mons) - 1)
            idx = max(1, min(idx, self.count))
            m = mons[idx]
            self.rect = (m["left"], m["top"], m["width"], m["height"])
            raw = self.sct.grab(m)
        except Exception:
            self.sct = None
            time.sleep(0.3)
            return None, None, last
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        cx = (pt.x - m["left"]) / m["width"]
        cy = (pt.y - m["top"]) / m["height"]
        sig = (zlib.crc32(raw.bgra), round(cx, 4), round(cy, 4), maxw, q, idx)
        info = {"t": "f", "cx": cx, "cy": cy, "m": idx, "mc": self.count}
        if sig == last:
            return None, info, sig
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        if img.width > maxw:
            img = img.resize((maxw, max(1, round(img.height * maxw / img.width))), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q)
        info.update(w=img.width, h=img.height)
        return buf.getvalue(), info, sig


QUALITY = {"0": (960, 45), "1": (1280, 60), "2": (1920, 78)}


# ---------------- сервер ----------------
class RemoteServer:
    def __init__(self):
        self.pin = self.new_pin()
        self.allow_control = True
        self.clients = []
        self.mon = 1
        self.grabber = Grabber()
        self.cap = ThreadPoolExecutor(1)
        self.loop = None
        self.fails = {}
        self.ip = lan_ip()

    @staticmethod
    def new_pin():
        return f"{random.randint(0, 999999):06d}"

    # --- http ---
    async def index(self, req):
        return web.Response(text=HTML, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def manifest(self, req):
        return web.json_response({"name": "SHKWIZI Remote", "short_name": "Remote", "display": "standalone",
                                  "background_color": BG, "theme_color": BG, "start_url": "/",
                                  "icons": [{"src": "/icon.png", "sizes": "180x180", "type": "image/png"}]})

    async def icon(self, req):
        return web.Response(body=ICON_PNG, content_type="image/png")

    async def ping(self, req):
        return web.json_response({"app": "shkwizi-remote", "name": socket.gethostname()},
                                 headers={"Access-Control-Allow-Origin": "*"})

    # --- websocket ---
    async def ws(self, req):
        ip = req.remote or "?"
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=0)
        await ws.prepare(req)
        blocked = self.fails.get(ip, (0, 0))
        if blocked[0] >= 5 and time.time() - blocked[1] < 60:
            await ws.send_json({"t": "auth", "ok": False, "msg": "Слишком много попыток, подожди минуту"})
            await ws.close()
            return ws
        try:
            m = await ws.receive(timeout=30)
            d = json.loads(m.data)
        except Exception:
            await ws.close()
            return ws
        if str(d.get("pin", "")) != self.pin:
            n = blocked[0] + 1 if time.time() - blocked[1] < 60 else 1
            self.fails[ip] = (n, time.time())
            await ws.send_json({"t": "auth", "ok": False, "msg": "Неверный PIN"})
            await ws.close()
            return ws
        self.fails.pop(ip, None)
        st = {"ws": ws, "ip": ip, "ack": asyncio.Event(), "view": asyncio.Event(), "ctl": False,
              "q": QUALITY["1"]}
        st["ack"].set()
        self.clients.append(st)
        await ws.send_json({"t": "auth", "ok": True, "name": socket.gethostname()})
        await self.push_state(st)
        task = asyncio.create_task(self.stream(st))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        self.on_msg(st, json.loads(msg.data))
                    except Exception as e:
                        print("msg error:", e)
        finally:
            task.cancel()
            if st in self.clients:
                self.clients.remove(st)
        return ws

    async def stream(self, st):
        loop = asyncio.get_running_loop()
        last, last_t = None, 0
        ws = st["ws"]
        while not ws.closed:
            await st["view"].wait()
            await st["ack"].wait()
            st["ack"].clear()
            while True:
                w, q = st["q"]
                jpg, info, sig = await loop.run_in_executor(self.cap, self.grabber.grab, self.mon, w, q, last)
                if jpg is not None:
                    break
                if info is not None and time.time() - last_t > 1.5:
                    last = None  # периодически шлём кадр, чтобы не «зависало»
                    continue
                await asyncio.sleep(0.012)
            last, last_t = sig, time.time()
            try:
                await ws.send_json(info)
                await ws.send_bytes(jpg)
            except Exception:
                return

    async def push_state(self, st):
        try:
            await st["ws"].send_json({"t": "state", "ctl": st["ctl"] and self.allow_control,
                                      "allowed": self.allow_control})
        except Exception:
            pass

    def push_all(self):
        for st in list(self.clients):
            asyncio.ensure_future(self.push_state(st))

    def to_abs(self, x, y):
        l, t, w, h = self.grabber.rect
        x = min(max(float(x), 0.0), 1.0)
        y = min(max(float(y), 0.0), 1.0)
        return int(l + x * (w - 1)), int(t + y * (h - 1))

    def on_msg(self, st, d):
        t = d.get("t")
        if t == "ack":
            st["ack"].set()
            return
        if t == "view":
            (st["view"].set if d.get("on") else st["view"].clear)()
            st["ack"].set()
            return
        if t == "cfg":
            st["q"] = QUALITY.get(str(d.get("q")), QUALITY["1"])
            return
        if t == "ctl":
            st["ctl"] = bool(d.get("on")) and self.allow_control
            asyncio.ensure_future(self.push_state(st))
            return
        if t == "mon":
            self.mon = self.mon % self.grabber.count + 1
            return
        if not (st["ctl"] and self.allow_control):
            return
        if t == "move":
            user32.SetCursorPos(*self.to_abs(d["x"], d["y"]))
        elif t == "down":
            mouse(BTN.get(d.get("b"), BTN["left"])[0])
        elif t == "up":
            mouse(BTN.get(d.get("b"), BTN["left"])[1])
        elif t == "click":
            dn, up = BTN.get(d.get("b"), BTN["left"])
            for _ in range(int(d.get("n", 1))):
                mouse(dn)
                mouse(up)
        elif t == "scroll":
            if d.get("dy"):
                mouse(0x0800, int(d["dy"]))
            if d.get("dx"):
                mouse(0x1000, int(d["dx"]))
        elif t == "text":
            type_text(str(d.get("s", ""))[:500])
        elif t == "key":
            combo(str(d.get("k", "")))

    # --- управление из окна ПК (вызывается из потока tkinter) ---
    def set_allowed(self, v):
        def f():
            self.allow_control = v
            if not v:
                for st in self.clients:
                    st["ctl"] = False
            self.push_all()
        self.loop.call_soon_threadsafe(f)

    def regen_pin(self):
        self.pin = self.new_pin()

        def f():
            for st in list(self.clients):
                asyncio.ensure_future(st["ws"].close())
        self.loop.call_soon_threadsafe(f)

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        app = web.Application()
        app.router.add_get("/", self.index)
        app.router.add_get("/manifest.json", self.manifest)
        app.router.add_get("/icon.png", self.icon)
        app.router.add_get("/ping", self.ping)
        app.router.add_get("/ws", self.ws)
        runner = web.AppRunner(app)
        loop.run_until_complete(runner.setup())
        loop.run_until_complete(web.TCPSite(runner, "0.0.0.0", PORT).start())
        loop.run_forever()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def make_icon():
    img = Image.new("RGB", (180, 180), BG)
    dr = ImageDraw.Draw(img)
    for i in range(180):
        r = int(0x6a + (0xff - 0x6a) * i / 180)
        dr.line([(0, i), (180, i)], fill=(r, 0x2a, 0xd0 - i // 3))
    dr.rounded_rectangle([42, 50, 138, 112], 10, outline="white", width=8)
    dr.rectangle([78, 112, 102, 128], fill="white")
    dr.rounded_rectangle([60, 126, 120, 136], 4, fill="white")
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


ICON_PNG = make_icon()


# ---------------- окно на ПК ----------------
class Gui:
    def __init__(self, srv):
        self.srv = srv
        r = self.root = tk.Tk()
        r.title("SHKWIZI Remote")
        r.configure(bg=BG)
        r.geometry("440x700")
        r.resizable(False, False)
        url = f"http://{srv.ip}:{PORT}"
        tk.Label(r, text="SHKWIZI Remote", font=("Segoe UI", 22, "bold"), fg=ACC, bg=BG).pack(pady=(18, 0))
        tk.Label(r, text="Управление ПК с телефона", font=("Segoe UI", 10), fg=DIM, bg=BG).pack()

        card = tk.Frame(r, bg=CARD, padx=16, pady=14)
        card.pack(padx=20, pady=14, fill="x")
        tk.Label(card, text="1. Подключи телефон к тому же Wi-Fi", font=("Segoe UI", 10), fg=TXT, bg=CARD,
                 anchor="w").pack(fill="x")
        tk.Label(card, text="2. Открой приложение SHKWIZI Remote на айфоне\n    (или наведи камеру на QR) — адрес ПК:",
                 font=("Segoe UI", 10), fg=TXT, bg=CARD, anchor="w", justify="left").pack(fill="x")
        e = tk.Entry(card, font=("Consolas", 15, "bold"), fg=PINK, bg=CARD, bd=0, justify="center",
                     readonlybackground=CARD)
        e.insert(0, url)
        e.config(state="readonly")
        e.pack(fill="x", pady=6)
        if qrcode:
            qimg = qrcode.make(f"shkwizi-remote://c?h={srv.ip}:{PORT}").convert("RGB").resize((200, 200), Image.NEAREST)
            self.qr = ImageTk.PhotoImage(qimg)
            tk.Label(card, image=self.qr, bg=CARD).pack(pady=4)
        tk.Label(card, text="3. Введи PIN:", font=("Segoe UI", 10), fg=TXT, bg=CARD, anchor="w").pack(fill="x")
        self.pin_lbl = tk.Label(card, text=srv.pin, font=("Consolas", 28, "bold"), fg="white", bg=CARD)
        self.pin_lbl.pack()

        self.status = tk.Label(r, text="", font=("Segoe UI", 11, "bold"), fg=DIM, bg=BG)
        self.status.pack(pady=(0, 8))

        self.allow = tk.BooleanVar(value=True)
        tk.Checkbutton(r, text="Разрешить управление с телефона", variable=self.allow, command=self.toggle,
                       font=("Segoe UI", 10), fg=TXT, bg=BG, selectcolor=CARD, activebackground=BG,
                       activeforeground=TXT).pack()
        self.btn(r, "СТОП — забрать управление", PINK, self.stop).pack(fill="x", padx=20, pady=(10, 6))
        self.btn(r, "Новый PIN (отключит всех)", CARD, self.newpin).pack(fill="x", padx=20)
        tk.Label(r, text="Если телефон не подключается — разреши Python\nв брандмауэре Windows (частная сеть).",
                 font=("Segoe UI", 8), fg=DIM, bg=BG).pack(pady=10)
        r.after(400, self.tick)

    def btn(self, parent, text, color, cmd):
        return tk.Button(parent, text=text, command=cmd, font=("Segoe UI", 11, "bold"), fg="white", bg=color,
                         activebackground=ACC, activeforeground="white", bd=0, pady=9, cursor="hand2")

    def toggle(self):
        self.srv.set_allowed(self.allow.get())

    def stop(self):
        self.allow.set(False)
        self.srv.set_allowed(False)

    def newpin(self):
        self.srv.regen_pin()
        self.pin_lbl.config(text=self.srv.pin)

    def tick(self):
        cl = list(self.srv.clients)
        ctl = any(c["ctl"] for c in cl) and self.srv.allow_control
        if not cl:
            self.status.config(text="Ждём телефон…", fg=DIM)
        elif ctl:
            self.status.config(text="● Телефон управляет компьютером", fg=PINK)
        else:
            self.status.config(text=f"● Подключено: {len(cl)} (только просмотр)", fg="#5dffa8")
        self.root.after(400, self.tick)


# ---------------- страница для телефона ----------------
HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="SHKWIZI Remote"><meta name="theme-color" content="#0b0614">
<link rel="apple-touch-icon" href="/icon.png"><link rel="manifest" href="/manifest.json">
<title>SHKWIZI Remote</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;-webkit-user-select:none;user-select:none}
html,body{height:100%;background:#0b0614;color:#f3eaff;font-family:-apple-system,system-ui,Segoe UI,sans-serif;overflow:hidden;position:fixed;inset:0;overscroll-behavior:none}
.hidden{display:none!important}
.screen{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:24px;
 background:radial-gradient(circle at 30% 20%,#3a1366 0,transparent 55%),radial-gradient(circle at 80% 90%,#5a0f45 0,transparent 50%),#0b0614}
.logo{font-size:30px;font-weight:800;background:linear-gradient(90deg,#b44cff,#ff4fa3);-webkit-background-clip:text;color:transparent}
.sub{color:#9b8bb5;font-size:14px;text-align:center}
.pin{width:240px;font-size:30px;letter-spacing:8px;text-align:center;padding:14px;border-radius:16px;border:1px solid #b44cff66;background:#170d26;color:#fff;outline:none;-webkit-user-select:text;user-select:text}
.big{border:0;border-radius:20px;color:#fff;font-weight:800;font-size:18px;padding:16px 28px;background:linear-gradient(135deg,#b44cff,#ff4fa3);box-shadow:0 8px 30px #b44cff55;touch-action:manipulation}
.demo-card{width:min(320px,90vw);aspect-ratio:16/10;border-radius:24px;border:1px solid #b44cff55;background:linear-gradient(160deg,#1c0f30,#120a1f);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;font-size:26px;font-weight:800;box-shadow:0 0 40px #b44cff33}
.demo-card span{font-size:13px;color:#9b8bb5;font-weight:500}
.link{background:none;border:0;color:#9b8bb5;font-size:14px;text-decoration:underline}
.err{color:#ff4fa3;font-size:14px;min-height:18px}
#demo{position:fixed;inset:0;background:#000}
#cv,#pad{position:absolute;left:0;top:0;width:100%;height:100%;touch-action:none}
#cur{position:absolute;left:0;top:0;width:22px;height:22px;pointer-events:none;will-change:transform;filter:drop-shadow(0 1px 2px #000)}
#bar{position:absolute;top:calc(env(safe-area-inset-top) + 8px);left:50%;transform:translateX(-50%);display:flex;gap:6px;padding:6px;border-radius:18px;background:#120a1fcc;backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);border:1px solid #b44cff44;z-index:5}
#bar button,#bShow,#more button,#more select{border:0;border-radius:12px;background:#ffffff14;color:#fff;font-size:15px;font-weight:700;padding:9px 12px;min-width:42px;touch-action:manipulation}
#bCtl{background:linear-gradient(135deg,#2bd47d,#18a35c);min-width:76px}
#bCtl.on{background:linear-gradient(135deg,#ff4f6d,#d11d4a)}
#bShow{position:absolute;top:calc(env(safe-area-inset-top) + 8px);right:10px;z-index:5;background:#120a1fcc;border:1px solid #b44cff44}
#more{position:absolute;top:calc(env(safe-area-inset-top) + 64px);left:50%;transform:translateX(-50%);display:grid;grid-template-columns:repeat(3,1fr);gap:6px;padding:10px;border-radius:18px;background:#120a1fee;border:1px solid #b44cff44;z-index:6;width:min(330px,94vw)}
#more select{grid-column:span 3;-webkit-appearance:none;text-align:center}
#toast{position:absolute;bottom:calc(env(safe-area-inset-bottom) + 14px);left:50%;transform:translateX(-50%);background:#120a1fdd;padding:8px 14px;border-radius:12px;font-size:13px;z-index:7;transition:opacity .3s;pointer-events:none;white-space:nowrap}
#fps{position:absolute;right:8px;bottom:calc(env(safe-area-inset-bottom) + 6px);font-size:10px;color:#ffffff66;z-index:4;pointer-events:none}
#kb{position:absolute;left:-1000px;top:0;opacity:0;width:10px;height:10px}
</style></head><body>

<div id="login" class="screen hidden">
 <div class="logo">SHKWIZI Remote</div>
 <div class="sub">Введи PIN, который показан<br>в окне на компьютере</div>
 <input id="pinIn" class="pin" inputmode="numeric" maxlength="6" placeholder="••••••">
 <div id="err" class="err"></div>
 <button id="bLogin" class="big">Подключиться</button>
 <button id="bChange" class="link hidden">Сменить компьютер</button>
</div>

<div id="home" class="screen hidden">
 <div class="logo">SHKWIZI Remote</div>
 <div id="pcName" class="sub">Подключено</div>
 <button id="bDemo" class="demo-card" style="color:#fff">🖥 Демка<span>Открыть экран компьютера</span></button>
 <div id="tip" class="sub">Совет: «Поделиться → На экран Домой» —<br>будет как приложение, на весь экран</div>
 <button id="bLogout" class="link">Отключиться</button>
</div>

<div id="demo" class="hidden">
 <canvas id="cv"></canvas>
 <svg id="cur" viewBox="0 0 22 22"><path d="M2 1 L2 18 L6.5 13.8 L9.6 20.6 L12.4 19.3 L9.4 12.6 L15.5 12.6 Z" fill="#fff" stroke="#000" stroke-width="1.3" stroke-linejoin="round"/></svg>
 <div id="pad"></div>
 <div id="bar">
  <button id="bBack">‹</button>
  <button id="bCtl">Старт</button>
  <button id="bMode">Тач</button>
  <button id="bKb">⌨︎</button>
  <button id="bMore">⋯</button>
  <button id="bHide">✕</button>
 </div>
 <button id="bShow" class="hidden">☰</button>
 <div id="more" class="hidden">
  <button data-k="win">⊞ Пуск</button><button data-k="alt+tab">Alt+Tab</button><button data-k="win+d">Рабочий</button>
  <button data-k="enter">Enter</button><button data-k="esc">Esc</button><button data-k="backspace">⌫</button>
  <button data-k="ctrl+c">Копир.</button><button data-k="ctrl+v">Встав.</button><button data-k="ctrl+z">Отмена</button>
  <button data-k="left">←</button><button data-k="up">↑</button><button data-k="right">→</button>
  <button data-a="rclick">ПКМ</button><button data-k="down">↓</button><button data-a="mon">Монитор</button>
  <select id="qSel"><option value="0">Качество: скорость</option><option value="1">Качество: баланс</option><option value="2">Качество: максимум</option></select>
 </div>
 <input id="kb" autocapitalize="off" autocomplete="off" autocorrect="off" spellcheck="false">
 <div id="fps"></div>
 <div id="toast" style="opacity:0"></div>
</div>

<script>
const $=id=>document.getElementById(id);
function load(k){try{return localStorage.getItem('sr_'+k)}catch(e){return null}}
function save(k,v){try{v==null?localStorage.removeItem('sr_'+k):localStorage.setItem('sr_'+k,v)}catch(e){}}
let ws=null,want=true,pin=load('pin')||'',info=null,bmp=null,ctl=false,allowed=true,inDemo=false;
let mode=load('mode')||'touch',quality=load('q')||'1',frames=0;
const cv=$('cv'),ctx=cv.getContext('2d'),cur=$('cur'),pad=$('pad');
let vw=1,vh=1,dpr=1,z=1,ox=0,oy=0,bw=1,bh=1,curX=.5,curY=.5;
const clamp01=v=>v<0?0:v>1?1:v;
function send(o){if(ws&&ws.readyState===1)ws.send(JSON.stringify(o))}
function show(id){['login','home','demo'].forEach(s=>$(s).classList.toggle('hidden',s!==id))}
let tt;function toast(t){const e=$('toast');e.textContent=t;e.style.opacity=1;clearTimeout(tt);tt=setTimeout(()=>e.style.opacity=0,1600)}

/* ---------- соединение ---------- */
function connect(){
  want=true;
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
  ws.binaryType='blob';
  ws.onopen=()=>send({t:'auth',pin});
  ws.onmessage=e=>{
    if(typeof e.data!=='string'){
      send({t:'ack'});frames++;
      createImageBitmap(e.data).then(b=>{if(bmp&&bmp.close)bmp.close();bmp=b;draw()}).catch(()=>{});
      return;
    }
    const d=JSON.parse(e.data);
    if(d.t==='f'){
      const ch=!info||info.w!==d.w||info.h!==d.h;info=d;
      if(ch){z=1;layout()}
      if(!ctl||!touching()){curX=d.cx;curY=d.cy;placeCursor()}
    }else if(d.t==='auth'){
      if(d.ok){save('pin',pin);$('pcName').textContent='Подключено к '+(d.name||'ПК');
        send({t:'cfg',q:quality});
        if(inDemo){send({t:'view',on:true})}else show('home');
      }else{want=false;save('pin',null);show('login');$('err').textContent=d.msg||'Неверный PIN'}
    }else if(d.t==='state'){
      const was=ctl;ctl=d.ctl;allowed=d.allowed;updCtl();
      if(was&&!ctl&&!allowed)toast('Управление забрали на ПК');
    }
  };
  ws.onclose=()=>{if(want){if(inDemo)toast('Переподключение…');setTimeout(connect,800)}};
}
$('bLogin').onclick=()=>{pin=$('pinIn').value.trim();if(pin.length<4){$('err').textContent='Введи PIN';return}
  $('err').textContent='';if(ws)try{want=false;ws.close()}catch(e){}connect()};
$('pinIn').addEventListener('keydown',e=>{if(e.key==='Enter')$('bLogin').click()});
const inApp=/[?&]app=1/.test(location.search)||load('app')==='1';if(inApp)save('app','1');
if(inApp){$('bLogout').textContent='Сменить компьютер';$('tip').classList.add('hidden');$('bChange').classList.remove('hidden')}
$('bChange').onclick=()=>{location.href='capacitor://localhost/?back=1'};
$('bLogout').onclick=()=>{want=false;save('pin',null);pin='';try{ws.close()}catch(e){}
  if(inApp){location.href='capacitor://localhost/?back=1'}else show('login')};

/* ---------- демка ---------- */
$('bDemo').onclick=()=>{inDemo=true;show('demo');layout();send({t:'view',on:true});updCtl();updMode()};
$('bBack').onclick=()=>{inDemo=false;if(ctl)send({t:'ctl',on:false});send({t:'view',on:false});$('more').classList.add('hidden');show('home')};
$('bCtl').onclick=()=>{if(!allowed&&!ctl){toast('Управление выключено на ПК');return}send({t:'ctl',on:!ctl})};
function updCtl(){const b=$('bCtl');b.textContent=ctl?'Стоп':'Старт';b.classList.toggle('on',ctl)}
$('bMode').onclick=()=>{mode=mode==='touch'?'pad':'touch';save('mode',mode);updMode();toast(mode==='touch'?'Тач: жми прямо по экрану':'Тачпад: води пальцем как по ноуту')};
function updMode(){$('bMode').textContent=mode==='touch'?'Тач':'Тачпад'}
$('bHide').onclick=()=>{$('bar').classList.add('hidden');$('more').classList.add('hidden');$('bShow').classList.remove('hidden')};
$('bShow').onclick=()=>{$('bar').classList.remove('hidden');$('bShow').classList.add('hidden')};
$('bMore').onclick=()=>$('more').classList.toggle('hidden');
$('qSel').value=quality;
$('qSel').onchange=e=>{quality=e.target.value;save('q',quality);send({t:'cfg',q:quality})};
document.querySelectorAll('#more button').forEach(b=>b.onclick=()=>{
  if(b.dataset.a==='mon'){send({t:'mon'});toast('Следующий монитор');return}
  if(!ctl){toast('Сначала нажми «Старт»');return}
  if(b.dataset.a==='rclick')send({t:'click',b:'right'});else send({t:'key',k:b.dataset.k});
});

/* клавиатура */
const kb=$('kb');
$('bKb').onclick=()=>{if(!ctl){toast('Сначала нажми «Старт»');return}kb.value=' ';kb.focus()};
kb.addEventListener('input',e=>{
  if(e.inputType==='deleteContentBackward')send({t:'key',k:'backspace'});
  else if(e.data)send({t:'text',s:e.data});
  else if(kb.value.length>1)send({t:'text',s:kb.value.slice(1)});
  kb.value=' ';
});
kb.addEventListener('keydown',e=>{if(e.key==='Enter'){send({t:'key',k:'enter'});e.preventDefault()}
  else if(e.key==='Backspace'&&kb.value.length<=1){send({t:'key',k:'backspace'});e.preventDefault()}});

/* ---------- отрисовка ---------- */
function layout(){
  vw=innerWidth;vh=innerHeight;dpr=Math.min(devicePixelRatio||1,3);
  cv.width=Math.round(vw*dpr);cv.height=Math.round(vh*dpr);
  if(info){const s=Math.min(vw/info.w,vh/info.h);bw=info.w*s;bh=info.h*s}
  clampView();draw();
}
function clampView(){
  const dw=bw*z,dh=bh*z;
  ox=dw<=vw?(vw-dw)/2:Math.min(0,Math.max(vw-dw,ox));
  oy=dh<=vh?(vh-dh)/2:Math.min(0,Math.max(vh-dh,oy));
}
function draw(){
  if(!bmp||!inDemo)return;
  ctx.setTransform(dpr,0,0,dpr,0,0);ctx.fillStyle='#000';ctx.fillRect(0,0,vw,vh);
  ctx.imageSmoothingEnabled=true;ctx.imageSmoothingQuality='high';
  ctx.drawImage(bmp,ox,oy,bw*z,bh*z);placeCursor();
}
function placeCursor(){
  const vis=curX>=0&&curX<=1&&curY>=0&&curY<=1;cur.style.display=vis?'block':'none';
  cur.style.transform=`translate(${ox+curX*bw*z-2}px,${oy+curY*bh*z-1}px)`;
}
addEventListener('resize',()=>setTimeout(layout,60));
addEventListener('orientationchange',()=>setTimeout(layout,250));
setInterval(()=>{$('fps').textContent=inDemo?frames+' fps':'';frames=0},1000);

/* ---------- жесты ---------- */
let g=null,lastEnd=0,pendingTap=null,lastViewTap=0,raf=0,moveDirty=false;const sc={x:0,y:0};
const now=()=>performance.now();
const P=t=>({x:t.clientX,y:t.clientY});
const dist=(a,b)=>Math.hypot(a.x-b.x,a.y-b.y);
const mid=(a,b)=>({x:(a.x+b.x)/2,y:(a.y+b.y)/2});
function norm(p){return{x:clamp01((p.x-ox)/(bw*z)),y:clamp01((p.y-oy)/(bh*z))}}
function setCur(q){curX=q.x;curY=q.y;placeCursor()}
function touching(){return g!==null||now()-lastEnd<400}
function queue(){moveDirty=true;if(!raf)raf=requestAnimationFrame(flush)}
function flush(){raf=0;
  if(moveDirty){moveDirty=false;send({t:'move',x:curX,y:curY})}
  const wy=Math.round(sc.y*3),wx=Math.round(sc.x*3);
  if(wy||wx){send({t:'scroll',dy:wy,dx:-wx});sc.x=sc.y=0}}
function flushNow(){if(raf){cancelAnimationFrame(raf);flush()}}
function padSpeed(dx,dy){const v=Math.hypot(dx,dy);return 1.3*(1+Math.min(v,40)/10)}

pad.addEventListener('touchstart',e=>{
  e.preventDefault();$('more').classList.add('hidden');
  const n=e.touches.length,t=now();
  if(n===1){
    const p=P(e.touches[0]);
    g={type:'one',start:p,last:p,t0:t,moved:false,drag:false,lp:0,done:false,dragReady:false};
    if(ctl&&mode==='touch'){setCur(norm(p));
      g.lp=setTimeout(()=>{if(!g||g.moved)return;const q=norm(g.start);send({t:'move',...q});send({t:'click',b:'right'});g.done=true;toast('Правый клик')},550)}
    if(ctl&&mode==='pad'&&pendingTap&&t-pendingTap.t<300){clearTimeout(pendingTap.timer);pendingTap=null;g.dragReady=true}
  }else if(n===2){
    if(g&&g.type==='one'){clearTimeout(g.lp);if(g.drag){flushNow();send({t:'up',b:'left'})}}
    const a=P(e.touches[0]),b=P(e.touches[1]);
    g={type:'two',d0:dist(a,b),c0:mid(a,b),c:mid(a,b),z0:z,ox0:ox,oy0:oy,lock:null,t0:t,moved:false};
  }
},{passive:false});

pad.addEventListener('touchmove',e=>{
  e.preventDefault();if(!g)return;
  if(g.type==='one'&&e.touches.length===1){
    const p=P(e.touches[0]),dx=p.x-g.last.x,dy=p.y-g.last.y;
    if(!g.moved&&dist(p,g.start)>8){g.moved=true;clearTimeout(g.lp);
      if(ctl&&mode==='touch'){const q=norm(g.start);send({t:'move',...q});send({t:'down',b:'left'});g.drag=true}
      if(ctl&&mode==='pad'&&g.dragReady){send({t:'down',b:'left'});g.drag=true}}
    if(g.moved){
      if(!ctl){ox+=dx;oy+=dy;clampView();draw()}
      else if(mode==='touch'){setCur(norm(p));queue()}
      else{const s=padSpeed(dx,dy);curX=clamp01(curX+dx*s/(bw*z));curY=clamp01(curY+dy*s/(bh*z));placeCursor();queue()}
    }
    g.last=p;
  }else if(g.type==='two'&&e.touches.length>=2){
    const a=P(e.touches[0]),b=P(e.touches[1]),d=dist(a,b),c=mid(a,b);
    if(!g.lock){if(Math.abs(d-g.d0)>28)g.lock='zoom';else if(dist(c,g.c0)>10)g.lock=ctl?'scroll':'zoom'}
    if(g.lock==='zoom'){
      const u=(g.c0.x-g.ox0)/(bw*g.z0),v=(g.c0.y-g.oy0)/(bh*g.z0);
      z=Math.min(6,Math.max(1,g.z0*d/g.d0));ox=c.x-u*bw*z;oy=c.y-v*bh*z;clampView();draw();
    }else if(g.lock==='scroll'){sc.y+=c.y-g.c.y;sc.x+=c.x-g.c.x;queue()}
    g.c=c;if(g.lock)g.moved=true;
  }
},{passive:false});

function tend(e){
  e.preventDefault();if(!g)return;const t=now();
  if(g.type==='one'&&e.touches.length===0){
    clearTimeout(g.lp);const dt=t-g.t0;
    if(ctl){
      if(g.drag){flushNow();send({t:'up',b:'left'})}
      else if(!g.moved&&!g.done&&dt<500){
        if(mode==='touch'){const q=norm(g.start);setCur(q);send({t:'move',...q});send({t:'click',b:'left'})}
        else if(g.dragReady){send({t:'click',b:'left',n:2})}
        else{pendingTap={t,timer:setTimeout(()=>{send({t:'click',b:'left'});pendingTap=null},260)}}
      }else flushNow();
    }else if(!g.moved&&dt<300){
      if(t-lastViewTap<300){z=1;clampView();draw();lastViewTap=0}else lastViewTap=t;
    }
    g=null;lastEnd=t;
  }else if(g.type==='two'&&e.touches.length<2){
    if(ctl&&!g.moved&&t-g.t0<350){send({t:'click',b:'right'});toast('Правый клик')}
    flushNow();g=e.touches.length?{type:'none'}:null;lastEnd=t;
  }else if(g.type==='none'&&e.touches.length===0){g=null;lastEnd=t}
}
pad.addEventListener('touchend',tend,{passive:false});
pad.addEventListener('touchcancel',tend,{passive:false});

/* мышь — чтобы можно было тестить и с ноутбука/планшета */
pad.addEventListener('mousedown',e=>{if(!ctl)return;setCur(norm({x:e.clientX,y:e.clientY}));send({t:'move',x:curX,y:curY});send({t:'down',b:e.button===2?'right':'left'})});
pad.addEventListener('mousemove',e=>{if(!ctl||!e.buttons)return;setCur(norm({x:e.clientX,y:e.clientY}));queue()});
pad.addEventListener('mouseup',e=>{if(!ctl)return;flushNow();send({t:'up',b:e.button===2?'right':'left'})});
pad.addEventListener('contextmenu',e=>e.preventDefault());
pad.addEventListener('wheel',e=>{if(!ctl)return;e.preventDefault();send({t:'scroll',dy:Math.round(-e.deltaY),dx:Math.round(e.deltaX)})},{passive:false});

document.addEventListener('visibilitychange',()=>{if(document.hidden&&ctl)send({t:'ctl',on:false})});
document.addEventListener('gesturestart',e=>e.preventDefault());

updMode();
if(pin){connect()}else show('login');
</script></body></html>"""


def main():
    srv = RemoteServer()
    threading.Thread(target=srv.run, daemon=True).start()
    print(f"SHKWIZI Remote: http://{srv.ip}:{PORT}   PIN: {srv.pin}")
    Gui(srv).root.mainloop()
    os._exit(0)


if __name__ == "__main__":
    main()
