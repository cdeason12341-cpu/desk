"""Secrets grep: no secret value (whole, or a PEM body line) in the repo, web/, logs/, published data/, shots/ or the db.
Covers GMGN_API_KEY + GMGN_SIGNING_KEY with TYPESAFE_API_KEY, TG_TOKEN, VAPID_PRIVATE_KEY, APP_TOKEN, DATA_PATH, GIST_ID.
Never prints a value: failures name the secret and the file only."""
import pathlib, subprocess
import secrets_store

ROOT = pathlib.Path(__file__).resolve().parents[1]
NAMES = ("GMGN_API_KEY", "GMGN_SIGNING_KEY", "TYPESAFE_API_KEY", "TG_TOKEN", "VAPID_PRIVATE_KEY", "APP_TOKEN", "DATA_PATH", "GIST_ID")


def needles():
    out = []
    for n in NAMES:
        v = secrets_store.get(n)
        if not v:
            continue
        out.append((n, v))
        if "PRIVATE KEY-----" in v:
            out += [(n, ln.strip()) for ln in v.splitlines() if ln.strip() and "-----" not in ln and len(ln.strip()) >= 16]
    return out


def targets():
    files = set()
    ls = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    files |= {ROOT / f for f in ls}
    for d in ("web", "logs", "data", "shots"):
        if (ROOT / d).exists():
            files |= {p for p in (ROOT / d).rglob("*") if p.is_file()}
    files |= {ROOT / f for f in ("desk.db", "desk.db-wal") if (ROOT / f).exists()}
    return [f for f in files if f.is_file() and ".secrets" not in f.parts]


def test_no_secret_anywhere():
    nd = needles()
    bad = []
    for f in targets():
        b = f.read_bytes()
        for n, v in nd:
            if v.encode() in b or v in str(f.relative_to(ROOT)):
                bad.append((n, str(f.relative_to(ROOT))))
    assert not bad, sorted(set(bad))


def test_secrets_never_tracked():
    tracked = subprocess.run(["git", "ls-files", ".secrets"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert tracked == ""
    assert ".secrets/" in (ROOT / ".gitignore").read_text().split()
    for n in ("GMGN_API_KEY", "GMGN_SIGNING_KEY"):
        p = secrets_store.DIR / n
        if p.exists():
            assert (p.stat().st_mode & 0o077) == 0, f"{n} must be owner-only"


def test_gmgn_errors_redacted(monkeypatch):
    import gmgn
    monkeypatch.setattr(secrets_store, "all_values", lambda: ["SUPERSECRETKEY123"])
    e = gmgn.GmgnError(401, 401, "AUTH", "bad key SUPERSECRETKEY123")
    assert "SUPERSECRETKEY123" not in str(e)
    g = gmgn.Gmgn(api_key="SUPERSECRETKEY123", pem="")
    assert "SUPERSECRETKEY123" not in repr(g)
