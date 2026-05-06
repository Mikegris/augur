"""
sec_filings_v2.py — High-level SEC wrapper using edgartools.

Augments the existing sec_edgar.py (raw HTTP) with structured access to
Form 4, 10-K, 10-Q, and 8-K via dgunning/edgartools.
"""

import logging
import threading
import time

log = logging.getLogger("augur.sec_v2")

_lock = threading.Lock()
_initialized = False


def _ensure_init():
    """edgartools requires set_identity() to be polite to SEC."""
    global _initialized
    with _lock:
        if _initialized:
            return
        try:
            from edgar import set_identity
            set_identity("AUGUR research@augur.local")
            _initialized = True
        except Exception as e:
            log.warning("edgartools init failed: %s", e)


def get_company_filings(ticker, form=None, limit=20):
    """Recent filings for ticker. form: '10-K' / '10-Q' / '8-K' / '4' / None"""
    _ensure_init()
    try:
        from edgar import Company
        co = Company(ticker)
        if not co:
            return []
        filings = co.get_filings(form=form) if form else co.get_filings()
        out = []
        for f in filings.head(limit):
            out.append({
                "form": getattr(f, "form", "") or "",
                "filed": str(getattr(f, "filing_date", "") or ""),
                "period_of_report": str(getattr(f, "period_of_report", "") or ""),
                "accession": getattr(f, "accession_no", "") or "",
                "primary_doc": getattr(f, "primary_doc_url", "") or "",
                "url": getattr(f, "homepage_url", "") or "",
            })
        return out
    except Exception as e:
        log.warning("get_company_filings %s: %s", ticker, e)
        return []


def get_form4_transactions(ticker, limit=30):
    """Structured Form 4 (insider) transactions for a ticker via edgartools."""
    _ensure_init()
    try:
        from edgar import Company
        co = Company(ticker)
        if not co:
            return []
        filings = co.get_filings(form="4").head(limit)
        out = []
        for f in filings:
            try:
                obj = f.obj()
                # Form 4 obj has reporting_owners, transactions
                owner = ""
                role = ""
                try:
                    owners = getattr(obj, "reporting_owners", None) or []
                    if owners:
                        owner = getattr(owners[0], "name", "") or str(owners[0])
                        role = getattr(owners[0], "officer_title", "") or ""
                except Exception:
                    pass
                txns = []
                try:
                    for t in (getattr(obj, "non_derivative_transactions", None) or []):
                        txns.append({
                            "code": getattr(t, "code", ""),
                            "date": str(getattr(t, "date", "") or ""),
                            "shares": getattr(t, "shares", None),
                            "price": getattr(t, "price", None),
                        })
                except Exception:
                    pass
                out.append({
                    "filed": str(getattr(f, "filing_date", "") or ""),
                    "owner": owner,
                    "role": role,
                    "accession": getattr(f, "accession_no", "") or "",
                    "transactions": txns,
                    "url": getattr(f, "homepage_url", "") or "",
                })
            except Exception as inner:
                log.debug("form4 row parse: %s", inner)
                continue
        return out
    except Exception as e:
        log.warning("form4 %s: %s", ticker, e)
        return []
