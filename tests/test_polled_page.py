import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db


class FakeTemplates:
    def __init__(self, *args, **kwargs):
        pass

    def TemplateResponse(self, request, template_name, context):
        return context


import fastapi.templating
import starlette.templating

fastapi.templating.Jinja2Templates = FakeTemplates
starlette.templating.Jinja2Templates = FakeTemplates

from routers import pages


def _rendered_polled_context(monkeypatch, **filters):
    captured = {}

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 8, 12, 0, tzinfo=tz)

    def fake_template_response(request, template_name, context):
        captured["template_name"] = template_name
        captured["context"] = context
        return context

    monkeypatch.setattr(pages.templates, "TemplateResponse", fake_template_response)
    monkeypatch.setattr(pages, "datetime", FixedDatetime)

    pages.polled_mrs(object(), **filters)

    assert captured["template_name"] == "polled.html"
    return captured["context"]


def _rendered_polled_rows(monkeypatch, **filters):
    return _rendered_polled_context(monkeypatch, **filters)["rows"]


def _insert_polled_mr(
    conn,
    *,
    mr_iid,
    mr_state,
    rules_matched,
    polled_at,
    mr_created_at="2026-05-01T00:00:00Z",
    mr_merged_at="",
):
    conn.execute(
        """
        INSERT INTO polled_mrs
          (mr_iid, mr_title, mr_state, target_branch, mr_created_at, mr_merged_at,
           rules_matched, success, polled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mr_iid,
            f"MR {mr_iid}",
            mr_state,
            "master",
            mr_created_at,
            mr_merged_at,
            rules_matched,
            1,
            polled_at,
        ),
    )


def test_polled_reset_does_not_hide_older_statuses_behind_implicit_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    _insert_polled_mr(
        conn,
        mr_iid=1,
        mr_state="opened",
        rules_matched=1,
        polled_at="2026-05-01 00:00:00",
    )
    for i in range(2, 503):
        _insert_polled_mr(
            conn,
            mr_iid=i,
            mr_state="merged",
            rules_matched=0,
            polled_at=f"2026-05-02 00:{i % 60:02d}:00",
            mr_merged_at="2026-05-02T00:00:00Z",
        )
    conn.commit()
    conn.close()

    rows = _rendered_polled_rows(monkeypatch)

    states = {row["mr_state"] for row in rows}
    assert states == {"merged", "opened"}


def test_polled_combines_status_and_matches_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    _insert_polled_mr(
        conn,
        mr_iid=1,
        mr_state="opened",
        rules_matched=1,
        polled_at="2026-05-01 00:00:00",
    )
    _insert_polled_mr(
        conn,
        mr_iid=2,
        mr_state="merged",
        rules_matched=1,
        polled_at="2026-05-02 00:00:00",
        mr_merged_at="2026-05-02T00:00:00Z",
    )
    _insert_polled_mr(
        conn,
        mr_iid=3,
        mr_state="opened",
        rules_matched=0,
        polled_at="2026-05-03 00:00:00",
    )
    conn.commit()
    conn.close()

    rows = _rendered_polled_rows(monkeypatch, mr_state="opened", has_matches=1)

    assert [row["mr_iid"] for row in rows] == [1]


def test_polled_default_hides_old_merged_and_closed_until_show_all(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    _insert_polled_mr(
        conn,
        mr_iid=1,
        mr_state="opened",
        rules_matched=0,
        polled_at="2026-05-08 10:00:00",
        mr_created_at="2025-01-01T00:00:00Z",
    )
    _insert_polled_mr(
        conn,
        mr_iid=2,
        mr_state="open",
        rules_matched=0,
        polled_at="2026-05-08 10:01:00",
        mr_created_at="2025-01-01T00:00:00Z",
    )
    _insert_polled_mr(
        conn,
        mr_iid=3,
        mr_state="merged",
        rules_matched=0,
        polled_at="2026-05-08 10:02:00",
        mr_created_at="2023-12-04T00:00:00Z",
        mr_merged_at="2023-12-04T13:55:27Z",
    )
    _insert_polled_mr(
        conn,
        mr_iid=4,
        mr_state="merged",
        rules_matched=0,
        polled_at="2026-05-08 10:03:00",
        mr_created_at="2026-05-01T00:00:00Z",
        mr_merged_at="2026-05-01T13:55:27Z",
    )
    _insert_polled_mr(
        conn,
        mr_iid=5,
        mr_state="closed",
        rules_matched=0,
        polled_at="2026-05-08 10:04:00",
        mr_created_at="2026-05-01T00:00:00Z",
    )
    conn.commit()
    conn.close()

    default_rows = _rendered_polled_rows(monkeypatch)
    all_rows = _rendered_polled_rows(monkeypatch, show_all=1)

    assert [row["mr_iid"] for row in default_rows] == [4, 2, 1]
    assert [row["mr_iid"] for row in all_rows] == [5, 4, 3, 2, 1]


def test_polled_shows_merged_mr_poll_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    from services import poller

    poller._set_merged_mr_poll_cursor("master", "2026-05-08T13:55:27Z")

    context = _rendered_polled_context(monkeypatch)

    assert context["stats"]["merged_cursors"] == [
        {"branch": "master", "merged_at": "2026-05-08T13:55:27Z"}
    ]
