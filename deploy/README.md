# Deployment examples (P7 / SPEC §17.1)

One long-running PAS daemon + one profile directory + systemd or a container supervisor.
**A sleeping laptop is not continuous cloud operation**; launchd works on macOS, but a real
around-the-clock deployment belongs on a server or an always-on host.

## Layout conventions

```text
/etc/pas/pas.yaml        configuration (subset syntax, SPEC §14.3)
/var/lib/pas/            state directory (pas.sqlite3, daemon.lock, pas.sock, health.json)
/var/lib/pas/backups/    where backup/restore writes (the tools never write on their own;
                         scheduling cron is the deployer's job)
/opt/pas/app/            the deployer's app module (executor/sources/sinks assembly, see
                         examples/pas_app.py)
```

Suggested ownership: a dedicated system user `pas`, state directory `0700`, configuration
`0600`. The control-plane token file (if configured) must be `0600` and must not enter git.

## Contents

| File | Purpose |
|---|---|
| `systemd/pas.service` | Minimal systemd unit (foreground process + Restart + hardening) |
| `container/Containerfile` | Container image (contains no Muse/private material) |
| `container/docker-compose.yml` | Compose example: volumes, health check, `init: true` |
| `launchd/com.proactive.pas.plist` | macOS launchd example |

## Start/stop semantics (matching the daemon implementation)

- Start: `pas serve` runs in the foreground; the supervisor handles restarts at the
  systemd/container layer.
- Stop: `systemctl stop pas` or `docker stop` (SIGTERM) → drain (wait for in-flight runs to
  finish, 20s by default, configured by `runtime.shutdown_grace_seconds`) → exit. A second
  signal forces exit: in-flight runs keep their running state and lease, and the next start
  recovers them as attempt+1 (a running run is never forged into completed).
- Readiness probe: read `health.json` (refreshed by the daemon every round) or call
  `pas rpc --method system.health`. `degraded` means the process is alive but a subsystem has a
  problem (low disk, delivery_unknown, stalled heartbeat) that needs a human to look at it.

## systemd quick start

```bash
sudo cp deploy/systemd/pas.service /etc/systemd/system/
sudoedit /etc/systemd/system/pas.service   # set User/WorkingDirectory/--app
sudo systemctl daemon-reload
sudo systemctl enable --now pas
systemctl status pas
journalctl -u pas -f                       # structured JSON logs go to stderr -> journal
```

## Backup

```bash
sudo -u pas pas --config /etc/pas/pas.yaml backup /var/lib/pas/backups/$(date +%F).bin
# restore (after stopping the daemon):
sudo systemctl stop pas
sudo -u pas pas --config /etc/pas/pas.yaml restore /var/lib/pas/backups/2026-10-07.bin --yes
sudo systemctl start pas
```

A backup file is 0600 plus a `.meta.json` (profile/schema_version/sha256). restore validates the
identity and the schema version and refuses on any mismatch (fail closed).
