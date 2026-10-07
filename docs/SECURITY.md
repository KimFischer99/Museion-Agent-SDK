# 安全政策（SECURITY）

## 报告

本项目为自用施工仓库，没有公开的漏洞赏金或对外服务。发现安全问题请直接
联系仓库维护者（不建公开 issue 讨论可利用细节）。

## 威胁模型的明确边界（与 AGENTS.md / SPEC 一致，不夸大）

以下是本 SDK **提供**的机制：

- 单 profile / 单数据库 / 单主机边界；store 的 profile identity 绑定。
- 路径安全（zip-slip、符号链接逃逸）与 Skill 源闭包校验。
- Hook 沙盒（macOS seatbelt / Linux bubblewrap；探测失败即 fail-closed，
  不静默降级为无隔离运行）。
- 出站网络 broker（域名 allowlist、禁重定向、回环 SSRF guard、TLS 校验）。
- 冻结参数审批（request hash 绑定）、撤销即时级联、本人目标不可替换。
- 投递记账（attempt journal、幂等键、unknown 不盲发）。
- 控制面认证：同 UID Unix socket peer 凭据，或 `token_file` bearer token
  （constant-time 比较）；token 不进日志、不暴露给模型与 Skill。
- 结构化日志默认脱敏（bearer/API key/私钥块/长熵串）。

以下是本 SDK **不提供**、不得声称提供的：

- 多租户隔离、跨主机 HA、外部网络 exactly-once。
- 防 root：同机 root 可读 state 目录、socket 与备份文件。
- 对不受控宿主的安全主动执行：原始 shell / 网络 / 凭据都可能绕过
  broker，不受控宿主不得用于安全主动执行。
- prompt、Skill allowed-tools、dry-run 环境变量都不是安全边界。
- `approval resolve` / `grants.create` 是可信 UI 入口，不向模型开放；
  认证 actor 由控制面提供，RPC 请求体自报的 owner 一律不被信任。

## 密钥与数据

- 控制面 token 文件必须 `0600`（`pas doctor` 会检查）。
- state 目录建议 `0700`；备份文件由工具强制 `0600` 并带 sha256 sidecar。
- 日志按结构化字段输出，不含原始私人内容；测试含敏感日志检查。
- 备份包含全部 profile 数据，按同等敏感级别保管。

## 已知缺口（如实）

- Gmail/Calendar/Webhook 的 e2e 联调未做（无授权凭据）。
- 控制面远程 TLS HTTP 未实现；仅本地 Unix socket。
- 大附件外置 blob 策略未实现（gmail +read 只回 inventory 元数据，附件
  字节不入 PAS——这是当前边界而非功能承诺）。
