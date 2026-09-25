import tempfile
import unittest
from collections import Counter
from pathlib import Path

from app import BusinessError, RandomizationStore


class RandomizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.trial = self.store.create_trial(
            "coord", "多中心降压研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-001"
        )
        self.store.start_trial("coord", self.trial["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_stratified_block_randomization_and_two_person_unblinding(self):
        participants = [
            self.store.enroll("site1", self.trial["id"], f"S001-{i:03d}", {"risk": "low"})
            for i in range(1, 5)
        ]
        self.assertNotIn("arm", participants[0])
        with self.store.connect() as conn:
            arms = [r["arm"] for r in conn.execute(
                "SELECT a.arm FROM allocations a JOIN participants p ON p.allocation_id=a.id WHERE p.trial_id=? ORDER BY p.id",
                (self.trial["id"],),
            ).fetchall()]
        self.assertEqual(Counter(arms), Counter({"A": 2, "B": 2}))
        request = self.store.request_unblinding("site1", participants[0]["id"], "受试者发生严重不良事件需要紧急处理")
        first = self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(first["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(ctx.exception.code, "distinct_approver_required")
        second = self.store.approve_unblinding("monitor2", request["id"])
        self.assertEqual(second["status"], "approved")
        self.assertIn(second["arm"], {"A", "B"})

    def test_idempotent_enrollment_site_isolation_and_protocol_lock(self):
        first = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        again = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["idempotent"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_participant("site2", first["id"])
        self.assertEqual(ctx.exception.code, "site_isolation")
        with self.assertRaises(BusinessError) as ctx:
            self.store.update_protocol("coord", self.trial["id"], "v2")
        self.assertEqual(ctx.exception.code, "protocol_locked")

    def test_amendment_review_switches_only_future_enrollment(self):
        tid = self.trial["id"]
        old = [self.store.enroll("site1", tid, f"S001-{i:03d}", {"risk": "low"}) for i in range(1, 3)]
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.submit("coord", tid, "太短", "v2.0", seed="seed-2026-002")
        self.assertEqual(ctx.exception.code, "reason_required")
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.submit("monitor1", tid, "调整随机种子与区组配置", "v2.0", seed="seed-2026-002")
        self.assertEqual(ctx.exception.code, "forbidden")
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.submit("coord", tid, "版本号与现行方案重复", "v1.0")
        self.assertEqual(ctx.exception.code, "version_exists")
        am = self.store.amendments.submit("coord", tid, "调整随机种子与区组配置", "v2.0", seed="seed-2026-002")
        self.assertEqual(am["status"], "pending")
        # 复核期间现场继续使用当前随机表，待审方案不提前占编号
        during = self.store.enroll("site1", tid, "S001-003", {"risk": "low"})
        self.assertEqual(during["scheme_version"], "v1.0")
        with self.store.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM allocations WHERE scheme_version_id=?", (am["id"],)).fetchone()[0], 0
            )
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.submit("coord", tid, "再次提交一个待审方案", "v2.1")
        self.assertEqual(ctx.exception.code, "amendment_pending")
        # 两名统计人员先后确认，监查员无权，同一人不能确认两次
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.approve("monitor1", am["id"])
        self.assertEqual(ctx.exception.code, "forbidden")
        first = self.store.amendments.approve("stat1", am["id"])
        self.assertEqual(first["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.approve("stat1", am["id"])
        self.assertEqual(ctx.exception.code, "distinct_approver_required")
        done = self.store.amendments.approve("stat2", am["id"])
        self.assertEqual(done["status"], "active")
        # 通过后只有后续入组切换，原有受试者保留登记版本和已占编号
        new = self.store.enroll("site1", tid, "S001-004", {"risk": "low"})
        self.assertEqual(new["scheme_version"], "v2.0")
        kept = self.store.get_participant("site1", old[0]["id"])
        self.assertEqual(kept["scheme_version"], "v1.0")
        with self.store.connect() as conn:
            rows = conn.execute(
                """SELECT p.scheme_version_id, a.sequence FROM participants p
                   JOIN allocations a ON a.id=p.allocation_id WHERE p.trial_id=? ORDER BY p.id""",
                (tid,),
            ).fetchall()
        sequences = [r["sequence"] for r in rows]
        self.assertEqual(len(set(sequences)), len(sequences))
        self.assertTrue(all(r["scheme_version_id"] != am["id"] for r in rows[:3]))
        # 版本沿革：协调员可见分组，中心用户看不到试验组
        history = self.store.amendments.history("coord", tid)
        self.assertEqual([h["status"] for h in history], ["superseded", "active"])
        self.assertIn("arms", history[0])
        site_history = self.store.amendments.history("site1", tid)
        self.assertNotIn("arms", site_history[0])
        self.assertEqual([h["participants"] for h in site_history], [3, 1])

    def test_amendment_rejection_voids_pending_with_reason(self):
        tid = self.trial["id"]
        self.store.enroll("site1", tid, "S001-001", {"risk": "high"})
        am = self.store.amendments.submit("coord", tid, "拟调整分层因素配置方案", "v2.0", strata_factors=["risk", "age"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.reject("stat1", am["id"], "短")
        self.assertEqual(ctx.exception.code, "reason_required")
        rejected = self.store.amendments.reject("stat1", am["id"], "分层因素调整未获伦理批准")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["rejected_by"], "stat1")
        # 驳回作废后现场仍按现行方案入组
        p = self.store.enroll("site1", tid, "S001-002", {"risk": "high"})
        self.assertEqual(p["scheme_version"], "v1.0")
        history = self.store.amendments.history("monitor1", tid)
        self.assertEqual(history[1]["status"], "rejected")
        self.assertEqual(history[1]["reject_reason"], "分层因素调整未获伦理批准")
        self.assertEqual(history[1]["rejected_by"], "stat1")
        # 已作废的修订不能再确认，可重新提交新修订
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.approve("stat2", am["id"])
        self.assertEqual(ctx.exception.code, "already_decided")
        again = self.store.amendments.submit("coord", tid, "重新拟定分层调整方案", "v3.0", seed="seed-2026-003")
        self.assertEqual(again["status"], "pending")


if __name__ == "__main__":
    unittest.main()
