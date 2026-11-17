"""跨站补估：依据快照、同日邻站折算法、月度实测/含估分账。

硬约束（缺一条结果就错）：
1. 依据只读邻站「实测」台账，任何估值都不是别的站的实测依据；
2. 依据快照（日期 + 值 + revision + 系数）参与摘要，落库后不可改写；
3. 目标站已有实测的日期不是缺口；缺口里有邻站实测的日期才产出估算；
4. 零雨是有效实测（0.0），既不跳过也不当成缺测；
5. 邻站依赖成环直接拒绝，不做隐式截断。
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import json
import re
from decimal import Decimal

from ... import errors

MM_QUANT = Decimal("0.001")
COEF_QUANT = Decimal("0.000001")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


# ---------------------------------------------------------------- 基础校验
def parse_month(month: str) -> tuple[dt.date, dt.date]:
    if not MONTH_RE.match(str(month or "")):
        raise errors.BadRequest(f"月份格式应为 YYYY-MM：{month!r}",
                                code="E_SUPPLEMENT_MONTH_INVALID")
    year, mon = int(month[:4]), int(month[5:7])
    days = calendar.monthrange(year, mon)[1]
    return dt.date(year, mon, 1), dt.date(year, mon, days)


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise errors.BadRequest(f"日期格式应为 YYYY-MM-DD：{value!r}",
                                code="E_SUPPLEMENT_DATE_INVALID") from exc


def parse_mm(value, *, field: str = "rainfall_mm") -> Decimal:
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise errors.BadRequest(f"{field} 无法解析为十进制：{value!r}",
                                code="E_SUPPLEMENT_VALUE_INVALID") from exc
    if number < 0:
        raise errors.BadRequest(f"{field} 不能为负：{number}",
                                code="E_SUPPLEMENT_VALUE_INVALID")
    return number.quantize(MM_QUANT)


def parse_coefficient(value) -> Decimal:
    try:
        number = Decimal(str(value))
    except Exception as exc:
        raise errors.BadRequest(f"系数无法解析为十进制：{value!r}",
                                code="E_SUPPLEMENT_COEFFICIENT_INVALID") from exc
    if number <= 0:
        raise errors.BadRequest(f"系数必须大于 0：{number}",
                                code="E_SUPPLEMENT_COEFFICIENT_INVALID")
    return number.quantize(COEF_QUANT)


def digest(payload) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def estimate_id_for(key: str) -> str:
    return "se-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def job_id_for(key: str) -> str:
    return "rv-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- 邻站图
def assert_acyclic(store, target: str) -> None:
    """邻站依赖不得回到目标站本身（含自引用）。"""
    seen: set[str] = set()

    def walk(station: str, path: tuple[str, ...]) -> None:
        for link in store.load_neighbors(station):
            neighbor = link["neighbor_id"]
            if neighbor == target:
                raise errors.UnprocessableInput(
                    "邻站依赖成环：" + " → ".join(path + (neighbor,)),
                    code="E_SUPPLEMENT_CYCLE",
                    detail={"cycle": list(path + (neighbor,))})
            if neighbor in seen or neighbor == station:
                continue
            seen.add(neighbor)
            walk(neighbor, path + (neighbor,))

    walk(target, (target,))


# ---------------------------------------------------------------- 依据快照
def build_basis(store, target: str, month: str) -> dict:
    start, end = parse_month(month)
    if not target:
        raise errors.BadRequest("缺少目标站号", code="E_SUPPLEMENT_STATION_REQUIRED")
    neighbors = store.load_neighbors(target)
    if not neighbors:
        raise errors.UnprocessableInput(
            f"目标站 {target} 没有配置邻站，无法补估",
            code="E_SUPPLEMENT_NO_NEIGHBOR",
            detail={"target_station": target, "month": month})
    assert_acyclic(store, target)

    target_rows = store.observations_between(target, start.isoformat(), end.isoformat())
    observations = [{
        "station_id": target,
        "role": "target",
        "rows": [{"date": row["obs_date"], "rainfall_mm": row["rainfall_mm"],
                  "revision": row["revision"]} for row in target_rows],
    }]
    links = []
    for link in neighbors:
        neighbor = link["neighbor_id"]
        rows = store.observations_between(neighbor, start.isoformat(), end.isoformat())
        links.append({"neighbor_id": neighbor, "coefficient": link["coefficient"]})
        observations.append({
            "station_id": neighbor,
            "role": "neighbor",
            "coefficient": link["coefficient"],
            "rows": [{"date": row["obs_date"], "rainfall_mm": row["rainfall_mm"],
                      "revision": row["revision"]} for row in rows],
        })
    measured_dates = {row["date"] for row in observations[0]["rows"]}
    gap_dates = [(start + dt.timedelta(days=offset)).isoformat()
                 for offset in range((end - start).days + 1)
                 if (start + dt.timedelta(days=offset)).isoformat() not in measured_dates]
    return {
        "target_station": target,
        "month": month,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "calendar_days": (end - start).days + 1,
        "neighbors": links,
        "observations": observations,
        "gap_dates": gap_dates,
    }


def basis_digest(basis: dict) -> str:
    return digest(basis)


# ---------------------------------------------------------------- 估算
def estimate_gaps(basis: dict) -> dict:
    """缺口逐日按同日邻站实测 × 系数求平均；无邻站实测的日期保持缺口。"""
    neighbor_rows: dict[str, dict[str, Decimal]] = {}
    coefficients: dict[str, Decimal] = {}
    for part in basis["observations"]:
        if part["role"] != "neighbor":
            continue
        coefficients[part["station_id"]] = Decimal(str(part["coefficient"]))
        neighbor_rows[part["station_id"]] = {
            row["date"]: Decimal(str(row["rainfall_mm"])) for row in part["rows"]}

    entries, unfilled = [], []
    for date in basis["gap_dates"]:
        used, predictions = [], []
        for neighbor in sorted(neighbor_rows):
            if date not in neighbor_rows[neighbor]:
                continue
            value = neighbor_rows[neighbor][date]
            coefficient = coefficients[neighbor]
            prediction = (value * coefficient).quantize(MM_QUANT)
            used.append({"neighbor_id": neighbor, "rainfall_mm": str(value),
                         "coefficient": str(coefficient),
                         "prediction_mm": str(prediction)})
            predictions.append(prediction)
        if not predictions:
            unfilled.append(date)
            continue
        estimated = (sum(predictions, Decimal("0")) / Decimal(len(predictions))
                     ).quantize(MM_QUANT)
        entries.append({"date": date, "estimated_mm": estimated,
                        "neighbor_basis": used})
    return {
        "entries": entries,
        "unfilled_dates": unfilled,
        "estimated_days": len(entries),
        "estimated_mm": sum((item["estimated_mm"] for item in entries),
                            Decimal("0")).quantize(MM_QUANT),
    }


def measured_total(basis: dict) -> tuple[Decimal, int]:
    rows = basis["observations"][0]["rows"]
    total = sum((Decimal(str(row["rainfall_mm"])) for row in rows), Decimal("0"))
    return total.quantize(MM_QUANT), len(rows)


def compute(store, target: str, month: str) -> dict:
    return finalize(build_basis(store, target, month))


def finalize(basis: dict) -> dict:
    estimate = estimate_gaps(basis)
    measured_mm, measured_days = measured_total(basis)
    return {
        "target_station": basis["target_station"],
        "month": basis["month"],
        "basis": basis,
        "basis_digest": basis_digest(basis),
        "entries": estimate["entries"],
        "unfilled_dates": estimate["unfilled_dates"],
        "measured_mm": measured_mm,
        "measured_days": measured_days,
        "estimated_mm": estimate["estimated_mm"],
        "estimated_days": estimate["estimated_days"],
        "gap_days": len(basis["gap_dates"]),
        "total_mm": (measured_mm + estimate["estimated_mm"]).quantize(MM_QUANT),
    }
