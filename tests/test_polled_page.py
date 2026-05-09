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
    release_branches = filters.pop("_release_branches", [])

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
    monkeypatch.setattr(pages, "_latest_release_branch_dates", lambda: release_branches)

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
    mr_url="",
):
    conn.execute(
        """
        INSERT INTO polled_mrs
          (mr_iid, mr_title, mr_url, mr_state, target_branch, mr_created_at, mr_merged_at,
           rules_matched, success, polled_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            mr_iid,
            f"MR {mr_iid}",
            mr_url,
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


def test_polled_filters_apply_to_latest_row_per_mr(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    _insert_polled_mr(
        conn,
        mr_iid=14326,
        mr_state="opened",
        rules_matched=1,
        polled_at="2026-05-09 10:10:37",
    )
    _insert_polled_mr(
        conn,
        mr_iid=14326,
        mr_state="opened",
        rules_matched=0,
        polled_at="2026-05-09 10:27:54",
    )
    conn.commit()
    conn.close()

    default_rows = _rendered_polled_rows(monkeypatch)
    matched_rows = _rendered_polled_rows(monkeypatch, has_matches=1)
    unmatched_rows = _rendered_polled_rows(monkeypatch, has_matches=0)

    assert [(row["mr_iid"], row["polled_at"], row["rules_matched"]) for row in default_rows] == [
        (14326, "2026-05-09 10:27:54", 0)
    ]
    assert matched_rows == []
    assert [(row["mr_iid"], row["polled_at"], row["rules_matched"]) for row in unmatched_rows] == [
        (14326, "2026-05-09 10:27:54", 0)
    ]


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


def test_polled_show_all_zero_keeps_default_filter_and_statuses_are_case_insensitive(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    conn = db.get_db()
    _insert_polled_mr(
        conn,
        mr_iid=1,
        mr_state="Opened",
        rules_matched=0,
        polled_at="2026-05-08 10:00:00",
        mr_created_at="2025-01-01T00:00:00Z",
    )
    _insert_polled_mr(
        conn,
        mr_iid=2,
        mr_state="Merged",
        rules_matched=0,
        polled_at="2026-05-08 10:01:00",
        mr_created_at="2026-05-01T00:00:00Z",
        mr_merged_at="2026-05-01T13:55:27Z",
    )
    _insert_polled_mr(
        conn,
        mr_iid=3,
        mr_state="Closed",
        rules_matched=0,
        polled_at="2026-05-08 10:02:00",
        mr_created_at="2026-05-01T00:00:00Z",
    )
    conn.commit()
    conn.close()

    context = _rendered_polled_context(monkeypatch, show_all="0")

    assert [row["mr_iid"] for row in context["rows"]] == [2, 1]
    assert context["stats"]["total"] == 2
    assert context["filters"]["show_all"] == 0


def test_polled_shows_merged_mr_poll_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    from services import poller

    poller._set_merged_mr_poll_cursor("master", "2026-05-08T13:55:27Z")
    conn = db.get_db()
    _insert_polled_mr(
        conn,
        mr_iid=5636,
        mr_state="merged",
        rules_matched=0,
        polled_at="2026-05-08 13:56:00",
        mr_merged_at="2026-05-08T13:55:27Z",
        mr_url="https://gitlab.example.test/project/-/merge_requests/5636",
    )
    conn.commit()
    conn.close()

    context = _rendered_polled_context(monkeypatch)

    assert context["stats"]["merged_cursors"] == [
        {
            "branch": "master",
            "merged_at": "2026-05-08T13:55:27Z",
            "mr_iid": 5636,
            "mr_url": "https://gitlab.example.test/project/-/merge_requests/5636",
        }
    ]


def test_latest_release_branch_dates_returns_two_latest_numeric_releases(monkeypatch):
    async def fake_get_project_id():
        return 26

    async def fake_get_branches(project_id, search="", per_page=100, page=1):
        assert project_id == 26
        assert search == "release/"
        if page == 1:
            return [
                {
                    "name": "release/101",
                    "commit": {"created_at": "2026-04-01T10:00:00Z"},
                },
                {
                    "name": "release/103",
                    "commit": {"created_at": "2026-05-01T10:00:00Z"},
                },
                {
                    "name": "release/102",
                    "created_at": "2026-04-15T10:00:00Z",
                },
                {
                    "name": "release/not-number",
                    "commit": {"created_at": "2026-05-02T10:00:00Z"},
                },
            ]
        return []

    monkeypatch.setattr(pages, "get_project_id", fake_get_project_id)
    monkeypatch.setattr(pages, "get_branches", fake_get_branches)

    result = pages._latest_release_branch_dates()

    assert result == [
        {
            "version": 103,
            "name": "release/103",
            "created_at": "2026-05-01T10:00:00Z",
        },
        {
            "version": 102,
            "name": "release/102",
            "created_at": "2026-04-15T10:00:00Z",
        },
    ]


def test_polled_context_includes_release_branch_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()

    release_branches = [
        {"version": 103, "name": "release/103", "created_at": "2026-05-01T10:00:00Z"},
        {"version": 102, "name": "release/102", "created_at": "2026-04-15T10:00:00Z"},
    ]
    context = _rendered_polled_context(
        monkeypatch,
        _release_branches=release_branches,
    )

    assert context["stats"]["release_branches"] == release_branches
