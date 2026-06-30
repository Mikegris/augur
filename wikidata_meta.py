"""
Wikidata SPARQL — corporate-metadata enrichment.

Free, keyless SPARQL endpoint at query.wikidata.org. We resolve a stock
ticker → Wikidata entity, then pull a fixed bundle of facts: HQ city,
country, inception date, employee count, CEO name, parent company,
website, industry. Useful "at-a-glance" context for the Research view.

Wikidata is community-maintained so coverage varies (Apple has rich data,
small-caps may have nothing). We return whatever's available + flag
missing fields explicitly.

Cache aggressively — corporate metadata changes maybe yearly.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

log = logging.getLogger("augur.wikidata")

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_SEARCH = "https://www.wikidata.org/w/api.php"
HEADERS = {
    "User-Agent": "AUGUR/1.0 wealth-tracker (research@augur-app.org)",
    "Accept": "application/sparql-results+json",
}

# Wikidata's P249 (ticker symbol) is sparsely populated — Apple's entity
# has no ticker stored at all. For reliability we hardcode the top US
# tickers → Wikidata QID. Anything not in this map falls back to a label
# search (wbsearchentities), which works for any company with a clean name.
TICKER_TO_QID = {
    "AAPL":"Q312", "MSFT":"Q2283", "GOOGL":"Q20800404", "GOOG":"Q20800404",
    "AMZN":"Q3884", "META":"Q380", "NVDA":"Q182477", "TSLA":"Q478214",
    "BRK.A":"Q217583", "BRK.B":"Q217583", "JPM":"Q192314", "JNJ":"Q333718",
    "V":"Q328880", "WMT":"Q483551", "PG":"Q203409", "MA":"Q44150",
    "UNH":"Q2103532", "HD":"Q864407", "BAC":"Q487907", "PFE":"Q207137",
    "XOM":"Q156238", "DIS":"Q7414", "KO":"Q103476", "PEP":"Q334777",
    "CVX":"Q319642", "ABBV":"Q502090", "MRK":"Q58863", "ABT":"Q300193",
    "ADBE":"Q189533", "NFLX":"Q907311", "T":"Q35476", "VZ":"Q467752",
    "CRM":"Q941127", "ORCL":"Q41506", "INTC":"Q248", "AMD":"Q128896",
    "CSCO":"Q34735", "QCOM":"Q160120", "TXN":"Q174684", "IBM":"Q37156",
    "NKE":"Q483915", "MCD":"Q38076", "SBUX":"Q37158", "BA":"Q66",
    "CAT":"Q459965", "GS":"Q193326", "MS":"Q1226523", "C":"Q219508",
    "WFC":"Q744149", "BLK":"Q1064902", "AXP":"Q126032",
    "GME":"Q1145897", "AMC":"Q272681", "PLTR":"Q24941398",
    "COIN":"Q98770591", "HOOD":"Q101076869", "SHOP":"Q3500117",
    "UBER":"Q15852335", "ABNB":"Q21709512",
}

_CACHE: dict = {}
DEFAULT_TTL = 7 * 24 * 3600  # 1 week


def _cache_get(key):
    hit = _CACHE.get(key)
    if not hit: return None
    val, exp = hit
    if time.time() > exp:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_set(key, val, ttl):
    _CACHE[key] = (val, time.time() + ttl)


def _sparql(query: str) -> Optional[list]:
    try:
        resp = requests.get(
            WIKIDATA_SPARQL,
            params={"query": query, "format": "json"},
            headers=HEADERS,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("results", {}).get("bindings", [])
    except Exception as e:
        log.debug("wikidata sparql failed: %s", e)
        return None


def _entity_for_ticker(symbol: str) -> Optional[str]:
    """Resolve ticker → Wikidata QID. Tries the hardcoded map first (covers
    top US names where Wikidata's P249 is missing), then falls back to
    label search via wbsearchentities for anything else."""
    sym_upper = symbol.upper()
    # Try the raw symbol and a "-"->"." swap so we accept both yfinance form
    # (BRK-B) and Bloomberg/SEC form (BRK.B) — the map is keyed on dots and
    # the previous "strip after dash" stripping turned BRK-B into BRK, which
    # missed the BRK.B entry entirely.
    candidates = [sym_upper, sym_upper.replace("-", "."), sym_upper.split("-")[0]]
    for cand in candidates:
        if cand in TICKER_TO_QID:
            return TICKER_TO_QID[cand]
    sym = candidates[-1]
    # Fallback: search for the ticker as a literal — sometimes the symbol
    # appears in an alias or description.
    try:
        resp = requests.get(
            WIKIDATA_SEARCH,
            params={
                "action": "wbsearchentities",
                "search": sym,
                "language": "en",
                "format": "json",
                "type": "item",
                "limit": 5,
            },
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=10,
        )
        resp.raise_for_status()
        for hit in resp.json().get("search", []):
            desc = (hit.get("description") or "").lower()
            # Prefer entries that look like a company
            if any(w in desc for w in ("company", "corporation", "inc.", "holdings", "group")):
                return hit.get("id")
        # No company-shaped match — return None rather than a random first hit.
        # wbsearchentities on a literal ticker symbol returns arbitrary entities
        # (e.g. "SPLK" → an unrelated 19th-century person). A random match would
        # populate the Research panel with junk facts; returning None lets the
        # UI show "no Wikidata entity" honestly.
        return None
    except Exception as e:
        log.debug("wikidata search failed for %s: %s", sym, e)
        return None


def fetch_facts(symbol: str) -> dict:
    """Return a flat dict of corporate facts. Empty values are kept as null
    so the UI knows to show '—' rather than hide the field entirely."""
    sym = symbol.upper()
    cache_key = ("wikidata", sym)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    qid = _entity_for_ticker(sym)
    if not qid:
        result = {
            "symbol": sym, "qid": None, "found": False,
            "note": "no Wikidata entity matches this ticker symbol",
        }
        _cache_set(cache_key, result, DEFAULT_TTL)
        return result

    # One query pulls every fact we care about.
    #
    # CEO (P169) and headquarters (P159) are frequently MULTI-VALUED — a
    # company entity lists every former CEO/HQ alongside the current one. The
    # old `wdt:P169`/`wdt:P159` truthy-statement reads + LIMIT 1 picked an
    # ARBITRARY value (often a former CEO). Resolve the CURRENT one explicitly:
    # use the full statement node (p:/ps:), prefer statements WITHOUT an
    # end-time (P582) — i.e. still in effect — and among those take the one
    # with the latest start-time (P580). Done in correlated subqueries so the
    # outer single-row result carries the current CEO/HQ rather than a random
    # historical one.
    query = f"""
SELECT ?label
       ?industryLabel ?countryLabel ?headquartersLabel
       ?inception ?employees
       ?ceoLabel ?parentLabel ?website
WHERE {{
  wd:{qid} rdfs:label ?label . FILTER(LANG(?label) = "en")
  OPTIONAL {{ wd:{qid} wdt:P452  ?industry . }}
  OPTIONAL {{ wd:{qid} wdt:P17   ?country  . }}
  OPTIONAL {{ wd:{qid} wdt:P571  ?inception . }}
  OPTIONAL {{ wd:{qid} wdt:P1128 ?employees . }}
  OPTIONAL {{ wd:{qid} wdt:P749  ?parent . }}
  OPTIONAL {{ wd:{qid} wdt:P856  ?website . }}

  OPTIONAL {{
    SELECT ?ceo WHERE {{
      wd:{qid} p:P169 ?ceoStmt .
      ?ceoStmt ps:P169 ?ceo .
      FILTER NOT EXISTS {{ ?ceoStmt wikibase:rank wikibase:DeprecatedRank . }}
      FILTER NOT EXISTS {{ ?ceoStmt pq:P582 ?ceoEnd . }}
      OPTIONAL {{ ?ceoStmt pq:P580 ?ceoStart . }}
    }}
    ORDER BY DESC(?ceoStart)
    LIMIT 1
  }}

  OPTIONAL {{
    SELECT ?headquarters WHERE {{
      wd:{qid} p:P159 ?hqStmt .
      ?hqStmt ps:P159 ?headquarters .
      FILTER NOT EXISTS {{ ?hqStmt wikibase:rank wikibase:DeprecatedRank . }}
      FILTER NOT EXISTS {{ ?hqStmt pq:P582 ?hqEnd . }}
      OPTIONAL {{ ?hqStmt pq:P580 ?hqStart . }}
    }}
    ORDER BY DESC(?hqStart)
    LIMIT 1
  }}

  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT 1
"""
    rows = _sparql(query)
    if not rows:
        result = {"symbol": sym, "qid": qid, "found": True, "note": "SPARQL returned no rows"}
        _cache_set(cache_key, result, DEFAULT_TTL)
        return result

    r = rows[0]
    def g(k):
        return (r.get(k) or {}).get("value")

    def _emp(val):
        # Wikidata P1128 ("employees") is stored as xsd:decimal — most
        # company entries are integers ("164000") but a meaningful tail
        # ships fractional values ("24000.13") or thousand-units ("10.814",
        # meant as "10,814"). The previous `.isdigit()` guard rejected every
        # decimal-shaped value as None, silently dropping coverage. Parse
        # via float and round to int — close enough for a research panel.
        if not val:
            return None
        try:
            n = float(val)
            return int(n) if n >= 0 else None
        except (TypeError, ValueError):
            return None

    result = {
        "symbol": sym,
        "qid": qid,
        "found": True,
        "name":         g("label"),
        "industry":     g("industryLabel"),
        "country":      g("countryLabel"),
        "headquarters": g("headquartersLabel"),
        "inception":    (g("inception") or "")[:10] or None,
        "employees":    _emp(g("employees")),
        "ceo":          g("ceoLabel"),
        "parent":       g("parentLabel"),
        "website":      g("website"),
        "wikidata_url": f"https://www.wikidata.org/wiki/{qid}",
    }
    _cache_set(cache_key, result, DEFAULT_TTL)
    return result
