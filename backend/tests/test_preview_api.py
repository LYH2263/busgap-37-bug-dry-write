import os

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.models import BunchReport
from app.services.seed import seed_if_empty


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSession()
    seed_if_empty(db)  # 内置 B12 线路

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield {"client": TestClient(app), "session": TestingSession}
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


def report_count(session_factory):
    db = session_factory()
    try:
        return db.scalar(select(func.count()).select_from(BunchReport))
    finally:
        db.close()


def test_preview_does_not_create_report(ctx):
    """对 B12 跑试算，历史报告条数前后保持不变。"""
    before = report_count(ctx["session"])

    resp = ctx["client"].post("/api/reports/preview?line_id=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] is False
    assert "id" not in body
    assert len(body["events"]) > 0
    assert body["line_code"] == "B12"

    after = report_count(ctx["session"])
    assert after == before
    # 报告列表条数同样不变
    listed = ctx["client"].get("/api/reports").json()
    assert len(listed) == before


def test_preview_twice_keeps_count_still(ctx):
    """连点两次试算，报告行数必须原地不动。"""
    before = report_count(ctx["session"])
    for _ in range(2):
        resp = ctx["client"].post("/api/reports/preview?line_id=1")
        assert resp.status_code == 200
        assert resp.json()["saved"] is False
    assert report_count(ctx["session"]) == before
    assert len(ctx["client"].get("/api/reports").json()) == before


def test_preview_specific_stop(ctx):
    """试算支持指定站点，只返回该站事件，仍不写报告。"""
    before = report_count(ctx["session"])
    resp = ctx["client"].post("/api/reports/preview?line_id=1&stop_name=市民中心")
    assert resp.status_code == 200
    body = resp.json()
    assert body["stop_name"] == "市民中心"
    assert body["events"]
    assert all(e["stop_name"] == "市民中心" for e in body["events"])
    assert report_count(ctx["session"]) == before


def test_run_still_creates_one_report(ctx):
    """真正检测写入且仅写入一条报告，事件与试算一致。"""
    before = report_count(ctx["session"])

    preview = ctx["client"].post("/api/reports/preview?line_id=1").json()
    run = ctx["client"].post("/api/reports/run?line_id=1")
    assert run.status_code == 200
    r = run.json()
    assert r["saved"] is True
    assert r["id"] is not None
    assert r["events"] == preview["events"]

    assert report_count(ctx["session"]) == before + 1
    listed = ctx["client"].get("/api/reports").json()
    assert len(listed) == before + 1
    assert listed[0]["id"] == r["id"]
    assert listed[0]["events"] == r["events"]


def test_run_failure_keeps_existing_reports(ctx, monkeypatch):
    """真检落库失败：如实报 500、不伪装成试算成功，旧报告行一条不丢，恢复后能正常补落。"""
    ok = ctx["client"].post("/api/reports/run?line_id=1")
    assert ok.status_code == 200
    before = report_count(ctx["session"])
    assert before >= 1
    surviving = ctx["client"].get("/api/reports").json()

    def broken_commit(self):
        raise RuntimeError("simulated disk failure")

    monkeypatch.setattr(Session, "commit", broken_commit)
    failed = ctx["client"].post("/api/reports/run?line_id=1")
    monkeypatch.undo()

    assert failed.status_code == 500
    # 失败响应不能伪装成试算/成功
    assert failed.json().get("saved") is not True
    # 旧行与轴点对应数据原封不动
    assert report_count(ctx["session"]) == before
    assert ctx["client"].get("/api/reports").json() == surviving

    # 故障恢复后真检不得空转：再跑一次应恰好新增一行
    again = ctx["client"].post("/api/reports/run?line_id=1")
    assert again.status_code == 200
    body = again.json()
    assert body["saved"] is True and body["id"] is not None
    assert report_count(ctx["session"]) == before + 1


def test_suggestions_empty_without_saved_report(ctx):
    """没有已落库报告时建议为空，且接口自身不产生报告。"""
    before = report_count(ctx["session"])
    resp = ctx["client"].get("/api/reports/suggestions?line_id=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] is False
    assert body["report_id"] is None
    assert body["suggestions"] == []
    assert report_count(ctx["session"]) == before


def test_suggestions_reflect_only_saved_report(ctx):
    """试算不得进建议；真检后建议与该报告同一套事件。"""
    ctx["client"].post("/api/reports/preview?line_id=1")
    empty = ctx["client"].get("/api/reports/suggestions?line_id=1").json()
    assert empty["suggestions"] == [] and empty["report_id"] is None

    run = ctx["client"].post("/api/reports/run?line_id=1").json()
    res = ctx["client"].get("/api/reports/suggestions?line_id=1").json()
    assert res["saved"] is True
    assert res["report_id"] == run["id"]
    expected = [e for e in run["events"] if e["status"] != "normal"]
    assert res["suggestions"] == expected

    # 再来一次试算不得污染已落库建议
    ctx["client"].post("/api/reports/preview?line_id=1&stop_name=市民中心")
    res2 = ctx["client"].get("/api/reports/suggestions?line_id=1").json()
    assert res2["report_id"] == run["id"]
    assert res2["suggestions"] == expected


def test_preview_and_run_do_not_touch_timeline(ctx):
    """时间轴只来自真实到站：试算不添脏点，真检也不单独加点。"""
    base = ctx["client"].get("/api/reports/timeline?line_id=1").json()
    assert base["marks"]

    ctx["client"].post("/api/reports/preview?line_id=1")
    after_preview = ctx["client"].get("/api/reports/timeline?line_id=1").json()
    assert after_preview == base

    ctx["client"].post("/api/reports/run?line_id=1")
    after_run = ctx["client"].get("/api/reports/timeline?line_id=1").json()
    assert after_run == base


def test_preview_unknown_line_404(ctx):
    resp = ctx["client"].post("/api/reports/preview?line_id=999")
    assert resp.status_code == 404
