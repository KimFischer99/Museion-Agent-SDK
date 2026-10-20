# Linux / systemd 部署样例

依据 2026-10-19 的脱敏生产部署记录整理，代码核对基线为
`112b6d5cd9aa3b451097d18011d312a8b26bbcf7`（0.1.4）。这里只包含可公开的配置与装配，
所有域名、模型和凭据均须在本机替换。不会导入生产数据库，也不会自动发送测试通知。

产品包布局：`runtime/` 是 wheel 与 `app.py`，`examples/deployment/` 是本目录，
`examples/pas_hermes_plugin/` 是可选的 Hermes 插件，`skills/` 独立交付。

## 运行行为

| job | 默认状态 | 行为 |
|---|---|---|
| `source-daily` | 启用 | 每天 09:00 Asia/Shanghai，显式检查两个 PageHash 源 |
| `proactive-hourly` | 停用 | 每小时 task + `always_run`；接好宿主上下文后再启用 |

23:00–08:00 为静默时段，日通知上限 5、最小间隔 1800 秒；这些通知限制不等于模型调用上限。
非手动的 job 在静默时段先被抑制，不抓源、不调用模型；手动触发绕过该分析门，
投递仍受原有策略约束。reminder 继续使用确定性提醒路径。

PageHash 必须出现在 `task.refresh_source_ids` 中；首轮只建 cursor 基线，无候选，
之后无变化的每日检查停在 L0，不调用模型。小时任务不选择这两个 PageHash 源。
默认通知写入本地 inbox；填写完整的 webhook 三项后，机会型通知使用该 webhook。

## 安装

需要 Linux、systemd、Python 3.11+ 和 venv/ensurepip。将完整产品包解压到 `/opt/pas`，
保留上述目录结构。以下命令由管理员执行；已有专用 `pas` 用户时跳过 useradd。

```bash
sudo useradd --system --home-dir /var/lib/pas-demo --shell /usr/sbin/nologin pas
sudo install -d -o pas -g pas -m 0700 /var/lib/pas-demo
sudo install -d -o root -g pas -m 0750 /etc/pas-demo
sudo install -o pas -g pas -m 0600 /opt/pas/examples/deployment/pas.yaml /etc/pas-demo/pas.yaml
sudo install -o pas -g pas -m 0600 /opt/pas/examples/deployment/sources.json /etc/pas-demo/sources.json
sudo install -o pas -g pas -m 0600 /opt/pas/examples/deployment/pas.env.example /etc/pas-demo/pas.env
sudoedit /etc/pas-demo/pas.env
sudoedit /etc/pas-demo/sources.json
sudo python3 -m venv /opt/pas/runtime/.venv
sudo /opt/pas/runtime/.venv/bin/python -m pip install --no-index --no-deps /opt/pas/runtime/proactive_sdk-*.whl
```

先替换站点 URL 与对应 `allowed_hosts`，确认服务器可达。不要在配置中放私人网络地址；
源连接器保留 HTTPS、host 白名单、响应大小、超时和重定向限制。
`PAS_RSS_SOURCES_FILE` / `PAS_SITEMAP_SOURCES_FILE` 也可使用同样的 JSON 数组格式。
同一类源的 JSON 环境变量与 `*_FILE` 二选一，同时配置会报错。

初始化由操作者显式授权 `notify.self` 和 `public.read`；只写本地 state，无模型调用。
首次运行前执行，重复执行相同配置可复用；修改已有 job 要提高 `JobSpec.revision`。
不要在已有生产 profile 上覆盖运行本样例的 seed。

```bash
sudo -u pas sh -c '
  set -a
  . /etc/pas-demo/pas.env
  set +a
  exec /opt/pas/runtime/.venv/bin/python /opt/pas/examples/deployment/seed.py --config /etc/pas-demo/pas.yaml --consent
'
sudo -u pas /opt/pas/runtime/.venv/bin/pas --config /etc/pas-demo/pas.yaml doctor
sudo -u pas /opt/pas/runtime/.venv/bin/pas --config /etc/pas-demo/pas.yaml jobs show source-daily
sudo install -m 0644 /opt/pas/examples/deployment/pas-demo.service /etc/systemd/system/pas-demo.service
sudo systemctl daemon-reload
sudo systemctl enable --now pas-demo
sudo -u pas /opt/pas/runtime/.venv/bin/pas --config /etc/pas-demo/pas.yaml rpc --method system.health
journalctl -u pas-demo -n 50 --no-pager
```

确认 health 的 `ready` 与 `degraded`，不要只看 systemd active。
`pas ... jobs activity source-daily` 查看分析/投递阶段，`pas ... notifications list` 读本地 inbox。
如需立即抓取基线，可由操作者执行 `pas ... jobs trigger source-daily`；daemon 下一轮拾取。
这里的 `pas ...` 代表上面的绝对 CLI 路径与 `--config` 参数。

## Hermes 接线与未包含的生产扩展

内置控制面使用换行 JSON-RPC，原生端点为 `unix:///var/lib/pas-demo/pas.sock`，并非 HTTP。
可选插件配置 `PAS_RPC_URL` 与 `PAS_RPC_TOKEN`，以及操作者预建的 `PAS_NOTIFY_GRANT`。
socket 为 0600，同机同 UID 可连接；token 不会绕过文件权限。
本样例服务没有设置 token_file，若插件需要 token 认证，应额外设置
`control_plane.token_file` 并创建 0600 文件，与插件 token 一致。不要把凭据提交到 Git。
插件每次调用使用新连接与 `system.hello`；中途失败不自动重放变更请求。
插件提交扁平 `instruction`，`always_run` 默认 false。它仍只有公开版本的五个工具；
生产自定义的 remind / ledger / watch 不包含在此包中。改完插件后须重启宿主网关。

启用小时任务前，用自有 app factory 接入经过 profile/通道授权过滤的 `ConversationPort`
与只读 `ActivityProbe`，再打开 `policy.conversation_context`。端口参考仓库的
`examples/scoped_conversation.py`；默认上下文界限是 12h / 12 轮 / 单轮 500 字 / 总 4000 字 / 2s。
需要生产记录中的 8h / 10 / 300 / 3000，或慢站点 25s 抓取 deadline 时，
在该 factory 传入相应 `CoordinatorConfig`，并保留 YAML 中已有的策略开关。
默认 probe 只认识 PAS 手动输入，无法感知 Hermes 的正常聊天。

以下行为仍需部署方实现，不能把本样例等同于生产补丁的完整复刻：IM sink 的去链接/400 字限制、
30 秒与 30 分钟通道节流、投递前再次检查用户活动并 hold、每日模型调用成本上限、
Hermes 文件记忆与 SQLite 学习记忆的合并，以及分类员路由与模型 JSON 剥壳。
SDK 默认的 SQLiteMemoryPort 已能读取 store 学习记忆；只有改用文件记忆端口时才需自行合并。
`memory_consolidation` / `goal_context` / `handoff` / `run_resume` 均保持关闭。
首次真实外部投递和整夜行为仍应由部署操作者验收。

## 本次核对结论与升级

已修复 `jobs show` 未传 `--json` 的异常、插件嵌套 instruction、原生 Unix RPC 接线、
缺少 `always_run` 参数、源 `*_FILE` 装载，以及 PageHash excerpt/threshold 参数未透传。
静默分析门复用 job 的 `delivery_policy`，无需新增另一套 quiet-window 配置。
与原部署记录的一处差异：基线 facade 的 `_with_policy_defaults` 已会将
`policy.notification_window` 的 start/end 原样写入新建 job 的静默时段；它并非完全无效。
本样例明确使用 23:00–08:00；显式 job 静默配置优先，既有 job 不会因修改 YAML 自动回填。
CLI/RPC 尚不支持选源字段，因此 seed 使用公开的 `JobSpec` + `jobs_upsert`，不调用私有 `_open_agent`。

升级前停服务并用 `pas ... backup /var/lib/pas-demo/backup.bin` 备份 state，保留 `.meta.json`；
迁移不支持降级。用新目录保留旧运行包，切换前做配置检查、doctor 与 health 验证。
勿照搬生产 reset 脚本清库。定时备份/监控由部署方管理，此样例不安装 cron/watchdog。
