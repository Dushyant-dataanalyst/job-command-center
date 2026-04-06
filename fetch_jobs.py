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
    "min_salary_inr": 4500000,
}

SEARCHES = [
    ("nl", "amsterdam",   "Amsterdam, Netherlands", "NL"),
    ("nl", "netherlands", "Netherlands",            "NL"),
    ("sg", "singapore",   "Singapore",              "SG"),
    ("in", "bengaluru",   "Bengaluru, India",       "IN"),
    ("in", "delhi",       "Delhi, India",           "IN"),
    ("in", "mumbai",      "Mumbai, India",          "IN"),
]


def safe_str(value):
    """Safely extract a string from any value — handles str, dict, None."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return str(value.get("display_name") or value.get("name") or "")
    return str(value)


def fetch(country, location, keyword, pages=2):
    results = []
    for page in range(1, pages + 1):
        try:
            r = requests.get(
                f"{BASE}/{country}/search/{page}",
                params={
                    "app_id":           APP_ID,
                    "app_key":          APP_KEY,
                    "what":             keyword,
                    "where":            location,
                    "results_per_page": 10,
                    "sort_by":          "date",
                    "max_days_old":     3,
                },
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                results += data.get("results", [])
            else:
                print(f"  API {r.status_code} for {country}/{location}")
        except Exception as e:
            print(f"  Error fetching {country}/{location}: {e}")
    return results


def get_title(job):
    return safe_str(job.get("title"))


def get_company(job):
    return safe_str(job.get("company"))


def score_job(job, geo):
    title   = get_title(job).lower()
    company = get_company(job).lower()
    desc    = safe_str(job.get("description")).lower()

    score = 5

    good_titles = ["program manager","programme manager","analytics manager",
                   "data transformation","ai manager","data manager","analytics lead"]
    if any(t in title for t in good_titles):
        score += 2

    if any(c in company for c in PROFILE["target_companies"]):
        score += 2

    if any(e in title for e in PROFILE["exclude"]):
        score -= 3

    power_words = ["alteryx","six sigma","programme","stakeholder","transformation",
                   "python","power bi","azure","databricks","snowflake"]
    score += min(2, sum(1 for w in power_words if w in desc))

    if geo == "NL":
        score += 1

    return max(1, min(10, score))


def make_tags(job, geo):
    tags    = [geo]
    title   = get_title(job).lower()
    company = get_company(job).lower()
    if any(c in company for c in PROFILE["target_companies"]):
        tags.append("Target Co")
    if "analytics" in title:
        tags.append("Analytics")
    if "program" in title or "programme" in title:
        tags.append("Programme Mgmt")
    if "ai" in title or "data" in title:
        tags.append("AI/Data")
    if job.get("salary_max"):
        tags.append("Salary listed")
    return tags[:4]


def format_salary(job, geo):
    lo = job.get("salary_min")
    hi = job.get("salary_max")
    if not lo and not hi:
        return "Negotiable"
    currency = "EUR " if geo == "NL" else "SGD " if geo == "SG" else "INR "
    if lo and hi:
        return f"{currency}{int(lo/1000)}K-{int(hi/1000)}K"
    return f"{currency}{int((lo or hi)/1000)}K+"


def dedupe(jobs):
    seen, out = set(), []
    for j in jobs:
        try:
            title   = get_title(j)
            company = get_company(j)
            key     = hashlib.md5((title + company).encode("utf-8", errors="replace")).hexdigest()
            if key not in seen:
                seen.add(key)
                out.append(j)
        except Exception as e:
            print(f"  Skipping job in dedupe: {e}")
            out.append(j)
    return out


def load_rejection_filters():
    """Load dynamic filters from rejection_reasons.json if it exists."""
    filters = {"companies": [], "reason_counts": {}, "raise_salary_floor": False}
    path = "rejection_reasons.json"
    if not os.path.exists(path):
        return filters
    try:
        with open(path) as f:
            entries = json.load(f)
        for e in entries:
            code = e.get("reason_code","")
            filters["reason_counts"][code] = filters["reason_counts"].get(code, 0) + 1
            co = (e.get("company") or "").lower().strip()
            if co and co not in filters["companies"]:
                filters["companies"].append(co)
        # If SALARY_LOW triggered 3+ times → raise salary floor flag
        if filters["reason_counts"].get("SALARY_LOW", 0) >= 3:
            filters["raise_salary_floor"] = True
        print(f"  Rejection filters loaded: {len(filters['companies'])} skipped companies, patterns: {filters['reason_counts']}")
    except Exception as e:
        print(f"  Could not load rejection_reasons.json: {e}")
    return filters


def passes_rejection_filter(job, geo, dyn_filters):
    """Return False if this job should be skipped."""
    title   = safe_str(job.get("title")).lower()
    company = safe_str(job.get("company")).lower()

    # Hardcoded filters
    if any(e in title for e in ["junior","intern","trainee","graduate"]):
        return False
    if "uae" in title or "dubai" in title:
        return False

    # Salary floor (hardcoded)
    salary = job.get("salary_max") or job.get("salary_min") or 0
    min_salary = {"NL": 60000, "SG": 100000, "IN": 3500000}
    if salary and salary < min_salary.get(geo, 0):
        return False

    # Raise floor if SALARY_LOW pattern detected 3+ times
    if dyn_filters.get("raise_salary_floor") and salary:
        raised = {"NL": 65000, "SG": 105000, "IN": 4000000}
        if salary < raised.get(geo, 0):
            return False

    # Skip companies Dushyant explicitly rejected
    if any(c in company for c in dyn_filters.get("companies", [])):
        return False

    return True


def main():
    now = datetime.datetime.utcnow()
    ist = now + datetime.timedelta(hours=5, minutes=30)
    print(f"\nDushyant Job Refresh - {ist.strftime('%A, %d %b %Y %I:%M %p IST')}\n")

    dyn_filters = load_rejection_filters()

    if not APP_ID or not APP_KEY:
        print("ERROR: ADZUNA_APP_ID or ADZUNA_APP_KEY not set. Check GitHub Secrets.")
        # Write a placeholder so the workflow does not fail the commit step
        output = {
            "last_refresh":  now.isoformat(),
            "refresh_date":  ist.strftime("%A, %d %b %Y"),
            "refresh_time":  ist.strftime("%I:%M %p IST"),
            "new_jobs":      [],
            "alert":         "API keys missing. Add ADZUNA_APP_ID and ADZUNA_APP_KEY to GitHub Secrets.",
            "message":       "API keys not configured.",
            "total_scanned": 0,
        }
        with open("jobs-update.json", "w") as f:
            json.dump(output, f, indent=2)
        return

    raw_jobs = []

    for (country, location, label, geo) in SEARCHES:
        for keyword in PROFILE["keywords"][:3]:
            print(f"  Searching: {keyword} | {label}")
            hits = fetch(country, location, keyword)
            for h in hits:
                h["_geo"]   = geo
                h["_label"] = label
            raw_jobs += hits

    print(f"\n  Raw results: {len(raw_jobs)} - deduplicating...")
    raw_jobs = dedupe(raw_jobs)
    print(f"  Unique results: {len(raw_jobs)}")

    scored = []
    skipped_by_filter = 0
    for j in raw_jobs:
        try:
            geo   = j.get("_geo", "IN")
            if not passes_rejection_filter(j, geo, dyn_filters):
                skipped_by_filter += 1
                continue
            score = score_job(j, geo)
            if score < 5:
                continue
            scored.append({
                "id":       hashlib.md5((get_title(j) + get_company(j)).encode("utf-8", errors="replace")).hexdigest()[:12],
                "title":    get_title(j) or "Unknown Role",
                "company":  get_company(j) or "Unknown Company",
                "geo":      geo,
                "location": j.get("_label", geo),
                "salary":   format_salary(j, geo),
                "fit":      score,
                "posted":   (safe_str(j.get("created")) or "")[:10] or ist.strftime("%d %b %Y"),
                "url":      j.get("redirect_url") or j.get("adref") or "#",
                "why_fit":  f"Matches AI/Data Programme Manager profile. Score: {score}/10.",
                "tags":     make_tags(j, geo),
            })
        except Exception as e:
            print(f"  Skipping job in scoring: {e}")

    scored.sort(key=lambda x: -x["fit"])
    top = scored[:10]

    print(f"  Filtered by rejection rules: {skipped_by_filter}")
    print(f"  Qualified jobs (score >= 5): {len(scored)} - showing top {len(top)}")

    alert  = ""
    perfect = [j for j in top if j["fit"] >= 9]
    target  = [j for j in top if "Target Co" in j.get("tags", [])]
    if perfect:
        alert = f"Exceptional match(es) today: {', '.join(j['company'] for j in perfect[:2])}. Apply immediately."
    elif target:
        alert = f"Target company roles found: {', '.join(j['company'] for j in target[:2])}. Check now."

    output = {
        "last_refresh":  now.isoformat(),
        "refresh_date":  ist.strftime("%A, %d %b %Y"),
        "refresh_time":  ist.strftime("%I:%M %p IST"),
        "new_jobs":      top,
        "alert":         alert,
        "message":       f"{len(top)} new roles found - Refreshed {ist.strftime('%d %b at %I:%M %p IST')}",
        "total_scanned": len(raw_jobs),
    }

    with open("jobs-update.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone - jobs-update.json written with {len(top)} jobs. Alert: {alert or 'none'}")


if __name__ == "__main__":
    main()
