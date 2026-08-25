"""The pollers' shared opener must not carry a bearer token off-origin.

urllib copies request headers onto a redirect verbatim, so following one to
another host would hand that host the user's provider OAuth token. No network
here: the redirect handler is exercised directly.
"""

import email.message
import io
import urllib.error
import urllib.request

import pytest

from ai_token_monitor.live.http import NoCrossOriginRedirect

USAGE = "https://chatgpt.com/backend-api/wham/usage"


def _req():
    return urllib.request.Request(
        USAGE, headers={"Authorization": "Bearer super-secret"})


def _redirect(newurl, code=302):
    return NoCrossOriginRedirect().redirect_request(
        _req(), io.BytesIO(b""), code, "Found", email.message.Message(), newurl)


@pytest.mark.parametrize("newurl", [
    "https://evil.example/collect",          # another host entirely
    "https://chatgpt.com.evil.example/x",    # lookalike host
    "http://chatgpt.com/backend-api/usage",  # scheme downgrade, same host
])
def test_cross_origin_redirect_is_refused(newurl):
    with pytest.raises(urllib.error.HTTPError):
        _redirect(newurl)


@pytest.mark.parametrize("code", [301, 302, 303, 307])
def test_refusal_covers_every_redirect_code(code):
    with pytest.raises(urllib.error.HTTPError):
        _redirect("https://evil.example/collect", code)


def test_same_origin_redirect_still_works():
    out = _redirect("https://chatgpt.com/backend-api/wham/usage2")
    assert out.full_url == "https://chatgpt.com/backend-api/wham/usage2"


def test_relative_redirect_resolves_against_the_original():
    out = _redirect("/backend-api/other")
    assert out.full_url == "https://chatgpt.com/backend-api/other"
