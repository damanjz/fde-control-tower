import pytest

from tower.breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker
from tower.carrier import backoff_delay
from tower.security import SignatureError, sign, verify

BODY = b'{"pings": []}'


def test_signature_roundtrip_and_failures():
    ts = 1_000_000
    sig = sign("s3cret", ts, BODY)
    assert verify("s3cret", BODY, sig, str(ts), now_ts=ts + 10, window_s=300) == ts
    cases = [
        (sign("other", ts, BODY), str(ts), "bad_signature"),
        (sig, str(ts + 1), "bad_signature"),             # timestamp is part of the signed string
        (None, str(ts), "missing_headers"),
        (sig, "abc", "bad_timestamp"),
    ]
    for s, t, code in cases:
        with pytest.raises(SignatureError) as e:
            verify("s3cret", BODY, s, t, now_ts=ts, window_s=300)
        assert e.value.code == code
    with pytest.raises(SignatureError) as e:
        verify("s3cret", BODY + b" ", sig, str(ts), now_ts=ts, window_s=300)
    assert e.value.code == "bad_signature"


def test_signature_window_is_symmetric():
    ts = 1_000_000
    sig = sign("k", ts, BODY)
    for now in (ts + 301, ts - 301):
        with pytest.raises(SignatureError) as e:
            verify("k", BODY, sig, str(ts), now_ts=now, window_s=300)
        assert e.value.code == "timestamp_outside_window"


class Tick:
    t = 0.0

    def __call__(self):
        return self.t


def test_breaker_transitions():
    clk, seen = Tick(), []
    b = CircuitBreaker(threshold=3, cooldown_s=10, max_cooldown_s=40, now=clk, on_change=lambda o, n: seen.append((o, n)))
    assert b.state == CLOSED and b.allow()
    b.record_failure()
    b.record_failure()
    b.record_success()                      # a success resets the count
    b.record_failure()
    b.record_failure()
    assert b.state == CLOSED
    b.record_failure()
    assert b.state == OPEN and not b.allow()
    clk.t = 9.9
    assert not b.allow()
    clk.t = 10.0
    assert b.allow() and b.state == HALF_OPEN
    b.record_failure()                      # probe fails: open again, cooldown doubles
    assert b.state == OPEN and b.cooldown == 20
    clk.t = 29.9
    assert not b.allow()
    clk.t = 30.0
    assert b.allow() and b.state == HALF_OPEN
    b.record_success()
    assert b.state == CLOSED and b.cooldown == 10
    assert seen == [(CLOSED, OPEN), (OPEN, HALF_OPEN), (HALF_OPEN, OPEN), (OPEN, HALF_OPEN), (HALF_OPEN, CLOSED)]


def test_backoff_is_exponential_with_jitter_and_capped():
    for i in range(6):
        d = backoff_delay(i, base=0.25, cap=2.0)
        assert min(2.0, 0.25 * 2**i) <= d <= min(2.0, 0.25 * 2**i) + 0.25 + 1e-9
    assert len({round(backoff_delay(3), 6) for _ in range(20)}) > 1       # jittered
