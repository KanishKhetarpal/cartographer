"""Pins the Windows write-mode fix in isolation from Docker.

The bug this guards is silent: `Path.write_text` on Windows succeeds, returns
a byte count, and raises nothing while quietly turning every "\\n" into
"\\r\\n". A test that only checked "the call didn't raise" would pass on the
broken behaviour just as happily as the fixed one -- so every assertion here
reads the file back and checks for the literal byte, not just for success.
"""

from __future__ import annotations

import pathlib

from cartographer.sandbox._win_launcher import _install_patch, _write_text_no_translation

_orig_write_text = pathlib.Path.write_text


def test_default_newline_writes_lf_only(tmp_path):
    p = tmp_path / "eval.sh"
    _write_text_no_translation(p, "#!/bin/bash\nset -uxo pipefail\n")
    raw = p.read_bytes()
    assert b"\r\n" not in raw
    assert raw == b"#!/bin/bash\nset -uxo pipefail\n"


def test_an_explicit_newline_argument_is_still_honoured(tmp_path):
    """The patch changes the default, not the contract -- a caller that
    explicitly asks for CRLF (there is a real one: `.txt` output some tool
    downstream expects Windows line endings from) must still get it."""
    p = tmp_path / "explicit.txt"
    _write_text_no_translation(p, "a\nb\n", newline="\r\n")
    assert p.read_bytes() == b"a\r\nb\r\n"


def test_install_patch_changes_path_write_text_process_wide(tmp_path, monkeypatch):
    """The integration point that actually matters: after `_install_patch()`,
    an ordinary `Path.write_text("...")` call anywhere -- including inside
    `swebench`'s own modules, which we never import here -- goes through the
    no-translation path with no other code change."""
    monkeypatch.setattr(pathlib.Path, "write_text", _orig_write_text, raising=True)
    try:
        _install_patch()
        p = tmp_path / "f.txt"
        p.write_text("line1\nline2\n")
        assert p.read_bytes() == b"line1\nline2\n"
    finally:
        pathlib.Path.write_text = _orig_write_text


def test_unpatched_path_write_text_is_the_known_broken_baseline(tmp_path, monkeypatch):
    """Documents the defect this module exists to fix, on whatever platform
    the suite runs on. On Windows this proves the bug is real; elsewhere it
    is a no-op assertion (no platform does the opposite translation), so the
    test is meaningful without being platform-gated."""
    monkeypatch.setattr(pathlib.Path, "write_text", _orig_write_text, raising=True)
    p = tmp_path / "unpatched.sh"
    p.write_text("a\nb\n")
    raw = p.read_bytes()
    import os

    if os.linesep == "\r\n":
        assert b"\r\n" in raw, "expected the unpatched Windows bug to reproduce here"
