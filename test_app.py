import sqlite3
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
            for i in range(1, 9)
        ]
        self.assertNotIn("arm", participants[0])
        with self.store.connect() as conn:
            arms = [r["arm"] for r in conn.execute(
                "SELECT a.arm FROM allocations a JOIN participants p ON p.allocation_id=a.id WHERE p.trial_id=? ORDER BY p.id",
                (self.trial["id"],),
            ).fetchall()]
        self.assertEqual(Counter(arms), Counter({"A": 4, "B": 4}))
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

    def _other_scheme_counts(self, v1_id):
        with self.store.connect() as conn:
            strata = conn.execute("SELECT COUNT(*) FROM strata WHERE scheme_id != ?", (v1_id,)).fetchone()[0]
            allocations = conn.execute(
                "SELECT COUNT(*) FROM allocations WHERE stratum_id NOT IN (SELECT id FROM strata WHERE scheme_id=?)",
                (v1_id,),
            ).fetchone()[0]
        return strata, allocations

    def test_amendment_review_switches_only_future_enrollment(self):
        first = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "low"})
        self.store.enroll("site1", self.trial["id"], "S001-002", {"risk": "low"})
        amendment = self.store.amendments.submit(
            "coord", self.trial["id"], "v2.0", "期中分析后调整分组设置",
            arms=["A", "B", "C"], block_size=6,
        )
        self.assertEqual(amendment["status"], "pending")
        self.assertEqual(amendment["version_no"], 2)
        with self.store.connect() as conn:
            v1_id = conn.execute(
                "SELECT id FROM scheme_versions WHERE trial_id=? AND version_no=1", (self.trial["id"],)
            ).fetchone()["id"]
        # 复核期间现场继续用当前随机表，待审方案不提前占编号
        during = self.store.enroll("site1", self.trial["id"], "S001-003", {"risk": "low"})
        self.assertEqual(during["scheme_version_no"], 1)
        self.assertEqual(self._other_scheme_counts(v1_id), (0, 0))
        # 两名统计人员先后确认，不能是同一人
        step1 = self.store.amendments.confirm("stat1", amendment["id"])
        self.assertEqual(step1["status"], "pending")
        self.assertTrue(step1["second_confirmation_required"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.confirm("stat1", amendment["id"])
        self.assertEqual(ctx.exception.code, "distinct_reviewer_required")
        step2 = self.store.amendments.confirm("stat2", amendment["id"])
        self.assertEqual(step2["status"], "active")
        # 批准动作本身也不占编号
        self.assertEqual(self._other_scheme_counts(v1_id), (0, 0))
        # 通过后只有后续入组切换到新方案
        after = self.store.enroll("site1", self.trial["id"], "S001-004", {"risk": "low"})
        self.assertEqual(after["scheme_version_no"], 2)
        self.assertEqual(after["protocol_version"], "v2.0")
        strata, allocations = self._other_scheme_counts(v1_id)
        self.assertGreater(strata, 0)
        self.assertGreater(allocations, 0)
        # 原有受试者保留登记版本和已占编号
        kept = self.store.get_participant("site1", first["id"])
        self.assertEqual(kept["scheme_version_no"], 1)
        self.assertEqual(kept["protocol_version"], "v1.0")
        self.assertEqual(kept["allocation_code"], first["allocation_code"])
        with self.store.connect() as conn:
            used = conn.execute(
                "SELECT a.used_by FROM allocations a JOIN participants p ON p.allocation_id=a.id WHERE p.id=?",
                (first["id"],),
            ).fetchone()["used_by"]
        self.assertEqual(used, first["id"])

    def test_amendment_submission_rules(self):
        draft = self.store.create_trial("coord", "未启动研究", "v0.1", ["X", "Y"], ["phase"], 2, "seed-2026-002")
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.submit("coord", draft["id"], "v0.2", "草稿期应直接修改方案")
        self.assertEqual(ctx.exception.code, "invalid_status")
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.submit("site1", self.trial["id"], "v2.0", "中心用户不能提交修订")
        self.assertEqual(ctx.exception.code, "forbidden")
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.submit("coord", self.trial["id"], "v2.0", "太短")
        self.assertEqual(ctx.exception.code, "reason_required")
        amendment = self.store.amendments.submit("coord", self.trial["id"], "v2.0", "调整分层因素以平衡基线")
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.submit("coord", self.trial["id"], "v2.1", "已有待审方案时重复提交")
        self.assertEqual(ctx.exception.code, "amendment_exists")
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.confirm("monitor1", amendment["id"])
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_rejected_amendment_voided_with_reason_handler_and_history_blinding(self):
        amendment = self.store.amendments.submit(
            "coord", self.trial["id"], "v2.0", "调整分层因素以平衡基线", strata_factors=["risk", "age"]
        )
        rejected = self.store.amendments.reject("stat1", amendment["id"], "分层因素调整依据不足")
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["rejected_by"], "stat1")
        with self.assertRaises(BusinessError) as ctx:
            self.store.amendments.confirm("stat2", amendment["id"])
        self.assertEqual(ctx.exception.code, "already_decided")
        # 驳回作废后可以重新提交，版本号继续递增
        again = self.store.amendments.submit("coord", self.trial["id"], "v2.1", "补充依据后重新提交修订")
        self.assertEqual(again["version_no"], 3)
        history = self.store.amendments.history("coord", self.trial["id"])
        self.assertEqual([h["status"] for h in history], ["active", "rejected", "pending"])
        self.assertEqual(history[1]["reject_reason"], "分层因素调整依据不足")
        self.assertEqual(history[1]["rejected_by"], "stat1")
        # 中心用户仍看不到试验组
        site_view = self.store.amendments.history("site1", self.trial["id"])
        self.assertNotIn("arms", site_view[0])
        self.assertIn("arms", history[0])
        self.assertNotIn("seed", history[0])

    def test_migration_from_legacy_schema(self):
        db = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('site','coordinator','monitor')),
                site_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)));
            CREATE TABLE trials(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                protocol_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
                    CHECK(status IN ('draft','running','stopped')),
                arms_json TEXT NOT NULL, strata_factors_json TEXT NOT NULL,
                block_size INTEGER NOT NULL CHECK(block_size >= 2),
                seed TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL, started_at TEXT);
            CREATE TABLE strata(id INTEGER PRIMARY KEY AUTOINCREMENT,
                trial_id INTEGER NOT NULL REFERENCES trials(id),
                stratum_key TEXT NOT NULL, factors_json TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(trial_id,stratum_key));
            CREATE TABLE allocations(id INTEGER PRIMARY KEY AUTOINCREMENT,
                trial_id INTEGER NOT NULL REFERENCES trials(id),
                stratum_id INTEGER NOT NULL REFERENCES strata(id),
                sequence INTEGER NOT NULL, block_no INTEGER NOT NULL,
                arm TEXT NOT NULL, used_by INTEGER, used_at TEXT,
                UNIQUE(stratum_id,sequence));
            CREATE TABLE participants(id INTEGER PRIMARY KEY AUTOINCREMENT,
                trial_id INTEGER NOT NULL REFERENCES trials(id),
                site_id TEXT NOT NULL, external_id TEXT NOT NULL,
                stratum_id INTEGER NOT NULL REFERENCES strata(id),
                allocation_id INTEGER NOT NULL UNIQUE REFERENCES allocations(id),
                allocation_code TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'enrolled'
                    CHECK(status IN ('enrolled','withdrawn','completed')),
                enrolled_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                UNIQUE(trial_id,external_id));
            CREATE TABLE audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, trial_id INTEGER REFERENCES trials(id),
                actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                detail TEXT NOT NULL, created_at TEXT NOT NULL);
            INSERT INTO users(id,name,role,site_id) VALUES
                ('coord','项目协调员','coordinator','CENTER'),('site1','中心一协调员','site','S001');
            INSERT INTO trials(name,protocol_version,status,arms_json,strata_factors_json,block_size,seed,created_by,created_at,started_at)
                VALUES('旧试验','v1.0','running','["A","B"]','["risk"]',4,'legacy-seed-1','coord','2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00');
            INSERT INTO strata(trial_id,stratum_key,factors_json,created_at)
                VALUES(1,'S001|{"risk":"low"}','{"risk":"low"}','2026-01-02T00:00:00+00:00');
            INSERT INTO allocations(trial_id,stratum_id,sequence,block_no,arm)
                VALUES(1,1,1,1,'A'),(1,1,2,1,'B');
            INSERT INTO participants(trial_id,site_id,external_id,stratum_id,allocation_id,allocation_code,enrolled_by,created_at)
                VALUES(1,'S001','S001-001',1,1,'LEGACYCODE1','site1','2026-01-03T00:00:00+00:00');
            UPDATE allocations SET used_by=1,used_at='2026-01-03T00:00:00+00:00' WHERE id=1;
            """
        )
        conn.commit()
        conn.close()
        store = RandomizationStore(db)
        store.init_schema()
        store.seed()
        with store.connect() as conn:
            v1 = conn.execute("SELECT * FROM scheme_versions WHERE trial_id=1 AND version_no=1").fetchone()
            self.assertEqual(v1["status"], "active")
            stratum = conn.execute("SELECT * FROM strata WHERE id=1").fetchone()
            self.assertEqual(stratum["scheme_id"], v1["id"])
            self.assertTrue(stratum["stratum_key"].startswith(f"{v1['id']}|"))
            participant = conn.execute("SELECT * FROM participants WHERE id=1").fetchone()
            self.assertEqual(participant["scheme_id"], v1["id"])
            conn.execute("INSERT INTO users(id,name,role,site_id) VALUES('stat9','统计','statistician','CENTER')")
        # 旧试验继续入组：沿用迁移后的 v1 方案与原层，已占编号不变
        enrolled = store.enroll("site1", 1, "S001-002", {"risk": "low"})
        self.assertEqual(enrolled["scheme_version_no"], 1)
        kept = store.get_participant("site1", 1)
        self.assertEqual(kept["allocation_code"], "LEGACYCODE1")


if __name__ == "__main__":
    unittest.main()
