"""应急改配草稿 API 端到端测试。"""

from app import models
from app.database import SessionLocal


# 3 堆栈 × 3 层共 9 槽，承重 100、力臂均为 2.0（总力矩恒为 60*2=120）
SLOT_PAYLOAD = {
    "slots": [
        {"code": f"{s}{t:02d}", "stack": s, "tier": t, "capacity": 100, "arm": 2.0}
        for s in "ABC"
        for t in (1, 2, 3)
    ],
    "adjacency": [],
    "incompatible_classes": [],
    "min_moment": -100000,
    "max_moment": 100000,
}

MANIFEST_PAYLOAD = {
    "containers": [
        {"no": "C001", "weight": 10, "hazmat": None, "discharge_order": 1},
        {"no": "C002", "weight": 20, "hazmat": None, "discharge_order": 1},
        {"no": "C003", "weight": 30, "hazmat": None, "discharge_order": 2},
    ]
}

NEW_MANIFEST = {
    "containers": [
        {"no": "C001", "weight": 15, "hazmat": None, "discharge_order": 1},
    ]
}


def _seed_and_confirm(client):
    sr = client.post("/api/slot-revisions", json=SLOT_PAYLOAD).json()["slots_revision"]
    mr = client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD) \
        .json()["manifest_revision"]
    plan = client.post("/api/plans/compute", json={}).json()
    pairs = {a["container_no"]: a["slot_code"] for a in plan["assignments"]}
    assert pairs == {"C001": "A01", "C002": "B01", "C003": "C01"}
    client.post(f"/api/plans/{plan['plan_id']}/confirm", json={
        "slots_revision": sr, "manifest_revision": mr
    })
    return plan


def _plan_count(client):
    return len(client.get("/api/plans").json())


def test_reblock_happy_path_min_moves_then_lex(client):
    p1 = _seed_and_confirm(client)
    r = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                    json={"unavailable_slots": ["C01"]})
    assert r.status_code == 201, r.text
    d = r.json()
    # 草稿修订号与修订链
    assert d["revision_no"] == 2
    assert d["status"] == "draft"
    assert d["base_plan_id"] == p1["plan_id"]
    assert d["parent_id"] == p1["plan_id"]
    assert d["root_id"] == p1["root_id"]
    assert d["slots_revision"] == 1 and d["manifest_revision"] == 1
    # 保持原方案全部箱；不可用槽位未占用；三阶段优选结果
    pairs = {a["container_no"]: a["slot_code"] for a in d["assignments"]}
    assert pairs == {"C001": "A01", "C002": "B02", "C003": "B01"}
    assert "C01" not in pairs.values()
    assert d["max_occupied_tier"] == 2
    assert d["total_moment"] == 120
    # 换槽明细
    assert d["moved_count"] == 2
    assert d["moved_containers"] == [
        {"container_no": "C002", "from_slot": "B01", "to_slot": "B02"},
        {"container_no": "C003", "from_slot": "C01", "to_slot": "B01"},
    ]
    assert d["unavailable_slots"] == ["C01"]
    # 原确认方案在新草稿确认前仍有效
    g = client.get(f"/api/plans/{p1['plan_id']}").json()
    assert g["status"] == "confirmed"


def test_reblock_banned_unused_slot_keeps_all_containers(client):
    p1 = _seed_and_confirm(client)
    r = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                    json={"unavailable_slots": ["A03"]})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["moved_count"] == 0
    assert d["moved_containers"] == []
    pairs = {a["container_no"]: a["slot_code"] for a in d["assignments"]}
    assert pairs == {"C001": "A01", "C002": "B01", "C003": "C01"}
    assert d["max_occupied_tier"] == 1


def test_original_plan_effective_until_draft_confirmed(client):
    p1 = _seed_and_confirm(client)
    draft = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                        json={"unavailable_slots": ["C01"]}).json()

    # 草稿确认前：原方案仍为已确认，可继续生成装船时间表
    entries = [
        {"no": n, "duration": 5, "release_minute": 0, "deadline_minute": 100}
        for n in ("C001", "C002", "C003")
    ]
    r = client.post(f"/api/plans/{p1['plan_id']}/schedules",
                    json={"crane_count": 1, "entries": entries})
    assert r.status_code == 201

    # 草稿沿用现有确认规则确认后，原方案被取代、槽位占用转移
    ok = client.post(f"/api/plans/{draft['plan_id']}/confirm",
                     json={"slots_revision": 1, "manifest_revision": 1})
    assert ok.status_code == 200
    assert ok.json()["status"] == "confirmed"
    old = client.get(f"/api/plans/{p1['plan_id']}").json()
    assert old["status"] == "superseded"
    # 原方案排程转为历史；草稿成为新的已确认方案
    sched = client.get(f"/api/plans/{p1['plan_id']}/schedules").json()
    assert sched["status"] == "historical"
    db = SessionLocal()
    try:
        occupied = {row.slot_code: row.plan_id
                    for row in db.query(models.ConfirmedSlot).all()}
        assert occupied == {
            "A01": draft["plan_id"],
            "B01": draft["plan_id"],
            "B02": draft["plan_id"],
        }
    finally:
        db.close()


def test_reblock_draft_can_reblock_again_after_confirm(client):
    p1 = _seed_and_confirm(client)
    d2 = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                     json={"unavailable_slots": ["C01"]}).json()
    client.post(f"/api/plans/{d2['plan_id']}/confirm",
                json={"slots_revision": 1, "manifest_revision": 1})
    # 新确认方案再次遇到槽位封锁：继续生成下一修订草稿
    r = client.post(f"/api/plans/{d2['plan_id']}/reblock",
                    json={"unavailable_slots": ["B01"]})
    assert r.status_code == 201, r.text
    d3 = r.json()
    assert d3["revision_no"] == 3
    assert d3["parent_id"] == d2["plan_id"]
    assert d3["root_id"] == p1["root_id"]
    pairs = {a["container_no"]: a["slot_code"] for a in d3["assignments"]}
    assert "B01" not in pairs.values()


def test_reblock_empty_list_rejected_422(client):
    p1 = _seed_and_confirm(client)
    r = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                    json={"unavailable_slots": []})
    assert r.status_code == 422
    assert "不能为空" in r.text
    assert _plan_count(client) == 1


def test_reblock_duplicate_slots_rejected_422(client):
    p1 = _seed_and_confirm(client)
    r = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                    json={"unavailable_slots": ["A01", "B01", "A01"]})
    assert r.status_code == 422
    assert "重复" in r.text
    assert _plan_count(client) == 1


def test_reblock_unknown_slot_rejected_422(client):
    p1 = _seed_and_confirm(client)
    r = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                    json={"unavailable_slots": ["Z99"]})
    assert r.status_code == 422
    assert "不属于" in r.json()["detail"]
    assert _plan_count(client) == 1


def test_reblock_plan_not_found_404(client):
    r = client.post("/api/plans/nope/reblock",
                    json={"unavailable_slots": ["A01"]})
    assert r.status_code == 404


def test_reblock_unconfirmed_plan_rejected_409(client):
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)
    client.post("/api/manifest-revisions", json=MANIFEST_PAYLOAD)
    plan = client.post("/api/plans/compute", json={}).json()
    r = client.post(f"/api/plans/{plan['plan_id']}/reblock",
                    json={"unavailable_slots": ["C01"]})
    assert r.status_code == 409
    assert "已确认" in r.json()["detail"]
    assert _plan_count(client) == 1


def test_reblock_superseded_plan_rejected_409(client):
    p1 = _seed_and_confirm(client)
    # 新箱清单 -> 修订并确认 p2，p1 变为 superseded
    client.post("/api/manifest-revisions", json=NEW_MANIFEST)
    p2 = client.post(f"/api/plans/{p1['plan_id']}/amend", json={}).json()
    client.post(f"/api/plans/{p2['plan_id']}/confirm",
                json={"slots_revision": 1, "manifest_revision": 2})
    r = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                    json={"unavailable_slots": ["C01"]})
    assert r.status_code == 409


def test_reblock_stale_slot_revision_rejected_409(client):
    p1 = _seed_and_confirm(client)
    client.post("/api/slot-revisions", json=SLOT_PAYLOAD)  # 资料产生新修订
    r = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                    json={"unavailable_slots": ["C01"]})
    assert r.status_code == 409
    assert "最新修订" in r.json()["detail"]
    assert _plan_count(client) == 1


def test_reblock_stale_manifest_rejected_409(client):
    p1 = _seed_and_confirm(client)
    client.post("/api/manifest-revisions", json=NEW_MANIFEST)
    r = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                    json={"unavailable_slots": ["C01"]})
    assert r.status_code == 409
    assert "最新修订" in r.json()["detail"]
    assert _plan_count(client) == 1


def test_reblock_infeasible_422_not_saved(client):
    p1 = _seed_and_confirm(client)
    # 封锁 8 槽仅剩 1 槽，少于 3 箱 -> 无解
    r = client.post(f"/api/plans/{p1['plan_id']}/reblock", json={
        "unavailable_slots": ["A01", "A02", "A03", "B01", "B02", "B03",
                              "C02", "C03"]
    })
    assert r.status_code == 422
    assert "无解" in r.json()["detail"]
    assert _plan_count(client) == 1


def test_save_time_recheck_rejects_if_base_superseded(client, monkeypatch):
    """求解完成后、落库前基础方案被并发取代：锁内复核须拒绝且不保存。"""
    from app import services

    p1 = _seed_and_confirm(client)
    real_solve = services.solve_reblock

    def raced_solve(*args, **kwargs):
        result = real_solve(*args, **kwargs)
        # 求解期间：新箱清单 -> 修订 p2 并确认，p1 被取代
        client.post("/api/manifest-revisions", json=NEW_MANIFEST)
        p2 = client.post(f"/api/plans/{p1['plan_id']}/amend", json={}).json()
        client.post(f"/api/plans/{p2['plan_id']}/confirm",
                    json={"slots_revision": 1, "manifest_revision": 2})
        return result

    monkeypatch.setattr(services, "solve_reblock", raced_solve)
    r = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                    json={"unavailable_slots": ["C01"]})
    assert r.status_code == 409
    assert "复核" in r.json()["detail"]
    assert _plan_count(client) == 2  # 仅 p1 与 p2，改配草稿未保存


def test_save_time_recheck_rejects_if_new_revision_in_root(client, monkeypatch):
    """求解完成后、落库前同根出现新修订：锁内复核须拒绝且不保存。"""
    from app import services

    p1 = _seed_and_confirm(client)
    real_solve = services.solve_reblock

    def raced_solve(*args, **kwargs):
        result = real_solve(*args, **kwargs)
        client.post(f"/api/plans/{p1['plan_id']}/amend", json={})  # 同根新草稿
        return result

    monkeypatch.setattr(services, "solve_reblock", raced_solve)
    r = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                    json={"unavailable_slots": ["C01"]})
    assert r.status_code == 409
    assert "复核" in r.json()["detail"]
    assert _plan_count(client) == 2  # 改配草稿未保存


def test_save_time_recheck_rejects_if_data_changed(client, monkeypatch):
    """求解完成后、落库前资料产生新修订：锁内复核须拒绝且不保存。"""
    from app import services

    p1 = _seed_and_confirm(client)
    real_solve = services.solve_reblock

    def raced_solve(*args, **kwargs):
        result = real_solve(*args, **kwargs)
        client.post("/api/slot-revisions", json=SLOT_PAYLOAD)
        return result

    monkeypatch.setattr(services, "solve_reblock", raced_solve)
    r = client.post(f"/api/plans/{p1['plan_id']}/reblock",
                    json={"unavailable_slots": ["C01"]})
    assert r.status_code == 409
    assert "复核" in r.json()["detail"]
    assert _plan_count(client) == 1
