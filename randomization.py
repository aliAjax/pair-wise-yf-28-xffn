"""随机算法：分层区组随机的纯函数实现，不接触数据库。

方案修订审批见 amendments.py，页面展示见 web/index.html。
"""
from __future__ import annotations

import hashlib
import random

from common import BusinessError

MAX_ARM_LENGTH = 40


def validate_scheme(arms, strata_factors, block_size, seed):
    """校验一版随机方案的分组、分层因素、区组长度和随机种子。"""
    if not isinstance(arms, list) or len(arms) < 2 or len(set(arms)) != len(arms):
        raise BusinessError("至少需要两个互不相同的试验组", 422, "invalid_arms")
    if any(not str(a).strip() or len(str(a).strip()) > MAX_ARM_LENGTH for a in arms):
        raise BusinessError("试验组名称必须非空且不过长", 422, "invalid_arms")
    if not isinstance(strata_factors, list) or any(not str(x).strip() for x in strata_factors) or len(set(strata_factors)) != len(strata_factors):
        raise BusinessError("分层因素必须是不重复的数组", 422, "invalid_strata")
    if isinstance(block_size, bool) or not isinstance(block_size, int) or block_size < len(arms) or block_size % len(arms):
        raise BusinessError("区组长度必须为试验组数的正整数倍", 422, "invalid_block_size")
    if len(str(seed).strip()) < 8:
        raise BusinessError("随机种子至少 8 位", 422, "invalid_seed")


def block_plan(seed, stratum_key, block_no, arms, block_size):
    """按“方案种子 + 层键 + 区组号”确定性生成一个区组的分组序列。"""
    rng = random.Random(f"{seed}:{stratum_key}:{block_no}")
    plan = []
    blocks = len(arms) if block_size > len(arms) else 1
    for _ in range(blocks * (block_size // len(arms))):
        plan.extend(arms)
    rng.shuffle(plan)
    return plan


def allocation_code(trial_id, external_id):
    return hashlib.sha256(f"{trial_id}:{external_id}".encode()).hexdigest()[:12].upper()
