"""
Which legendary long-term investor's playbook this stock fits best —
a transparent, rule-based classifier over the SAME sub-scores screen.py
already computed (quality/growth/valuation/redflags), not a separate
opinion. This is a heuristic labeling of "which style of reasoning this
stock satisfies best", not a claim about what Buffett/Lynch/etc. would
actually buy.
"""

PHILOSOPHIES = {
    "Buffett": "Durable moat + high returns on capital + low debt. Price is secondary to business quality.",
    "Munger": "Quality business at a rational price. Zero tolerance for red flags — 'avoid stupidity' over chasing upside.",
    "Lynch": "Growth at a reasonable price (PEG < 1-1.5). Buy what you understand, in a business that's still growing.",
    "Graham": "Absolute cheapness + margin of safety. Balance-sheet discipline matters more than growth story.",
    "Jhunjhunwala": "Long-runway India growth story, held with conviction through volatility — the industry tailwind matters as much as this quarter's numbers.",
}

_RUNWAY_BONUS = {"High": 1.0, "Medium": 0.5, "Low": 0.0, "Unknown": 0.25}


def classify_investor_style(result, outlook):
    q = result["quality"]["score"] / result["quality"]["of"]
    g = result["growth"]["score"] / result["growth"]["of"]
    v = result["valuation"]["score"] / result["valuation"]["of"]
    r = result["redflags"]["score"] / result["redflags"]["of"]
    runway = _RUNWAY_BONUS.get(outlook.get("rating", "Unknown"), 0.25)

    fits = {
        "Buffett": round(q * 0.7 + r * 0.3, 2),
        "Munger": round(q * 0.5 + r * 0.5, 2),
        "Lynch": round(g * 0.5 + v * 0.5, 2),
        "Graham": round(v * 0.6 + r * 0.4, 2),
        "Jhunjhunwala": round(g * 0.5 + runway * 0.5, 2),
    }
    ranked = sorted(fits.items(), key=lambda kv: kv[1], reverse=True)
    primary, _ = ranked[0]
    runner_up, _ = ranked[1]

    reason_by_style = {
        "Buffett": f"Quality sub-score {result['quality']['score']}/{result['quality']['of']} and red-flag cleanliness {result['redflags']['score']}/{result['redflags']['of']} — moat/low-debt profile matters more here than price.",
        "Munger": f"High quality ({result['quality']['score']}/{result['quality']['of']}) combined with a clean red-flag read ({result['redflags']['score']}/{result['redflags']['of']}) — rational price for a business with nothing obviously wrong.",
        "Lynch": f"Growth {result['growth']['score']}/{result['growth']['of']} paired with valuation {result['valuation']['score']}/{result['valuation']['of']} — still growing and not overpriced for it.",
        "Graham": f"Valuation sub-score {result['valuation']['score']}/{result['valuation']['of']} and balance-sheet cleanliness {result['redflags']['score']}/{result['redflags']['of']} carry this more than the growth story.",
        "Jhunjhunwala": f"Growth {result['growth']['score']}/{result['growth']['of']} plus a '{outlook.get('rating', 'Unknown')}' secular industry tailwind — the long-runway story outweighs this quarter's valuation.",
    }

    return {
        "primary": primary,
        "primary_philosophy": PHILOSOPHIES[primary],
        "reason": reason_by_style[primary],
        "runner_up": runner_up,
        "all_fit_scores": fits,
    }
