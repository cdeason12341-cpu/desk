"""FOMO fees on Solana, per side, after the referral discount. Gas = 0 (FOMO pays it)."""

def fee_side(ticket_usd: float, ref_discount: float = 0.10) -> float:
    t = max(0.0, float(ticket_usd))
    if t <= 47.50:
        base = 0.02 * t          # $5 to $47.50 -> 2%
    elif t <= 190.0:
        base = 0.95              # $47.50 to $190 -> flat $0.95
    else:
        base = 0.005 * t         # above $190 -> 0.50%
    return round(base * (1.0 - ref_discount), 6)


def round_trip(entry_usd: float, exit_usd: float, ref_discount: float = 0.10) -> float:
    return round(fee_side(entry_usd, ref_discount) + fee_side(exit_usd, ref_discount), 6)
