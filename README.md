# Museion Agent SDK v0.1.2

Museion Agent SDK is developed based on Muse. It provides a local Python runtime for scheduled reminders, opt-in news research, persistent interests, and notification delivery.

The Python package is `proactive-sdk`, imported as `proactive_sdk`. The core runtime uses the Python standard library and supports model APIs, Pi, and Hermes as analysis backends.

## Getting started

Use macOS or Linux with Python 3.11+ and `venv`/`ensurepip` available. Download and extract the archive from [Releases](https://github.com/KimFischer99/Museion-Agent-SDK/releases), then run from its `runtime/` directory:

```bash
python3 app.py
```

The first run installs the bundled wheel into a local `.venv` and guides you through configuration. Choose reminder-only mode to run without a model, or configure a backend for research. Notifications go to the local inbox or your own HTTPS webhook.

Keep the terminal open while the agent runs. Press Ctrl-C to stop it. In another terminal, use:

```bash
python3 app.py check     # Check configuration without calling the model
python3 app.py status    # View execution and delivery status
python3 app.py inbox     # Read notifications
python3 app.py setup     # Reconfigure after stopping the agent
```

## Public interests

Add an interest and enable research:

```bash
python3 app.py say "I follow NASA public news"
python3 app.py say "Enable proactive notifications"
```

Research checks Bing News RSS hourly and analyzes supplied headlines and summaries. Interests and preferences are saved across restarts. To turn proactive notifications off:

```bash
python3 app.py say "Stop proactive notifications"
```

Configuration is saved in `settings.json`. State defaults to `~/.local/state/museion`, or an existing runtime `state/` directory. Keep credentials and runtime data out of Git and shared release copies.

## Building and extending

Build a local release from the repository root using an environment with `setuptools>=77`:

```bash
python3 tools/build_release.py
```

Use `--output <new-directory>` if `release/` already exists. The build creates separate `runtime/` and `skills/` directories. Skills are reference material and require explicit import and service configuration.

See [examples](https://github.com/KimFischer99/Museion-Agent-SDK/tree/main/examples) for SDK integration and [compatibility notes](https://github.com/KimFischer99/Museion-Agent-SDK/blob/main/docs/COMPATIBILITY.md) for supported adapters.

## License

Original SDK code is licensed under [MIT](https://github.com/KimFischer99/Museion-Agent-SDK/blob/main/LICENSE). Muse-derived Skill references retain their source notices and licensing status. Third-party redistribution permissions have not been fully verified; see the [license inventory](https://github.com/KimFischer99/Museion-Agent-SDK/blob/main/docs/LICENSES.md).
