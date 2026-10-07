# 部署示例（P7 / SPEC §17.1）

一个长期在线的 PAS daemon + 一个 profile 目录 + systemd 或容器 supervisor。
**本地电脑睡眠不等于云端持续运行**；macOS 上可用 launchd，但真正的
全天候部署应放在服务器或常开主机上。

## 布局约定

```text
/etc/pas/pas.yaml        配置（本仓库 README/SPEC §14.3 的子集语法）
/var/lib/pas/            state directory（pas.sqlite3、daemon.lock、pas.sock、health.json）
/var/lib/pas/backups/    backup/restore 的落点（工具不自动写入，部署者自行安排 cron）
/opt/pas/app/            部署者的 app module（executor/sources/sinks 装配，见 examples/pas_app.py）
```

目录属主建议：专用系统用户 `pas`，state 目录 `0700`，配置 `0600`。
控制面 token 文件（如配置）必须 `0600` 且不进 git。

## 内容

| 文件 | 用途 |
|---|---|
| `systemd/pas.service` | 最小 systemd unit（前台进程 + Restart + 加固项） |
| `container/Containerfile` | 容器镜像（不包含任何 Muse/私有素材） |
| `container/docker-compose.yml` | compose 示例：卷、健康检查、`init: true` |
| `launchd/com.proactive.pas.plist` | macOS launchd 示例 |

## 启动/停止语义（与 daemon 实现一致）

- 启动：`pas serve` 前台运行；supervisor 负责 systemd/容器层重启。
- 停止：`systemctl stop pas` 或 `docker stop`（SIGTERM）→ drain（等
  在途 run 完成，默认 20s，配置 `runtime.shutdown_grace_seconds`）→
  退出。第二次信号强制退出：在途 run 保持 running + lease，下次启动
  以 attempt+1 恢复（不会把 running 伪造成 completed）。
- 就绪探针：读 `health.json`（daemon 每轮刷新）或
  `pas rpc --method system.health`。`degraded` 说明进程活着但子系统
  有问题（磁盘低、delivery_unknown、心跳停滞），需要人看。

## systemd 快速上手

```bash
sudo cp deploy/systemd/pas.service /etc/systemd/system/
sudoedit /etc/systemd/system/pas.service   # 改 User/WorkingDirectory/--app
sudo systemctl daemon-reload
sudo systemctl enable --now pas
systemctl status pas
journalctl -u pas -f                       # 结构化 JSON 日志走 stderr → journal
```

## 备份

```bash
sudo -u pas pas --config /etc/pas/pas.yaml backup /var/lib/pas/backups/$(date +%F).bin
# 恢复（停止 daemon 后）：
sudo systemctl stop pas
sudo -u pas pas --config /etc/pas/pas.yaml restore /var/lib/pas/backups/2026-10-07.bin --yes
sudo systemctl start pas
```

backup 文件 0600 + `.meta.json`（profile/schema_version/sha256）。
restore 校验 identity 与 schema 版本，不匹配即拒绝（fail closed）。
