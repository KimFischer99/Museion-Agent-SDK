# Muse Skill References

These 88 Skill entries and their accompanying assets are derived from Muse. The release provides them in `skills/`, alongside the separate `runtime/` directory.

From `runtime/`, audit or explicitly import the references:

```bash
.venv/bin/python -m proactive_sdk.service skills audit --source ../skills --state-dir ~/.local/state/museion
.venv/bin/python -m proactive_sdk.service skills import --source ../skills --state-dir ~/.local/state/museion
```

Configure the tools, service connections, credentials, and grants required by each Skill. Import records metadata; the SDK does not automatically execute Skills. Live functionality depends on the configured services.

Retain the source and license notices in [NOTICE.md](NOTICE.md). Third-party redistribution permissions have not been fully verified.
