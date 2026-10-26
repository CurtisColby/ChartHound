"""
ChartHound — log redaction.
Every log line (app, httpx, uvicorn access, tracebacks, Veterinarian console) has
secret query values replaced with *** before any handler sees it.
"""
import logging
import re

_SECRET = re.compile(
    r"((?:[?&]|\b)(?:x-plex-token|api_key|apikey|api-key|client|token|access_token|key|password|pass|auth|t)=)"
    r"[^&\s\"'<>]+", re.I)


def redact(s):
    return _SECRET.sub(r"\1***", s) if isinstance(s, str) else s


def install():
    old_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        rec = old_factory(*args, **kwargs)
        rec.msg = redact(rec.msg)
        if isinstance(rec.args, tuple):
            rec.args = tuple(redact(a) for a in rec.args)
        elif isinstance(rec.args, dict):
            rec.args = {k: redact(v) for k, v in rec.args.items()}
        return rec

    logging.setLogRecordFactory(factory)
    _fmt_exc = logging.Formatter.formatException
    logging.Formatter.formatException = lambda self, ei: redact(_fmt_exc(self, ei))
    # httpx logs every request URL (incl. ?X-Plex-Token=…) at INFO — keep only problems
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
