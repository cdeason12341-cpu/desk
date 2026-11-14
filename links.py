"""The ONLY place FOMO URLs are built. Every URL carries the referral r=savipww."""
import logging

CHAIN_ID = {"solana": 1399811149, "robinhood": 4663, "base": 8453, "bnb": 56}
SIGNUP_URL = "https://fomo.family/r/savipww"
_log = logging.getLogger("desk.links")


def coin_url(address, chain="solana"):
    if chain not in CHAIN_ID:
        _log.warning(f"coin_url: unknown chain {chain!r} for {address!r}, returning SIGNUP_URL")
        return SIGNUP_URL
    return f"https://fomo.family/coin?address={address}&chainId={CHAIN_ID[chain]}&r=savipww&source=share_link"


def profile_url(handle):
    h = handle.lstrip("@")
    return f"https://fomo.family/profile/{h}?source=share_link&r=savipww"
