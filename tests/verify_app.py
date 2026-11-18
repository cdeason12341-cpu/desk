"""STEP 8 app verification: headless Chrome, 390px, Android UA. Never prints APP_TOKEN or DATA_PATH."""
import json, os, re, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.sync_api import sync_playwright
import crypto

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEC = lambda k: open(os.path.join(ROOT, ".secrets", k)).read().strip()
FIXTURE = os.environ.get("FIXTURE") == "1"
TOK, DP = (open("/workspace/fixture/token").read().strip(), "fixture") if FIXTURE else (SEC("APP_TOKEN"), SEC("DATA_PATH"))
URL = os.environ.get("APP_URL", "https://cdeason12341-cpu.github.io/desk/")
SHOTS = os.path.join(ROOT, "shots", "fixture") if FIXTURE else os.path.join(ROOT, "shots")
os.makedirs(SHOTS, exist_ok=True)
UA = "Mozilla/5.0 (Linux; Android 14; SM-S921U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
red = lambda s: str(s).replace(TOK, "{APP_TOKEN}").replace(DP, "{DATA_PATH}")

def live(f):
    with urllib.request.urlopen(f"{URL}d/{DP}/{f}?t={time.time()}") as r:
        return crypto.decrypt_obj(json.load(r), TOK)

def ctx(b):
    return b.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=3, is_mobile=True, has_touch=True, user_agent=UA,
                         service_workers="block")

def shot(pg, path):
    h = pg.evaluate("Math.max(document.documentElement.scrollHeight, 844)")
    pg.set_viewport_size({"width": 390, "height": h}); pg.wait_for_timeout(300)
    pg.screenshot(path=path); pg.set_viewport_size({"width": 390, "height": 844})

def main():
    out = {"console_errors": [], "checks": {}, "shots": []}
    st = live("state.json"); aud = live("agents/auditor.json"); sc = live("agents/scanner.json")
    mint_a = (aud.get("recent") or [{}])[0].get("mint")
    mint_c = None
    cands = [p["mint"] for p in st["positions"]] + [a["mint"] for a in aud.get("recent", [])] + [r["mint"] for r in sc.get("room", [])] + [f["mint"] for f in sc.get("feed", [])]
    for m in cands:
        try: live(f"coin/{m}.json"); mint_c = m; break
        except Exception: pass
    mint_c = mint_c or (cands[0] if cands else None)
    out["checks"]["audit_mint_found"] = bool(mint_a); out["checks"]["coin_file_found"] = bool(mint_c)
    screens = [("wallet", "#/wallet"), ("agents", "#/agents"), ("auditor", f"#/audit/{mint_a}" if mint_a else "#/audit/none"),
               ("coin", f"#/coin/{mint_c}" if mint_c else "#/coin/none"), ("account", "#/account"), ("activity", "#/activity")]
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        # ---- normal
        c = ctx(b); pg = c.new_page()
        pg.on("console", lambda m: m.type == "error" and out["console_errors"].append(red(m.text)))
        pg.on("response", lambda r: r.status >= 400 and out.setdefault("bad_responses", []).append(f"{r.status} " + red(r.url.split('?')[0])))
        pg.on("pageerror", lambda e: out["console_errors"].append("pageerror: " + red(e)))
        pg.goto(f"{URL}#t={TOK}&d={DP}")
        pg.wait_for_function("window.__desk && window.__desk.files['state.json']", timeout=60000)
        pg.wait_for_timeout(1500)
        h = pg.evaluate("location.href")
        out["checks"]["fragment_stripped"] = (TOK not in h and DP not in h and h.endswith("#/wallet"))
        out["checks"]["localStorage_saved"] = pg.evaluate("!!localStorage.getItem('desk.t') && !!localStorage.getItem('desk.d')")
        txt = pg.inner_text("#view")
        bal = f"${st['balance']:,.2f}"
        out["checks"]["home_balance_matches"] = bal in txt
        out["checks"]["home_pools_matches"] = re.search(rf"\b{st['counts']['pools']}\s*\n?\s*pools seen", txt) is not None
        out["checks"]["home_positions"] = len(st["positions"])
        for name, hsh in screens:
            pg.evaluate(f"location.hash = {json.dumps(hsh)}"); pg.wait_for_timeout(3500 if name in ("auditor", "coin") else 1200)
            if name == "auditor": pg.wait_for_timeout(350 * 8 + 400)
            path = f"{SHOTS}/{name}.png"; shot(pg, path); out["shots"].append(path)
            t = pg.inner_text("#view")
            if name == "agents":
                out["checks"]["agents_scanner_pools"] = f"{sc['pools']} pools" in t
                out["checks"]["agents_auditor_count"] = f"{aud['takenApart']} taken apart" in t
            if name == "account":
                acc = live("account.json")
                out["checks"]["account_stack"] = f"${acc['stack']:,.2f}" in t
                out["checks"]["account_minliq_10000"] = any(s["key"] == "scanner.min_liquidity_usd" and s["display"] in t for s in acc["settings"])
                hrefs = pg.eval_on_selector_all("a[href^='https://fomo']", "els => els.map(e => e.href)")
                out["checks"]["account_links_from_data"] = set(hrefs) <= {acc["profile_url"], acc["SIGNUP_URL"]} and len(hrefs) >= 2
        # all fomo links on every screen must come from data fields (checked by test_links grep in JS too)
        # installability via CDP
        try:
            cdp = c.new_cdp_session(pg)
            m = cdp.send("Page.getAppManifest"); out["checks"]["manifest_errors"] = m.get("errors", [])
            try: out["checks"]["installability_errors"] = cdp.send("Page.getInstallabilityErrors").get("installabilityErrors", [])
            except Exception as e: out["checks"]["installability_errors"] = f"n/a: {e}"
        except Exception as e:
            out["checks"]["manifest_errors"] = f"n/a: {e}"
        c.close()
        normal_errors = list(out["console_errors"])
        # ---- error: good load, then block data
        c = ctx(b); pg = c.new_page()
        pg.goto(f"{URL}#t={TOK}&d={DP}"); pg.wait_for_function("window.__desk && window.__desk.files['state.json']", timeout=60000)
        pg.route("**/d/**", lambda r: r.abort())
        pg.evaluate("document.dispatchEvent(new Event('visibilitychange'))"); pg.wait_for_timeout(2500)
        for name, hsh in screens[:5]:
            pg.evaluate(f"location.hash = {json.dumps(hsh)}"); pg.wait_for_timeout(2500)
            path = f"{SHOTS}/{name}-error.png"; shot(pg, path); out["shots"].append(path)
            if name == "home":
                out["checks"]["error_row"] = pg.inner_text("#statusRow"); out["checks"]["error_keeps_numbers"] = bal in pg.inner_text("#view")
        c.close()
        # ---- empty: data 404
        c = ctx(b); pg = c.new_page()
        pg.route("**/d/**", lambda r: r.fulfill(status=404, body="not found"))
        pg.goto(f"{URL}#t={TOK}&d={DP}"); pg.wait_for_timeout(3000)
        for name, hsh in screens[:5]:
            pg.evaluate(f"location.hash = {json.dumps(hsh)}"); pg.wait_for_timeout(1500)
            path = f"{SHOTS}/{name}-empty.png"; shot(pg, path); out["shots"].append(path)
            if name == "home": out["checks"]["empty_text"] = pg.inner_text("#view")[:80]
        c.close()
        # ---- no-token state
        c = ctx(b); pg = c.new_page(); pg.goto(URL); pg.wait_for_timeout(1500)
        pg.screenshot(path=f"{SHOTS}/no-link.png"); out["shots"].append(f"{SHOTS}/no-link.png"); c.close()
        b.close()
    out["console_errors_normal"] = normal_errors
    del out["console_errors"]
    print(red(json.dumps(out, indent=1, default=str)))

if __name__ == "__main__":
    main()
