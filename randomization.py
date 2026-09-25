"""分层区组随机的确定性生成算法（纯函数，不依赖存储与审批流程）。"""
import random


def block_plan(seed, stratum_key, block_no, arms, block_size):
    """按“试验种子 + 层键 + 区组号”确定性生成一个区组的分组序列。

    同一组输入永远得到同一序列，便于核查与重现；序列中各组按区组长度等比例出现。
    """
    rng = random.Random(f"{seed}:{stratum_key}:{block_no}")
    plan = []
    blocks = len(arms) if block_size > len(arms) else 1
    for _ in range(blocks * (block_size // len(arms))):
        plan.extend(arms)
    rng.shuffle(plan)
    return plan
