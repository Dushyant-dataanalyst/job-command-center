"""
System B scoring engine — Master Brief Part 4. Each stock scored 0-100
across QUALITY (35) / GROWTH+RUNWAY (30) / VALUATION (20) / INDIA RED FLAGS
(15, hard-fail zeroes the total). Every sub-score carries a plain-English
reason string, same discipline as the rest of this project (no unexplained
numbers).

yfinance's annual financials/balance_sheet/cashflow give up to 5 columns
(newest first) but the oldest is frequently all-NaN for NSE tickers, so
"5-year consistency" checks below work off however many usable years are
actually present (typically 3-4) rather than assuming a fixed count.
"""
import json
import pathlib

import config
from industry_outlook import get_outlook
from investor_style import classify_investor_style

REDFLAGS_PATH = pathlib.Path(__file__).parent / config.MANUAL_REDFLAGS_FILE


def _usable_years(row, min_years=2):
    """row: a pandas Series indexed by year-end Timestamps, newest first.
    Returns [(timestamp, value), ...] oldest-first, dropping NaN columns."""
    pairs = [(col, row[col]) for col in row.index if row[col] == row[col]]  # NaN != NaN
    pairs.sort(key=lambda p: p[0])
    return pairs if len(pairs) >= min_years else []


def _cagr(oldest_val, newest_val, n_periods):
    if oldest_val is None or newest_val is None or oldest_val <= 0 or n_periods <= 0:
        return None
    return (newest_val / oldest_val) ** (1 / n_periods) - 1


def score_quality(data):
    fin, bs, cf = data["financials"], data["balance_sheet"], data["cashflow"]
    reasons = []
    points = 0.0  # out of 35

    # --- ROE consistency (15 pts) ---
    roe_pts = 0.0
    try:
        ni_years = dict(_usable_years(fin.loc["Net Income"]))
        eq_years = dict(_usable_years(bs.loc["Stockholders Equity"]))
        common = sorted(set(ni_years) & set(eq_years))
        roes = [ni_years[c] / eq_years[c] for c in common if eq_years[c]]
        if roes:
            avg_roe = sum(roes) / len(roes)
            min_roe = min(roes)
            if avg_roe >= config.ROE_EXCELLENT and min_roe >= config.ROE_GOOD:
                roe_pts = 15.0
                reasons.append(f"ROE strong and consistent — avg {avg_roe:.1%} across {len(roes)} yrs, never below {min_roe:.1%}")
            elif avg_roe >= config.ROE_GOOD:
                roe_pts = 10.0
                reasons.append(f"ROE decent — avg {avg_roe:.1%} across {len(roes)} yrs (brief's 15-18% bar), min {min_roe:.1%}")
            elif avg_roe >= 0.10:
                roe_pts = 5.0
                reasons.append(f"ROE below the brief's 15-18% bar — avg {avg_roe:.1%} across {len(roes)} yrs")
            else:
                reasons.append(f"ROE weak — avg {avg_roe:.1%} across {len(roes)} yrs")
        else:
            reasons.append("ROE: insufficient multi-year data from yfinance")
    except (KeyError, ZeroDivisionError):
        reasons.append("ROE: required rows not present in yfinance data for this ticker")
    points += roe_pts

    # --- Debt/Equity (10 pts) ---
    de_pts = 0.0
    try:
        debt_years = dict(_usable_years(bs.loc["Total Debt"], min_years=1))
        eq_years = dict(_usable_years(bs.loc["Stockholders Equity"], min_years=1))
        latest_col = max(set(debt_years) & set(eq_years))
        de = debt_years[latest_col] / eq_years[latest_col] if eq_years[latest_col] else None
        if de is not None:
            if de < 0.3:
                de_pts = 10.0
                reasons.append(f"Very low debt — D/E {de:.2f} (brief's cap is {config.MAX_DEBT_EQUITY})")
            elif de < config.MAX_DEBT_EQUITY:
                de_pts = 8.0
                reasons.append(f"Low debt — D/E {de:.2f}, under the {config.MAX_DEBT_EQUITY} cap")
            elif de < 1.0:
                de_pts = 3.0
                reasons.append(f"D/E {de:.2f} — above the brief's {config.MAX_DEBT_EQUITY} cap")
            else:
                reasons.append(f"D/E {de:.2f} — high leverage, well above the {config.MAX_DEBT_EQUITY} cap")
    except (KeyError, ValueError):
        reasons.append("D/E: required rows not present in yfinance data for this ticker")
    points += de_pts

    # --- FCF positive & growing (10 pts) ---
    fcf_pts = 0.0
    try:
        fcf_years = _usable_years(cf.loc["Free Cash Flow"])
        vals = [v for _, v in fcf_years]
        if vals:
            all_positive = all(v > 0 for v in vals)
            growing = vals[-1] > vals[0] if len(vals) >= 2 else None
            if all_positive and growing:
                fcf_pts = 10.0
                reasons.append(f"FCF positive across all {len(vals)} yrs and growing (oldest to newest {vals[0]/1e7:.0f}Cr -> {vals[-1]/1e7:.0f}Cr)")
            elif all_positive:
                fcf_pts = 6.0
                reasons.append(f"FCF positive across all {len(vals)} yrs but not consistently growing")
            elif vals[-1] > 0:
                fcf_pts = 3.0
                reasons.append("FCF currently positive but was negative in an earlier year (capex cycle or one-off)")
            else:
                reasons.append("FCF currently negative")
    except (KeyError, IndexError):
        reasons.append("FCF: required rows not present in yfinance data for this ticker")
    points += fcf_pts

    return round(points, 1), reasons


def score_growth(data):
    fin = data["financials"]
    reasons = []
    points = 0.0  # out of 30

    # --- Revenue CAGR + lumpiness (12 pts) ---
    rev_pts = 0.0
    try:
        rev_years = _usable_years(fin.loc["Total Revenue"])
        vals = [v for _, v in rev_years]
        if len(vals) >= 3:
            cagr = _cagr(vals[0], vals[-1], len(vals) - 1)
            yoy = [(vals[i] - vals[i - 1]) / vals[i - 1] for i in range(1, len(vals)) if vals[i - 1]]
            avg_yoy = sum(yoy) / len(yoy) if yoy else 0
            lumpy = any(y > 0 and avg_yoy > 0 and y > 2 * avg_yoy for y in yoy)
            if cagr is not None and cagr >= 0.10 and not lumpy:
                rev_pts = 12.0
                reasons.append(f"Revenue CAGR {cagr:.1%} over {len(vals)-1} yrs, steady (no single lumpy year)")
            elif cagr is not None and cagr >= 0.05:
                rev_pts = 8.0
                reasons.append(f"Revenue CAGR {cagr:.1%} over {len(vals)-1} yrs" + (" — one year looks lumpy vs the trend" if lumpy else ""))
            elif cagr is not None and cagr > 0:
                rev_pts = 4.0
                reasons.append(f"Revenue growing slowly — CAGR {cagr:.1%} over {len(vals)-1} yrs")
            else:
                reasons.append(f"Revenue CAGR {cagr:.1%} — flat or declining" if cagr is not None else "Revenue CAGR: could not compute")
        else:
            reasons.append("Revenue: fewer than 3 usable years from yfinance")
    except (KeyError, ZeroDivisionError):
        reasons.append("Revenue: required rows not present in yfinance data for this ticker")
    points += rev_pts

    # --- Earnings (EPS proxy via Net Income) CAGR (12 pts) ---
    eps_pts = 0.0
    try:
        ni_years = _usable_years(fin.loc["Net Income"])
        vals = [v for _, v in ni_years]
        if len(vals) >= 3 and vals[0] > 0:
            cagr = _cagr(vals[0], vals[-1], len(vals) - 1)
            if cagr is not None and cagr >= 0.12:
                eps_pts = 12.0
                reasons.append(f"Earnings CAGR {cagr:.1%} over {len(vals)-1} yrs")
            elif cagr is not None and cagr >= 0.06:
                eps_pts = 8.0
                reasons.append(f"Earnings CAGR {cagr:.1%} over {len(vals)-1} yrs — solid, not exceptional")
            elif cagr is not None and cagr > 0:
                eps_pts = 4.0
                reasons.append(f"Earnings growing slowly — CAGR {cagr:.1%} over {len(vals)-1} yrs")
            else:
                reasons.append("Earnings flat or declining")
        else:
            reasons.append("Earnings: fewer than 3 usable positive-base years from yfinance")
    except (KeyError, ZeroDivisionError):
        reasons.append("Earnings: required rows not present in yfinance data for this ticker")
    points += eps_pts

    # --- Gross margin stability (6 pts bonus) ---
    margin_pts = 0.0
    try:
        rev_years = dict(_usable_years(fin.loc["Total Revenue"]))
        gp_years = dict(_usable_years(fin.loc["Gross Profit"]))
        common = sorted(set(rev_years) & set(gp_years))
        margins = [gp_years[c] / rev_years[c] for c in common if rev_years[c]]
        if len(margins) >= 2:
            spread = max(margins) - min(margins)
            if margins[-1] >= margins[0] and spread < 0.05:
                margin_pts = 6.0
                reasons.append(f"Gross margin stable/expanding — {margins[0]:.1%} to {margins[-1]:.1%}")
            elif spread < 0.08:
                margin_pts = 3.0
                reasons.append(f"Gross margin roughly stable — {margins[0]:.1%} to {margins[-1]:.1%}")
            else:
                reasons.append(f"Gross margin volatile — ranged {min(margins):.1%} to {max(margins):.1%}")
    except (KeyError, ZeroDivisionError):
        pass  # bonus category — silently skip rather than clutter reasons with a 3rd missing-data note
    points += margin_pts

    return round(points, 1), reasons


def score_valuation(data):
    info = data["info"]
    reasons = []
    points = 0.0  # out of 20

    peg = info.get("pegRatio")
    pe = info.get("trailingPE")

    if peg and peg > 0:
        if peg < 1.0:
            points = 20.0
            reasons.append(f"PEG {peg:.2f} — growth at a genuine discount (Lynch's <1 bar)")
        elif peg < 1.5:
            points = 15.0
            reasons.append(f"PEG {peg:.2f} — reasonably priced for its growth")
        elif peg < 2.0:
            points = 10.0
            reasons.append(f"PEG {peg:.2f} — priced above the classic 1.0-1.5 comfort zone")
        elif peg < 3.0:
            points = 5.0
            reasons.append(f"PEG {peg:.2f} — expensive relative to growth")
        else:
            reasons.append(f"PEG {peg:.2f} — very expensive relative to growth")
    elif pe:
        # Fallback heuristic — the brief asks for PE vs the stock's own 5-year
        # median, which needs historical EPS-matched-to-price data this free
        # source doesn't cleanly provide. Using fixed reasonableness bands
        # instead; documented here rather than silently substituted.
        if pe < 15:
            points = 18.0
            reasons.append(f"Trailing PE {pe:.1f} — cheap in absolute terms (PEG unavailable, used fixed bands not 5yr median)")
        elif pe < 25:
            points = 14.0
            reasons.append(f"Trailing PE {pe:.1f} — reasonable (PEG unavailable, used fixed bands not 5yr median)")
        elif pe < 40:
            points = 8.0
            reasons.append(f"Trailing PE {pe:.1f} — rich (PEG unavailable, used fixed bands not 5yr median)")
        elif pe < 60:
            points = 4.0
            reasons.append(f"Trailing PE {pe:.1f} — expensive (PEG unavailable, used fixed bands not 5yr median)")
        else:
            reasons.append(f"Trailing PE {pe:.1f} — very expensive (PEG unavailable, used fixed bands not 5yr median)")
    else:
        reasons.append("Valuation: no PEG or PE available from yfinance")

    return round(points, 1), reasons


def _load_manual_redflags():
    if not REDFLAGS_PATH.exists():
        return {}
    try:
        data = json.loads(REDFLAGS_PATH.read_text(encoding="utf-8"))
        return data.get("stocks", {})
    except Exception:
        return {}


def score_redflags(ticker, data, manual_redflags):
    fin, cf = data["financials"], data["cashflow"]
    reasons = []
    points = 15.0  # out of 15, starts full and gets deducted
    hard_fail = None

    manual = manual_redflags.get(ticker)
    if manual is None:
        points -= 5.0  # unknown isn't rewarded as clean, but isn't a hard fail either
        reasons.append("PENDING_MANUAL_CHECK: promoter pledging/holding trend not yet entered in manual_redflags.json")
    else:
        pledging = manual.get("promoter_pledging_pct", 0.0)
        trend = manual.get("promoter_holding_trend", "stable")
        if pledging is not None and pledging > config.PLEDGING_HARD_FAIL_PCT:
            hard_fail = f"HARD FAIL: promoter pledging {pledging}% exceeds the {config.PLEDGING_HARD_FAIL_PCT}% threshold"
        elif trend == "falling":
            hard_fail = "HARD FAIL: promoter holding trend is falling"
        else:
            reasons.append(f"Promoter pledging {pledging}%, holding trend '{trend}' — as of {manual.get('as_of', 'unknown date')}")
        if manual.get("auditor_changes_flag"):
            points -= 3.0
            reasons.append("Penalized: frequent auditor changes flagged")
        if manual.get("related_party_flag"):
            points -= 3.0
            reasons.append("Penalized: heavy related-party transactions flagged")

    # Automated check: recurring negative operating cash flow despite reported profit
    try:
        ni_years = dict(_usable_years(fin.loc["Net Income"]))
        ocf_years = dict(_usable_years(cf.loc["Operating Cash Flow"]))
        common = sorted(set(ni_years) & set(ocf_years))
        bad_years = sum(1 for c in common if ni_years[c] > 0 and ocf_years[c] < 0)
        if bad_years >= 2:
            points -= 4.0
            reasons.append(f"Penalized: {bad_years} yrs of reported profit but negative operating cash flow — earnings quality concern")
        elif bad_years == 1:
            points -= 2.0
            reasons.append("Penalized: 1 yr of reported profit but negative operating cash flow")
    except (KeyError, ZeroDivisionError):
        pass

    if hard_fail:
        return 0.0, [hard_fail], hard_fail
    return round(max(points, 0.0), 1), reasons, None


def _build_summary(ticker, result, outlook, style):
    """One paragraph tying together: the strongest reason this scored well,
    the forward industry view, and which investor philosophy it matches —
    always present in the output, per standing instruction."""
    categories = [
        ("quality", result["quality"]),
        ("growth", result["growth"]),
        ("valuation", result["valuation"]),
    ]
    best_cat, best = max(categories, key=lambda kv: (kv[1]["score"] / kv[1]["of"]) if kv[1]["of"] else 0)
    headline_reason = best["reasons"][0] if best["reasons"] else f"strongest on {best_cat}"

    outlook_line = f"{outlook['rating']} secular tailwind — {outlook['thesis']}" if outlook.get("rating") != "Unknown" else outlook["thesis"]

    return (
        f"Why: {headline_reason}. "
        f"Industry outlook ({outlook.get('source', 'none')}): {outlook_line} "
        f"Closest investor style: {style['primary']} ({style['primary_philosophy']}) — {style['reason']}"
    )


def score_stock(ticker, data, theme=None):
    if "error" in data:
        return {"ticker": ticker, "error": data["error"], "composite": None, "theme": theme}

    manual_redflags = _load_manual_redflags()
    info = data["info"]
    sector, industry = info.get("sector"), info.get("industry")
    outlook = get_outlook(sector, industry)

    q_pts, q_reasons = score_quality(data)
    g_pts, g_reasons = score_growth(data)
    v_pts, v_reasons = score_valuation(data)
    r_pts, r_reasons, hard_fail = score_redflags(ticker, data, manual_redflags)

    composite = 0.0 if hard_fail else round(q_pts + g_pts + v_pts + r_pts, 1)

    result = {
        "ticker": ticker,
        "composite": composite,
        "hard_fail": hard_fail,
        "theme": theme,
        "sector": sector,
        "industry": industry,
        "quality": {"score": q_pts, "of": config.WEIGHT_QUALITY, "reasons": q_reasons},
        "growth": {"score": g_pts, "of": config.WEIGHT_GROWTH, "reasons": g_reasons},
        "valuation": {"score": v_pts, "of": config.WEIGHT_VALUATION, "reasons": v_reasons},
        "redflags": {"score": r_pts, "of": config.WEIGHT_REDFLAGS, "reasons": r_reasons},
        "industry_outlook": outlook,
    }

    style = classify_investor_style(result, outlook)
    result["investor_style_match"] = style
    result["summary"] = _build_summary(ticker, result, outlook, style)

    return result
