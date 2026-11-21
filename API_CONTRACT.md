# AI 运营工作台 API

## 本地公开版（2026-11-22）

Web/API 与单一 Worker 使用同一运行目录和 SQLite。首次启动前需要创建所有者密码。`POST /api/auth/login` 创建设备会话。访问 `GET /api/bootstrap` 后，写入请求须携带会话 Cookie 和返回的 `X-Workbench-Token`。`GET /api/ready` 检查 Web 入口，登录后的 `GET /api/runtime-status` 返回独立的 Worker 运行状态。

`GET /api/runtime-mode` 返回本地内容模式。平台自动登录、检查账号及保存草稿由服务端闸门拒绝。工作台生成的内容由作者在官方应用人工发布。

### 候选工作稿

- `POST /api/topics/{id}/create-drafts` 接收 `expected_revision`、`platforms` 和 `request_id`。平台须与当前选题范围一致。此请求在同一事务中批准创作并入队，相同请求键可安全重试。
- `GET /api/topics/{id}/candidate-drafts` 返回候选与材料摘要。Worker 尽可能先保存可编辑候选。已有候选不代表选题已定稿或完成审阅。
- `PUT /api/candidate-drafts/{id}` 以 `expected_revision` 保存候选。版本冲突返回 `409`，调用方可以刷新后重新合并。
- `POST /api/topics/{id}/candidate-commit` 以 `candidate_ids`、`expected_revisions` 和 `expected_version_id` 尝试定稿。首次定稿的 `expected_version_id` 为 `null`。所有已批准平台和输入版本须一致，成功后返回正式版本。实际作品不合格返回 `422`，并发变更或版本过期返回 `409`，候选仍可查看。

请求字段及返回结构可从本机 FastAPI 的 `/openapi.json` 查看。正文仅使用本机 LM Studio。接口未启动、任务被取消或模型输出不合格时，页面会显示失败或需要处理的工作稿。
