"""STEP 13.5: Auditor on a REAL mint with freeze authority set (and mint authority revoked):
red at layer 2, layers 3-8 'not checked'. Uses the live default RPC."""
import asyncio
from agents.auditor import Auditor
from net import Net
from tests.helpers import make_ctx

FREEZE_MINT = "TMPKzJfNTHAUYz7QsaB51pb6Dap5diwTxbUDksj7n8G"   # found 2026-11-14: mintAuthority null, freezeAuthority set


def test_freeze_red_at_layer_2():
    c, _ = make_ctx()

    async def go():
        c.net = Net(c.s)
        try:
            return await Auditor(c).run(FREEZE_MINT, {"mint": FREEZE_MINT, "pool": None})
        finally:
            await c.net.close()
    r = asyncio.run(go())
    L = {l["n"]: l for l in r["layers"]}
    print("\n" + "\n".join(f"  {l['n']} {l['name']}: {l['value']} ({'ok' if l['pass'] else 'red' if l['pass'] is False else '-'})" for l in r["layers"]))
    assert L[1]["pass"] is True and L[1]["value"] == "Revoked"
    assert L[2]["pass"] is False and L[2]["value"] == "freeze not revoked"
    assert all(L[n]["value"] == "not checked" and L[n]["pass"] is None for n in range(3, 9))
    assert r["passed"] is False and r["layersChecked"] == 2 and r["reason"] == "freeze not revoked"
    a = c.db.one("SELECT * FROM activity")
    assert a["sub"] == "Auditor · freeze not revoked" and a["right1"] == "Skipped"
