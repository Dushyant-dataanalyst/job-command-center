"""
Forward-looking industry outlook — qualitative, editorial view based on
general sector knowledge, NOT fetched from any data source and not
auto-updated. This is the honest label for it: revisit and rewrite these
periodically rather than trusting them as current fact. Keyed by yfinance's
exact `industry` string first, falling back to `sector`, so new watchlist
additions degrade to "not yet curated" instead of a silent guess.

rating: "High"/"Medium"/"Low" = strength of the secular (5-10yr) tailwind,
not a near-term price call.
"""

BY_INDUSTRY = {
    "Information Technology Services": {
        "rating": "Medium",
        "thesis": "India IT services has shifted from a linear headcount-growth model to one squeezed by AI-driven productivity gains on the client side — deal volumes are healthy but pricing/headcount growth per deal is structurally slower than the 2010s.",
        "drivers": [
            "Global enterprises still outsourcing IT/digital transformation, cloud migration, and now AI-agent integration work",
            "India retains the largest trained tech-services talent pool at the lowest blended cost among viable alternatives",
        ],
        "risks": [
            "AI coding/agent tools compress the billable-hours model this industry is priced on",
            "US visa policy and onshore-hiring pressure raise delivery costs",
        ],
    },
    "Banks - Regional": {
        "rating": "High",
        "thesis": "India's credit-to-GDP ratio is still low versus comparable economies, and private banks keep taking share from PSU banks in retail/SME lending — a multi-year structural runway, not a cyclical bounce.",
        "drivers": [
            "Rising formal-credit penetration as more of the economy moves out of informal lending",
            "Private banks' technology/underwriting edge over PSU peers keeps compounding market share",
        ],
        "risks": [
            "Asset quality is cyclical — a sharp slowdown hits unsecured retail/SME books first",
            "NIM compression risk if rate cuts outpace deposit repricing",
        ],
    },
    "Credit Services": {
        "rating": "High",
        "thesis": "Consumer and SME credit penetration in India is still expanding from a low base, and NBFCs with strong underwriting data continue to out-grow bank balance sheets in the segments banks avoid.",
        "drivers": [
            "Formalization of consumer credit (BNPL-adjacent, used-vehicle, SME) still early-innings",
            "Digital underwriting/collections reduce the cost-to-serve gap vs banks",
        ],
        "risks": [
            "Regulatory tightening on NBFC leverage/co-lending norms",
            "More leverage-sensitive to a credit-cycle downturn than a deposit-funded bank",
        ],
    },
    "Specialty Chemicals": {
        "rating": "Medium",
        "thesis": "India chemicals benefits from the 'China+1' manufacturing diversification trend, but it's a capex-heavy, cyclical business tied to global demand and crude-derivative input costs, not a clean secular compounder.",
        "drivers": [
            "Global buyers actively diversifying supply chains away from China for specialty/agro chemicals",
            "Domestic demand growth in paints/adhesives tracks India construction and auto-refinish activity",
        ],
        "risks": [
            "Chinese overcapacity dumping can compress margins with little warning",
            "Input-cost (crude derivative) volatility flows straight into margins",
        ],
    },
    "Luxury Goods": {
        "rating": "Medium",
        "thesis": "India's premiumization trend (more households crossing into discretionary/luxury spending) is real and structural, but jewelry/watches specifically also carries gold-price and import-duty policy sensitivity that a pure premiumization thesis glosses over.",
        "drivers": [
            "Rising per-capita income pushing more households into branded/organized retail from unorganized jewelers",
            "Wedding/gifting demand in India is culturally resilient even in slower macro years",
        ],
        "risks": [
            "Gold import duty changes directly move margins and demand",
            "Thin net margins mean working-capital and gold-price swings hit profitability disproportionately",
        ],
    },
    "Packaged Foods": {
        "rating": "Medium",
        "thesis": "Structural rural/urban premiumization of packaged food continues, but growth has slowed from the 2010s pace as category penetration matures in urban India — this is a steady compounder, not a fast grower anymore.",
        "drivers": [
            "Continuing shift from unbranded/loose food to packaged/branded, especially in smaller towns",
            "Pricing power from strong brand moats supports margin even when volume growth is modest",
        ],
        "risks": [
            "Rural demand is macro-sensitive (monsoon, agri income) and has been inconsistent",
            "Private-label/D2C competition chipping at the low end",
        ],
    },
    "Household & Personal Products": {
        "rating": "Medium",
        "thesis": "Same structural premiumization tailwind as packaged foods, with an additional headwind from D2C/digital-first challenger brands taking share in personal care specifically.",
        "drivers": [
            "Rural per-capita consumption of branded personal care still below urban levels — runway exists",
            "Distribution moat (reach into small-town/rural retail) is genuinely hard to replicate",
        ],
        "risks": [
            "D2C brands (skincare, D2C personal care) eroding share in higher-margin urban categories",
            "Input cost (palm oil, crude derivatives) volatility",
        ],
    },
    "Drug Manufacturers - Specialty & Generic": {
        "rating": "High",
        "thesis": "Indian pharma has a durable structural role as the world's generics/API supplier, and the CDMO (contract development/manufacturing) shift away from China is a genuine multi-year tailwind on top of the base generics business.",
        "drivers": [
            "US/EU pharma supply-chain diversification away from China favors Indian CDMO capacity",
            "Domestic formulary growth as India's healthcare spend and insurance penetration rise",
        ],
        "risks": [
            "US FDA plant inspections/import alerts can halt a facility's exports with little notice",
            "US generic pricing pressure compresses margins on the base (non-CDMO) business",
        ],
    },
    "Engineering & Construction": {
        "rating": "High",
        "thesis": "India's infrastructure capex cycle (roads, railways, defense, renewable energy, data centers) is a multi-year government-and-private-capex-driven tailwind that large diversified E&C players are structurally positioned to capture.",
        "drivers": [
            "Government capex on infrastructure has been a sustained multi-year budget priority",
            "Private capex (data centers, renewables, semiconductor-adjacent) is now adding to public capex, not just replacing it",
        ],
        "risks": [
            "Order-book-to-execution lag means growth is lumpy and working-capital-heavy",
            "Government capex is budget-cycle and election-cycle sensitive",
        ],
    },
    "Auto Manufacturers": {
        "rating": "Medium",
        "thesis": "India passenger-vehicle penetration (cars per capita) is still low vs comparable economies, a genuine structural runway, but the EV transition is a real disruption risk to incumbents who under-invest, not a tailwind for all players equally.",
        "drivers": [
            "Low car-per-capita base vs China/developed markets means unit volume growth has real runway left",
            "Rising incomes pushing first-time buyers and premiumization (SUV mix) simultaneously",
        ],
        "risks": [
            "EV transition rewards whoever executes the platform switch well and punishes whoever doesn't — incumbency is not automatically protective here",
            "Cyclical — vehicle demand tracks rural income, financing rates, and fuel prices closely",
        ],
    },
}

BY_SECTOR_FALLBACK = {
    "Technology": {"rating": "Medium", "thesis": "Broad technology sector — see industry-level entry for specifics.", "drivers": [], "risks": []},
    "Financial Services": {"rating": "High", "thesis": "Broad financial services — India's credit/financialization runway is structurally long. See industry-level entry for specifics.", "drivers": [], "risks": []},
    "Consumer Defensive": {"rating": "Medium", "thesis": "Broad consumer staples — steady premiumization tailwind, maturing growth rate. See industry-level entry for specifics.", "drivers": [], "risks": []},
    "Consumer Cyclical": {"rating": "Medium", "thesis": "Broad consumer discretionary — tracks India income growth, cyclical. See industry-level entry for specifics.", "drivers": [], "risks": []},
    "Healthcare": {"rating": "High", "thesis": "Broad healthcare/pharma — India's generics/CDMO role is structurally durable. See industry-level entry for specifics.", "drivers": [], "risks": []},
    "Industrials": {"rating": "High", "thesis": "Broad industrials/capex-cycle exposure. See industry-level entry for specifics.", "drivers": [], "risks": []},
    "Basic Materials": {"rating": "Medium", "thesis": "Broad materials/chemicals — cyclical, input-cost sensitive. See industry-level entry for specifics.", "drivers": [], "risks": []},
}


def get_outlook(sector, industry):
    if industry in BY_INDUSTRY:
        return dict(BY_INDUSTRY[industry], source="industry")
    if sector in BY_SECTOR_FALLBACK:
        return dict(BY_SECTOR_FALLBACK[sector], source="sector_fallback")
    return {
        "rating": "Unknown",
        "thesis": f"Not yet curated for sector='{sector}' / industry='{industry}' — add an entry to industry_outlook.py rather than assume a rating.",
        "drivers": [],
        "risks": [],
        "source": "none",
    }
