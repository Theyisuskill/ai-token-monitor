"""Shared HTTP for the pollers: an opener that can't leak a bearer token.

``urllib`` copies a request's headers onto a redirect verbatim — cross-origin
included, since only Content-Length/Content-Type are stripped. Every poller
here sends the user's provider OAuth token in an ``Authorization`` header, so
an endpoint answering ``302`` with a URL on another host (a compromised or
misconfigured provider, a captive portal, a TLS-terminating middlebox) would
be handed that token by the redirect alone.

None of these usage APIs redirect. So a redirect that leaves the request's
scheme+host is refused outright instead of followed, and the poller reports it
as a failed poll like any other bad response. Same-origin redirects still work.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


class NoCrossOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that refuses to change scheme or host.

    Raising ``HTTPError`` is how the stdlib handler itself rejects a redirect
    it won't follow, so callers need no new except-clause: the poller maps it
    to an ``http_<code>`` status and keeps serving the last good data.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urljoin because a Location header may be relative — and because the
        # origin decision and the URL actually followed must be the same one.
        target = urllib.parse.urljoin(req.full_url, newurl)
        if _origin(target) != _origin(req.full_url):
            raise urllib.error.HTTPError(
                req.full_url, code, "cross-origin redirect refused",
                headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, target)


#: Built once: same default handler set as urllib.request.urlopen (proxies,
#: HTTPS, cookies-less), with the redirect handler swapped for the strict one.
_OPENER = urllib.request.build_opener(NoCrossOriginRedirect)


def urlopen(req: urllib.request.Request, timeout: float) -> Any:
    """``urllib.request.urlopen`` that never follows a cross-origin redirect.

    Use for every authenticated request to a provider. Loopback probes (no
    credential in the headers) can use urllib directly.
    """
    return _OPENER.open(req, timeout=timeout)
