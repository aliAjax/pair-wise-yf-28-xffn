"""方案修订审批：协调员提交、两名统计人员先后确认、驳回作废与版本沿革。

复核期间现场继续使用当前随机表；待审方案不生成随机表、不提前占用编号。
随机算法见 randomization.py，页面展示见 web/index.html。
"""
from __future__ import annotations

import json

from common import BusinessError, now
from randomization import validate_scheme

REASON_MIN_LENGTH = 8
REVIEWER_ROLES = {"statistician"}
VIEWER_ROLES = {"site", "coordinator", "monitor", "statistician"}


class AmendmentService:
    """承接方案修订的提交、复核与版本沿革查询，随机表生成不在此处。"""

    def __init__(self, store):
        self.store = store

    # ---- 查询 -------------------------------------------------------
    def active_scheme(self, conn, trial_id):
        row = conn.execute(
            "SELECT * FROM scheme_versions WHERE trial_id=? AND status='active'", (trial_id,)
        ).fetchone()
        if not row:
            raise BusinessError("试验缺少生效中的随机方案", 409, "no_active_scheme")
        return row

    def history(self, user_id, trial_id):
        """版本沿革：含初始方案、待复核、已驳回与已替换的各版方案。"""
        with self.store.connect() as conn:
            actor = self.store._user(conn, user_id, VIEWER_ROLES)
            self.store._trial(conn, trial_id)
            rows = conn.execute(
                "SELECT * FROM scheme_versions WHERE trial_id=? ORDER BY version_no", (trial_id,)
            ).fetchall()
            return [self._to_dict(row, actor) for row in rows]

    @staticmethod
    def _to_dict(row, viewer):
        data = {
            "id": row["id"], "trial_id": row["trial_id"], "version_no": row["version_no"],
            "protocol_version": row["protocol_version"], "status": row["status"],
            "reason": row["reason"], "submitted_by": row["submitted_by"],
            "created_at": row["created_at"],
            "first_reviewer": row["first_reviewer"], "first_reviewed_at": row["first_reviewed_at"],
            "second_reviewer": row["second_reviewer"], "second_reviewed_at": row["second_reviewed_at"],
            "rejected_by": row["rejected_by"], "reject_reason": row["reject_reason"],
            "decided_at": row["decided_at"], "effective_at": row["effective_at"],
            "strata_factors": json.loads(row["strata_factors_json"]),
            "block_size": row["block_size"],
        }
        if viewer["role"] != "site":  # 中心用户仍看不到试验组
            data["arms"] = json.loads(row["arms_json"])
        return data

    # ---- 提交 -------------------------------------------------------
    def submit(self, user_id, trial_id, protocol_version, reason,
               arms=None, strata_factors=None, block_size=None, seed=None):
        """协调员提交修订：只登记待审方案，不触碰随机表和编号。"""
        protocol_version = str(protocol_version).strip()
        reason = str(reason).strip()
        if not protocol_version:
            raise BusinessError("新方案版本号不能为空", 422, "invalid_amendment")
        if len(reason) < REASON_MIN_LENGTH:
            raise BusinessError("修订原因至少 8 字", 422, "reason_required")
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                actor = self.store._user(conn, user_id, {"coordinator"})
                trial = self.store._trial(conn, trial_id)
                if trial["status"] != "running":
                    raise BusinessError("只有入组中的试验可以提交方案修订", 409, "invalid_status")
                pending = conn.execute(
                    "SELECT id FROM scheme_versions WHERE trial_id=? AND status='pending'", (trial_id,)
                ).fetchone()
                if pending:
                    raise BusinessError("已有待复核的修订方案，请先完成审批", 409, "amendment_exists")
                current = self.active_scheme(conn, trial_id)
                new_arms = [str(a).strip() for a in arms] if arms is not None else json.loads(current["arms_json"])
                new_strata = [str(x).strip() for x in strata_factors] if strata_factors is not None else json.loads(current["strata_factors_json"])
                new_block = block_size if block_size is not None else current["block_size"]
                new_seed = str(seed).strip() if seed is not None else current["seed"]
                validate_scheme(new_arms, new_strata, new_block, new_seed)
                version_no = conn.execute(
                    "SELECT COALESCE(MAX(version_no),0)+1 FROM scheme_versions WHERE trial_id=?", (trial_id,)
                ).fetchone()[0]
                cur = conn.execute(
                    """INSERT INTO scheme_versions(trial_id,version_no,protocol_version,arms_json,strata_factors_json,
                          block_size,seed,status,reason,submitted_by,created_at)
                       VALUES(?,?,?,?,?,?,?,'pending',?,?,?)""",
                    (trial_id, version_no, protocol_version, json.dumps(new_arms, ensure_ascii=False),
                     json.dumps(new_strata, ensure_ascii=False), new_block, new_seed, reason, user_id, now()),
                )
                self.store._audit(conn, trial_id, user_id, "amendment.submit",
                                  {"amendment_id": cur.lastrowid, "version_no": version_no,
                                   "protocol_version": protocol_version})
                conn.commit()
                row = conn.execute("SELECT * FROM scheme_versions WHERE id=?", (cur.lastrowid,)).fetchone()
                return self._to_dict(row, actor)
            except Exception:
                conn.rollback()
                raise

    # ---- 复核 -------------------------------------------------------
    def _pending(self, conn, amendment_id):
        row = conn.execute("SELECT * FROM scheme_versions WHERE id=?", (amendment_id,)).fetchone()
        if not row:
            raise BusinessError("修订方案不存在", 404, "not_found")
        if row["status"] != "pending":
            raise BusinessError("该修订方案已处理完毕", 409, "already_decided")
        return row

    def confirm(self, user_id, amendment_id):
        """两名统计人员先后确认；第二人确认后新方案生效，仅影响后续入组。"""
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self.store._user(conn, user_id, REVIEWER_ROLES)
                row = self._pending(conn, amendment_id)
                if row["first_reviewer"] is None:
                    conn.execute(
                        "UPDATE scheme_versions SET first_reviewer=?,first_reviewed_at=? WHERE id=?",
                        (user_id, now(), amendment_id),
                    )
                    self.store._audit(conn, row["trial_id"], user_id, "amendment.confirm.first",
                                      {"amendment_id": amendment_id})
                    conn.commit()
                    return {"id": amendment_id, "status": "pending", "first_reviewer": user_id,
                            "second_confirmation_required": True}
                if row["first_reviewer"] == user_id:
                    raise BusinessError("两次确认必须由不同统计人员完成", 409, "distinct_reviewer_required")
                previous = self.active_scheme(conn, row["trial_id"])
                conn.execute("UPDATE scheme_versions SET status='superseded' WHERE id=?", (previous["id"],))
                conn.execute(
                    """UPDATE scheme_versions SET status='active',second_reviewer=?,second_reviewed_at=?,
                          decided_at=?,effective_at=? WHERE id=?""",
                    (user_id, now(), now(), now(), amendment_id),
                )
                conn.execute(
                    "UPDATE trials SET protocol_version=?,arms_json=?,strata_factors_json=?,block_size=?,seed=? WHERE id=?",
                    (row["protocol_version"], row["arms_json"], row["strata_factors_json"],
                     row["block_size"], row["seed"], row["trial_id"]),
                )
                self.store._audit(conn, row["trial_id"], user_id, "amendment.confirm.second",
                                  {"amendment_id": amendment_id, "version_no": row["version_no"],
                                   "superseded_id": previous["id"]})
                conn.commit()
                return {"id": amendment_id, "status": "active",
                        "first_reviewer": row["first_reviewer"], "second_reviewer": user_id,
                        "applies_to": "future_enrollment_only"}
            except Exception:
                conn.rollback()
                raise

    def reject(self, user_id, amendment_id, reason):
        """统计人员驳回：待审方案作废，留下理由和处理人。"""
        reason = str(reason).strip()
        if len(reason) < REASON_MIN_LENGTH:
            raise BusinessError("驳回理由至少 8 字", 422, "reason_required")
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self.store._user(conn, user_id, REVIEWER_ROLES)
                row = self._pending(conn, amendment_id)
                conn.execute(
                    "UPDATE scheme_versions SET status='rejected',rejected_by=?,reject_reason=?,decided_at=? WHERE id=?",
                    (user_id, reason, now(), amendment_id),
                )
                self.store._audit(conn, row["trial_id"], user_id, "amendment.reject",
                                  {"amendment_id": amendment_id, "reason": reason})
                conn.commit()
                return {"id": amendment_id, "status": "rejected",
                        "rejected_by": user_id, "reject_reason": reason}
            except Exception:
                conn.rollback()
                raise
