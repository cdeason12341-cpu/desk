"""STEP 13.1 test_links: every FOMO URL carries r=savipww; unknown chain -> SIGNUP_URL; no shortened URLs."""
import json, re, subprocess, pathlib
import links, crypto, secrets_store
from tests.helpers import make_ctx, pair, run

ROOT = pathlib.Path(__file__).resolve().parents[1]
FOMO = re.compile(r"https?://(?:www\.)?fomo\.family[^\s\"'<>)\]]*")
SHORT = re.compile(r"https?://(bit\.ly|t\.co|tinyurl\.com|goo\.gl|ow\.ly|is\.gd|buff\.ly|rebrand\.ly|cutt\.ly|lnkd\.in)/", re.I)
MINT = "DFev755KT1b6nkfAFNj1fyRQMwoVzNywsSBEBamepump"


def ok_url(u):
    return ("r=savipww" in u) or u.rstrip("/").endswith("/r/savipww")


def test_links_module():
    assert links.SIGNUP_URL == "https://fomo.family/r/savipww"
    u = links.coin_url(MINT, "solana")
    assert u == f"https://fomo.family/tokens/solana/{MINT}?r=savipww"
    for ch in ("base", "bnb", "robinhood"):
        assert "r=savipww" in links.coin_url("0xabc", ch)
    assert links.coin_url(MINT, "dogechain") == links.SIGNUP_URL
    assert links.coin_url("", "solana") == links.SIGNUP_URL
    assert links.profile_url("cd12345") == "https://fomo.family/profile/cd12345?source=share_link&r=savipww"


def repo_files():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    return [ROOT / f for f in out if (ROOT / f).is_file()]


def test_grep_repo():
    bad, builders = [], []
    for f in repo_files():
        try:
            t = f.read_text()
        except Exception:
            continue
        for u in FOMO.findall(t):
            if "{" in u:      # template in docs, e.g. /tokens/{chain}/{address}
                continue
            if f.name in ("links.py", "test_links.py") and u == "https://fomo.family":
                continue      # links.BASE origin constant; every URL links.py returns is checked above
            if not ok_url(u):
                bad.append((str(f.relative_to(ROOT)), u))
        if SHORT.search(t):
            bad.append((str(f.relative_to(ROOT)), "shortener"))
        if f.suffix == ".py" and f.name != "links.py" and "tests" not in f.parts and "fomo.family" in t:
            builders.append(str(f.relative_to(ROOT)))
    assert not bad, bad
    assert not builders, f"FOMO URLs built outside links.py: {builders}"


def test_rendered_pushes_and_buttons():
    from agents.executor import buy_buttons, buy_text
    from agents.risk import sell_buttons
    for mode in ("PAPER", "LIVE"):
        for b in buy_buttons(MINT, mode) + sell_buttons(MINT, mode):
            for btn in b:
                if "url" in btn:
                    assert ok_url(btn["url"]) and "/tokens/solana/" in btn["url"], btn
    from telegram import Telegram
    html = Telegram.linkify("PAPER · Buy USPR · $47.50", [("USPR", MINT)])
    for u in re.findall(r'href="([^"]+)"', html):
        assert ok_url(u.replace("&amp;", "&"))


def test_published_files_decrypt_and_links(tmp_path):
    """Account tab + every published file: decrypt, every FOMO URL has r=savipww."""
    import main
    d = main.Desk(db_path=str(tmp_path / "t.db"), log_files=False, with_telegram=False)
    c = d.ctx
    c.db.x("INSERT INTO coins(mint,name,symbol,status,migrated_at,first_seen,night) VALUES(?,?,?,?,?,?,?)",
           (MINT, "USPR", "USPR", "passed", 1, 1, c.night))
    c.db.x("INSERT INTO audits(ts,mint,passed,reason,layer,json,night) VALUES(1,?,0,'freeze not revoked',2,'{}',?)", (MINT, c.night))
    files = d.publisher.builder.build()
    tok = "test-token-" + "x" * 40
    seen = 0
    for rel, obj in files.items():
        env = crypto.encrypt_obj(obj, tok)
        assert set(env) == {"v", "iv", "ct"}
        back = crypto.decrypt_obj(env, tok)
        txt = json.dumps(back)
        for u in FOMO.findall(txt):
            seen += 1
            assert ok_url(u), (rel, u)
    assert seen > 0
    acct = files["account.json"]
    assert acct["signupUrl"] == links.SIGNUP_URL and ok_url(acct["profileUrl"])
