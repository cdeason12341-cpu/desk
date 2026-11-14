"""The ONLY place FOMO URLs are built. Every URL carries the referral r=savipww."""
from urllib.parse import quote

REF = "savipww"
BASE = "https://fomo.family"
SIGNUP_URL = f"{BASE}/r/{REF}"
# Chain slugs from FOMO's token route /tokens/{chain}/{address}
CHAINS = {"solana": "solana", "sol": "solana", "base": "base", "bnb": "bnb", "bsc": "bnb",
          "robinhood": "robinhood"}


def coin_url(mint: str, chain: str = "solana") -> str:
    slug = CHAINS.get((chain or "").lower())
    if not slug or not mint:
        return SIGNUP_URL
    return f"{BASE}/tokens/{slug}/{quote(mint, safe='')}?r={REF}"


def profile_url(handle: str) -> str:
    if not handle:
        return SIGNUP_URL
    return f"{BASE}/profile/{quote(handle, safe='')}?source=share_link&r={REF}"
