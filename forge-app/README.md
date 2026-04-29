# Atticus Forge App

This is the Arch-native local desktop control app for Forge. It does not start a
Python web server and does not use Electron. The GUI is a GTK4 native process
compiled with the system `gcc` and `pkg-config` packages, and it controls a Node
Forge core that invokes the local OpenClaw/Claude-Code-style engine.

Run from this directory:

```bash
npm run app -- --repo /path/to/clean/target/repo
```

Run core commands without Electron:

```bash
node src/main/forge-core.mjs status --repo /path/to/repo
node src/main/forge-core.mjs run-one --repo /path/to/clean/repo --offline-review
node src/main/forge-core.mjs loop --repo /path/to/clean/repo --offline-review --max-iterations 3
```

The default source engine is the local OpenClaw/Claude-Code-style checkout at:

```text
/home/alba/open-systeme-Repo 1 Claude Code/openclaw.mjs
```

Override it with `FORGE_OPENCLAW_ENGINE` or the GUI engine command field.

Arch packages expected at runtime/build time: `gtk4`, `glib2`, `gcc`,
`pkgconf`, and `nodejs`.

Optional per-user desktop integration:

```bash
npm run install:desktop
```
