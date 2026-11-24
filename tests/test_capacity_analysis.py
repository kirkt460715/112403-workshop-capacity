import copy
import unittest
from pathlib import Path

import capacity_analysis as ca


ROOT = Path(__file__).resolve().parent.parent
TABLES = ca.load_tables(ROOT / "data")
REPORT = ca.build_report(TABLES)
SESSIONS = {session["session_id"]: session for session in REPORT["sessions"]}


def waitlist_row(group, member, changed_at="2026-10-02T11:00", access="none"):
    return {
        "booking_id": f"B-{group}",
        "revision": "1",
        "group_token": group,
        "member_token": member,
        "session_id": "SX",
        "requested_access": access,
        "status": "waitlist",
        "changed_at": changed_at,
    }


class CapacityTests(unittest.TestCase):
    def test_effective_capacity_is_min_of_three_limits(self):
        s1 = SESSIONS["S1"]["capacity"]
        self.assertEqual(s1["limits"], {"room": 12, "table": 6, "guide": 2})
        self.assertEqual(s1["effective"], 2)
        self.assertEqual(s1["binding"], ["guide"])

        self.assertEqual(SESSIONS["S2"]["capacity"]["effective"], 3)
        self.assertEqual(SESSIONS["S2"]["capacity"]["binding"], ["guide"])
        self.assertEqual(SESSIONS["S3"]["capacity"]["effective"], 2)

    def test_latest_revision_per_member_wins(self):
        # B03/M04: revision 1 confirmed -> revision 2 cancelled
        s2 = SESSIONS["S2"]["confirmation"]
        self.assertEqual(s2["confirmed_count"], 0)
        self.assertIn("B03/M04", s2["cancelled"])
        self.assertIn("B03/M04", s2["cancelled_after_confirm"])

    def test_partial_revision_only_changes_named_member(self):
        latest, _ = ca.resolve_members([
            {"booking_id": "B9", "revision": "1", "group_token": "G9",
             "member_token": "M1", "session_id": "S", "requested_access": "none",
             "status": "confirmed", "changed_at": "2026-10-01T10:00"},
            {"booking_id": "B9", "revision": "1", "group_token": "G9",
             "member_token": "M2", "session_id": "S", "requested_access": "none",
             "status": "confirmed", "changed_at": "2026-10-01T10:00"},
            {"booking_id": "B9", "revision": "2", "group_token": "G9",
             "member_token": "M1", "session_id": "S", "requested_access": "none",
             "status": "cancelled", "changed_at": "2026-10-03T10:00"},
        ])
        self.assertEqual(latest[("B9", "M1")]["status"], "cancelled")
        self.assertEqual(latest[("B9", "M2")]["status"], "confirmed")

    def test_waitlist_blocked_and_named_constraint(self):
        plan = SESSIONS["S1"]["plan"]
        self.assertEqual(plan["promotable"], [])
        self.assertEqual(plan["unmet_count"], 1)
        blocked = plan["blocked"][0]
        self.assertEqual(blocked["group"], "G02")
        self.assertEqual(blocked["members"], ["M03"])
        self.assertEqual(blocked["blocked_by"], ["guide"])
        # 确认后指导能力已占满
        self.assertEqual(plan["remaining_after_confirmed"]["guide"], 0)

    def test_group_booking_is_not_split_to_fill_gap(self):
        rows = [waitlist_row("GX", "M1"), waitlist_row("GX", "M2")]
        promoted, blocked, left = ca.simulate_promotions(
            rows, {"room": 9, "table": 4, "guide": 1}
        )
        self.assertEqual(promoted, [])
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["blocked_by"], ["guide"])
        self.assertEqual(left["guide"], 1)  # 空位保留,没有只放进 1 人

    def test_fitting_group_is_promoted_as_a_whole(self):
        rows = [waitlist_row("GX", "M1"), waitlist_row("GX", "M2")]
        promoted, blocked, left = ca.simulate_promotions(
            rows, {"room": 9, "table": 4, "guide": 2}
        )
        self.assertEqual([g["group"] for g in promoted], ["GX"])
        self.assertEqual(blocked, [])
        self.assertEqual(left["guide"], 0)

    def test_companion_uses_room_only(self):
        usage = ca.member_usage(waitlist_row("G", "M", access="companion"))
        self.assertEqual(usage, {"room": 2, "table": 1, "guide": 1})
        promoted, blocked, _ = ca.simulate_promotions(
            [waitlist_row("G", "M", access="companion")],
            {"room": 1, "table": 5, "guide": 5},
        )
        self.assertEqual(promoted, [])
        self.assertEqual(blocked[0]["blocked_by"], ["room"])


class ExecutionReconciliationTests(unittest.TestCase):
    def test_latecomer_not_counted_for_briefing(self):
        execution = SESSIONS["S1"]["execution"]
        self.assertEqual(execution["stage_served"]["briefing"], 1)
        self.assertEqual(execution["stage_served"]["practice"], 2)
        self.assertEqual(execution["served_unique"], 2)
        self.assertEqual(execution["late"],
                         [{"member": "M02", "missed": ["briefing"]}])
        self.assertEqual(execution["assisted"], ["M02"])

    def test_consumption_is_issued_minus_returned(self):
        stock = SESSIONS["S1"]["stock"]
        self.assertEqual(stock["rubbing_sheet"]["issued"], 11)
        self.assertEqual(stock["rubbing_sheet"]["consumed"], 9)
        self.assertEqual(stock["ink_pad"]["consumed"], 3)
        # 已领用始终保留在 issued 中,不会被归还/签到冲回
        self.assertEqual(stock["rubbing_sheet"]["issued"], 11)

    def test_completed_session_with_zero_participation_flags_waste(self):
        s2 = SESSIONS["S2"]
        self.assertEqual(s2["execution"]["served_unique"], 0)
        self.assertEqual(s2["stock"]["rubbing_sheet"]["consumed"], 8)
        self.assertTrue(any("无实际参与" in w for w in s2["warnings"]))

    def test_planned_session_with_execution_facts_is_flagged(self):
        self.assertTrue(any("planned" in w for w in SESSIONS["S1"]["warnings"]))
        self.assertTrue(any("planned" in w for w in SESSIONS["S3"]["warnings"]))
        self.assertFalse(any("planned" in w for w in SESSIONS["S2"]["warnings"]))

    def test_walk_in_detection(self):
        attendance = [
            {"booking_id": "BX", "member_token": "W1", "session_id": "SX",
             "stage": "practice", "status": "attended"},
        ]
        result = ca.analyze_attendance(attendance, confirmed_tokens={"M1"})
        self.assertEqual(result["walk_ins"], ["W1"])

    def test_attendance_does_not_rewrite_confirmations_or_inputs(self):
        snapshot = copy.deepcopy(TABLES)
        ca.build_report(TABLES)
        self.assertEqual(TABLES, snapshot)
        # M02 未参加讲解,但仍是已确认成员
        s1 = SESSIONS["S1"]["confirmation"]
        self.assertEqual(s1["confirmed_count"], 2)
        self.assertIn("B01/M02", s1["confirmed"])

    def test_next_round_per_capita_and_suggested_stock(self):
        s3 = SESSIONS["S3"]["next_round"]
        self.assertEqual(s3["served"], 1)
        self.assertEqual(s3["capacity"], 2)
        self.assertEqual(s3["per_capita_consumption"]["rubbing_sheet"], 11.0)
        self.assertEqual(s3["suggested_opening"]["rubbing_sheet"], 22)

    def test_summary_names_total_unmet_and_binding_factors(self):
        summary = REPORT["summary"]
        self.assertEqual(summary["session_count"], 3)
        self.assertEqual(summary["unmet_total"], 1)
        self.assertEqual(summary["binding_factors"], {"guide": 3})


if __name__ == "__main__":
    unittest.main()
