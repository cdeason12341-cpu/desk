"""Labeled FIXTURE for STEP 8 screenshots: a scratch copy of desk.db, a REAL Auditor run on a real recently scanned mint,
REAL DexScreener prices polled for a few minutes, plus ONE SYNTHETIC PAPER position (so the Risk line shows).
Encrypted with a throwaway token and served from localhost only. Nothing is published, nothing pushed to Telegram."""
import asyncio, json, os, shutil, sqlite3, sys, time, secrets
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = "/workspace/fixture"
MINT = sys.argv[1]; POLLS = int(sys.argv[2]) if len(sys.argv) > 2 else 24
src = sqlite3.connect(f"file:{ROOT}/desk.db?mode=ro", uri=True); dst = sqlite3.connect(f"{FIX}/fix.db"); src.backup(dst); dst.close(); src.close()
from main import Desk
import crypto, fields
d = Desk(db_path=f"{FIX}/fix.db", notifier=None, log_files=False, with_telegram=False)
c = d.ctx
async def go():
    scan = json.loads(c.db.q("SELECT json FROM scans WHERE mint=? ORDER BY id DESC LIMIT 1", (MINT,))[0][0])
    res = await d.auditor.run(MINT, scan, record=True)
    print("audit:", res and {k: res.get(k) for k in ("passed", "reason", "layersChecked")})
    first = None
    for i in range(POLLS):
        try:
            pairs = await c.net.dex_tokens([MINT])
            p = max(pairs, key=lambda x: float(fields.get(x, "liquidity.usd") or 0))
            px = float(p["priceUsd"]); vol = float(fields.get(p, "volume.m5") or 0)
            c.db.x("INSERT INTO prices(ts,mint,price,vol_m5,src) VALUES(?,?,?,?,?)", (time.time(), MINT, px, vol, "fixture-dex"))
            if first is None:
                first = px
                c.db.x("INSERT INTO positions(mint,name,symbol,mode,entry_price,size_usd,peak,stop_pct,trail_active,opened_at,status,last_price,pct,night) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (MINT, scan.get("name"), scan.get("symbol"), "PAPER", px, 47.5, px, 6, 0, time.time(), "open", px, 0.0, c.night))
            c.db.x("UPDATE positions SET last_price=?, peak=MAX(peak,?), pct=(?-entry_price)/entry_price*100 WHERE mint=?", (px, px, px, MINT))
        except Exception as e:
            print("poll err", type(e).__name__)
        await asyncio.sleep(15)
    files = d.publisher.builder.build() if hasattr(d.publisher, "builder") else __import__("publisher").Builder(d).build()
    tok = secrets.token_hex(32); dp = "fixture"
    out = f"{FIX}/site"; shutil.rmtree(out, ignore_errors=True); shutil.copytree(f"{ROOT}/web", out)
    for rel, obj in files.items():
        fp = f"{out}/d/{dp}/{rel}"; os.makedirs(os.path.dirname(fp), exist_ok=True)
        json.dump(crypto.encrypt_obj(obj, tok), open(fp, "w"))
    open(f"{FIX}/token", "w").write(tok)
    print("files:", sorted(files))
asyncio.run(go())
