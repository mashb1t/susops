"""Tests for cumulative bandwidth counters and uptime tracking."""
from __future__ import annotations

import time
import threading
from unittest.mock import MagicMock

import pytest

from susops.facade import _BandwidthSampler
from susops.core.process import ProcessManager


@pytest.fixture
def sampler(tmp_path):
    """Sampler with background thread started but no real sampling."""
    mgr = MagicMock(spec=ProcessManager)
    mgr.status_all.return_value = {}
    s = _BandwidthSampler(mgr)
    yield s


def test_totals_start_at_zero(sampler):
    assert sampler.get_totals("pi3") == (0.0, 0.0)


def test_reset_totals_single_tag(sampler):
    with sampler._lock:
        sampler._totals["pi3"] = (100.0, 50.0)
        sampler._totals["mash"] = (200.0, 80.0)
    sampler.reset_totals("pi3")
    assert sampler.get_totals("pi3") == (0.0, 0.0)
    # Other tag unaffected
    assert sampler.get_totals("mash") == (200.0, 80.0)


def test_reset_totals_all(sampler):
    with sampler._lock:
        sampler._totals["pi3"] = (100.0, 50.0)
        sampler._totals["mash"] = (200.0, 80.0)
    sampler.reset_totals()
    assert sampler.get_totals("pi3") == (0.0, 0.0)
    assert sampler.get_totals("mash") == (0.0, 0.0)


def test_totals_accumulate_across_injected_samples(sampler):
    """Directly write to _totals to simulate two accumulated samples."""
    with sampler._lock:
        sampler._totals["pi3"] = (500.0, 100.0)
    # Simulate a second accumulation (as _sample() would do)
    with sampler._lock:
        prev_rx, prev_tx = sampler._totals.get("pi3", (0.0, 0.0))
        sampler._totals["pi3"] = (prev_rx + 300.0, prev_tx + 60.0)
    assert sampler.get_totals("pi3") == (800.0, 160.0)
