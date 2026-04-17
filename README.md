# plottrbot-python

Phase 1 Python/Linux port of the `plottrbot-csharp` desktop app.

## Scope in this phase

- BMP workflow: load, move, slice, preview, and stream commands to Nano.
- Manual robot controls over USB serial (`9600`, newline commands, `GO` ack).
- SVG draw/send is intentionally deferred in this phase.
- Linux sleep is inhibited while USB is connected and an active stream is running.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python3 -m plottrbot
```

Settings are stored at `~/.config/plottrbot/config.json`.
Per-draw debug logs are stored in the sibling `draw_logs` directory next to that config file.

## Streaming safety behaviors

- USB connect runs a preflight (`G92 H`) and requires a valid `GO` acknowledgement.
- Manual serial actions run asynchronously to avoid UI freezing on slow serial responses.
- Streaming includes `Pause` and `Stop`; optional stop recovery sends `G1 Z1` then `G28`.
- While a stream is paused, manual controls remain available (`M17`, `M18`, tool up/down, home, raw serial).
- Out-of-bounds generated paths are blocked before streaming.

## Draw session logs

Every draw start creates a JSON session log with:

- image file/path, placement, size, and DPI
- machine profile and USB port
- start command/line index and total command/line counts
- full generated G-code payload
- timeline events (`started`, `paused`, `resumed`, `stop_requested`, final session status)
- progress counters (commands/lines sent, including stop-time counts)

## Run tests

```bash
pytest
```

## Full validation procedure

For repeatable software + hardware validation steps, see:

- `docs/testing-suite-procedure.md`
- `scripts/run_hardware_validation.py`
- `scripts/run_deep_hardware_validation.py`

## Hardware smoke order

1. Connect to Nano and test `M17`, `M18`, `G1 Z0`, `G1 Z1`, `G28`.
2. Load a small BMP and verify preview placement + selected-line highlight.
3. Send a short BMP job, pause, resume, then restart from a line number.
4. Run bounding-box trace with pen-up and pen-down.
5. Compare the same BMP run against the C# app behavior.
