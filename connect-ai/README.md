# connect-AI runtime (vendored)

This directory is the agent runtime and GUI that [connect-AI](../README.md) runs on: the FastAPI
server, the turn engine, the connector and tool layer (`coworker/`), and the Workspace GUI
(`surfaces/gui/`).

It is a **vendored copy of [OpenWorker](https://github.com/andrewyng/openworker) by Andrew Ng**,
patched in place. Every difference from upstream is listed in [../PATCHES.md](../PATCHES.md) —
read that before pulling a newer upstream, because the patches live directly in these files
rather than in a patch series.

The original code is MIT-licensed; see [LICENSE](LICENSE). Upstream's own documentation lives in
[docs/](docs/).

## Don't run this directly

Use the launcher at the repo root — it wires the runtime, the GUI and the local helper together,
loads `.env`, and seeds MCP bridges:

```bat
cd ..
run-web.bat
```

## Tests

```bash
../.venv/Scripts/python.exe -m pytest        # backend
cd surfaces/gui && npm test                  # GUI unit tests
```
