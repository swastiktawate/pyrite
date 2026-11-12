"""An update refused because its result would not load names the refused field (#1472).

The file on disk is fine: it is the update that produced bytes the entry's
type cannot load, so the refusal must say that, name the refused field with
one ``errors`` row per field, and say nothing was written, instead of reusing
the on-disk load error's text ("does not load as its type ... Pyrite reads it
but writes nothing to it until the file is fixed by hand").

The trigger is an injected refusing loader, as in
``tests/test_unloadable_file_is_never_written.py``: the open PRs for #1247
make ``rank`` load leniently, so no test here may depend on it.
"""

from __future__ import annotations

import json

import pytest

from pyrite.config import KBConfig
from tests.test_unloadable_file_is_never_written import (  # noqa: F401 - the autouse fixture
    REL,
    Shape,
    _loaders_that_refuse,
    _text,
)


@pytest.fixture
def make_world(tmp_path):
    import contextlib

    from tests._world import build_world

    stack = contextlib.ExitStack()
    count = {"n": 0}

    def make(files: dict[str, str]):
        count["n"] += 1
        kb = tmp_path / f"sw{count['n']}"
        for rel, text in files.items():
            (kb / rel).parent.mkdir(parents=True, exist_ok=True)
            (kb / rel).write_text(text, encoding="utf-8")
        w = build_world(
            tmp_path / f"w{count['n']}", kbs=[KBConfig(name="sw", path=kb, kb_type="software")]
        )
        stack.callback(w.close)
        stack.enter_context(w.pointed())
        return w

    with stack:
        yield make


def _outcome(code, message, errors):
    return {"code": code, "message": message, "errors": errors}


def _cli_update(world, *field_args):
    result = world.cli("update", "x", "-k", "sw", *field_args, "--format", "json")
    assert result.exit_code == 1, result.output
    data = json.loads(result.stdout[result.stdout.index("{") :])
    return _outcome(data.get("error_code"), data.get("error", ""), data.get("errors"))


def _rest_patch(world, field, value):
    resp = world.client.patch("/api/entries/x", json={"kb": "sw", "field": field, "value": value})
    # 400, not the central handler's 422: the write endpoints map a
    # ValidationError through `refusal_http` (a bad request), as for every
    # other write refusal.
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    return _outcome(detail.get("code"), detail.get("message", ""), detail.get("errors"))


def _mcp_update(world, **fields):
    res = world.mcp()._dispatch_tool("kb_update", {"entry_id": "x", "kb_name": "sw", **fields})
    error = res.get("error")
    message = error.get("message", "") if isinstance(error, dict) else str(error or "")
    return _outcome(res.get("error_code"), message, res.get("errors"))


@pytest.mark.parametrize("surface", ["cli", "rest", "mcp"])
def test_a_refused_update_names_the_field_on_every_surface(make_world, surface):
    # The injected loader refuses `status: weird` (see _loaders_that_refuse);
    # the on-disk file loads, only the update's result does not.
    world = make_world({REL: _text("backlog_item", Shape())})
    path = world.kb_path("sw") / REL
    before = path.read_bytes()

    if surface == "cli":
        outcome = _cli_update(world, "-f", "status=weird")
    elif surface == "rest":
        outcome = _rest_patch(world, "status", "weird")
    else:
        outcome = _mcp_update(world, status="weird")

    assert outcome["code"] == "VALIDATION_FAILED", outcome
    assert "status" in outcome["message"], outcome
    assert "nothing was written" in outcome["message"], outcome
    assert "until the file is fixed by hand" not in outcome["message"], outcome
    rows = outcome["errors"] or []
    assert [row.get("field") for row in rows] == ["status"], outcome
    assert path.read_bytes() == before


def _refusing_on(monkeypatch, condition, exc):
    """A ``from_frontmatter`` that raises ``exc`` when ``condition`` holds on
    the frontmatter, and otherwise loads as before."""
    from pyrite_software_kb.entry_types import BacklogItemEntry

    original = BacklogItemEntry.from_frontmatter.__func__

    def from_frontmatter(klass, meta, body=""):
        if condition(meta):
            raise exc
        return original(klass, meta, body)

    monkeypatch.setattr(BacklogItemEntry, "from_frontmatter", classmethod(from_frontmatter))


def test_a_cross_field_refusal_keeps_the_loaders_message_and_no_errors_row(make_world, monkeypatch):
    # Done and a bug together: neither key alone explains the refusal, so the
    # load error carries no fields and the refusal keeps the loader's message.
    _refusing_on(
        monkeypatch,
        lambda meta: meta.get("status") == "done" and meta.get("kind") == "bug",
        ValueError("a done item cannot be a bug"),
    )
    text = _text("backlog_item", Shape()).replace("title: T", "title: T\nkind: bug")
    world = make_world({REL: text})
    path = world.kb_path("sw") / REL
    before = path.read_bytes()

    outcome = _cli_update(world, "-f", "status=done")

    assert outcome["code"] == "VALIDATION_FAILED", outcome
    assert "a done item cannot be a bug" in outcome["message"], outcome
    assert "nothing was written" in outcome["message"], outcome
    assert outcome["errors"] is None, outcome
    assert path.read_bytes() == before


@pytest.mark.control(reason="pins today's wrapping for a non-EntryLoadError load failure")
def test_a_non_entry_load_error_keeps_todays_wrapping(make_world, monkeypatch):
    # A loader that raises a FrontmatterError itself is not an EntryLoadError,
    # so the result-load refusal keeps today's wrapping.
    from pyrite.exceptions import FrontmatterError

    _refusing_on(
        monkeypatch,
        lambda meta: meta.get("title") == "Boom",
        FrontmatterError("the loader refused this frontmatter"),
    )
    world = make_world({REL: _text("backlog_item", Shape())})
    path = world.kb_path("sw") / REL
    before = path.read_bytes()

    outcome = _cli_update(world, "-f", "title=Boom")

    assert outcome["code"] == "VALIDATION_FAILED", outcome
    assert "the result would not load" in outcome["message"], outcome
    assert "the loader refused this frontmatter" in outcome["message"], outcome
    assert outcome["errors"] is None, outcome
    assert path.read_bytes() == before
