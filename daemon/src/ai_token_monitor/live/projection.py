"""Burn-rate projection over the provider's real used-percent samples.

The live pollers sample the authoritative used% every ~90s; a linear rate over
the recent samples says when the window would hit 100%. Only a depletion
BEFORE the provider's own reset is interesting — otherwise the window rolls
over first and there is nothing to warn about.
"""

from __future__ import annotations

#: Ignore samples older than this when fitting the rate: the projection should
#: react to the current pace, not the whole window's average.
SAMPLE_HORIZON_S = 60 * 60.0
#: Two samples closer together than this measure polling jitter, not a rate.
MIN_SPAN_S = 5 * 60.0


def project_depletion(samples: list[tuple[float, float]], now: float,
                      resets_at: float | None) -> float | None:
    """Epoch when used% reaches 100 at the current pace, or ``None``.

    ``samples`` are ``(ts, used_percent)`` pairs for the CURRENT window, in
    chronological order (the caller resets the list when the window rolls
    over). ``None`` when there's no usable rate, the projection is already in
    the past (the 100% alert covers that), or depletion would land after
    ``resets_at``.
    """
    recent = [(ts, pct) for ts, pct in samples if now - ts <= SAMPLE_HORIZON_S]
    if len(recent) < 2:
        return None
    t0, p0 = recent[0]
    t1, p1 = recent[-1]
    if t1 - t0 < MIN_SPAN_S or p1 <= p0 or p1 >= 100.0:
        return None
    rate = (p1 - p0) / (t1 - t0)  # %/s, > 0 here
    depletes_at = t1 + (100.0 - p1) / rate
    if depletes_at <= now:
        return None
    if resets_at is not None and depletes_at >= float(resets_at):
        return None
    return depletes_at
