"""Tests for verify_rename.py robustness (#86): scheme guard + clean summary."""

from __future__ import annotations

import pytest

import verify_rename as vr


def test_get_json_rejects_non_http_scheme():
    # #86: urlopen would otherwise honour file:// and custom handlers.
    for bad in ("file:///etc/passwd", "ftp://h/x", "gopher://h"):
        with pytest.raises(ValueError, match="non-http"):
            vr._get_json(bad)


def test_summary_reports_pass_and_fail():
    vr.checks.clear()
    vr.checks.append((True, "a ok"))
    assert vr._summary() == 0  # all pass -> 0
    vr.checks.append((False, "b broke"))
    assert vr._summary() == 1  # any fail -> 1
    vr.checks.clear()
