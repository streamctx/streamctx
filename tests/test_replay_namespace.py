"""Regression: streamctx.replay must stay a callable after inner imports.

Issue #6: importing CounterfactualReplayer from a submodule named ``replay``
inside ``streamctx/__init__.py`` rebound ``streamctx.replay`` to the module
object. Subsequent calls then raised TypeError: 'module' object is not callable.
"""

from __future__ import annotations

import streamctx


def test_replay_stays_callable_after_list_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMCTX_HOME", str(tmp_path))
    session_id = 1
    streamctx.list_checkpoints(session_id)
    streamctx.replay(session_id, from_step=1, dry_run=True)
    assert callable(streamctx.replay)


def test_replay_stays_callable_when_called_twice(tmp_path, monkeypatch):
    monkeypatch.setenv("STREAMCTX_HOME", str(tmp_path))
    session_id = 1
    streamctx.replay(session_id, from_step=1, dry_run=True)
    streamctx.replay(session_id, from_step=1, dry_run=True)
    assert callable(streamctx.replay)
