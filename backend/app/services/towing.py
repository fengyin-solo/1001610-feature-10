"""航空器牵引业务规则：状态流转、字段校验、时段机位把关与批量顺延都收在这里。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.store import store

MODULE = "towing"
REQUIRED_FIELDS = ["牵引编号", "关联航班", "牵引车号"]
STATUS_ORDER = ["待牵引", "牵引中", "已完成", "已取消"]
ACTION_RULES = {"安排牵引": "牵引中", "确认完成": "已完成", "取消任务": "已取消"}
NEGATIVE_ACTIONS = []
# 已排牵引：仍占着牵引车与机位的任务，完成/取消后释放资源
ACTIVE_STATUSES = ["待牵引", "牵引中"]
WINDOW_START = "生效开始"
WINDOW_END = "生效结束"
WINDOW_LABEL = "生效时段"

_DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")


def parse_window(value: Any) -> datetime | None:
    """把页面传来的生效时刻解析成 datetime；空值或解析不出来时返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def format_window(value: Any) -> str:
    """生效时刻的统一展示口径：分钟粒度、横日期格式。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value or "").strip()
    moment = parse_window(text)
    return moment.strftime("%Y-%m-%d %H:%M") if moment else text


def windows_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """两个半开时段有交集即视为重叠。"""
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
        return [self.present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self.present(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return self.present(entry), []

    def present(self, entry: dict[str, Any]) -> dict[str, Any]:
        """列表与明细的统一口径：生效时段由起止时刻拼出，起终点机位原样带出。"""
        data = dict(entry)
        start_text = format_window(entry.get(WINDOW_START, ""))
        end_text = format_window(entry.get(WINDOW_END, ""))
        data[WINDOW_LABEL] = f"{start_text} ~ {end_text}" if start_text and end_text else ""
        return data

    def _closed_stands(self) -> set[str]:
        return {
            str(row.get("机位编号", "")).strip()
            for row in store.rows("stand")
            if row.get("status") == "维护封闭" and str(row.get("机位编号", "")).strip()
        }

    def _find_conflict(
        self,
        *,
        truck: str,
        start: datetime,
        end: datetime,
        origin: str,
        closed_stands: set[str],
        ignore_id: int | None = None,
    ) -> str | None:
        """返回冲突原因；没有冲突时返回 None。

        比对数据仓库中当前仍有效的牵引任务（待牵引、牵引中），
        ignore_id 用于重排时放行任务自身原来占用的时段。
        """
        if origin in closed_stands:
            return f"起点机位 {origin} 处于维护封闭，不能安排牵引"
        for other in store.rows(MODULE):
            if other.get("id") == ignore_id or other.get("status") not in ACTIVE_STATUSES:
                continue
            if str(other.get("牵引车号", "")).strip() != truck:
                continue
            other_start = parse_window(other.get(WINDOW_START))
            other_end = parse_window(other.get(WINDOW_END))
            if other_start is None or other_end is None:
                continue
            if windows_overlap(start, end, other_start, other_end):
                return (
                    f"牵引车 {truck} 在 {format_window(start)}~{format_window(end)} "
                    f"已排牵引任务 {other.get('牵引编号', other.get('id'))}，时段冲突"
                )
        return None

    def _schedule(
        self, entry: dict[str, Any], values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """安排牵引的把关：生效时段要完整、起止合法、起终点机位不能相同，
        且起点机位不能封闭、牵引车号+时段不能与已排牵引冲突。"""
        origin = str(values.get("起点机位") or entry.get("起点机位") or "").strip()
        destination = str(values.get("终点机位") or entry.get("终点机位") or "").strip()
        truck = str(values.get("牵引车号") or entry.get("牵引车号") or "").strip()
        start = parse_window(values.get(WINDOW_START))
        end = parse_window(values.get(WINDOW_END))
        missing = [
            label for label, value in (
                ("牵引车号", truck), ("起点机位", origin), ("终点机位", destination),
                (WINDOW_START, start), (WINDOW_END, end),
            ) if not value
        ]
        if missing:
            return None, f"安排牵引缺少必要信息：{'、'.join(missing)}"
        if start >= end:
            return None, "生效时段不合法：开始时刻必须早于结束时刻"
        if origin == destination:
            return None, f"起点机位与终点机位相同（{origin}），不能安排牵引"
        reason = self._find_conflict(
            truck=truck, start=start, end=end, origin=origin,
            closed_stands=self._closed_stands(), ignore_id=int(entry.get("id", 0)),
        )
        if reason:
            return None, reason
        entry["牵引车号"] = truck
        entry["起点机位"] = origin
        entry["终点机位"] = destination
        entry[WINDOW_START] = format_window(start)
        entry[WINDOW_END] = format_window(end)
        return entry, ""

    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"牵引任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于航空器牵引可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "安排牵引":
            updated, reason = self._schedule(entry, values or {})
            if updated is None:
                return None, reason
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return self.present(entry), f"牵引任务已{action}"

    def postpone_batch(
        self,
        *,
        range_start: Any,
        range_end: Any,
        delta_minutes: Any,
        entry_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """按生效时段批量顺延：选中时段内仍有效的任务统一后移若干分钟，
        顺延结果若触发机位封闭或牵引车占用则单列为冲突，其余照常顺延。"""
        start = parse_window(range_start)
        end = parse_window(range_end)
        if start is None or end is None:
            return {"ok": False, "message": "请提供完整的生效时段范围（开始、结束）", "postponed": [], "conflicts": []}
        if start >= end:
            return {"ok": False, "message": "生效时段范围不合法：开始时刻必须早于结束时刻", "postponed": [], "conflicts": []}
        try:
            delta = int(delta_minutes)
        except (TypeError, ValueError):
            delta = 0
        if delta <= 0:
            return {"ok": False, "message": "顺延分钟数必须为正整数", "postponed": [], "conflicts": []}

        wanted = {int(value) for value in (entry_ids or []) if str(value).strip()}
        candidates: list[dict[str, Any]] = []
        for row in store.rows(MODULE):
            if row.get("status") not in ACTIVE_STATUSES:
                continue
            row_start = parse_window(row.get(WINDOW_START))
            row_end = parse_window(row.get(WINDOW_END))
            if row_start is None or row_end is None:
                continue
            if wanted and int(row.get("id", 0)) not in wanted:
                continue
            if windows_overlap(row_start, row_end, start, end):
                candidates.append(row)
        if not candidates:
            return {"ok": False, "message": "所选生效时段内没有可顺延的任务", "postponed": [], "conflicts": []}

        # 整批任务同时平移：统一平移不会改变同车任务之间的先后关系，
        # 因此只可能与批外任务、或因冲突留下不走的批内任务相撞。
        # 先按批外冲突与封闭机位定下最初走不了的任务，再迭代扩散判定。
        batch_ids = {int(row.get("id", 0)) for row in candidates}
        closed_stands = self._closed_stands()
        ordered = sorted(
            candidates,
            key=lambda row: (parse_window(row[WINDOW_START]), int(row.get("id", 0))),
        )
        shifted_windows: dict[int, tuple[datetime, datetime]] = {}
        for row in ordered:
            row_id = int(row.get("id", 0))
            shifted_windows[row_id] = (
                parse_window(row[WINDOW_START]) + timedelta(minutes=delta),
                parse_window(row[WINDOW_END]) + timedelta(minutes=delta),
            )

        def collision_reason(
            row_id: int,
            truck: str,
            new_start: datetime,
            new_end: datetime,
            *,
            retained: dict[int, tuple[datetime, datetime]],
        ) -> str | None:
            """新窗口是否撞上批外在排任务，或批内已确定走不了的任务。"""
            for other in store.rows(MODULE):
                other_id = int(other.get("id", 0))
                if other_id == row_id or other.get("status") not in ACTIVE_STATUSES:
                    continue
                if str(other.get("牵引车号", "")).strip() != truck:
                    continue
                if other_id in batch_ids:
                    if other_id not in retained:
                        continue  # 对方会一起平移，批内互不冲突
                    other_start, other_end = retained[other_id]
                else:
                    other_start = parse_window(other.get(WINDOW_START))
                    other_end = parse_window(other.get(WINDOW_END))
                if other_start is None or other_end is None:
                    continue
                if windows_overlap(new_start, new_end, other_start, other_end):
                    return (
                        f"牵引车 {truck} 顺延后在 {format_window(new_start)}~{format_window(new_end)} "
                        f"与已排牵引任务 {other.get('牵引编号', other_id)} 时段冲突"
                    )
            return None

        blocked: dict[int, str] = {}
        for row in ordered:
            row_id = int(row.get("id", 0))
            new_start, new_end = shifted_windows[row_id]
            origin = str(row.get("起点机位", "")).strip()
            if origin in closed_stands:
                blocked[row_id] = f"起点机位 {origin} 处于维护封闭，不能顺延"
                continue
            reason = collision_reason(
                row_id, str(row.get("牵引车号", "")).strip(),
                new_start, new_end, retained={},
            )
            if reason:
                blocked[row_id] = reason

        # 扩散：新窗口撞上走不了任务原窗口的，同样留下；迭代到稳定为止
        while True:
            retained = {
                row_id: (parse_window(store.find(MODULE, row_id)[WINDOW_START]),
                         parse_window(store.find(MODULE, row_id)[WINDOW_END]))
                for row_id in blocked
            }
            progressed = False
            for row in ordered:
                row_id = int(row.get("id", 0))
                if row_id in blocked:
                    continue
                new_start, new_end = shifted_windows[row_id]
                reason = collision_reason(
                    row_id, str(row.get("牵引车号", "")).strip(),
                    new_start, new_end, retained=retained,
                )
                if reason:
                    blocked[row_id] = reason
                    progressed = True
            if not progressed:
                break

        postponed: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for row in ordered:
            row_id = int(row.get("id", 0))
            if row_id in blocked:
                conflicts.append({"id": row.get("id"), "牵引编号": row.get("牵引编号", ""), "reason": blocked[row_id], "entry": self.present(row)})
                continue
            new_start, new_end = shifted_windows[row_id]
            row[WINDOW_START] = format_window(new_start)
            row[WINDOW_END] = format_window(new_end)
            postponed.append(self.present(row))

        message = f"已顺延 {len(postponed)} 条牵引任务"
        if conflicts:
            message += f"，{len(conflicts)} 条冲突未顺延"
        return {"ok": True, "message": message, "postponed": postponed, "conflicts": conflicts}

    def stats(self) -> list[dict[str, Any]]:
        """统计口径与列表同源：直接遍历当前牵引任务计算，不缓存计数。"""
        rows = store.rows(MODULE)
        pending = sum(1 for row in rows if row.get("status") == "待牵引")
        cancelled = sum(1 for row in rows if row.get("status") == "已取消")
        month_prefix = datetime.now().strftime("%Y-%m")
        month_count = sum(
            1 for row in rows
            if row.get("status") == "已完成" and format_window(row.get(WINDOW_START, "")).startswith(month_prefix)
        )
        return [
            {"label": "待牵引任务", "value": pending},
            {"label": "本月牵引次数", "value": month_count},
            {"label": "取消任务数", "value": cancelled},
        ]
