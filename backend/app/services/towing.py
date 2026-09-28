"""航空器牵引业务规则：时段/机位把关、批量顺延、状态流转与统计口径都收在这里。

约束口径：
- 安排牵引时校验牵引车号在生效时段内是否与已排牵引（待牵引、牵引中）时段重叠；
- 起点机位与终点机位相同、起点机位处于机位资源「维护封闭」时，不允许安排；
- 批量顺延按生效时段圈定一批待办任务，冲突的单列、其余照常顺延，直接落库；
- 列表与统计共用同一份 store 数据，顺延后的起终点机位与时段范围口径保持一致。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.store import store

MODULE = "towing"
STAND_MODULE = "stand"
REQUIRED_FIELDS = ["牵引编号", "关联航班", "牵引车号", "起点机位", "终点机位", "生效开始", "生效结束"]
OPTIONAL_FIELDS = ["牵引人员", "完成时刻"]
STATUS_ORDER = ["待牵引", "牵引中", "已完成", "已取消"]
ACTION_RULES = {"安排牵引": "牵引中", "确认完成": "已完成", "取消任务": "已取消"}
NEGATIVE_ACTIONS = []
# 还会占用牵引车的状态：已完成、已取消的任务不再参与时段冲突判定。
ACTIVE_STATUSES = ["待牵引", "牵引中"]
# 可参与批量顺延的状态：已经完成或取消的任务不再挪动。
POSTPONE_STATUSES = ["待牵引", "牵引中"]
CLOSED_STAND_STATUS = "维护封闭"
TIME_FORMAT = "%Y-%m-%d %H:%M"


def parse_time(value: Any) -> datetime | None:
    """宽松解析生效时刻：兼容 'YYYY-MM-DD HH:MM'、'YYYY-MM-DDTHH:MM' 等写法。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("T", " ").replace("/", "-")
    if len(text) == 16:
        for fmt in (TIME_FORMAT, "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    for fmt in (TIME_FORMAT, "%Y-%m-%d %H", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def format_time(value: datetime) -> str:
    return value.strftime(TIME_FORMAT)


def windows_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """两个半开时段 [start, end) 是否重叠；端点相接不算冲突。"""
    return start_a < end_b and start_b < end_a


class TowingService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("牵引编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        problems = self._validate_schedule(
            tractor=str(values.get("牵引车号") or "").strip(),
            start_stand=str(values.get("起点机位") or "").strip(),
            end_stand=str(values.get("终点机位") or "").strip(),
            start_raw=values.get("生效开始"),
            end_raw=values.get("生效结束"),
        )
        if problems:
            return None, problems
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            entry[field] = values.get(field, "")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"牵引任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于航空器牵引可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "安排牵引":
            problems = self._validate_schedule(
                tractor=str(entry.get("牵引车号") or "").strip(),
                start_stand=str(entry.get("起点机位") or "").strip(),
                end_stand=str(entry.get("终点机位") or "").strip(),
                start_raw=entry.get("生效开始"),
                end_raw=entry.get("生效结束"),
                ignore_id=entry_id,
            )
            if problems:
                return None, "；".join(problems)
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"牵引任务已{action}"

    def postpone_entries(
        self,
        *,
        start_raw: Any,
        end_raw: Any,
        delta_minutes: Any,
    ) -> tuple[dict[str, Any] | None, str]:
        """按生效时段批量顺延：时段完整落在 [start, end] 内的待办任务整体平移。

        冲突判定增量进行：先拿未入选的已排任务做底，再逐条把成功顺延的任务加入，
        保证顺延后同一牵引车号的时段依旧互不冲突；冲突的任务原样保留、单独列出。
        """
        window_start = parse_time(start_raw)
        window_end = parse_time(end_raw)
        if window_start is None or window_end is None:
            return None, "生效时段格式无法识别，请填写如 2026-09-28 08:00 的起止时刻"
        if window_end <= window_start:
            return None, "生效时段结束时刻必须晚于开始时刻"
        try:
            delta = int(str(delta_minutes).strip())
        except (TypeError, ValueError, AttributeError):
            return None, "顺延分钟数必须是整数"
        if delta == 0:
            return None, "顺延分钟数不能为 0"

        rows = store.rows(MODULE)
        candidates: list[dict[str, Any]] = []
        for row in rows:
            if row.get("status") not in POSTPONE_STATUSES:
                continue
            start_at = parse_time(row.get("生效开始"))
            end_at = parse_time(row.get("生效结束"))
            if start_at is None or end_at is None or end_at <= start_at:
                continue
            if window_start <= start_at and end_at <= window_end:
                candidates.append(row)
        candidates.sort(key=lambda row: (parse_time(row.get("生效开始")), int(row.get("id", 0))))

        candidate_ids = {int(row["id"]) for row in candidates}
        shift = timedelta(minutes=abs(delta))
        if delta < 0:
            shift = -shift

        # 占用底图：未入选的已排（待牵引/牵引中）任务，按牵引车号归档。
        occupied: dict[str, list[tuple[datetime, datetime]]] = {}
        for row in rows:
            if int(row.get("id", 0)) in candidate_ids:
                continue
            if row.get("status") not in ACTIVE_STATUSES:
                continue
            start_at = parse_time(row.get("生效开始"))
            end_at = parse_time(row.get("生效结束"))
            tractor = str(row.get("牵引车号") or "").strip()
            if start_at and end_at and end_at > start_at and tractor:
                occupied.setdefault(tractor, []).append((start_at, end_at))

        postponed: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for row in candidates:
            tractor = str(row.get("牵引车号") or "").strip()
            start_at = parse_time(row.get("生效开始"))
            end_at = parse_time(row.get("生效结束"))
            assert start_at is not None and end_at is not None
            new_start = start_at + shift
            new_end = end_at + shift
            reasons: list[str] = []
            for busy_start, busy_end in occupied.get(tractor, []):
                if windows_overlap(new_start, new_end, busy_start, busy_end):
                    reasons.append(
                        f"牵引车号「{tractor}」顺延后与 {format_time(busy_start)}~{format_time(busy_end)} 的已排牵引时段冲突"
                    )
                    break
            if reasons:
                conflicts.append({
                    "id": row.get("id"),
                    "牵引编号": row.get("牵引编号", ""),
                    "牵引车号": tractor,
                    "起点机位": row.get("起点机位", ""),
                    "终点机位": row.get("终点机位", ""),
                    "生效开始": row.get("生效开始", ""),
                    "生效结束": row.get("生效结束", ""),
                    "顺延开始": format_time(new_start),
                    "顺延结束": format_time(new_end),
                    "原因": "；".join(reasons),
                })
                continue
            row["生效开始"] = format_time(new_start)
            row["生效结束"] = format_time(new_end)
            occupied.setdefault(tractor, []).append((new_start, new_end))
            postponed.append({
                "id": row.get("id"),
                "牵引编号": row.get("牵引编号", ""),
                "牵引车号": tractor,
                "起点机位": row.get("起点机位", ""),
                "终点机位": row.get("终点机位", ""),
                "生效开始": row["生效开始"],
                "生效结束": row["生效结束"],
            })

        selected = len(candidates)
        message = (
            f"生效时段 {format_time(window_start)}~{format_time(window_end)} 内圈定 {selected} 条待办牵引任务，"
            f"成功顺延 {len(postponed)} 条，{len(conflicts)} 条因牵引车时段冲突未顺延"
        )
        return {
            "ok": True,
            "message": message,
            "window_start": format_time(window_start),
            "window_end": format_time(window_end),
            "delta_minutes": delta,
            "selected": selected,
            "postponed": postponed,
            "conflicts": conflicts,
        }, ""

    def statistics(self) -> dict[str, Any]:
        """统计口径与列表同源：直接遍历 store 里的牵引任务。"""
        rows = store.rows(MODULE)
        pending_count = sum(1 for row in rows if row.get("status") == "待牵引")
        in_progress_count = sum(1 for row in rows if row.get("status") == "牵引中")
        cancelled_count = sum(1 for row in rows if row.get("status") == "已取消")
        month_count = 0
        starts = [parsed for parsed in (parse_time(row.get("生效开始")) for row in rows) if parsed]
        ends = [parsed for parsed in (parse_time(row.get("生效结束")) for row in rows) if parsed]
        for row in rows:
            start_at = parse_time(row.get("生效开始"))
            if start_at is not None and start_at.strftime("%Y-%m") == "2026-09":
                month_count += 1
        return {
            "待牵引任务": pending_count,
            "牵引中任务": in_progress_count,
            "本月牵引次数": month_count,
            "取消任务数": cancelled_count,
            "统计任务总数": len(rows),
            "时段最早开始": format_time(min(starts)) if starts else "",
            "时段最晚结束": format_time(max(ends)) if ends else "",
            "涉及起点机位数": len({str(row.get("起点机位") or "").strip() for row in rows if str(row.get("起点机位") or "").strip()}),
            "涉及终点机位数": len({str(row.get("终点机位") or "").strip() for row in rows if str(row.get("终点机位") or "").strip()}),
        }

    def _closed_stands(self) -> set[str]:
        return {
            str(row.get("机位编号") or "").strip()
            for row in store.rows(STAND_MODULE)
            if row.get("status") == CLOSED_STAND_STATUS
        }

    def _validate_schedule(
        self,
        *,
        tractor: str,
        start_stand: str,
        end_stand: str,
        start_raw: Any,
        end_raw: Any,
        ignore_id: int | None = None,
    ) -> list[str]:
        """安排牵引的把关口径：起终点机位、维护封闭、牵引车号时段冲突。"""
        problems: list[str] = []
        if not tractor:
            problems.append("牵引车号不能为空")
        if not start_stand:
            problems.append("起点机位不能为空")
        if not end_stand:
            problems.append("终点机位不能为空")
        if start_stand and end_stand and start_stand == end_stand:
            problems.append(f"起点机位与终点机位相同（{start_stand}），不允许安排牵引")
        if start_stand and start_stand in self._closed_stands():
            problems.append(f"起点机位「{start_stand}」处于维护封闭，不允许安排牵引")
        start_at = parse_time(start_raw)
        end_at = parse_time(end_raw)
        if start_at is None or end_at is None:
            problems.append("生效时段格式无法识别，请填写如 2026-09-28 08:00 的起止时刻")
        elif end_at <= start_at:
            problems.append("生效时段结束时刻必须晚于开始时刻")
        if tractor and start_at and end_at and end_at > start_at:
            for row in store.rows(MODULE):
                if ignore_id is not None and int(row.get("id", 0)) == ignore_id:
                    continue
                if row.get("status") not in ACTIVE_STATUSES:
                    continue
                if str(row.get("牵引车号") or "").strip() != tractor:
                    continue
                busy_start = parse_time(row.get("生效开始"))
                busy_end = parse_time(row.get("生效结束"))
                if busy_start is None or busy_end is None or busy_end <= busy_start:
                    continue
                if windows_overlap(start_at, end_at, busy_start, busy_end):
                    problems.append(
                        f"牵引车号「{tractor}」在 {format_time(start_at)}~{format_time(end_at)} "
                        f"与已排牵引任务「{row.get('牵引编号', '')}」"
                        f"（{format_time(busy_start)}~{format_time(busy_end)}）时段冲突"
                    )
                    break
        return problems
