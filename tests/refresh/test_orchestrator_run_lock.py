"""LOCKSTEP INVARIANT — one refresh run at a time per relay, whatever the
requested scope, and a refused request is always visible.

The CLI is the one relay where the collision is between *processes*: the
browser guards with a ref in its refresh hook and macOS with
``AppState.isRefreshing``, neither of which a second `costcompass mtd refresh`
would ever see. These pin the advisory ``flock`` that stands in for them, and
that its release is the kernel's job rather than a cleanup path that a crash
could skip.
"""

from __future__ import annotations

import contextlib
import multiprocessing
import os
from pathlib import Path

import pytest

from costcompass import config
from costcompass.refresh import orchestrator

# `_run` is stubbed in the two tests that use this, so nothing reads it.
CFG = config.Config(api_url="https://x/api/v1", api_key=None)


def _lock_path() -> Path:
    return Path(os.environ["XDG_CONFIG_HOME"]) / "costcompass" / "refresh.lock"


def _acquire_again() -> None:
    """Attempt a second acquisition. The body must never run."""
    with orchestrator._acquire_run_lock():
        pytest.fail("the second acquisition must not be granted")


def test_second_acquisition_is_refused_with_the_shared_message() -> None:
    with (
        orchestrator._acquire_run_lock(),
        pytest.raises(orchestrator.RefreshAlreadyRunning) as excinfo,
    ):
        _acquire_again()
    # The same sentence the browser hook and macOS ErrorText show, so the
    # refusal reads identically wherever the user meets it.
    assert str(excinfo.value) == "A refresh is already running."
    # RefreshError, so main.py's existing handler reports it through _fail —
    # a non-zero exit with the sentence, never a silent no-op.
    assert isinstance(excinfo.value, orchestrator.RefreshError)


def test_lock_is_released_after_a_clean_run() -> None:
    with orchestrator._acquire_run_lock():
        pass
    with orchestrator._acquire_run_lock():
        pass


def test_lock_is_released_when_the_body_raises() -> None:
    """A failed run must not wedge the relay: the next refresh is a retry, not
    a second refusal."""
    with pytest.raises(ValueError), orchestrator._acquire_run_lock():
        raise ValueError("run blew up")
    with orchestrator._acquire_run_lock():
        pass


def test_lock_file_is_not_unlinked_on_release() -> None:
    """Unlinking would race a process that already holds the descriptor — it
    would unlink the file the *other* process is locked on, and the next
    acquisition would create a fresh inode and be granted alongside it."""
    with orchestrator._acquire_run_lock():
        pass
    assert _lock_path().exists()


def _hold_then_report(path: str, acquired: multiprocessing.Queue[str]) -> None:
    """Child body for the cross-process test — module level so it is picklable
    under the ``spawn`` start method macOS defaults to."""
    os.environ["XDG_CONFIG_HOME"] = path
    try:
        with orchestrator._acquire_run_lock():
            acquired.put("granted")
    except orchestrator.RefreshAlreadyRunning:
        acquired.put("refused")


def test_a_separate_process_is_refused_while_the_lock_is_held() -> None:
    """The point of the whole mechanism: an in-memory flag cannot see another
    process, and this is what proves the file lock does."""
    ctx = multiprocessing.get_context("spawn")
    reported: multiprocessing.Queue[str] = ctx.Queue()
    with orchestrator._acquire_run_lock():
        child = ctx.Process(
            target=_hold_then_report, args=(os.environ["XDG_CONFIG_HOME"], reported)
        )
        child.start()
        child.join(timeout=30)
        assert child.exitcode == 0
    assert reported.get(timeout=5) == "refused"


def test_the_child_is_granted_once_the_parent_has_released() -> None:
    """The mirror of the test above — a refusal that never lifts would be a
    wedge, not a mutex."""
    ctx = multiprocessing.get_context("spawn")
    reported: multiprocessing.Queue[str] = ctx.Queue()
    with orchestrator._acquire_run_lock():
        pass
    child = ctx.Process(
        target=_hold_then_report, args=(os.environ["XDG_CONFIG_HOME"], reported)
    )
    child.start()
    child.join(timeout=30)
    assert child.exitcode == 0
    assert reported.get(timeout=5) == "granted"


def test_run_scopes_the_lock_over_the_whole_run(monkeypatch) -> None:
    """``run`` is a thin wrapper whose only job is to hold the lock across
    everything ``_run`` does — including the vault fetch and create-run, which
    is where a second process would otherwise do its damage."""
    events: list[str] = []

    @contextlib.contextmanager
    def probe():
        events.append("acquired")
        try:
            yield
        finally:
            events.append("released")

    monkeypatch.setattr(
        orchestrator, "_run", lambda *_a, **_k: events.append("ran") or "result"
    )
    assert orchestrator.run(CFG, "key", None, "pw", run_lock=probe) == "result"
    assert events == ["acquired", "ran", "released"]


def test_run_takes_the_real_file_lock_by_default(monkeypatch) -> None:
    """The injected seam above must not be the only thing that locks — the
    default has to be the real one, or production runs unguarded."""
    refused: list[bool] = []

    def _probe(*_a, **_k) -> str:
        # The run body is inside the lock, so a second acquisition — even from
        # this same process — must be refused.
        try:
            with orchestrator._acquire_run_lock():
                refused.append(False)
        except orchestrator.RefreshAlreadyRunning:
            refused.append(True)
        return "result"

    monkeypatch.setattr(orchestrator, "_run", _probe)
    assert orchestrator.run(CFG, "key", None, "pw") == "result"
    assert refused == [True]
    # Released afterwards, so the next invocation is not refused.
    with orchestrator._acquire_run_lock():
        pass
