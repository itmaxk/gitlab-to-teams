import sys
from pathlib import Path

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

    def fake_template_response(request, template_name, context):
        captured["template_name"] = template_name
        captured["context"] = context
        return context

    monkeypatch.setattr(pages.templates, "TemplateResponse", fake_template_response)

    pages.polled_mrs(object(), **filters)

    assert captured["template_name"] == "polled.html"
    return captured["context"]


def _rendered_polled_rows(monkeypatch, **filters):
    return _rendered_polled_context(monkeypatch, **filters)["rows"]


def _insert_polled_mr(conn, *, mr_iid, mr_state, rules_matched, polled_at):
    conn.execute(
        """
        INSERT INTO polled_mrs
          (mr_iid, mr_title, mr_state, target_branch, rules_matched, success, polled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mr_iid,
            f"MR {mr_iid}",
            mr_state,
            "master",
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


def test_polled_shows_merged_mr_poll_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    from services import poller

    poller._set_merged_mr_poll_cursor("master", "2026-05-08T13:55:27Z")

    context = _rendered_polled_context(monkeypatch)

    assert context["stats"]["merged_cursors"] == [
        {"branch": "master", "merged_at": "2026-05-08T13:55:27Z"}
    ]
