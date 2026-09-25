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

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`/`reviewer2`/`reviewer3`、`claimant1`/`claimant2`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。

## 共同审理（多名主张人）

- `POST /api/objects/{id}/joint-case`：为藏品建立共同审理案件（一件藏品一个），初始为 `pending_verification`。
- `POST /api/joint-cases/{id}/parties`：登记参与人——身份关系、授权材料证据、关联主张，资格默认 `pending`。
- `POST /api/joint-cases/{id}/parties/{pid}/qualification`：审查员核验参与资格（`confirmed`/`rejected` + 说明）。
- `POST /api/joint-cases/{id}/panels`：开庭合议；`POST /api/panels/{id}/votes`：留下意见、投票（`return`/`reject`/`abstain`）、所用来源证据和是否公开。
- `POST /api/panels/{id}/conclude`：形成结论；`GET /api/joint-cases/{id}`：按角色分层查看。

规则：任一参与人资格未确认时案件停在 `pending_verification`，返还结论不能生效；结论生效需参与人数超过审查员总数一半且至少两名审查员同意。结论生效后联动已确认参与人的主张到 `resolved_return`/`rejected`。新事件或证据补入后，生效结论立即失效，并在该次合议的 `invalidation` 中显示哪项依据变了（新材料与合议原依据）。公众只能看到案件阶段和已公开意见；主张人另可见自己的参与资格。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。
