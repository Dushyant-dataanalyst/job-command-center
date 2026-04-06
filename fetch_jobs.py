"""
Dushyant Kapoor — Daily Job Search Agent
Runs on GitHub Actions at 10am IST (4:30 UTC) daily.
Uses Adzuna API (free tier, covers NL + SG + India).
Writes jobs-update.json for the dashboard to consume.
"""

import os, json, requests, datetime, hashlib

# ─── CONFIG ────────────────────────────────────────────────
APP_ID  = os.environ.get("ADZUNA_APP_ID", "")
APP_KEY = os.environ.get("ADZUNA_APP_KEY", "")
BASE    = "https://api.adzuna.com/v1/api/jobs"

PROFILE = {
    "keywords": [
        "program manager analytics",
        "analytics manager data transformation",
        "data transformation lead",
        "AI program manager",
        "data analytics manager",
    ],
    "exclude": ["senior manager", "director", "VP", "vice president", "junior", "intern"],
    "target_companies": [
        "accenture","capgemini","bcg","bain","deloitte","booking.com",
        "amazon","microsoft","databricks","snowflake","visa","mastercard","revolut"
    ],
    "min_salary_inr": 4500000,   # ₹45L
}

SEARCHES = [
    # (country_code, location, label, geo_flag)
    ("nl", "amsterdam",  "🇳🇱 Amsterdam, Netherlands", "NL"),
    ("nl", "netherlands","🇳🇱 Netherlands",            "NL"),
    ("sg", "singapore",  "🇸🇬 Singapore",              "SG"),
    ("in", "bengaluru",  "🇮🇳 Bengaluru, India",       "IN"),
    ("in", "delhi",      "🇮🇳 Delhi, India",           "IN"),
    ("in", "mumbai",     "🇮🇳 Mumbai, India",          "IN"),
]


def fetch(country, location, keyword, pages=2):
    results = []
    for page in range(1, pages + 1):
        try:
            r = requests.get(
                f"{BASE}/{country}/search/{page}",
                params={
                    "app_id":    APP_ID,
                    "app_key":   APP_KEY,
                    "what":      keyword,
                    "where":     location,
                    "results_per_page": 10,
                    "sort_by":   "date",
                    "max_days_old": 3,
                },
                timeout=10,
            )
            if r.status_code == 200:
                results += r.json().get("results", [])
        except Exception as e:
            print(f"  Error fetching {country}/{location}: {e}")
    return results


def score_job(job, geo):
    """Return fit score 1-10 based on title, company, salary."""
    title   = (job.get("title") or "").lower()
    company = (job.get("company", {}).get("display_name") or "").lower()
    desc    = (job.get("description") or "").lower()
    salary  = job.get("salary_max") or job.get("salary_min") or 0

    score = 5  # base

    # Title match
    good_titles = ["program manager","programme manager","analytics manager",
                   "data transformation","ai manager","data manager","analytics lead"]
    if any(t in title for t in good_titles):
        score += 2

    # Target company bonus
    if any(c in company for c in PROFILE["target_companies"]):
        score += 2

    # Exclude bad seniority
    if any(e in title for e in PROFILE["exclude"]):
        score -= 3

    # Description keyword bonus
    power_words = ["alteryx","six sigma","programme","stakeholder","transformation",
                   "python","power bi","azure","databricks","snowflake"]
    score += min(2, sum(1 for w in power_words if w in desc))

    # Geo priority bonus
    if geo == "NL": score += 1  # Tier 1 priority

    return max(1, min(10, score))


def make_tags(job, geo):
    tags = [geo]
    title = (job.get("title") or "").lower()
    company = (job.get("company", {}).get("display_name") or "").lower()
    if any(c in company for c in PROFILE["target_companies"]):
        tags.append("⭐ Target Co")
    if "analytics" in title: tags.append("Analytics")
    if "program" in title or "programme" in title: tags.append("Programme Mgmt")
    if "ai" in title or "data" in title: tags.append("AI/Data")
    if job.get("salary_max"): tags.append(f"Salary listed")
    return tags[:4]


def format_salary(job, geo):
    lo = job.get("salary_min")
    hi = job.get("salary_max")
    if not lo and not hi:
        return "Negotiable"
    currency = "€" if geo == "NL" else "SGD " if geo == "SG" else "₹"
    if lo and hi:
        return f"{currency}{int(lo/1000)}K–{currency}{int(hi/1000)}K"
    return f"{currency}{int((lo or hi)/1000)}K+"


def dedupe(jobs):
    seen, out = set(), []
    for j in jobs:
        key = hashlib.md5((j["title"] + j["company"]).encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            out.append(j)
    return out


def main():
    now   = datetime.datetime.now()
    ist   = now + datetime.timedelta(hours=5, minutes=30)  # IST offset
    print(f"\n🔍 Dushyant Job Refresh — {ist.strftime('%A, %d %b %Y %I:%M %p IST')}\n")

    raw_jobs = []

    for (country, location, label, geo) in SEARCHES:
        for keyword in PROFILE["keywords"][:3]:   # top 3 keywords to stay in free limit
            print(f"  Searching: {keyword} | {label}")
            hits = fetch(country, location, keyword)
            for h in hits:
                h["_geo"]   = geo
                h["_label"] = label
            raw_jobs += hits

    print(f"\n  Raw results: {len(raw_jobs)} → deduplicating...")
    raw_jobs = dedupe(raw_jobs)
    print(f"  Unique results: {len(raw_jobs)}")

    # Score and filter
    scored = []
    for j in raw_jobs:
        geo   = j.get("_geo", "IN")
        score = score_job(j, geo)
        if score < 5:
            continue
        scored.append({
            "id":       abs(hash(j.get("title","") + j.get("company",{}).get("display_name",""))),
            "title":    j.get("title", "Unknown"),
            "company":  j.get("company", {}).get("display_name", "Unknown"),
            "geo":      geo,
            "location": j.get("_label", geo),
            "salary":   format_salary(j, geo),
            "fit":      score,
            "posted":   (j.get("created") or "")[:10] or ist.strftime("%d %b %Y"),
            "url":      j.get("redirect_url") or j.get("adref") or "#",
            "why_fit":  f"Matches AI/Data Programme Manager profile. Score: {score}/10.",
            "tags":     make_tags(j, geo),
        })

    # Sort by fit desc, take top 10
    scored.sort(key=lambda x: -x["fit"])
    top = scored[:10]

    print(f"  Qualified jobs (score ≥ 5): {len(scored)} → showing top {len(top)}")

    # Alert logic
    alert = ""
    perfect = [j for j in top if j["fit"] >= 9]
    target  = [j for j in top if "⭐ Target Co" in j.get("tags", [])]
    if perfect:
        alert = f"🔥 {len(perfect)} exceptional match(es) today — {', '.join(j['company'] for j in perfect[:2])}. Apply immediately."
    elif target:
        alert = f"⭐ Target company roles found: {', '.join(j['company'] for j in target[:2])}. Check now."

    output = {
        "last_refresh": now.isoformat(),
        "refresh_date": ist.strftime("%A, %d %b %Y"),
        "refresh_time": ist.strftime("%I:%M %p IST"),
        "new_jobs":     top,
        "alert":        alert,
        "message":      f"{len(top)} new roles found · Refreshed {ist.strftime('%d %b at %I:%M %p IST')}",
        "total_scanned": len(raw_jobs),
    }

    with open("jobs-update.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✅ jobs-update.json written — {len(top)} jobs, alert: '{alert or 'none'}'")


if __name__ == "__main__":
    main()
