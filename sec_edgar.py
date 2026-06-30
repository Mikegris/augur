"""
EDGAR data layer — fetches from SEC EDGAR public APIs.
All requests use proper User-Agent and rate limiting.
Python 3.9 compatible.
"""

import os
import time
import json
import logging
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import cache_store
except Exception:  # pragma: no cover — import-time safety only
    cache_store = None

logger = logging.getLogger(__name__)

EDGAR_BASE = "https://data.sec.gov"
EDGAR_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
# SEC's fair-use policy requires a UA with a real contact email. The original
# `research@augur.local` was being filtered as suspicious (`.local` is reserved
# for mDNS) and getting 429-rate-limited on every CIK lookup. AUGUR_SEC_UA lets
# the operator drop in their own contact; the fallback uses a registered
# domain so SEC doesn't reject it.
EDGAR_HEADERS = {
    "User-Agent": os.environ.get(
        "AUGUR_SEC_UA",
        "AUGUR Research Bot research@augur-app.org",
    ),
    "Accept-Encoding": "gzip, deflate",
    "Accept": "application/json",
}

TRACKED_FUNDS = {
    "Berkshire Hathaway": "0001067983",
    "Pershing Square": "0001336528",
    # Appaloosa: old CIK 0000814599 404s; David Tepper's pre-2016 LP (CIK
    # 0001006438) stopped filing 13Fs when he converted to a family office.
    # 0001656456 ("Appaloosa LP") is the post-conversion entity that's still
    # filing 13Fs (most recent 2026-02-17).
    "Appaloosa Management": "0001656456",
    "Duquesne Family Office": "0001536411",
    "Scion Asset Management": "0001649339",
    "Third Point": "0001040273",
    # Elliott: old CIK 0001061219 actually resolved to "Enterprise Products
    # Partners L.P." (an unrelated energy MLP). The current Elliott entity is
    # 0001791786 ("Elliott Investment Management L.P.") — active 13F filer.
    "Elliott Investment": "0001791786",
}

# Module-level cache. `_CIK_CACHE` stores `(value_or_none, expiry_unix_ts)`.
# Negative entries (value=None) get a short TTL so transient SEC 429s don't
# permanently mark a ticker as un-resolvable, but long enough to absorb
# repeated lookups during a single page load without re-hammering SEC.
_CIK_CACHE = {}
_CIK_NEGATIVE_TTL = 300.0  # 5 minutes — only for a GENUINE no-match
# Transient request errors (429/timeout) must NOT poison a valid ticker for
# the full negative TTL. Cache those for only a few seconds — long enough to
# absorb a burst of concurrent lookups during one page load, short enough that
# the ticker resolves as soon as SEC recovers.
_CIK_ERROR_TTL = 15.0
_CIK_POSITIVE_TTL = 24 * 3600.0  # CIKs almost never change
_SESSION = None


def _get_session():
    """Return a requests Session with retry logic.

    Note: 429 is deliberately NOT in `status_forcelist`. urllib3 retries 429
    with backoff_factor=1 means each blocked call burns ~7s (1+2+4) before
    giving up — and when N parallel threads each burn 7s waiting on SEC,
    the cache_store.coalesce wait timeout (15s) expires and every waiter
    falls through to its own retry storm. Fail fast on 429 instead and let
    the caller's negative cache (see _CIK_CACHE TTL) absorb repeated
    failures.
    """
    global _SESSION
    if _SESSION is None:
        session = requests.Session()
        retry = Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],  # 429 deliberately omitted
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(EDGAR_HEADERS)
        _SESSION = session
    return _SESSION


# SEC's documented limit is 10 req/sec/IP. The 0.12s in-thread sleep below
# would enforce that on a serial caller, but `get_form4_transactions` fans
# requests out across a ThreadPoolExecutor, so each thread sleeps in parallel
# and the actual request rate can spike. A module-level semaphore bounds the
# number of concurrent SEC requests across *all* callers/threads. 8 leaves
# headroom under SEC's 10/sec cap once you factor in the 0.12s spacing per
# slot.
_EDGAR_CONCURRENCY = threading.Semaphore(8)


def _edgar_ttl_for(url: str) -> float:
    """Pick a cache TTL based on the URL pattern.

    - Form 4 / 13F XML files (under /Archives/edgar/data/) are immutable once
      filed; cap at 24h so we still re-sweep occasionally for safety.
    - Filing index pages (-index.html, directory listings) update only when
      the SEC posts amendments — 6h is fine.
    - Submissions JSON (/submissions/CIK*.json) lists new filings as they
      come in; 6h keeps us close to fresh without hammering on every page
      load.
    """
    u = url.lower()
    if "/archives/edgar/data/" in u and (u.endswith(".xml") or "/primary_doc" in u):
        return 24 * 3600.0
    if "/submissions/cik" in u:
        return 6 * 3600.0
    if "-index.html" in u or u.endswith(".json") or u.endswith("/"):
        return 6 * 3600.0
    # Other archive fetches (filing text HTM, exhibit pages) are immutable
    # too — give them the 24h cap.
    if "/archives/edgar/data/" in u:
        return 24 * 3600.0
    return 6 * 3600.0


class _CachedResp:
    """Minimal stand-in for a requests.Response — just enough surface for
    the callers in this module: .text / .json() / .status_code / .content."""

    def __init__(self, payload):
        self.status_code = payload.get("status_code", 200)
        self.text = payload.get("text", "")
        self._json = payload.get("json")
        content = payload.get("content")
        # cache_store JSON-serialises everything; bytes round-trip as str via
        # default=str, so .content is only available if it was set originally.
        self.content = content.encode("utf-8") if isinstance(content, str) else content

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _edgar_get(url, params=None, timeout=30):
    """GET from EDGAR with rate limiting + persistent caching + request coalescing.

    Cached on the full (url, params) tuple via `cache_store`. TTL is picked
    by URL pattern (see `_edgar_ttl_for`). Concurrency across threads is
    bounded by `_EDGAR_CONCURRENCY` so the parallel Form 4 fetcher can't
    burst past SEC's 10 req/sec limit.

    Uses `cache_store.coalesce` so simultaneous misses for the same URL
    only result in ONE upstream fetch — the other callers wait on the
    in-flight Event and read the cached result. Without this, cold-cache
    multi-symbol scans (peerdiv, divmap, cluster) all hit
    `/files/company_tickers.json` in parallel and trigger SEC's 429
    rate-limiter, which then burns through urllib3's Retry budget and
    stalls every caller for tens of seconds.
    """
    cache_key = ("edgar_get", url, json.dumps(params, sort_keys=True) if params else "")
    ttl = _edgar_ttl_for(url)

    def _do_fetch():
        with _EDGAR_CONCURRENCY:
            time.sleep(0.12)
            session = _get_session()
            resp = session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
        payload = {"status_code": resp.status_code, "text": resp.text}
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "json" in ctype:
            try:
                payload["json"] = resp.json()
            except Exception:
                pass
        return payload

    if cache_store is not None:
        # IMPORTANT: do NOT blanket-catch here. The old code wrapped coalesce in
        # `try/except: pass` and then fell through to a SECOND _do_fetch() on
        # ANY exception — including when _do_fetch itself raised (e.g. a 429
        # bubbling up through raise_for_status). During a 429 storm that turned
        # every failed request into TWO SEC hits, accelerating the rate-limit
        # spiral. Let a fetch error propagate (the caller already handles it);
        # only fall back to a direct fetch when coalesce can't run at all.
        try:
            coalesce_fn = cache_store.coalesce
        except Exception:
            coalesce_fn = None
        if coalesce_fn is not None:
            payload = coalesce_fn(cache_key, ttl, _do_fetch)
            return _CachedResp(payload)

    # cache_store unavailable — direct fetch.
    payload = _do_fetch()
    return _CachedResp(payload)


def _normalize_ticker_for_sec(ticker):
    """SEC's company_tickers.json uses hyphens for share-class separators
    (BRK-B, BF-A, MOG-A). Users / Yahoo Finance / our other data sources
    use dots (BRK.B) or slashes (BRK/B). Without normalisation a lookup
    for `BRK.B` walks the entire 10k-entry JSON, finds nothing, falls
    through to the slow `efts.sec.gov` search, and usually still misses.
    """
    return ticker.upper().replace(".", "-").replace("/", "-")


def get_cik(ticker):
    """
    Look up 10-digit zero-padded CIK for a ticker symbol.
    Returns string or None.

    Both positive and negative results are cached with TTLs (positive 24h,
    negative 5min). The negative cache prevents the cluster / peerdiv /
    divmap multi-symbol scans from re-hammering SEC's 429-prone
    /files/company_tickers.json endpoint when a transient outage marks a
    ticker as un-resolvable.
    """
    ticker_upper = _normalize_ticker_for_sec(ticker)
    now = time.time()
    hit = _CIK_CACHE.get(ticker_upper)
    if hit is not None:
        value, expiry = hit
        if expiry > now:
            return value
        # expired — fall through and refetch

    # Track whether either lookup actually COMPLETED a request (a genuine
    # "ticker not in SEC's dataset" answer) vs. failed with a request error
    # (429/timeout/network). Only a genuine no-match earns the long negative
    # TTL; a transient error gets the short error TTL so the ticker isn't
    # poisoned for 5 minutes by a momentary SEC blip.
    request_error = False
    try:
        resp = _edgar_get("https://www.sec.gov/files/company_tickers.json")
        data = resp.json()
        for _, entry in data.items():
            entry_ticker = (entry.get("ticker") or "").upper()
            # SEC stores hyphenated; we already normalised on the way in,
            # but normalise the SEC side too in case the dataset ever
            # introduces dot-form tickers.
            if entry_ticker.replace(".", "-") == ticker_upper:
                cik_int = entry["cik_str"]
                cik_padded = str(cik_int).zfill(10)
                _CIK_CACHE[ticker_upper] = (cik_padded, now + _CIK_POSITIVE_TTL)
                return cik_padded
    except Exception as e:
        request_error = True
        logger.warning("CIK bulk lookup failed for %s: %s", ticker, e)

    # Fallback: try submissions search
    try:
        search_url = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker_upper}%22&dateRange=custom&startdt=2020-01-01&forms=10-K"
        resp = _edgar_get(search_url)
        hits = resp.json().get("hits", {}).get("hits", [])
        if hits:
            src = hits[0].get("_source", {})
            entity_id = src.get("entity_id") or src.get("cik")
            if entity_id:
                cik_padded = str(entity_id).zfill(10)
                _CIK_CACHE[ticker_upper] = (cik_padded, now + _CIK_POSITIVE_TTL)
                return cik_padded
    except Exception as e:
        request_error = True
        logger.warning("CIK fallback search failed for %s: %s", ticker, e)

    # No CIK found. Negative-cache, but with a TTL that depends on WHY:
    #   - request_error  → transient SEC failure; short TTL so a recovered SEC
    #                      is picked up almost immediately (don't poison a valid
    #                      ticker on a 429/timeout).
    #   - genuine no-match (both requests completed cleanly with no hit) →
    #                      long TTL so multi-symbol scans don't re-hammer SEC.
    ttl = _CIK_ERROR_TTL if request_error else _CIK_NEGATIVE_TTL
    _CIK_CACHE[ticker_upper] = (None, now + ttl)
    return None


def get_recent_filings(ticker, forms=None, limit=20):
    """
    Fetch recent SEC filings for a ticker.
    Returns list of filing dicts.
    """
    if forms is None:
        forms = ["8-K", "10-K", "10-Q", "S-1", "SC 13G", "13F-HR"]

    cik = get_cik(ticker)
    if not cik:
        logger.warning("No CIK found for %s", ticker)
        return []

    cik_int = str(int(cik))  # non-padded integer for URL paths

    try:
        url = f"{EDGAR_BASE}/submissions/CIK{cik}.json"
        resp = _edgar_get(url)
        data = resp.json()
    except Exception as e:
        logger.error("Failed to fetch submissions for %s: %s", ticker, e)
        return []

    recent = data.get("filings", {}).get("recent", {})
    if not recent:
        return []

    form_types   = recent.get("form", [])
    accessions   = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    descriptions = recent.get("primaryDocDescription", [])
    primary_docs = recent.get("primaryDocument", [])
    items_list   = recent.get("items", [])          # 8-K item codes e.g. "2.02,9.01"

    # Human-readable labels for common 8-K item codes
    ITEM_LABELS = {
        "1.01": "Entry into Material Agreement",
        "1.02": "Termination of Material Agreement",
        "1.03": "Bankruptcy or Receivership",
        "2.01": "Completion of Acquisition",
        "2.02": "Results of Operations / Earnings",
        "2.03": "Creation of Direct Financial Obligation",
        "2.05": "Departure of Directors / Officers",
        "2.06": "Material Impairments",
        "3.01": "Notice of Delisting",
        "4.01": "Changes in Registrant's Certifying Accountant",
        "5.01": "Changes in Control",
        "5.02": "Departure / Appointment of Directors / Officers",
        "5.03": "Amendments to Articles / Bylaws",
        "7.01": "Regulation FD Disclosure",
        "8.01": "Other Events",
        "9.01": "Financial Statements and Exhibits",
    }

    results = []
    for i, form_type in enumerate(form_types):
        if form_type not in forms:
            continue
        if len(results) >= limit:
            break

        acc         = accessions[i]   if i < len(accessions)   else ""
        filing_date = filing_dates[i] if i < len(filing_dates) else ""
        report_date = report_dates[i] if i < len(report_dates) else ""
        raw_desc    = descriptions[i] if i < len(descriptions) else ""
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""
        items_raw   = items_list[i]   if i < len(items_list)   else ""

        # Build a human-readable description
        if items_raw and form_type == "8-K":
            codes = [c.strip() for c in str(items_raw).split(",") if c.strip()]
            labels = [ITEM_LABELS.get(c, f"Item {c}") for c in codes if c in ITEM_LABELS]
            description = "; ".join(labels) if labels else (raw_desc or form_type)
        else:
            description = raw_desc or form_type

        acc_nodash = acc.replace("-", "")
        doc_url = (f"{EDGAR_ARCHIVES}/{cik_int}/{acc_nodash}/{primary_doc}"
                   if primary_doc else f"{EDGAR_ARCHIVES}/{cik_int}/{acc_nodash}/")

        results.append({
            "accession":        acc,
            "form_type":        form_type,
            "filing_date":      filing_date,
            "report_date":      report_date,
            "description":      description,
            "items":            items_raw,
            "primary_document": primary_doc,
            "cik":              cik_int,
            "ticker":           ticker.upper(),
            "document_url":     doc_url,
        })

    return results


def _best_document_from_index(cik_int, acc_nodash, form_type=""):
    """
    Fetch the filing index and return the filename of the best human-readable document.
    Parses the -index.html page (the -index.json URL does not exist on EDGAR).
    Priority: EX-99.1 / EX-99 exhibits (press releases) > primary non-XBRL HTM > primary doc.
    Returns (filename, doc_type) or (None, None).
    """
    import re as _re
    # The dashed accession number is used in the index filename
    # e.g. acc_nodash="000032019326000005" -> "0000320193-26-000005"
    acc_dashed = "-".join([acc_nodash[:10], acc_nodash[10:12], acc_nodash[12:]])
    index_url = f"{EDGAR_ARCHIVES}/{cik_int}/{acc_nodash}/{acc_dashed}-index.html"
    try:
        resp = _edgar_get(index_url, timeout=15)
        from bs4 import BeautifulSoup as _BS
        soup = _BS(resp.text, "html.parser")
    except Exception:
        return None, None

    exhibit_doc = None
    primary_doc = None

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        # Layout: seq | description | filename | type | size (5 cols)
        # description column (index 1)
        desc_text = tds[1].get_text(strip=True) if len(tds) > 1 else ""
        # filename column (index 2) — may have an <a> link or plain text
        fname_tag = tds[2].find("a") if len(tds) > 2 else None
        fname = fname_tag.get_text(strip=True) if fname_tag else tds[2].get_text(strip=True)
        # type column (index 3)
        dtype = tds[3].get_text(strip=True).upper() if len(tds) > 3 else ""

        fname_lower = fname.lower()
        if not fname_lower.endswith((".htm", ".html", ".txt")):
            continue
        # Skip XBRL artifacts
        if fname_lower.endswith(("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml", "_htm.xml")):
            continue
        if _re.match(r"r\d+\.htm$", fname_lower):
            continue
        if "xsd" in fname_lower or "filesummary" in fname_lower:
            continue
        # iXBRL marker in description text (e.g. "8-K iXBRL")
        is_ixbrl = "ixbrl" in desc_text.lower() or "ixbrl" in fname_lower

        # EX-99 exhibit — actual press release
        if dtype.startswith("EX-99") and exhibit_doc is None:
            exhibit_doc = fname
        # Primary doc: form type match; skip if iXBRL
        elif dtype == form_type.upper() and not is_ixbrl and primary_doc is None:
            primary_doc = fname
        elif dtype in ("8-K", "10-K", "10-Q", "S-1", "10-K/A", "10-Q/A"):
            if not is_ixbrl and primary_doc is None:
                primary_doc = fname

    # Return exhibit first (for 8-K this is the actual press release)
    if exhibit_doc:
        return exhibit_doc, "exhibit"
    if primary_doc:
        return primary_doc, "primary"
    return None, None


def _extract_readable_text(raw, filename=""):
    """
    Extract clean human-readable text from raw HTML/XML/text.
    Handles iXBRL by stripping ix: namespace elements first.
    """
    import re
    fname_lower = filename.lower()

    if fname_lower.endswith((".htm", ".html")) or "<html" in raw[:200].lower():
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(raw, "html.parser")
            # Remove script, style, head, and all ix: namespace XBRL tags
            for tag in soup(["script", "style", "head", "ix:header", "ix:hidden"]):
                tag.decompose()
            # Remove all tags with ix: prefix (iXBRL inline tags wrap actual values)
            for tag in soup.find_all(True):
                if tag.name and tag.name.startswith("ix:"):
                    tag.unwrap()  # keep text content, remove the tag itself
            text = soup.get_text(separator=" ", strip=True)
            # Collapse whitespace
            text = re.sub(r"\s{3,}", "  ", text)
            return text
        except Exception:
            text = re.sub(r"<[^>]+>", " ", raw)
            return re.sub(r"\s+", " ", text).strip()

    elif fname_lower.endswith(".xml") or raw.strip().startswith("<?xml"):
        try:
            root = ET.fromstring(raw)
            texts = [e.text.strip() for e in root.iter() if e.text and e.text.strip()]
            return " ".join(texts)
        except Exception:
            return raw

    return raw


def get_filing_text(cik, accession, primary_document, form_type=""):
    """
    Fetch the actual filing document and return clean readable text (max 12000 chars).
    Tries to find the best document (EX-99.1 exhibit > primary non-XBRL) via index.
    """
    acc_nodash = accession.replace("-", "")
    cik_int = str(int(cik))

    # Try to find the best document via filing index
    best_doc, doc_kind = _best_document_from_index(cik_int, acc_nodash, form_type)

    # Fall back to the provided primary_document if index lookup fails
    doc_to_fetch = best_doc or primary_document
    if not doc_to_fetch:
        return ""

    url = f"{EDGAR_ARCHIVES}/{cik_int}/{acc_nodash}/{doc_to_fetch}"
    try:
        resp = _edgar_get(url, timeout=30)
        raw = resp.text
        text = _extract_readable_text(raw, doc_to_fetch)
        return text[:12000]
    except Exception as e:
        logger.error("Failed to fetch filing text %s: %s", url, e)
        # Last resort: try primary_document if we tried something else first
        if doc_to_fetch != primary_document and primary_document:
            try:
                url2 = f"{EDGAR_ARCHIVES}/{cik_int}/{acc_nodash}/{primary_document}"
                resp2 = _edgar_get(url2, timeout=30)
                text = _extract_readable_text(resp2.text, primary_document)
                return text[:12000]
            except Exception:
                pass
        return ""


def get_form4_transactions(ticker, limit=30):
    """
    Fetch and parse Form 4 insider transactions for a ticker.
    Returns list of transaction dicts.
    """
    cik = get_cik(ticker)
    if not cik:
        return []

    cik_int = str(int(cik))

    try:
        url = f"{EDGAR_BASE}/submissions/CIK{cik}.json"
        resp = _edgar_get(url)
        data = resp.json()
    except Exception as e:
        logger.error("Failed to fetch submissions for %s: %s", ticker, e)
        return []

    recent = data.get("filings", {}).get("recent", {})
    form_types = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    # Collect Form 4 filings — include amendments (4/A) so corrected
    # transactions don't get silently ignored.
    form4_filings = []
    for i, ft in enumerate(form_types):
        if ft in ("4", "4/A"):
            raw_doc = primary_docs[i] if i < len(primary_docs) else ""
            # Strip XSLT subdirectory prefix: "xslF345X06/form4.xml" → "form4.xml"
            if "/" in raw_doc:
                raw_doc = raw_doc.split("/")[-1]
            form4_filings.append({
                "accession": accessions[i] if i < len(accessions) else "",
                "filing_date": filing_dates[i] if i < len(filing_dates) else "",
                "primary_doc": raw_doc,
            })
        if len(form4_filings) >= limit:
            break

    # Parse Form 4 XMLs in parallel (4 concurrent to respect EDGAR rate limits)
    import safe_executor

    def _parse_one_form4(filing):
        """Fetch and parse a single Form 4 filing. Returns list of txn dicts."""
        acc = filing["accession"]
        acc_nodash = acc.replace("-", "")
        primary_doc = filing["primary_doc"]
        results = []

        if not primary_doc:
            # The -index.json URL does not exist on EDGAR; parse the -index.html
            # table for the Form 4 XML, mirroring _best_document_from_index.
            try:
                acc_dashed = "-".join([acc_nodash[:10], acc_nodash[10:12], acc_nodash[12:]])
                index_url = f"{EDGAR_ARCHIVES}/{cik_int}/{acc_nodash}/{acc_dashed}-index.html"
                idx_resp = _edgar_get(index_url, timeout=15)
                from bs4 import BeautifulSoup as _BS
                soup = _BS(idx_resp.text, "html.parser")
                fallback_xml = None
                for tr in soup.find_all("tr"):
                    tds = tr.find_all("td")
                    if len(tds) < 3:
                        continue
                    fname_tag = tds[2].find("a") if len(tds) > 2 else None
                    fname = fname_tag.get_text(strip=True) if fname_tag else tds[2].get_text(strip=True)
                    dtype = tds[3].get_text(strip=True) if len(tds) > 3 else ""
                    if not fname.lower().endswith(".xml"):
                        continue
                    if dtype == "4":
                        primary_doc = fname
                        break
                    if fallback_xml is None:
                        fallback_xml = fname
                if not primary_doc and fallback_xml:
                    primary_doc = fallback_xml
            except Exception:
                pass

        if not primary_doc:
            return results

        xml_url = f"{EDGAR_ARCHIVES}/{cik_int}/{acc_nodash}/{primary_doc}"
        form_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{primary_doc}"

        try:
            resp = _edgar_get(xml_url, timeout=20)
            xml_text = resp.text

            try:
                root = ET.fromstring(xml_text)
            except ET.ParseError:
                import re
                xml_text_clean = re.sub(r' xmlns[^"]*"[^"]*"', '', xml_text)
                try:
                    root = ET.fromstring(xml_text_clean)
                except Exception:
                    return results

            owner_name = (
                root.findtext(".//reportingOwner/reportingOwnerId/rptOwnerName") or
                root.findtext(".//rptOwnerName") or "Unknown"
            )
            is_director_text = (
                root.findtext(".//reportingOwner/reportingOwnerRelationship/isDirector") or
                root.findtext(".//isDirector") or "0"
            )
            is_officer_text = (
                root.findtext(".//reportingOwner/reportingOwnerRelationship/isOfficer") or
                root.findtext(".//isOfficer") or "0"
            )
            officer_title = (
                root.findtext(".//reportingOwner/reportingOwnerRelationship/officerTitle") or
                root.findtext(".//officerTitle") or ""
            )
            is_director = is_director_text.strip().lower() in ("1", "true", "y", "yes")
            is_officer = is_officer_text.strip().lower() in ("1", "true", "y", "yes")

            def _parse_txn(txn, derivative=False):
                security = txn.findtext("securityTitle/value") or txn.findtext("securityTitle") or ""
                txn_date = txn.findtext("transactionDate/value") or txn.findtext("transactionDate") or filing["filing_date"]
                shares_text = txn.findtext("transactionAmounts/transactionShares/value") or txn.findtext("transactionAmounts/transactionShares") or "0"
                price_text = txn.findtext("transactionAmounts/transactionPricePerShare/value") or txn.findtext("transactionAmounts/transactionPricePerShare") or "0"
                # transactionCode is the *action* (P=Open-Market Purchase,
                # S=Open-Market Sale, M=Exercise of derivative, F=Tax payment,
                # A=Grant/award, G=Gift, D=Disposition to issuer, etc.).
                # transactionAcquiredDisposedCode is just A/D ("Acquired" vs
                # "Disposed") — derived from the action but ambiguous on its
                # own: an RSU grant has A/A (Acquired via Award), which the
                # old code mis-labelled BUY; an M-exercise typically has
                # multiple sub-transactions including a D, which got labelled
                # SELL. Use the real action code for the buy/sell decision,
                # fall back to A/D direction only when transactionCode is
                # absent.
                action_code = (
                    txn.findtext("transactionCoding/transactionCode")
                    or txn.findtext(".//transactionCode")
                    or ""
                ).strip().upper()
                direction_code = (
                    txn.findtext("transactionAmounts/transactionAcquiredDisposedCode/value")
                    or txn.findtext("transactionAmounts/transactionAcquiredDisposedCode")
                    or ""
                ).strip().upper()
                shares_after_text = txn.findtext("postTransactionAmounts/sharesOwnedFollowingTransaction/value") or txn.findtext("postTransactionAmounts/sharesOwnedFollowingTransaction") or "0"
                try:
                    shares = float(shares_text.replace(",", ""))
                except (ValueError, AttributeError):
                    shares = 0.0
                try:
                    price = float(price_text.replace(",", ""))
                except (ValueError, AttributeError):
                    price = 0.0
                try:
                    shares_after = float(shares_after_text.replace(",", ""))
                except (ValueError, AttributeError):
                    shares_after = 0.0
                # Classify by action code first (open-market only counts as
                # a real signal); everything else is "OTHER" so downstream
                # filters (smart-money, opportunity_scanner) don't count
                # grants and tax-withholding withholdings as insider buys.
                if action_code == "P":
                    txn_type = "BUY"
                elif action_code == "S":
                    txn_type = "SELL"
                elif action_code:
                    # Tag with the code so callers can decide (A=Award,
                    # M=Exercise, F=Tax-paid, G=Gift, D=Disposition, etc.)
                    txn_type = "OTHER:" + action_code
                else:
                    # No action code at all — fall back to A/D direction.
                    txn_type = "BUY" if direction_code == "A" else "SELL"
                value = shares * price if shares and price else 0.0
                if derivative and shares <= 0:
                    return None
                title_str = (officer_title + " (derivative)") if derivative and officer_title else (officer_title if not derivative else "derivative")
                return {
                    "ticker": ticker.upper(), "insider_name": owner_name,
                    "title": title_str, "is_director": is_director, "is_officer": is_officer,
                    "transaction_type": txn_type, "transaction_code": action_code,
                    "security": security,
                    "shares": shares, "price": price, "value": value,
                    "shares_after": shares_after, "date": txn_date,
                    "accession": acc, "form_url": form_url,
                }

            for txn in root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
                t = _parse_txn(txn, derivative=False)
                if t:
                    results.append(t)
            for txn in root.findall(".//derivativeTable/derivativeTransaction"):
                t = _parse_txn(txn, derivative=True)
                if t:
                    results.append(t)

        except Exception as e:
            logger.warning("Failed to parse Form 4 %s for %s: %s", acc, ticker, e)

        return results

    # Daemon-thread fan-out; _parse_one_form4 catches its own errors and
    # returns a list, so a None slot (work fn raised) is just skipped.
    transactions = []
    for parsed in safe_executor.parallel_map(
        _parse_one_form4, form4_filings,
        max_workers=4, thread_name_prefix="edgar-form4",
    ):
        if parsed:
            transactions.extend(parsed)

    transactions.sort(key=lambda x: x.get("date", ""), reverse=True)
    return transactions


def _strip_ns(tag):
    """Strip XML namespace from a tag name."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_text_ns(elem, tag):
    """Find text in element, stripping namespace."""
    # Try direct
    result = elem.findtext(tag)
    if result:
        return result
    # Try with wildcard namespace
    try:
        result = elem.findtext("{*}" + tag)
        if result:
            return result
    except Exception:
        pass
    # Try iterating
    for child in elem.iter():
        if _strip_ns(child.tag) == tag and child.text:
            return child.text.strip()
    return None


def get_institutional_holdings(fund_cik, fund_name):
    """
    Fetch most recent 13F-HR filing for a fund and return holdings.
    Returns dict with holdings list.
    """
    # Ensure CIK is zero-padded
    cik_padded = str(fund_cik).lstrip("0").zfill(10)
    cik_int = str(int(cik_padded))

    try:
        url = f"{EDGAR_BASE}/submissions/CIK{cik_padded}.json"
        resp = _edgar_get(url)
        data = resp.json()
    except Exception as e:
        logger.error("Failed to fetch submissions for fund %s: %s", fund_name, e)
        return {"error": str(e), "fund_name": fund_name}

    recent = data.get("filings", {}).get("recent", {})
    form_types = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])

    # Find most recent 13F-HR — but also accept amendments (13F-HR/A).
    # SEC lists newest-first, so the first match is the freshest version of
    # this period. Originally we only matched the bare "13F-HR" string,
    # which meant funds that filed a corrected 13F-HR/A (to fix CUSIP or
    # share-count errors) kept serving the pre-correction holdings — a
    # silent stale-cache problem since our coalesce/file cache happily
    # returned the stale submissions JSON for hours.
    filing_idx = None
    for i, ft in enumerate(form_types):
        if ft in ("13F-HR", "13F-HR/A"):
            filing_idx = i
            break

    if filing_idx is None:
        return {"error": "No 13F-HR filing found", "fund_name": fund_name, "holdings": []}

    acc = accessions[filing_idx]
    filing_date = filing_dates[filing_idx] if filing_idx < len(filing_dates) else ""
    period = report_dates[filing_idx] if filing_idx < len(report_dates) else ""
    acc_nodash = acc.replace("-", "")

    # Fetch filing directory HTML to find infotable document
    infotable_doc = None
    dir_url = f"{EDGAR_ARCHIVES}/{cik_int}/{acc_nodash}/"
    try:
        dir_resp = _edgar_get(dir_url)
        dir_html = dir_resp.text
        # Parse links looking for the infotable XML
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(dir_html, "html.parser")
            xml_links = []
            for a in soup.find_all("a"):
                href = a.get("href", "")
                fname = href.split("/")[-1].lower()
                if fname.endswith(".xml"):
                    xml_links.append(href.split("/")[-1])
            # Prefer files containing 'info', '13f', or numbered XML files (not primary_doc)
            for fn in xml_links:
                fn_lower = fn.lower()
                if "infotable" in fn_lower or "13f" in fn_lower or "information" in fn_lower:
                    infotable_doc = fn
                    break
            # If no keyword match, use any non-primary XML
            if not infotable_doc:
                for fn in xml_links:
                    if fn.lower() != "primary_doc.xml" and fn.lower().endswith(".xml"):
                        infotable_doc = fn
                        break
        except Exception:
            import re
            matches = re.findall(r'href="[^"]*?/([^"/]+\.xml)"', dir_html, re.IGNORECASE)
            for fn in matches:
                if fn.lower() != "primary_doc.xml":
                    infotable_doc = fn
                    break
            if not infotable_doc and matches:
                infotable_doc = matches[0]
    except Exception as e:
        logger.warning("Could not fetch filing directory for %s: %s", fund_name, e)

    # Fallback: try common filenames
    if not infotable_doc:
        for candidate in ["form13fInfoTable.xml", "infotable.xml", "13F_InfoTable.xml"]:
            test_url = f"{EDGAR_ARCHIVES}/{cik_int}/{acc_nodash}/{candidate}"
            try:
                test_resp = _edgar_get(test_url)
                if test_resp.status_code == 200:
                    infotable_doc = candidate
                    break
            except Exception:
                pass

    if not infotable_doc:
        return {
            "fund_name": fund_name,
            "fund_cik": fund_cik,
            "filing_date": filing_date,
            "period_of_report": period,
            "error": "Could not find infotable document",
            "holdings": [],
        }

    # Fetch infotable XML
    infotable_url = f"{EDGAR_ARCHIVES}/{cik_int}/{acc_nodash}/{infotable_doc}"
    try:
        xml_resp = _edgar_get(infotable_url, timeout=30)
        xml_text = xml_resp.text
    except Exception as e:
        logger.error("Failed to fetch infotable for %s: %s", fund_name, e)
        return {"error": str(e), "fund_name": fund_name, "holdings": []}

    # Parse XML — handle namespaces
    holdings = []
    try:
        root = ET.fromstring(xml_text)

        # Try to find infoTable elements (with or without namespace)
        info_tables = root.findall(".//infoTable")
        if not info_tables:
            info_tables = root.findall(".//{*}infoTable")
        if not info_tables:
            info_tables = [elem for elem in root.iter() if _strip_ns(elem.tag) == "infoTable"]

        for info in info_tables:
            def ft(elem, tag):
                """Find text within element, namespace-agnostic."""
                val = elem.findtext(tag)
                if val:
                    return val.strip()
                for child in elem.iter():
                    if _strip_ns(child.tag) == tag and child.text:
                        return child.text.strip()
                return None

            h_name = ft(info, "nameOfIssuer") or ""
            h_cusip = ft(info, "cusip") or ""
            value_text = ft(info, "value") or "0"
            title_of_class = ft(info, "titleOfClass") or ""

            # shares can be under shrsOrPrnAmt/sshPrnamt
            shares_text = ft(info, "sshPrnamt") or "0"

            try:
                h_shares = float(str(shares_text).replace(",", ""))
            except (ValueError, AttributeError):
                h_shares = 0.0

            try:
                # SEC 13F spec (post-2023 amendments): the `value` field is in
                # WHOLE DOLLARS as of the 2023-01-03 form revision; pre-2023
                # filings reported in THOUSANDS. The old per-row "which scaling
                # gives a plausible price" heuristic 1000x-misvalued any holding
                # whose share price sat near a band edge (e.g. a $1.20 penny
                # stock or a $40k Berkshire-A share), because BOTH scalings can
                # look "plausible" or NEITHER does.
                #
                # Trust the spec: apply the ×1000 (thousands→dollars)
                # convention, then SANITY-CHECK via the implied price-per-share
                # using the reported share count. If the implied price is wildly
                # implausible, LOG the anomaly (don't silently guess) and fall
                # back to the unscaled value only when that yields a saner price.
                raw_val = float(str(value_text).replace(",", ""))
                value_usd = raw_val * 1000  # spec default: thousands → dollars
                if h_shares > 0 and raw_val > 0:
                    price_if_thousands = (raw_val * 1000) / h_shares
                    price_if_dollars = raw_val / h_shares
                    # Plausible US equity price band.
                    _LO, _HI = 0.50, 100000.0
                    thousands_ok = _LO <= price_if_thousands <= _HI
                    dollars_ok = _LO <= price_if_dollars <= _HI
                    if not thousands_ok and dollars_ok:
                        # Spec scaling is implausible but the as-filed dollars
                        # are sane → almost certainly a whole-dollar (post-2023)
                        # filing. Use it, but record the anomaly.
                        logger.info(
                            "13F value-unit anomaly for %s (%s): thousands→$%.2f/sh "
                            "implausible, using whole-dollar→$%.2f/sh",
                            fund_name, h_name, price_if_thousands, price_if_dollars,
                        )
                        value_usd = raw_val
                    elif not thousands_ok and not dollars_ok:
                        # Neither scaling is plausible — keep the spec default
                        # but log so the anomaly is visible rather than guessed.
                        logger.warning(
                            "13F value-unit anomaly for %s (%s): neither scaling "
                            "yields a plausible price (shares=%.0f, raw_val=%.0f); "
                            "keeping spec ×1000",
                            fund_name, h_name, h_shares, raw_val,
                        )
            except (ValueError, AttributeError):
                value_usd = 0.0

            if h_name and value_usd > 0:
                holdings.append({
                    "name": h_name,
                    "cusip": h_cusip,
                    "title_of_class": title_of_class,
                    "value_usd": value_usd,
                    "shares": h_shares,
                    "pct_of_portfolio": 0.0,  # computed below
                })

    except ET.ParseError as e:
        logger.error("XML parse error for %s infotable: %s", fund_name, e)
        return {"error": f"XML parse error: {e}", "fund_name": fund_name, "holdings": []}

    # Sort by value descending and compute pct
    holdings.sort(key=lambda x: x["value_usd"], reverse=True)
    total_value = sum(h["value_usd"] for h in holdings)
    for h in holdings:
        h["pct_of_portfolio"] = round((h["value_usd"] / total_value * 100), 4) if total_value else 0.0

    return {
        "fund_name": fund_name,
        "fund_cik": fund_cik,
        "filing_date": filing_date,
        "period_of_report": period,
        "total_value": total_value,
        "holdings": holdings,
    }


def get_portfolio_filing_feed(symbols, limit_per_symbol=5):
    """
    Fetch recent filings for a list of symbols.
    Returns merged list sorted by filing_date descending (max 50 entries).
    """
    all_filings = []
    forms = ["8-K", "10-K", "10-Q", "S-1"]

    for symbol in symbols:
        try:
            filings = get_recent_filings(symbol, forms=forms, limit=limit_per_symbol)
            all_filings.extend(filings)
        except Exception as e:
            logger.warning("Failed to fetch filings for %s: %s", symbol, e)
            continue

    # Sort by filing_date descending
    all_filings.sort(key=lambda x: x.get("filing_date", ""), reverse=True)
    return all_filings[:50]
