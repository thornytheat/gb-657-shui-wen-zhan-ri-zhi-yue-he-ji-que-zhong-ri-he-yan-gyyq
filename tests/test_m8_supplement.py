"""M8 跨站补估：依据快照、实测/含估分账、补测取代、失效、撤销与幂等。"""
from __future__ import annotations

import concurrent.futures
from decimal import Decimal

from app import errors
from app.modules.m8_supplement import estimator, service
from app.store import Store


def _neighbor(client, target="0012", neighbor="0031", coefficient="1.2"):
    response = client.post("/api/supplement/neighbors",
                           json={"station_id": target, "neighbor_id": neighbor,
                                 "coefficient": coefficient})
    assert response.status_code == 200, response.text
    return response.json()


def _obs(client, station, date, mm, **extra):
    response = client.post("/api/supplement/observations",
                           json={"station_id": station, "date": date,
                                 "rainfall_mm": mm, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def _apply(client, target="0012", month="2024-02", key="k-1", **extra):
    return client.post("/api/supplement/estimates",
                       json={"station_id": target, "month": month,
                             "idempotency_key": key, **extra})


def _seed_four_days(client, target="0012", neighbor="0031"):
    """邻站 4 天实测，目标站只有 2 天 → 缺口 2 天可估。"""
    _neighbor(client, target=target, neighbor=neighbor)
    for day in ("2024-02-01", "2024-02-02", "2024-02-03", "2024-02-04"):
        _obs(client, neighbor, day, "10.0")
    _obs(client, target, "2024-02-01", "5.0")
    _obs(client, target, "2024-02-04", "5.0")


def _readback(client, target="0012", month="2024-02", **params):
    response = client.get("/api/supplement/readback",
                          params={"station_id": target, "month": month, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_supplement_binds_neighbor_measured_and_coefficient(client):
    _seed_four_days(client)
    body = _apply(client).json()
    assert body["estimate_status"] == "active"
    assert body["measured_mm"] == "10.000"
    assert body["estimated_mm"] == "24.000"       # 2 天 × 10.0 × 1.2
    assert body["total_mm"] == "34.000"
    assert len(body["basis_digest"]) == 64
    for entry in body["entries"]:
        assert entry["neighbor_basis"][0]["neighbor_id"] == "0031"
        assert entry["neighbor_basis"][0]["coefficient"] == "1.200000"
        assert entry["neighbor_basis"][0]["rainfall_mm"] == "10.000"


def test_measured_gap_and_with_estimate_are_separate(client):
    _seed_four_days(client)
    _apply(client)
    body = _readback(client)
    numbers = body["month_numbers"]
    estimate_dates = {entry["date"] for entry in body["current"]["entries"]}
    assert numbers["measured"]["days"] == 2
    assert numbers["measured"]["rainfall_mm"] == "10.000"
    assert numbers["measured"]["gap_days"] == 27
    assert numbers["estimated"]["days"] == 2
    assert numbers["estimated"]["rainfall_mm"] == "24.000"
    assert numbers["with_estimate"]["days"] == 4
    assert numbers["with_estimate"]["rainfall_mm"] == "34.000"
    assert numbers["estimate_is_measured_basis"] is False
    # 缺日清单仍按实测口径给出：02-02 / 02-03 有估值但没有实测
    assert {"2024-02-02", "2024-02-03"} <= set(numbers["measured"]["gap_dates"])
    assert estimate_dates == {"2024-02-02", "2024-02-03"}


def test_estimate_never_becomes_another_station_measured_basis(client, store):
    _seed_four_days(client, target="0012", neighbor="0031")
    _apply(client, target="0012")
    # 0012 的 02-02/02-03 只有估值，台账里不应出现 0012 的实测行
    assert store.get_observation("0012", "2024-02-02") is None
    # 把 0012 当成 0044 的邻站：只能看到 2 天实测，不能借用估值
    _neighbor(client, target="0044", neighbor="0012", coefficient="1.0")
    body = _apply(client, target="0044", key="k-44").json()
    assert body["estimated_days"] == 2          # 只有邻站真有的 02-01/02-04
    assert body["estimated_mm"] == "10.000"
    assert "2024-02-02" in body["unfilled_dates"]
    assert "2024-02-03" in body["unfilled_dates"]


def test_zero_rain_is_measured_not_gap(client):
    _neighbor(client, target="0012", neighbor="0031", coefficient="2")
    _obs(client, "0012", "2024-02-01", "0.0")
    _obs(client, "0031", "2024-02-01", "0.0")
    _obs(client, "0031", "2024-02-02", "0.0")
    body = _apply(client, month="2024-02").json()
    numbers = _readback(client)["month_numbers"]
    assert "2024-02-01" not in numbers["measured"]["gap_dates"]
    assert numbers["measured"]["rainfall_mm"] == "0.000"
    # 邻站零雨也是有效实测：估值为 0.000，而不是跳过该日
    entry = next(item for item in body["entries"] if item["date"] == "2024-02-02")
    assert entry["estimated_mm"] == "0.000"
    assert entry["status"] == "active"


def test_no_neighbor_is_rejected_without_writing(client):
    _obs(client, "0012", "2024-02-01", "1.0")
    response = _apply(client)                       # 没有任何邻站
    assert response.status_code == 422
    assert response.json()["error"] == "E_SUPPLEMENT_NO_NEIGHBOR"
    assert _readback(client)["estimates"] == []


def test_cycle_between_neighbors_is_rejected(client):
    _neighbor(client, target="0012", neighbor="0031")
    _neighbor(client, target="0031", neighbor="0012")
    response = _apply(client)
    assert response.status_code == 422
    assert response.json()["error"] == "E_SUPPLEMENT_CYCLE"
    assert response.json()["detail"]["cycle"] == ["0012", "0031", "0012"]


def test_self_reference_is_rejected(client):
    response = client.post("/api/supplement/neighbors",
                           json={"station_id": "0012", "neighbor_id": "0012",
                                 "coefficient": "1"})
    assert response.status_code == 422
    assert response.json()["error"] == "E_SUPPLEMENT_CYCLE"


def test_retry_with_same_key_does_not_double_estimate(client, store):
    _seed_four_days(client)
    first = _apply(client, key="once").json()
    second = _apply(client, key="once").json()
    assert second["replayed"] is True
    assert second["estimate_id"] == first["estimate_id"]
    rows = store.list_supplements(target_station="0012", month="2024-02")
    assert len(rows) == 1
    assert sum(Decimal(row["estimated_mm"]) for row in rows) == Decimal("24.000")


def test_stale_basis_digest_is_rejected(client):
    _seed_four_days(client)
    preview = client.post("/api/supplement/preview",
                          json={"station_id": "0012", "month": "2024-02"}).json()
    payload = preview["basis_digest"]
    _obs(client, "0031", "2024-02-02", "40.0")      # 预览后依据被补测改写
    response = _apply(client, expected_basis_digest=payload)
    assert response.status_code == 409
    assert response.json()["error"] == "E_SUPPLEMENT_BASIS_STALE"
    assert _readback(client)["estimates"] == []


def test_expired_revision_is_rejected_for_observation(client):
    _obs(client, "0031", "2024-02-01", "10.0")
    response = client.post("/api/supplement/observations",
                           json={"station_id": "0031", "date": "2024-02-01",
                                 "rainfall_mm": "12.0", "expected_revision": 0})
    assert response.status_code == 409
    assert response.json()["error"] == "E_SUPPLEMENT_REVISION_STALE"


def test_concurrent_observation_submissions_keep_one_winner(settings):
    store = Store(settings.db_path)
    store.write_observation(station_id="0031", obs_date="2024-02-01",
                            rainfall_mm="1.0", source="seed",
                            stamp=service.now_iso())

    def attempt(value):
        local = Store(settings.db_path)
        try:
            return service.put_observation(local, {
                "station_id": "0031", "date": "2024-02-01",
                "rainfall_mm": value, "expected_revision": 1})["revision"]
        except errors.Conflict as exc:
            return exc.code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["2.0", "3.0"]))
    assert sorted(map(str, outcomes)) == ["2", "E_SUPPLEMENT_REVISION_STALE"]
    assert store.get_observation("0031", "2024-02-01")["revision"] == 2


def test_basis_change_inside_write_transaction_is_rejected(client, store,
                                                           monkeypatch):
    """并发窗口：摘要校验通过后依据才变，落库事务里仍要拒绝。"""
    _seed_four_days(client)
    stale_basis = estimator.build_basis(store, "0012", "2024-02")
    _obs(client, "0031", "2024-02-03", "99.0")      # 依据已变
    monkeypatch.setattr(service.est, "build_basis",
                        lambda store_, target, month: stale_basis)
    response = _apply(client, key="race")
    assert response.status_code == 409
    assert response.json()["error"] == "E_SUPPLEMENT_BASIS_STALE"
    assert store.list_supplements(target_station="0012") == []


def test_failed_apply_still_reads_old_result(client):
    _seed_four_days(client)
    old = _apply(client, key="old").json()
    _obs(client, "0031", "2024-02-03", "77.0")
    failed = _apply(client, key="new", expected_basis_digest=old["basis_digest"])
    assert failed.status_code == 409
    body = _readback(client)
    # 新补估失败不写任何行；旧结果仍可读，且原依据/原值一字不改
    assert [row["estimate_id"] for row in body["estimates"]] == [old["estimate_id"]]
    row = body["estimates"][0]
    assert row["estimated_mm"] == "24.000"
    assert row["basis_digest"] == old["basis_digest"]
    # 邻站实测变化使旧估值退出当前口径，但历史可回读
    assert row["estimate_status"] == "stale"


def test_neighbor_change_marks_estimate_stale_but_keeps_snapshot(client):
    _seed_four_days(client)
    created = _apply(client).json()
    response = _obs(client, "0031", "2024-02-02", "88.0")
    assert created["estimate_id"] in response["invalidated_estimates"]
    body = _readback(client)
    assert body["current"] is None
    row = body["estimates"][0]
    assert row["estimate_status"] == "stale"
    assert row["status_reason"] == "BASIS_CHANGED"
    assert row["basis_digest"] == created["basis_digest"]
    assert row["estimated_mm"] == created["estimated_mm"]
    assert [entry["estimated_mm"] for entry in row["entries"]] == \
        [entry["estimated_mm"] for entry in created["entries"]]


def test_new_measurement_supersedes_estimate_and_replaces_value(client):
    _seed_four_days(client)
    created = _apply(client).json()
    response = _obs(client, "0012", "2024-02-02", "7.5")
    assert created["estimate_id"] in response["superseded_estimates"]
    body = _readback(client)
    assert body["current"] is None
    row = body["estimates"][0]
    assert row["estimate_status"] == "superseded"
    assert row["status_reason"] == "MEASURED_FILLED"
    # 快照与估算原值不变，旧的应急结果仍按当时依据复现
    assert row["estimated_mm"] == "24.000"
    assert row["basis_digest"] == created["basis_digest"]
    superseded_entry = next(entry for entry in row["entries"]
                            if entry["date"] == "2024-02-02")
    assert superseded_entry["status"] == "superseded"
    assert superseded_entry["superseded_by_revision"] == 1
    numbers = body["month_numbers"]
    assert numbers["measured"]["days"] == 3
    assert numbers["measured"]["rainfall_mm"] == "17.500"
    assert numbers["estimated"]["days"] == 0

    # 仍缺 02-03，新补估只估这一天，不会重复估已补测的 02-02
    fresh = _apply(client, key="k-2").json()
    assert [entry["date"] for entry in fresh["entries"]] == ["2024-02-03"]
    assert fresh["estimated_mm"] == "12.000"


def test_revocation_keeps_basis_and_is_idempotent(client):
    _seed_four_days(client)
    created = _apply(client).json()
    body = client.post("/api/supplement/revocations",
                       json={"idempotency_key": "rv-1",
                             "estimate_ids": [created["estimate_id"]],
                             "reason": "邻站依据更正"}).json()
    assert body["job"]["status"] == "done"
    assert body["job"]["items"][0]["code"] == "K_REVOKED"
    again = client.post("/api/supplement/revocations",
                        json={"idempotency_key": "rv-1",
                              "estimate_ids": [created["estimate_id"]]}).json()
    assert again["replayed"] is True
    assert again["job"]["job_id"] == body["job"]["job_id"]
    row = _readback(client)["estimates"][0]
    assert row["estimate_status"] == "revoked"
    assert row["revoked_at"] is not None
    assert row["revoke_job_id"] == body["job"]["job_id"]
    assert row["estimated_mm"] == created["estimated_mm"]
    assert row["basis_digest"] == created["basis_digest"]
    assert row["entries"][0]["estimated_mm"] == created["entries"][0]["estimated_mm"]


def test_interrupted_revocation_resumes_without_duplicate_effects(client):
    _seed_four_days(client)
    first = _apply(client, key="a").json()
    second = _apply(client, key="b").json()
    job = client.post("/api/supplement/revocations",
                      json={"idempotency_key": "rv-partial",
                            "estimate_ids": [first["estimate_id"],
                                             second["estimate_id"]],
                            "interrupt_after": 1}).json()["job"]
    assert job["status"] == "pending"
    assert job["processed"] == 1
    resumed = client.post("/api/supplement/revocations/resume",
                          json={"job_id": job["job_id"]}).json()["jobs"][0]
    assert resumed["status"] == "done"
    assert resumed["processed"] == 2
    again = client.post("/api/supplement/revocations/resume",
                        json={"job_id": job["job_id"]}).json()["jobs"][0]
    assert again["processed"] == 2
    statuses = {row["estimate_status"] for row in _readback(client)["estimates"]}
    assert statuses == {"revoked"}


def test_revocation_survives_process_restart(client, settings, app):
    _seed_four_days(client)
    created = _apply(client).json()
    job = client.post("/api/supplement/revocations",
                      json={"idempotency_key": "rv-crash",
                            "estimate_ids": [created["estimate_id"]],
                            "interrupt_after": 0}).json()["job"]
    assert job["status"] == "pending"
    from app.main import create_app
    fresh = create_app(settings)
    from fastapi.testclient import TestClient
    restarted = TestClient(fresh)
    body = restarted.get("/api/supplement/readback",
                         params={"station_id": "0012", "month": "2024-02"}).json()
    assert body["pending_revocations"] == []
    assert body["estimates"][0]["estimate_status"] == "revoked"


def test_revoked_estimate_is_still_reproducible_from_snapshot(client):
    _seed_four_days(client)
    created = _apply(client).json()
    client.post("/api/supplement/revocations",
                json={"idempotency_key": "rv-repro",
                      "estimate_ids": [created["estimate_id"]]})
    # 依据之后又被改：撤销件仍按落库快照复现，不受现状影响
    _obs(client, "0031", "2024-02-02", "100.0")
    replay = client.get(f"/api/supplement/estimates/{created['estimate_id']}").json()
    assert replay["reproducible"] is True
    assert replay["recomputed_basis_digest"] == created["basis_digest"]
    assert replay["result"]["estimated_mm"] == "24.000"
    assert replay["result"]["total_mm"] == "34.000"
    assert replay["estimate_status"] == "revoked"
    missing = client.get("/api/supplement/estimates/se-0000000000000000")
    assert missing.status_code == 404
    assert missing.json()["error"] == "E_ESTIMATE_NOT_FOUND"


def test_month_validation_and_missing_fields(client):
    bad_month = _apply(client, month="2024-2")
    assert bad_month.status_code == 400
    assert bad_month.json()["error"] == "E_SUPPLEMENT_MONTH_INVALID"
    missing = client.post("/api/supplement/estimates",
                          json={"station_id": "0012", "month": "2024-02"})
    assert missing.status_code == 400
    assert missing.json()["error"] == "E_SUPPLEMENT_FIELD_REQUIRED"
    bad_value = client.post("/api/supplement/observations",
                            json={"station_id": "0012", "date": "2024-02-01",
                                  "rainfall_mm": "-1"})
    assert bad_value.status_code == 400
    assert bad_value.json()["error"] == "E_SUPPLEMENT_VALUE_INVALID"


def test_supplement_page_renders(client):
    _seed_four_days(client)
    _apply(client)
    page = client.get("/supplement", params={"station_id": "0012",
                                             "month": "2024-02"})
    assert page.status_code == 200
    assert "hydrocalendar" in page.text
    assert "跨站补估" in page.text
    empty = client.get("/supplement")
    assert empty.status_code == 200
