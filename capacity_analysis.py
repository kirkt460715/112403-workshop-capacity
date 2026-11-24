"""古钱币拓印体验:场次容量、预约、签到与物料的核对分析。

读取 data/manifest.json 声明的输入表,为每个场次输出四个可相互核对的视图:

* 计划 plan:按场地安全人数、拓印台、指导人员上限核算可接待规模,
  共同预约(group_token)整体参与候补转正模拟,不拆散;
* 确认 confirmation:以每个 (booking_id, member_token) 的最新修订为准,
  还原当前正式确认、候补与取消记录(曾确认后取消单独标注);
* 执行 execution:分环节统计现场签到(讲解/实操),核算材料领用与消耗;
* 下一轮 next_round:用实际参与与材料消耗估计利用率、人均消耗与建议备货。

工具只读不写:补录签到不会改写已发出的预约确认,已领用的材料不会被
重新计为未消耗,因此工作人员可在确认场次前反复重新核算名额。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"

# 资源占用假设:一名参与者同时占用 1 个安全名额、1 个拓印台、1 份指导能力;
# 陪同人员只占用安全名额。venues.csv 中三项上限各自使用自己的单位,
# 可接待规模取三者换算后的最小值,最小者即该场次的限制因素。
PARTICIPANT_USE = {"room": 1, "table": 1, "guide": 1}
COMPANION_USE = {"room": 1, "table": 0, "guide": 0}

RESOURCE_LABELS = {"room": "安全人数", "table": "拓印台", "guide": "指导人员"}
RESOURCE_ORDER = ("room", "table", "guide")
STAGE_LABELS = {"briefing": "讲解", "practice": "实操"}
STAGE_ORDER = ["briefing", "practice"]
SERVED_STATUSES = {"attended", "assisted"}
COMPANION_ACCESS = "companion"
NO_ACCESS = "none"


# ---------------------------------------------------------------- 输入读取

def load_tables(data_dir):
    """按 manifest 声明读取各表,校验列名与主键(口径与 input_audit 一致)。"""
    data_dir = Path(data_dir)
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    tables = {}
    for table in manifest["tables"]:
        path = data_dir / table["file"]
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != table["columns"]:
                raise ValueError(f"{table['file']}: unexpected columns")
            rows = list(reader)
        key = table.get("key")
        if key:
            values = [row[key] for row in rows]
            if len(values) != len(set(values)):
                raise ValueError(f"{table['file']}: duplicate {key}")
        tables[path.stem] = rows
    return tables


# ---------------------------------------------------------------- 预约状态

def resolve_members(bookings):
    """按 (booking_id, member_token) 取最新修订。

    一次变更可以只调整部分成员,因此逐成员独立解析;
    返回 (最新行字典, 曾确认后取消的引用列表)。
    """
    history = defaultdict(list)
    for row in bookings:
        history[(row["booking_id"], row["member_token"])].append(row)
    latest = {}
    cancelled_after_confirm = []
    for (booking_id, member_token), rows in history.items():
        rows.sort(key=lambda r: (int(r["revision"]), r["changed_at"]))
        latest[(booking_id, member_token)] = rows[-1]
        earlier_statuses = {r["status"] for r in rows[:-1]}
        if rows[-1]["status"] == "cancelled" and "confirmed" in earlier_statuses:
            cancelled_after_confirm.append(f"{booking_id}/{member_token}")
    return latest, cancelled_after_confirm


def member_usage(row):
    """单个成员的资源占用;陪同需求额外占 1 个安全名额。"""
    use = dict(PARTICIPANT_USE)
    if row["requested_access"] == COMPANION_ACCESS:
        for resource, extra in COMPANION_USE.items():
            use[resource] += extra
    return use


def total_usage(rows):
    total = dict.fromkeys(PARTICIPANT_USE, 0)
    for row in rows:
        for resource, amount in member_usage(row).items():
            total[resource] += amount
    return total


# ---------------------------------------------------------------- 容量与候补

def session_capacity(venue):
    """返回 (各资源上限, 可接待人数, 限制因素列表)。"""
    limits = {
        "room": int(venue["room_limit"]),
        "table": int(venue["table_limit"]),
        "guide": int(venue["guide_limit"]),
    }
    per_participant = {r: limits[r] // PARTICIPANT_USE[r] for r in limits}
    effective = min(per_participant.values())
    binding = [r for r in RESOURCE_ORDER if per_participant[r] == effective]
    return limits, effective, binding


def simulate_promotions(waitlist_rows, remaining):
    """候补转正模拟:共同预约整体进出,按报送时间先后尝试,不拆散分组。

    返回 (可转正组, 受限组及限制资源, 模拟后剩余容量)。
    """
    groups = defaultdict(list)
    for row in waitlist_rows:
        groups[row["group_token"]].append(row)
    ordered = sorted(
        groups.items(),
        key=lambda item: min(r["changed_at"] for r in item[1]),
    )
    left = dict(remaining)
    promoted, blocked = [], []
    for token, rows in ordered:
        need = total_usage(rows)
        members = sorted(r["member_token"] for r in rows)
        if all(need[r] <= left[r] for r in left):
            for resource in left:
                left[resource] -= need[resource]
            promoted.append({"group": token, "members": members})
        else:
            blocked_by = [r for r in RESOURCE_ORDER if need[r] > left[r]]
            blocked.append({"group": token, "members": members, "blocked_by": blocked_by})
    return promoted, blocked, left


# ---------------------------------------------------------------- 现场执行

def _stage_sort_key(stage):
    if stage in STAGE_ORDER:
        return (STAGE_ORDER.index(stage), stage)
    return (len(STAGE_ORDER), stage)


def analyze_attendance(rows, confirmed_tokens):
    """分环节统计签到;迟到者不计入其未出现的早前环节。"""
    stage_names = sorted({r["stage"] for r in rows}, key=_stage_sort_key)
    by_member = defaultdict(dict)
    duplicates = []
    for row in rows:
        member_stages = by_member[row["member_token"]]
        if row["stage"] in member_stages:
            duplicates.append(f"{row['member_token']}:{row['stage']}")
        member_stages[row["stage"]] = row["status"]

    stage_members = {stage: [] for stage in stage_names}
    served_members = set()
    assisted, late = [], []
    absent_records = 0
    for member, stages in by_member.items():
        served = [s for s in stage_names if stages.get(s) in SERVED_STATUSES]
        if served:
            served_members.add(member)
            first = stage_names.index(served[0])
            if first > 0:
                late.append({"member": member, "missed": stage_names[:first]})
            for stage in served:
                stage_members[stage].append(member)
        if any(stages.get(s) == "assisted" for s in stage_names):
            assisted.append(member)
        absent_records += sum(1 for s in stage_names if stages.get(s) == "absent")

    return {
        "stage_names": stage_names,
        "stage_members": stage_members,
        "served_members": served_members,
        "assisted": sorted(assisted),
        "late": sorted(late, key=lambda item: item["member"]),
        "absent_records": absent_records,
        "walk_ins": sorted(served_members - set(confirmed_tokens)),
        "duplicates": sorted(duplicates),
    }


def analyze_stock(rows, served_count):
    """消耗 = 领用 - 归还;已领用不计回库存。返回 (物料明细, 数据提示)。"""
    items, warnings = {}, []
    for row in rows:
        opening = int(row["opening_units"])
        issued = int(row["issued_units"])
        returned = int(row["returned_units"])
        if returned > issued:
            warnings.append(
                f"{row['item']}: 归还 {returned} 超过领用 {issued},请核对物料记录"
            )
        if issued > opening:
            warnings.append(
                f"{row['item']}: 领用 {issued} 超过期初备货 {opening},请核对物料记录"
            )
        consumed = issued - returned
        items[row["item"]] = {
            "opening": opening,
            "issued": issued,
            "returned": returned,
            "consumed": consumed,
            "per_capita": round(consumed / served_count, 2) if served_count else None,
        }
    return items, warnings


# ---------------------------------------------------------------- 场次汇总

def analyze_session(session, venue, booking_bucket, attendance_rows, stock_rows,
                    cancelled_after_confirm, sessions_on_venue):
    warnings = []
    if venue is None:
        raise ValueError(f"{session['session_id']}: unknown venue {session['venue_id']}")

    limits, effective, binding = session_capacity(venue)
    date, start = session["start_time"].split("T")
    end = session["end_time"].split("T")[1]
    if venue["session_date"] != date:
        warnings.append(
            f"场次日期 {date} 与场地 {venue['venue_id']} 记录日期 {venue['session_date']} 不一致"
        )
    if sessions_on_venue > 1:
        warnings.append(
            f"场地 {venue['venue_id']} 当日共有 {sessions_on_venue} 个场次,容量上限为各场次共用"
        )

    # ---- 确认视图:以最新修订为准,与现场记录分开保存
    confirmed = sorted(booking_bucket["confirmed"],
                       key=lambda r: (r["group_token"], r["member_token"]))
    waitlist = sorted(booking_bucket["waitlist"],
                      key=lambda r: (r["changed_at"], r["member_token"]))
    cancelled = sorted(booking_bucket["cancelled"], key=lambda r: r["member_token"])
    confirmed_tokens = {r["member_token"] for r in confirmed}

    groups = defaultdict(list)
    for row in confirmed:
        groups[row["group_token"]].append(row["member_token"])
    access_needs = defaultdict(list)
    for row in confirmed:
        if row["requested_access"] and row["requested_access"] != NO_ACCESS:
            access_needs[row["requested_access"]].append(row["member_token"])

    usage = total_usage(confirmed)
    remaining = {r: limits[r] - usage[r] for r in RESOURCE_ORDER}

    # ---- 计划视图:候补整体转正模拟
    promoted, blocked, left_after = simulate_promotions(waitlist, remaining)

    # ---- 执行视图:签到与物料
    attendance = analyze_attendance(attendance_rows, confirmed_tokens)
    served_members = attendance["served_members"]
    stock, stock_warnings = analyze_stock(stock_rows, len(served_members))
    warnings.extend(stock_warnings)

    access_of = {r["member_token"]: r["requested_access"] for r in confirmed}
    for member in attendance["assisted"]:
        if access_of.get(member, NO_ACCESS) == NO_ACCESS:
            warnings.append(
                f"{member} 现场需协助,但预约未登记无障碍或陪同需求,建议补录"
            )
    if attendance["walk_ins"]:
        warnings.append(
            "现场参与但无确认记录:" + ", ".join(attendance["walk_ins"])
            + "(临时加入,需补登记)"
        )
    if attendance["duplicates"]:
        warnings.append("签到记录重复:" + ", ".join(attendance["duplicates"]))

    has_execution_facts = bool(attendance_rows) or any(
        int(r["issued_units"]) > 0 for r in stock_rows
    )
    if session["status"] == "planned" and has_execution_facts:
        warnings.append(
            "场次状态为 planned,但已存在签到或物料领用记录;确认场次前请核对状态"
        )

    no_shows = []
    if session["status"] == "completed":
        no_shows = sorted(confirmed_tokens - served_members)
        if no_shows:
            warnings.append(
                "已确认但未到场:" + ", ".join(no_shows)
                + "(记为缺席,不改写已发出的确认)"
            )
        issued_total = sum(int(r["issued_units"]) for r in stock_rows)
        if issued_total > 0 and not served_members:
            warnings.append("材料已领用但无实际参与,消耗系缺席或取消造成,不可计回库存")

    # ---- 下一轮估计:同一套能力口径解释容量去向
    next_round = {}
    if served_members or session["status"] == "completed":
        next_round["served"] = len(served_members)
        next_round["capacity"] = effective
        per_capita = {item: v["per_capita"] for item, v in stock.items()
                      if v["per_capita"] is not None}
        if per_capita:
            next_round["per_capita_consumption"] = per_capita
            next_round["suggested_opening"] = {
                item: math.ceil(stock[item]["consumed"] / len(served_members) * effective)
                for item in per_capita
            }
    if session["status"] == "completed" and confirmed:
        next_round["no_show_rate"] = round(
            (len(confirmed) - len(confirmed_tokens & served_members)) / len(confirmed), 3
        )

    member_ref = lambda r: f"{r['booking_id']}/{r['member_token']}"
    return {
        "session_id": session["session_id"],
        "venue_id": session["venue_id"],
        "date": date,
        "start": start,
        "end": end,
        "status": session["status"],
        "capacity": {"limits": limits, "effective": effective, "binding": binding},
        "plan": {
            "remaining_after_confirmed": remaining,
            "promotable": promoted,
            "blocked": blocked,
            "unmet_count": sum(len(g["members"]) for g in blocked),
            "remaining_after_promotion": left_after,
        },
        "confirmation": {
            "confirmed": [member_ref(r) for r in confirmed],
            "confirmed_count": len(confirmed),
            "groups": {token: sorted(members) for token, members in sorted(groups.items())},
            "waitlist": [member_ref(r) for r in waitlist],
            "cancelled": [member_ref(r) for r in cancelled],
            "cancelled_after_confirm": [
                member_ref(r) for r in cancelled
                if member_ref(r) in cancelled_after_confirm
            ],
            "access_needs": {k: sorted(v) for k, v in sorted(access_needs.items())},
            "usage": usage,
        },
        "execution": {
            "stage_served": {
                s: len(attendance["stage_members"][s]) for s in attendance["stage_names"]
            },
            "stage_members": {
                s: sorted(attendance["stage_members"][s]) for s in attendance["stage_names"]
            },
            "served_members": sorted(served_members),
            "served_unique": len(served_members),
            "late": attendance["late"],
            "assisted": attendance["assisted"],
            "absent_records": attendance["absent_records"],
            "walk_ins": attendance["walk_ins"],
            "no_shows": no_shows,
        },
        "stock": stock,
        "next_round": next_round,
        "warnings": warnings,
    }


def build_report(tables):
    venues = {row["venue_id"]: row for row in tables["venues"]}
    sessions = tables["sessions"]
    latest, cancelled_after_confirm = resolve_members(tables["bookings"])
    cancelled_after_confirm = set(cancelled_after_confirm)

    by_session = defaultdict(lambda: {"confirmed": [], "waitlist": [], "cancelled": []})
    for row in latest.values():
        bucket = by_session[row["session_id"]]
        if row["status"] == "confirmed":
            bucket["confirmed"].append(row)
        elif row["status"] == "waitlist":
            bucket["waitlist"].append(row)
        else:
            bucket["cancelled"].append(row)

    attendance_by_session = defaultdict(list)
    for row in tables["attendance"]:
        attendance_by_session[row["session_id"]].append(row)
    stock_by_session = defaultdict(list)
    for row in tables["stock"]:
        stock_by_session[row["session_id"]].append(row)
    sessions_on_venue = defaultdict(int)
    for session in sessions:
        sessions_on_venue[session["venue_id"]] += 1

    reports = [
        analyze_session(
            session,
            venues.get(session["venue_id"]),
            by_session[session["session_id"]],
            attendance_by_session[session["session_id"]],
            stock_by_session[session["session_id"]],
            cancelled_after_confirm,
            sessions_on_venue[session["venue_id"]],
        )
        for session in sessions
    ]

    known_sessions = {s["session_id"] for s in sessions}
    summary_warnings = []
    for table_name in ("bookings", "attendance", "stock"):
        orphans = sorted({r["session_id"] for r in tables[table_name]} - known_sessions)
        if orphans:
            summary_warnings.append(
                f"{table_name} 引用了未登记的场次:" + ", ".join(orphans)
            )

    binding_factors = defaultdict(int)
    for report in reports:
        for resource in report["capacity"]["binding"]:
            binding_factors[resource] += 1
    summary = {
        "session_count": len(reports),
        "unmet_total": sum(r["plan"]["unmet_count"] for r in reports),
        "binding_factors": dict(sorted(binding_factors.items())),
        "warning_count": sum(len(r["warnings"]) for r in reports),
        "warnings": summary_warnings,
    }
    return {"sessions": reports, "summary": summary}


# ---------------------------------------------------------------- 文本输出

def _resources_text(values):
    return " / ".join(f"{RESOURCE_LABELS[r]} {values[r]}" for r in RESOURCE_ORDER)


def _stage_label(stage):
    return STAGE_LABELS.get(stage, stage)


def render_text(report):
    lines = []
    for session in report["sessions"]:
        cap = session["capacity"]
        plan = session["plan"]
        conf = session["confirmation"]
        execution = session["execution"]
        lines.append("=" * 66)
        lines.append(
            f"场次 {session['session_id']} | 场地 {session['venue_id']} | "
            f"{session['date']} {session['start']}-{session['end']} | 状态 {session['status']}"
        )
        lines.append("-" * 66)
        binding_text = "、".join(RESOURCE_LABELS[r] for r in cap["binding"])
        lines.append(
            f"[计划] 可接待 {cap['effective']} 人(限制因素:{binding_text});"
            + _resources_text(cap["limits"])
        )
        lines.append(
            f"       已确认占用:{_resources_text(conf['usage'])};"
            f"确认后剩余:{_resources_text(plan['remaining_after_confirmed'])}"
        )
        for group in plan["promotable"]:
            lines.append(
                f"       候补可转正:{group['group']}({', '.join(group['members'])})"
            )
        for group in plan["blocked"]:
            why = "、".join(RESOURCE_LABELS[r] for r in group["blocked_by"])
            lines.append(
                f"       候补暂无法满足:{group['group']}({', '.join(group['members'])})"
                f"— 受限于{why}"
            )

        groups_text = "; ".join(
            f"{token}: {', '.join(members)}"
            for token, members in conf["groups"].items()
        ) or "无"
        lines.append(
            f"[确认] 已确认 {conf['confirmed_count']} 人({groups_text});"
            f"候补 {len(conf['waitlist'])} 人;取消 {len(conf['cancelled'])} 人"
        )
        if conf["cancelled_after_confirm"]:
            lines.append(
                "       曾确认后取消:" + ", ".join(conf["cancelled_after_confirm"])
            )
        if conf["access_needs"]:
            needs_text = "; ".join(
                f"{need} × {len(members)}({', '.join(members)})"
                for need, members in conf["access_needs"].items()
            )
            lines.append(f"       无障碍/陪同需求:{needs_text}")

        if execution["stage_served"] or execution["absent_records"]:
            stage_text = "; ".join(
                f"{_stage_label(stage)} {count} 人"
                for stage, count in execution["stage_served"].items()
            ) or "无到场记录"
            lines.append(
                f"[执行] {stage_text};实际参与(去重){execution['served_unique']} 人;"
                f"缺席记录 {execution['absent_records']} 条"
            )
            for item in execution["late"]:
                missed = "、".join(_stage_label(s) for s in item["missed"])
                lines.append(f"       迟到:{item['member']}(未参加:{missed})")
            if execution["assisted"]:
                lines.append("       现场协助:" + ", ".join(execution["assisted"]))
            if execution["no_shows"]:
                lines.append("       缺席:" + ", ".join(execution["no_shows"]))
        else:
            lines.append("[执行] 暂无签到记录")
        if session["stock"]:
            stock_text = "; ".join(
                f"{item} 领 {v['issued']} 还 {v['returned']} 耗 {v['consumed']}"
                f"(期初 {v['opening']})"
                for item, v in session["stock"].items()
            )
            lines.append(f"       物料:{stock_text}")

        next_round = session["next_round"]
        if next_round:
            parts = [f"参与 {next_round['served']}/{next_round['capacity']}"]
            if "per_capita_consumption" in next_round:
                parts.append(
                    "人均消耗 "
                    + "; ".join(
                        f"{item} {value}"
                        for item, value in next_round["per_capita_consumption"].items()
                    )
                )
                parts.append(
                    "满员建议备货 "
                    + "; ".join(
                        f"{item} {value}"
                        for item, value in next_round["suggested_opening"].items()
                    )
                )
            if "no_show_rate" in next_round:
                parts.append(f"缺席率 {next_round['no_show_rate']:.0%}")
            lines.append("[下一轮] " + ";".join(parts))

        if session["warnings"]:
            lines.append("[核对提示]")
            lines.extend(f"  - {warning}" for warning in session["warnings"])

    summary = report["summary"]
    lines.append("=" * 66)
    binding_summary = "、".join(
        f"{RESOURCE_LABELS.get(r, r)} {count} 次"
        for r, count in summary["binding_factors"].items()
    ) or "无"
    lines.append(
        f"汇总:{summary['session_count']} 个场次;"
        f"未满足需求共 {summary['unmet_total']} 人;"
        f"限制因素:{binding_summary};核对提示 {summary['warning_count']} 条"
    )
    for warning in summary["warnings"]:
        lines.append(f"  - {warning}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="场次容量与预约/签到/物料核对分析")
    parser.add_argument("--data", default=str(DATA_DIR),
                        help="输入数据目录(含 manifest.json)")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)
    report = build_report(load_tables(args.data))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report))


if __name__ == "__main__":
    main()
