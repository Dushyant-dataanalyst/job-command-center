/**
 * Vercel serverless function — syncs ad-hoc "I BOUGHT" positions from the
 * dashboard's browser localStorage into my_positions.json at the repo root,
 * via GitHub's Contents API, so equity_brain.py (which only runs server-side
 * in CI) can see and live-price them.
 *
 * The GitHub token lives ONLY in this function's environment (Vercel project
 * env var GITHUB_TOKEN) — it is never sent to or readable from the browser.
 * See nse-trading-bot/equity_brain.py for the read side of this sync.
 *
 * Threat model note: this endpoint has no per-user auth (this is a personal,
 * single-user dashboard with no login system) — origin-checking below stops
 * other websites from triggering writes via a victim's browser (CSRF), but
 * does not stop someone who directly crafts a request to this URL. The
 * GitHub token should be a fine-grained PAT scoped to ONLY this repo with
 * ONLY Contents read/write, so the worst case of abuse is spam commits to
 * this one repo, not broader account compromise. Acceptable tradeoff for a
 * low-value personal hobby project; revisit if this ever handles real
 * multi-user data.
 */

const OWNER = 'Dushyant-dataanalyst';
const REPO = 'job-command-center';
const BRANCH = 'master';
const FILE_PATH = 'my_positions.json';
const ALLOWED_ORIGIN = 'https://job-command-center-nu.vercel.app'; // must match the dashboard's actual Vercel domain

function sanitizePositions(input) {
  if (!Array.isArray(input)) return [];
  return input
    .filter((p) => p && typeof p.name === 'string' && p.name.trim())
    .map((p) => ({
      name: String(p.name).toUpperCase().trim().slice(0, 30),
      entry: Number(p.entry) || 0,
      sl: Number(p.sl) || 0,
      t1: Number(p.t1) || 0,
      t2: Number(p.t2) || 0,
      votes_at_buy: Number(p.votes_at_buy) || 0,
      bought_at: String(p.bought_at || '').slice(0, 30),
    }))
    .slice(0, 50);
}

function sanitizeRemoved(input) {
  if (!Array.isArray(input)) return [];
  return input
    .filter((n) => typeof n === 'string' && n.trim())
    .map((n) => String(n).toUpperCase().trim().slice(0, 30))
    .slice(0, 20);
}

const STRATEGY_VOTE_VALUES = new Set(['STRONG_BUY', 'BUY', 'WATCH', 'NO_SIGNAL']);
function sanitizeStrategiesAtBuy(input) {
  if (!input || typeof input !== 'object') return {};
  const out = {};
  for (const name of ['Inna', 'Pham', 'Cianni', 'Unger']) {
    const v = input[name];
    out[name] = STRATEGY_VOTE_VALUES.has(v) ? v : 'NO_SIGNAL';
  }
  return out;
}

// Closed trades accumulate forever (never removed by the user) — capped at
// the most recent 200 so the sync payload/repo file don't grow unbounded,
// matching the same cap used for run_history.json.
function sanitizeClosedTrades(input) {
  if (!Array.isArray(input)) return [];
  return input
    .filter((t) => t && typeof t.name === 'string' && t.name.trim())
    .map((t) => ({
      name: String(t.name).toUpperCase().trim().slice(0, 30),
      entry: Number(t.entry) || 0,
      exit: Number(t.exit) || 0,
      outcome_pct: t.outcome_pct == null ? null : Number(t.outcome_pct),
      votes_at_buy: Number(t.votes_at_buy) || 0,
      strategies_at_buy: sanitizeStrategiesAtBuy(t.strategies_at_buy),
      bought_at: String(t.bought_at || '').slice(0, 30),
      closed_at: String(t.closed_at || '').slice(0, 30),
    }))
    .slice(-200);
}

module.exports = async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin', ALLOWED_ORIGIN);
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    return res.status(204).end();
  }
  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const origin = req.headers.origin || req.headers.referer || '';
  if (origin && !origin.startsWith(ALLOWED_ORIGIN)) {
    return res.status(403).json({ error: 'Origin not allowed' });
  }

  const token = process.env.GITHUB_TOKEN;
  if (!token) {
    return res.status(500).json({ error: 'Server not configured — GITHUB_TOKEN env var missing' });
  }

  const body = req.body || {};
  const sanitized = sanitizePositions(body.positions);
  const removedDefaults = sanitizeRemoved(body.removedDefaults);
  const closedTrades = sanitizeClosedTrades(body.closedTrades);

  const apiBase = `https://api.github.com/repos/${OWNER}/${REPO}/contents/${FILE_PATH}`;
  const headers = {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github+json',
    'Content-Type': 'application/json',
    'User-Agent': 'nse-dashboard-save-positions',
  };

  try {
    let sha;
    const getRes = await fetch(`${apiBase}?ref=${BRANCH}`, { headers });
    if (getRes.ok) {
      const data = await getRes.json();
      sha = data.sha;
    } else if (getRes.status !== 404) {
      throw new Error(`GitHub GET failed: ${getRes.status}`);
    }

    const payload = { positions: sanitized, removedDefaults, closedTrades };
    const content = Buffer.from(JSON.stringify(payload, null, 2)).toString('base64');
    const putRes = await fetch(apiBase, {
      method: 'PUT',
      headers,
      body: JSON.stringify({
        message: `chore: sync ${sanitized.length} ad-hoc position(s), ${removedDefaults.length} removed default(s), ${closedTrades.length} closed trade(s) from dashboard`,
        content,
        branch: BRANCH,
        ...(sha ? { sha } : {}),
      }),
    });

    if (!putRes.ok) {
      const errText = await putRes.text();
      throw new Error(`GitHub PUT failed: ${putRes.status} ${errText}`);
    }

    return res.status(200).json({ ok: true, count: sanitized.length, removed: removedDefaults.length, closed: closedTrades.length });
  } catch (e) {
    return res.status(502).json({ error: 'Sync failed', detail: String((e && e.message) || e) });
  }
}
