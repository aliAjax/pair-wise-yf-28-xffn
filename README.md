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

- `randomization.py`：随机算法。分层区组计划、分配编号、方案参数校验，纯函数不接触数据库。
- `amendments.py`：方案修订审批。提交、两名统计人员先后确认、驳回作废、版本沿革查询。
- `web/index.html`：页面展示。试验摘要、版本沿革表格与修订操作入口。
- `app.py`：存储、HTTP 接口与流程编排，把以上三者连接起来。

## 主要接口

- `POST /api/trials`：创建草稿试验，指定分组、分层因素、区组长度和随机种子。
- `POST /api/trials/{id}/protocol`：入组前修改方案；一旦入组即锁定，之后只能走修订流程。
- `POST /api/trials/{id}/start`：开始入组。
- `POST /api/trials/{id}/enroll`：按当前用户中心入组；响应只返回分配编号和登记版本，不返回分组。
- `GET /api/trials/{id}/participants`：分中心返回数据，中心用户看不到其他中心。
- `POST /api/trials/{id}/amendments`：协调员提交方案修订，必填新版本号和修订原因。
- `GET /api/trials/{id}/amendments`：版本沿革；中心用户的返回中不含试验组。
- `POST /api/amendments/{id}/confirm`：统计人员确认；两人先后确认且不能为同一人。
- `POST /api/amendments/{id}/reject`：统计人员驳回，必填驳回理由。
- `POST /api/participants/{id}/unblinding-requests`：发起揭盲。
- `POST /api/unblinding-requests/{id}/approve`：两人独立审批；同一人不能审批两次。
- `GET /api/trials/{id}/summary`：中心级汇总、当前方案版本和审计记录。

## 方案修订流程

1. 协调员提交修订（`POST /api/trials/{id}/amendments`），未填写的分组、分层因素、区组长度、种子沿用当前方案。同一试验同一时间只允许一个待复核方案。
2. 复核期间现场继续按当前随机表入组；待审方案不生成随机表、不提前占用编号。
3. 两名统计人员先后确认（`POST /api/amendments/{id}/confirm`）。第二人确认后新方案生效：只有后续入组切换到新方案，原有受试者保留登记版本和已占编号。
4. 统计人员驳回（`POST /api/amendments/{id}/reject`）后，待审方案作废并留下理由和处理人，可重新提交，版本号继续递增。
5. 页面与 `GET /api/trials/{id}/amendments` 展示版本沿革（初始、生效中、待复核、已驳回、已替换）；中心用户仍看不到试验组，随机种子不经过接口输出。

随机表按“方案种子 + 层键（含方案号）+ 中心 + 分层因素”确定性生成，每个区组为分组数的整数倍并打乱；分配在 SQLite `BEGIN IMMEDIATE` 事务中原子占用。每版方案的分层和随机表相互独立，修订生效不影响既有层中已占用的编号。

旧版本数据库在启动时自动迁移：扩展用户角色、回填初始方案为 v1、为既有分层键加方案前缀并保留全部已占编号。实现适合作为流程原型，不替代经认证的临床试验随机化系统。
