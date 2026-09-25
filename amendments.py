"""随机方案修订：协调员提交、两名统计人员先后复核、驳回作废与版本沿革。

与随机算法（randomization.py）和页面展示（web/）分离；本模块只负责修订审批流，
通过 store 的事务与审计助手落库，不直接生成任何随机分配。
"""
from __future__ import annotations

import json

from common import BusinessError, MIN_REASON_LENGTH, MAX_ARM_LENGTH, now


class AmendmentService:
    def __init__(self, store):
        self.store = store

    def _pending(self, conn, amendment_id):
        row = conn.execute(
            "SELECT * FROM scheme_versions WHERE id=? AND source='amendment'", (amendment_id,)
        ).fetchone()
        if not row:
            raise BusinessError("方案修订不存在", 404, "not_found")
        if row["status"] != "pending":
            raise BusinessError("该方案修订已完成复核", 409, "already_decided")
        return row

    def submit(self, user_id, trial_id, reason, protocol_version, arms=None, strata_factors=None, block_size=None, seed=None):
        """协调员写明原因提交修订；待审方案只是记录，不触碰随机表、不提前占编号。"""
        reason = (reason or "").strip()
        protocol_version = (protocol_version or "").strip()
        if len(reason) < MIN_REASON_LENGTH:
            raise BusinessError("修订原因至少 8 字", 422, "reason_required")
        if not protocol_version:
            raise BusinessError("新方案版本号不能为空", 422, "invalid_protocol_version")
        with self.store.connect() as conn:
            self.store._user(conn, user_id, {"coordinator"})
            trial = self.store._trial(conn, trial_id)
            if trial["status"] != "running":
                raise BusinessError("只有入组中的试验可以提交方案修订", 409, "invalid_status")
            current = self.store._active_scheme(conn, trial_id)
            if protocol_version == current["protocol_version"]:
                raise BusinessError("新方案版本号不能与现行版本相同", 409, "version_exists")
            if conn.execute("SELECT 1 FROM scheme_versions WHERE trial_id=? AND status='pending'", (trial_id,)).fetchone():
                raise BusinessError("已有待复核的方案修订，请先完成复核", 409, "amendment_pending")
            new_arms = json.loads(current["arms_json"]) if arms is None else [str(a).strip() for a in arms]
            new_strata = json.loads(current["strata_factors_json"]) if strata_factors is None else [str(x).strip() for x in strata_factors]
            new_block = current["block_size"] if block_size is None else block_size
            new_seed = current["seed"] if seed is None else str(seed).strip()
            self.store.create_trial_validation_only(new_arms, new_strata, new_block, new_seed)
            if any(not a or len(a) > MAX_ARM_LENGTH for a in new_arms) or any(not s for s in new_strata):
                raise BusinessError("试验组与分层因素名称必须非空且不过长", 422, "invalid_scheme")
            version_no = conn.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM scheme_versions WHERE trial_id=?", (trial_id,)
            ).fetchone()[0]
            cur = conn.execute(
                """INSERT INTO scheme_versions(trial_id,version_no,protocol_version,arms_json,strata_factors_json,
                                              block_size,seed,status,source,reason,submitted_by,created_at)
                   VALUES(?,?,?,?,?,?,?,'pending','amendment',?,?,?)""",
                (trial_id, version_no, protocol_version, json.dumps(new_arms), json.dumps(new_strata),
                 new_block, new_seed, reason, user_id, now()),
            )
            self.store._audit(conn, trial_id, user_id, "amendment.submit",
                              {"amendment_id": cur.lastrowid, "version_no": version_no, "protocol_version": protocol_version})
            return {"id": cur.lastrowid, "trial_id": trial_id, "version_no": version_no,
                    "protocol_version": protocol_version, "status": "pending"}

    def approve(self, user_id, amendment_id):
        """两名统计人员先后确认；第二次确认通过后才切换，且只影响后续入组。"""
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self.store._user(conn, user_id, {"statistician"})
                row = self._pending(conn, amendment_id)
                if row["first_approver"] is None:
                    conn.execute("UPDATE scheme_versions SET first_approver=? WHERE id=?", (user_id, amendment_id))
                    self.store._audit(conn, row["trial_id"], user_id, "amendment.approve.first", {"amendment_id": amendment_id})
                    return {"id": amendment_id, "status": "pending", "first_approver": user_id, "second_approval_required": True}
                if row["first_approver"] == user_id:
                    raise BusinessError("两次复核确认必须由不同统计人员完成", 409, "distinct_approver_required")
                conn.execute("UPDATE scheme_versions SET status='superseded' WHERE trial_id=? AND status='active'", (row["trial_id"],))
                conn.execute(
                    "UPDATE scheme_versions SET second_approver=?,status='active',decided_at=? WHERE id=?",
                    (user_id, now(), amendment_id),
                )
                conn.execute(
                    "UPDATE trials SET protocol_version=?,arms_json=?,strata_factors_json=?,block_size=?,seed=? WHERE id=?",
                    (row["protocol_version"], row["arms_json"], row["strata_factors_json"], row["block_size"], row["seed"], row["trial_id"]),
                )
                self.store._audit(conn, row["trial_id"], user_id, "amendment.approve.second",
                                  {"amendment_id": amendment_id, "version_no": row["version_no"], "protocol_version": row["protocol_version"]})
                return {"id": amendment_id, "status": "active", "version_no": row["version_no"],
                        "protocol_version": row["protocol_version"],
                        "first_approver": row["first_approver"], "second_approver": user_id}
            except Exception:
                conn.rollback()
                raise

    def reject(self, user_id, amendment_id, reason):
        """统计人员驳回：待审方案作废，留下理由和处理人。"""
        reason = (reason or "").strip()
        if len(reason) < MIN_REASON_LENGTH:
            raise BusinessError("驳回理由至少 8 字", 422, "reason_required")
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self.store._user(conn, user_id, {"statistician"})
                row = self._pending(conn, amendment_id)
                conn.execute(
                    "UPDATE scheme_versions SET status='rejected',reject_reason=?,rejected_by=?,decided_at=? WHERE id=?",
                    (reason, user_id, now(), amendment_id),
                )
                self.store._audit(conn, row["trial_id"], user_id, "amendment.reject",
                                  {"amendment_id": amendment_id, "reason": reason})
                return {"id": amendment_id, "status": "rejected", "rejected_by": user_id, "reject_reason": reason}
            except Exception:
                conn.rollback()
                raise

    def history(self, user_id, trial_id):
        """版本沿革：所有角色可见状态流转；中心用户看不到试验组配置。"""
        with self.store.connect() as conn:
            actor = self.store._user(conn, user_id, {"site", "coordinator", "monitor", "statistician"})
            self.store._trial(conn, trial_id)
            rows = conn.execute("SELECT * FROM scheme_versions WHERE trial_id=? ORDER BY version_no", (trial_id,)).fetchall()
            items = []
            for r in rows:
                if actor["role"] == "site":
                    count = conn.execute(
                        "SELECT COUNT(*) FROM participants WHERE scheme_version_id=? AND site_id=?", (r["id"], actor["site_id"])
                    ).fetchone()[0]
                else:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM participants WHERE scheme_version_id=?", (r["id"],)
                    ).fetchone()[0]
                item = {
                    "id": r["id"], "version_no": r["version_no"], "protocol_version": r["protocol_version"],
                    "status": r["status"], "source": r["source"], "reason": r["reason"],
                    "submitted_by": r["submitted_by"], "first_approver": r["first_approver"],
                    "second_approver": r["second_approver"], "rejected_by": r["rejected_by"],
                    "reject_reason": r["reject_reason"], "created_at": r["created_at"],
                    "decided_at": r["decided_at"], "participants": count,
                }
                if actor["role"] != "site":
                    item["arms"] = json.loads(r["arms_json"])
                    item["strata_factors"] = json.loads(r["strata_factors_json"])
                    item["block_size"] = r["block_size"]
                items.append(item)
            return items
