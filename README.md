# 临床试验随机分配与盲法服务

仅使用 Python 3.11+ 标准库的独立随机化服务。支持分层区组随机、试验方案锁定、方案修订双人复核、隐藏分组、外部编号并发幂等、中心隔离、双人揭盲和审计。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8104>，默认数据库 `randomization.db`。测试：

```bash
python3 -m unittest -v
```

演示用户：`site1`、`site2`（研究中心），`coord`（协调员），`monitor1`、`monitor2`（监查员），`stat1`、`stat2`（统计人员）。

## 模块划分

随机算法、修订审批和页面展示分开承接：

- `randomization.py`：分层区组随机的确定性生成算法（纯函数，按“种子 + 层键 + 区组号”重现）。
- `amendments.py`：方案修订审批流——协调员提交、两名统计人员先后确认、驳回作废、版本沿革。
- `app.py`：存储、接口与盲法控制；`web/index.html`：页面展示（版本沿革、受试者登记版本、修订操作）。

## 主要接口

- `POST /api/trials`：创建草稿试验，指定分组、分层因素、区组长度和随机种子。
- `POST /api/trials/{id}/protocol`：入组前修改方案；一旦入组即锁定。
- `POST /api/trials/{id}/start`：开始入组。
- `POST /api/trials/{id}/enroll`：按当前用户中心入组；响应只返回分配编号和登记版本，不返回分组。
- `GET /api/trials/{id}/participants`：分中心返回数据，中心用户看不到其他中心。
- `POST /api/trials/{id}/amendments`：协调员写明原因提交方案修订；复核期间现场继续使用当前随机表，待审方案不提前占编号；同一试验同一时间只允许一个待审修订。
- `POST /api/amendments/{id}/approve`：两名统计人员先后确认（同一人不能确认两次）；通过后仅后续入组切换新方案，原有受试者保留登记版本和已占编号。
- `POST /api/amendments/{id}/reject`：统计人员驳回，待审方案作废并记录理由和处理人。
- `GET /api/trials/{id}/scheme-versions`：版本沿革（状态、提交人、复核人、驳回理由、各版登记例数）；中心用户看不到试验组配置。
- `POST /api/participants/{id}/unblinding-requests`：发起揭盲。
- `POST /api/unblinding-requests/{id}/approve`：两人独立审批；同一人不能审批两次。
- `GET /api/trials/{id}/summary`：中心级汇总和审计记录。

随机表按“试验种子 + 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用，编号在同一层内跨版本连续、不复用。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
