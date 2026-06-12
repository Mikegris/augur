"""
congress.py — Congressional Trading Intelligence
Parses House PTR (Periodic Transaction Report) PDFs from the official
House Financial Disclosure website to extract real stock trades.
"""

import re
import io
import zipfile
import logging
import threading
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

try:
    import pdfplumber
    _PDF_OK = True
except ImportError:
    _PDF_OK = False

try:
    import cache_store
except Exception:  # pragma: no cover — import safety only
    cache_store = None

log = logging.getLogger(__name__)

# PTR PDFs are immutable once filed — cache the parse output for 30 days.
# pdfplumber on a multi-page PTR is the slowest single operation in AUGUR.
_PTR_CACHE_TTL = 30 * 24 * 3600

# pdfplumber occasionally hangs on malformed PDFs. Bound any single parse to
# 20 s — past that we abandon the PDF rather than starve the pool.
_PDF_PARSE_TIMEOUT_S = 20

# pdfplumber is pure-Python and CPU-bound: unbounded concurrent parses (the
# warmer's cluster sweep hits congress for ~100 symbols) monopolize the GIL
# until Flask's accept loop starves and the server stops answering. At most
# 2 parses run at once; the rest queue inside their daemon workers.
_PDF_PARSE_GATE = threading.BoundedSemaphore(2)

# Circuit breaker state. Two trip conditions, because genuine hangs hold
# gate permits forever and therefore CAP at the gate's capacity (2) — a
# higher hung-threshold could never be reached:
#   • hung >= gate capacity  → both permits leaked, the gate is starved
#   • a streak of gate-busy acquire timeouts → starvation detected even if
#     the hung accounting missed it. Streak resets on any successful parse.
_MAX_HUNG_PARSES = 2   # == BoundedSemaphore capacity above
_BUSY_TRIP = 5
_hung_lock = threading.Lock()
_breaker = {"hung": 0, "busy_streak": 0}


def _note_hung_parse():
    with _hung_lock:
        _breaker["hung"] += 1


def _note_gate_busy():
    with _hung_lock:
        _breaker["busy_streak"] += 1


def _note_parse_ok():
    with _hung_lock:
        _breaker["busy_streak"] = 0


def _parse_circuit_open():
    with _hung_lock:
        return (_breaker["hung"] >= _MAX_HUNG_PARSES
                or _breaker["busy_streak"] >= _BUSY_TRIP)

# Negative-result TTL: timeouts, parse failures, 404s and legitimately
# trade-less PTRs were never cached, so every sweep re-downloaded and
# re-ground the same documents (observed: one doc_id parsed 3× in 2s by
# concurrent sweeps). 30 min of "known bad/empty" stops the thundering herd;
# a doc that timed out only because the gate was busy gets retried next sweep.
_PTR_NEG_TTL = 1800

HOUSE_FD_BASE = "https://disclosures-clerk.house.gov"
HOUSE_PTR_PDF = HOUSE_FD_BASE + "/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
HOUSE_FD_ZIP  = HOUSE_FD_BASE + "/public_disc/financial-pdfs/{year}FD.ZIP"

# disclosures-clerk.house.gov's WAF intermittently returns 403/empty bodies
# for the default `python-requests/X.Y` UA. Always identify ourselves with a
# real-looking UA + contact so the firewall lets the bytes through.
_HOUSE_HEADERS = {
    "User-Agent": "AUGUR Research Bot research@augur-app.org",
    "Accept": "application/zip, application/pdf, */*",
}

# Amount range midpoints for sorting/display
AMOUNT_MIDPOINTS = {
    "$1,001 - $15,000":      8000,
    "$15,001 - $50,000":    32500,
    "$50,001 - $100,000":   75000,
    "$100,001 - $250,000": 175000,
    "$250,001 - $500,000": 375000,
    "$500,001 - $1,000,000": 750000,
    "$1,000,001 - $5,000,000": 3000000,
    "Over $5,000,000": 5000000,
}

OWNER_LABELS = {
    "JT": "Joint",
    "SP": "Spouse",
    "DC": "Dep. Child",
    "": "Self",
}

TXN_LABELS = {
    "P": "Buy",
    "S": "Sell",
    "S (partial)": "Sell (Partial)",
    "S (Exchange)": "Exchange",
    "E (Exchange)": "Exchange",
    "PE": "Purchase (Exercise)",
    "SE": "Sale (Exercise)",
}


def _fetch_fd_index(year):
    """Download and parse the House FD ZIP for a given year.
    Returns list of PTR dicts: {last, first, state, date_str, doc_id, year}"""
    url = HOUSE_FD_ZIP.format(year=year)
    try:
        resp = requests.get(url, headers=_HOUSE_HEADERS, timeout=20)
        if resp.status_code != 200:
            return []
        zdata = zipfile.ZipFile(io.BytesIO(resp.content))
        xml_name = f"{year}FD.xml"
        if xml_name not in zdata.namelist():
            return []
        xml_bytes = zdata.read(xml_name)
        root = ET.fromstring(xml_bytes.decode("utf-8-sig", errors="replace"))
        records = []
        for m in root.findall("Member"):
            if m.findtext("FilingType", "") != "P":
                continue
            records.append({
                "last":    m.findtext("Last", ""),
                "first":   m.findtext("First", ""),
                "state":   m.findtext("StateDst", ""),
                "date_str": m.findtext("FilingDate", ""),
                "doc_id":  m.findtext("DocID", ""),
                "year":    year,
            })
        return records
    except Exception as e:
        log.warning("FD index fetch error (%s): %s", year, e)
        return []


def _parse_filing_date(date_str):
    """Parse M/D/YYYY or MM/DD/YYYY to datetime."""
    # %m/%d/%Y already parses unpadded "1/5/2024"; the "%-m/%-d/%Y" variant is
    # platform-dependent (undefined on non-glibc/Windows) and redundant.
    for fmt in ("%m/%d/%Y",):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            pass
    return None


def _parse_amount(amount_str):
    """Convert amount range string to midpoint integer."""
    s = amount_str.strip()
    for k, v in AMOUNT_MIDPOINTS.items():
        if k.lower() in s.lower():
            return v
    # Try parsing raw numbers
    nums = re.findall(r"[\d,]+", s)
    if len(nums) >= 2:
        try:
            lo = int(nums[0].replace(",", ""))
            hi = int(nums[1].replace(",", ""))
            return (lo + hi) // 2
        except Exception:
            pass
    if nums:
        try:
            return int(nums[0].replace(",", ""))
        except Exception:
            pass
    return 0


def _parse_ptr_pdf(pdf_bytes, member_name, doc_id, filing_year):
    """Parse a PTR PDF binary and return list of transaction dicts."""
    if not _PDF_OK:
        return []
    transactions = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full_text = ""
            for page in pdf.pages:
                full_text += (page.extract_text() or "") + "\n"
    except Exception as e:
        log.warning("PDF parse error for %s: %s", doc_id, e)
        return []

    # Clean null bytes and normalize
    text = full_text.replace("\x00", " ").replace("\r", "")

    # Extract member name from PDF (more reliable than XML)
    name_match = re.search(r"Name:\s*Hon\.\s*(.+)", text)
    if name_match:
        member_name = name_match.group(1).strip()

    state_match = re.search(r"State/District:\s*(\w+)", text)
    state = state_match.group(1).strip() if state_match else ""

    # Remove header/footer boilerplate
    text = re.sub(r"ID\s+Owner\s+Asset.*?Cap\.\s*\n.*?Gains.*?\n.*?\$200\?", "", text, flags=re.DOTALL)
    text = re.sub(r"F\s+S\s*:\s*New", "", text)
    text = re.sub(r"S\s+O\s*:.*?\n", "\n", text)
    # Remove description lines (options metadata like "Expires MM/DD/YYYY")
    text = re.sub(r"D\s*:\s*.*?\n", "\n", text)
    text = re.sub(r"Filing ID.*?\n", "", text)
    text = re.sub(r"Clerk of the House.*?\n", "", text)
    text = re.sub(r"For the complete list.*?\n", "", text)
    text = re.sub(r"I CERTIFY.*", "", text, flags=re.DOTALL)
    text = re.sub(r"Digitally Signed.*", "", text, flags=re.DOTALL)
    text = re.sub(r"I\s+V\s+D\s*\n.*", "", text, flags=re.DOTALL)
    text = re.sub(r"I\s+P\s+O.*", "", text, flags=re.DOTALL)
    text = re.sub(r"C\s+S\s*\n", "", text)
    text = re.sub(r"F\s+I\s*\n", "", text)
    text = re.sub(r"T\s*\n", "", text)

    # Collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Now search for transaction patterns
    # Pattern: find lines containing (TICKER) [ASSET_TYPE]
    # and extract the surrounding transaction data

    # Date pattern: MM/DD/YYYY
    DATE_RE = r"(\d{1,2}/\d{1,2}/\d{4})"
    AMOUNT_RE = r"(\$[\d,]+\s*-\s*\$[\d,]+|Over\s*\$[\d,]+)"
    # Allow dotted/class tickers like BRK.B; the [asset-type] bracket anchors
    # the match so broadening the char class can't grab stray parentheticals.
    TICKER_RE = r"\(([A-Z][A-Z.]{0,5})\)\s*\[(ST|OP|MF|GS|OT|RE|PF|HC|VI)\]"
    TXN_TYPE_RE = r"\b(S\s*\(partial\)|S\s*\(Exchange\)|E\s*\(Exchange\)|P|S|PE|SE)\b"
    OWNER_RE = r"^(JT|SP|DC)\s+"

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Check if this line or combined with next contains a ticker
        combined = line
        if i + 1 < len(lines):
            combined = line + " " + lines[i + 1].strip()
        if i + 2 < len(lines):
            combined3 = combined + " " + lines[i + 2].strip()
        else:
            combined3 = combined

        ticker_match = re.search(TICKER_RE, combined3)
        if not ticker_match:
            i += 1
            continue

        ticker = ticker_match.group(1)
        asset_type = ticker_match.group(2)

        # Look for transaction type, dates, amount in the surrounding context
        # Collect up to 4 lines starting at i
        block_lines = []
        for j in range(i, min(i + 5, len(lines))):
            block_lines.append(lines[j].strip())
        block = " ".join(block_lines)
        block = block.replace("\x00", " ")

        # Owner
        owner = ""
        owner_match = re.match(OWNER_RE, line)
        if owner_match:
            owner = owner_match.group(1)

        # Transaction type (P=Buy, S=Sell, S (partial), etc.)
        txn_type = ""
        # Look for transaction type after the asset type bracket
        after_ticker = block[block.find(ticker_match.group(0)) + len(ticker_match.group(0)):]
        # Also check in a window before the first date
        date_matches = list(re.finditer(DATE_RE, block))
        pre_date = block
        if date_matches:
            pre_date = block[:date_matches[0].start()]

        # Priority: check the full block for S (partial) first
        if re.search(r"S\s*\(partial\)", block, re.IGNORECASE):
            txn_type = "S (partial)"
        elif re.search(r"S\s*\(Exchange\)", block, re.IGNORECASE):
            txn_type = "S (Exchange)"
        elif re.search(r"E\s*\(Exchange\)", block, re.IGNORECASE):
            txn_type = "E (Exchange)"
        else:
            # Look for isolated P or S after ticker
            m = re.search(r"\]\s+(P|S|PE|SE)\b", after_ticker)
            if not m:
                # Check in pre_date window after removing owner prefix
                clean = re.sub(OWNER_RE, "", pre_date).strip()
                m = re.search(r"\b(P|S|PE|SE)\b", clean[:100])
            if m:
                txn_type = m.group(1)

        if not txn_type:
            i += 1
            continue

        # Dates
        txn_date = ""
        notif_date = ""
        if date_matches:
            txn_date = date_matches[0].group(1)
            if len(date_matches) >= 2:
                notif_date = date_matches[1].group(1)

        # Amount
        amount_str = ""
        amount_val = 0
        amount_match = re.search(AMOUNT_RE, block)
        if amount_match:
            amount_str = amount_match.group(1).strip()
            amount_val = _parse_amount(amount_str)

        # Only record stock and option trades (filter out government securities, etc.)
        if asset_type not in ("ST", "OP"):
            i += 1
            continue

        # Parse transaction date
        txn_dt = None
        if txn_date:
            try:
                txn_dt = datetime.strptime(txn_date, "%m/%d/%Y")
            except Exception:
                pass

        transactions.append({
            "member_name": member_name,
            "state":       state,
            "doc_id":      doc_id,
            "filing_year": filing_year,
            "owner":       OWNER_LABELS.get(owner, owner or "Self"),
            "ticker":      ticker,
            "asset_type":  asset_type,
            "txn_type":    TXN_LABELS.get(txn_type, txn_type),
            "txn_type_raw": txn_type,
            "txn_date":    txn_date,
            "txn_dt":      txn_dt,
            "notif_date":  notif_date,
            "amount_str":  amount_str,
            "amount_val":  amount_val,
            "pdf_url":     HOUSE_PTR_PDF.format(year=filing_year, doc_id=doc_id),
        })

        i += 1

    # Deduplicate (same ticker + date + type within same filing)
    seen = set()
    unique = []
    for t in transactions:
        key = (t["ticker"], t["txn_date"], t["txn_type_raw"], t["owner"])
        if key not in seen:
            seen.add(key)
            unique.append(t)

    return unique


def get_recent_trades(days=90, max_pdfs=100, tickers=None):
    """
    Fetch and parse recent House PTR filings.
    Returns list of trade dicts sorted by transaction date desc.

    days      — how many days back to include (based on filing date)
    max_pdfs  — cap on number of PDFs to parse (newest first)
    tickers   — optional list to filter results
    """
    if not _PDF_OK:
        return {"error": "pdfplumber not installed", "trades": []}

    # Anchor to UTC so we don't accidentally exclude today's filings when the
    # host is in a UTC-ahead time zone (House filing dates are US Eastern but
    # naive vs UTC differs by ~5h — well within the "days" tolerance below).
    _now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = _now - timedelta(days=days)
    current_year = _now.year

    # Get filing indices for current and previous year
    all_ptrs = []
    for year in [current_year, current_year - 1]:
        recs = _fetch_fd_index(year)
        for r in recs:
            dt = _parse_filing_date(r["date_str"])
            if dt and dt >= cutoff:
                r["filing_dt"] = dt
                all_ptrs.append(r)

    # Sort newest first
    all_ptrs.sort(key=lambda x: x.get("filing_dt", datetime.min), reverse=True)
    all_ptrs = all_ptrs[:max_pdfs]

    # Parse PDFs in parallel (5 concurrent downloads)
    def _rehydrate_dt(trades):
        """Cache round-trip drops datetime objects to strings (json.dumps
        default=str). Re-derive txn_dt from txn_date so downstream sort
        comparisons stay homogenous."""
        for t in trades:
            td = t.get("txn_date")
            if td:
                try:
                    t["txn_dt"] = datetime.strptime(td, "%m/%d/%Y")
                except Exception:
                    t["txn_dt"] = None
            else:
                t["txn_dt"] = None
        return trades

    def _fetch_one(ptr):
        doc_id = ptr["doc_id"]
        year   = ptr["year"]
        member = f"{ptr['first']} {ptr['last']}"
        url    = HOUSE_PTR_PDF.format(year=year, doc_id=doc_id)

        # PDFs are immutable once filed — serve the parse output from cache
        # whenever possible. Skips the download + pdfplumber pass entirely.
        cache_key = ("congress_ptr", doc_id, year)
        if cache_store is not None:
            cached = cache_store.cache_get(cache_key)
            if cached is not None:
                return _rehydrate_dt(cached)

        def _neg_cache():
            """Remember a bad/empty doc so concurrent + later sweeps skip it."""
            if cache_store is not None:
                try:
                    cache_store.cache_set(cache_key, [], _PTR_NEG_TTL)
                except Exception:
                    pass

        try:
            resp = requests.get(url, headers=_HOUSE_HEADERS, timeout=15)
            if resp.status_code != 200:
                _neg_cache()  # 404s are permanent; don't re-fetch each sweep
                return []
        except Exception as e:
            log.warning("PTR fetch error for %s: %s", doc_id, e)
            return []  # transient network error — no negative cache

        # Bound the pdfplumber parse so a single malformed PDF can't stall
        # the whole batch. A plain DAEMON thread + join(timeout) gives the
        # same hard cap a ThreadPoolExecutor did, without its fatal flaw:
        # TPE workers are non-daemon, and concurrent.futures' atexit hook
        # joins them — one hung pdfplumber thread left by shutdown(wait=False)
        # made interpreter shutdown block forever, which wedged the Werkzeug
        # reloader holding the port (and py2app bundles at quit). A daemon
        # thread that hangs simply dies with the process.
        import threading

        # Circuit breaker: every parse whose join() times out leaves one
        # permanently-hung daemon thread behind. A handful is survivable;
        # dozens exhaust the process's OS thread budget and Werkzeug can no
        # longer spawn request handlers ("can't start new thread" → dead
        # server, observed live). Past the limit, stop attempting parses
        # entirely — neg-cache and move on; the breaker resets on restart.
        if _parse_circuit_open():
            log.warning("congress: parse circuit open (hung=%d busy_streak=%d); skipping %s",
                        _breaker["hung"], _breaker["busy_streak"], doc_id)
            _neg_cache()
            return []

        parse_out = {}

        def _parse_worker():
            # Acquire the gate WITH a timeout. A blocking `with _PDF_PARSE_GATE:`
            # made every queued worker wait forever behind a hung parse — the
            # thread-pileup that caused the exhaustion above.
            acquired = False
            try:
                acquired = _PDF_PARSE_GATE.acquire(timeout=_PDF_PARSE_TIMEOUT_S)
                if not acquired:
                    parse_out["gate_busy"] = True
                    parse_out["err"] = TimeoutError("parse gate busy")
                    return
                parse_out["trades"] = _parse_ptr_pdf(resp.content, member, doc_id, year)
                _note_parse_ok()
            except Exception as ex:  # surfaced after join below
                parse_out["err"] = ex
            finally:
                if acquired:
                    try:
                        _PDF_PARSE_GATE.release()
                    except ValueError:
                        pass

        try:
            t = threading.Thread(target=_parse_worker, daemon=True,
                                 name="ptr-parse-{}".format(doc_id))
            t.start()
        except RuntimeError as e:
            # Interpreter already tearing down (atexit fired mid-session) —
            # parse in-thread; we lose the timeout bound, but a well-formed
            # PDF parses in <1s and returning [] would silently lose trades.
            log.warning("congress: parse thread start failed (%s); parsing %s in-thread",
                        e, doc_id)
            try:
                trades = _parse_ptr_pdf(resp.content, member, doc_id, year)
            except Exception as ex:
                log.warning("congress: in-thread parse failed for %s: %s", doc_id, ex)
                return []
            if cache_store is not None and trades:
                try:
                    cache_store.cache_set(cache_key, trades, _PTR_CACHE_TTL)
                except Exception:
                    pass
            return trades

        # Worst case inside the worker: gate wait (≤timeout) + parse. Give the
        # join both phases before declaring the thread hung.
        t.join(_PDF_PARSE_TIMEOUT_S * 2)
        if t.is_alive():
            log.warning("PTR parse timeout for %s — abandoning", doc_id)
            _note_hung_parse()
            _neg_cache()
            return []
        if "err" in parse_out:
            log.warning("congress: parse failed for %s: %s", doc_id, parse_out["err"])
            if parse_out.get("gate_busy"):
                _note_gate_busy()  # starvation evidence — feeds the breaker
            _neg_cache()
            return []
        trades = parse_out.get("trades", [])

        if cache_store is not None:
            try:
                # Cache empty results too (short TTL): a legitimately
                # trade-less PTR used to be re-downloaded and re-parsed on
                # every sweep because only truthy lists were cached.
                cache_store.cache_set(cache_key, trades,
                                      _PTR_CACHE_TTL if trades else _PTR_NEG_TTL)
            except Exception:
                pass
        return trades

    # Bundle-state guard: route through safe_executor so a tripped
    # `concurrent.futures.thread._shutdown` falls back to serial fetches
    # rather than 500-ing the congress endpoints. _fetch_one returns a
    # list (or [] on error), so parallel_map's None-on-raise rule rarely
    # trips here.
    import safe_executor
    all_trades = []
    fetched = safe_executor.parallel_map(
        _fetch_one, all_ptrs, max_workers=5, thread_name_prefix="congress-ptr",
    )
    for trades in fetched:
        if trades:
            all_trades.extend(trades)

    # Filter by ticker if requested
    if tickers:
        upper = [t.upper() for t in tickers]
        all_trades = [t for t in all_trades if t["ticker"] in upper]

    # Sort by transaction date desc
    all_trades.sort(
        key=lambda x: x.get("txn_dt") or datetime.min,
        reverse=True
    )

    return {"trades": all_trades, "filings_parsed": len(all_ptrs)}


def get_trades_for_ticker(ticker, days=180):
    """Get all recent congressional trades for a specific ticker."""
    result = get_recent_trades(days=days, max_pdfs=200, tickers=[ticker])
    return result.get("trades", [])


def get_congress_summary(days=90, max_pdfs=80):
    """
    Get summary of recent congressional trading activity:
    - most traded tickers
    - recent trades table
    - buy/sell ratio by ticker
    """
    result = get_recent_trades(days=days, max_pdfs=max_pdfs)
    trades = result.get("trades", [])

    # Aggregate by ticker
    ticker_stats = {}
    for t in trades:
        sym = t["ticker"]
        if sym not in ticker_stats:
            ticker_stats[sym] = {
                "ticker": sym,
                "buys": 0,
                "sells": 0,
                "total_trades": 0,
                "members": set(),
                "latest_date": "",
                "_latest_dt": None,
                "total_amount": 0,
            }
        s = ticker_stats[sym]
        raw = t.get("txn_type_raw", "")
        # "SP" was a typo — it's the owner code for Spouse, never a
        # transaction type. The exchange variants ("S (Exchange)",
        # "E (Exchange)") aren't unambiguous sells either, but we keep
        # them under sells the way the parser tags them.
        if raw in ("P", "PE"):
            s["buys"] += 1
        elif raw in ("S", "S (partial)", "SE", "S (Exchange)", "E (Exchange)"):
            s["sells"] += 1
        s["total_trades"] += 1
        s["members"].add(t["member_name"])
        s["total_amount"] += t.get("amount_val", 0)
        # Compare on the parsed datetime, not the MM/DD/YYYY string.
        # Lexicographic compare of "12/05/2025" > "01/15/2026" is True
        # because '1' > '0' — but Jan-2026 is later than Dec-2025, so
        # the recorded "latest" was perpetually a December trade.
        cand_dt = t.get("txn_dt")
        if cand_dt is None and t.get("txn_date"):
            try:
                cand_dt = datetime.strptime(t["txn_date"], "%m/%d/%Y")
            except Exception:
                cand_dt = None
        if cand_dt is not None and (s["_latest_dt"] is None or cand_dt > s["_latest_dt"]):
            s["_latest_dt"] = cand_dt
            s["latest_date"] = t["txn_date"]

    # Convert sets to counts
    ticker_list = []
    for sym, s in ticker_stats.items():
        member_count = len(s["members"])
        s["member_count"] = member_count
        del s["members"]
        # _latest_dt is an internal sort key; downstream callers (UI /
        # JSON serialiser) only need the string form in latest_date.
        s.pop("_latest_dt", None)
        buy_pct = round(s["buys"] / s["total_trades"] * 100) if s["total_trades"] else 0
        s["buy_pct"] = buy_pct
        s["sentiment"] = "BULLISH" if buy_pct >= 70 else ("BEARISH" if buy_pct <= 30 else "MIXED")
        ticker_list.append(s)

    ticker_list.sort(key=lambda x: x["total_trades"], reverse=True)

    return {
        "trades": trades[:200],
        "ticker_summary": ticker_list[:50],
        "filings_parsed": result.get("filings_parsed", 0),
        "total_trades_found": len(trades),
    }
