# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张和审查阶段，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>。数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`reviewer2`、`reviewer3`、`claimant1`、`claimant2`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

## 共同审理（多名主张人）

- `POST /api/objects/{id}/joint-cases`：工作人员为同一藏品的多名主张人建共同审理案，并指定合议审查员名单（至少两人）。
- `POST /api/joint-cases/{id}/participants`：登记参与人（可关联既有主张），记录身份关系、授权材料（可附证据），参与资格初始为 `pending`。
- `POST /api/joint-cases/{id}/participants/{pid}/verify`：审查员/工作人员确认（`confirmed`）或驳回（`rejected`，须说明理由）参与资格。
- `POST /api/joint-cases/{id}/sessions`：合议成员录入合议意见、投票和所用来源证据（`basis` 可引用 `source` 或 `evidence`）。
- `GET /api/objects/{id}/joint-cases`、`GET /api/joint-cases/{id}`：分层查看共同审理。

规则：

- 只要还有参与人资格待核验，案件停留在「待核验」阶段，返还/驳回结论一律不能生效（记为 `blocked_qualification`）。
- 结论生效须满足：投票人数超过合议名单一半，且至少两名审查员投同意；不满足记为 `not_effective`，合议记录仍完整保留。
- 新来源（带来源的事件）或新证据补入藏品后，已生效结论自动失效（`invalidated`），并在失效记录中标明是哪项依据变了；案件回到「合议中」。
- 公众只能看到案件阶段和已公开意见；主张人另可查看自己的参与资格；审查员与工作人员可见投票、依据、失效记录及同一身份关系被多人主张的资格冲突提示。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。
