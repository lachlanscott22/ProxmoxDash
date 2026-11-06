"""Demo-mode Playwright screenshots, .

    python tests/screenshots.py          ->  screenshots/*.png
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "screenshots")
PORT = 8791
PASSWORD = "demo-password-1"
os.makedirs(OUT, exist_ok=True)

priv = tempfile.mkdtemp(prefix="pvedemo-")
K = lambda c: c * 30
with open(os.path.join(priv, "pve-dash.json"), "w") as f:
    json.dump({"branding": {"name": "Proxmox Dashboard", "show_name": True},
               "destinations": [{"id": "d1", "label": "My phone", "enabled": True, "token": K("a"), "user": K("b"), "color": "#007aff"},
                                {"id": "d2", "label": "On-call", "enabled": True, "token": K("a"), "user": K("c"), "sound": "siren", "color": "#ff9500"},
                                {"id": "d3", "label": "Workshop iPad", "enabled": False, "token": K("d"), "user": K("b"), "device": "ipad", "color": "#34c759"}],
               "rules": {"cpu_temp": {"to": ["d1", "d2"]}, "digest": {"enabled": True, "to": ["d1"]}},
               "delivery": {"quiet": {"enabled": True, "from": "22:30", "to": "06:30", "allow_high": True}, "repeat": 240, "resolved": True}}, f)
env = dict(os.environ, PVE_DASH_DEMO="1", PVE_DASH_PRIV=priv, PVE_DASH_PORT=str(PORT), PVE_DASH_PASSWORD=PASSWORD)
subprocess.run([sys.executable, os.path.join(ROOT, "pve-dash.py"), "--set-password"], env=env, check=True, capture_output=True)
srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "pve-dash.py")], env=env, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
BASE = "http://127.0.0.1:%d/" % PORT
try:
    for _ in range(40):
        try:
            urllib.request.urlopen(BASE + "healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    problems = []
    with sync_playwright() as p:
        br = p.chromium.launch()

        def page(scheme="light", w=1440, h=1000, url=BASE):
            ctx = br.new_context(viewport={"width": w, "height": h}, color_scheme=scheme, device_scale_factor=1)
            pg = ctx.new_page()
            pg.on("pageerror", lambda e: problems.append("pageerror: %s" % e))
            pg.on("console", lambda m: problems.append("console: %s" % m.text) if m.type == "error" else None)
            pg.goto(url)
            pg.wait_for_selector(".node-card")
            pg.wait_for_timeout(1500)
            return ctx, pg

        for name, scheme, w, h in (("desktop-light", "light", 1440, 1000), ("desktop-dark", "dark", 1440, 1000), ("mobile-390", "light", 390, 844)):
            ctx, pg = page(scheme, w, h)
            pg.screenshot(path=os.path.join(OUT, name + ".png"), full_page=True)
            sw = pg.evaluate("document.documentElement.scrollWidth")
            print("%-14s scrollWidth=%d viewport=%d" % (name, sw, w))
            if sw > w:
                problems.append("%s has horizontal scroll (%d > %d)" % (name, sw, w))
            if name == "desktop-light":
                print("title:", pg.title(), "| header:", pg.inner_text("#title"), "| footer:", pg.inner_text("#foot"))
                print("live pill:", pg.inner_text("#liveText"), "| nodes:", pg.locator(".node-card").count(), "| guests rows:", pg.locator("#tbody tr").count())
            ctx.close()

        # settings: password sheet -> settings page -> destination sheet
        ctx, pg = page("light", 1440, 1000)
        pg.click("#openSettings")
        pg.wait_for_selector("#pwSheet.open")
        pg.wait_for_timeout(600)
        pg.screenshot(path=os.path.join(OUT, "settings-password.png"))
        pg.fill("#inPw", "wrong-password")
        pg.press("#inPw", "Enter")
        pg.wait_for_selector("#fPw.bad", timeout=5000)
        pg.fill("#inPw", PASSWORD)
        pg.press("#inPw", "Enter")
        pg.wait_for_selector("#destList .cell", timeout=8000)
        pg.wait_for_timeout(800)
        pg.screenshot(path=os.path.join(OUT, "settings.png"), full_page=True)
        pg.click("[data-edit=d2]")
        pg.wait_for_selector("#sheet.open")
        pg.wait_for_timeout(600)
        pg.screenshot(path=os.path.join(OUT, "settings-destination-sheet.png"))
        pg.click("#sheetTest")
        pg.wait_for_timeout(800)
        print("test toast:", pg.inner_text("#toast"))
        ctx.close()

        br.close()
    print("problems:", problems or "none")
finally:
    srv.terminate()
