# Warhol Slicer

Warhol Slicer is the Python/Linux desktop app for preparing, placing, and running Plottrbot drawings.

## Scope in this phase

- BMP workflow: load, move, slice, preview, and stream commands to Nano.
- JPG prep workflow: preprocess `.jpg/.jpeg` in-app and generate deterministic processed BMP output.
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

On Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m plottrbot
```

Settings are stored at `~/.config/plottrbot/config.json`.
Per-draw debug logs are stored in the sibling `draw_logs` directory next to that config file.

## Debug without hardware

Use dummy serial mode when you want to click through the app, test slicing, and stream generated commands without a Nano connected:

```bash
python3 -m plottrbot --dummy-serial
```

If the package is installed in editable mode, these console commands are also available:

```bash
warhol-slicer --dummy-serial
warhol-slicer-demo
```

Dummy serial mode shows a `DUMMY-PLOTTRBOT` port in `Run`, acknowledges every non-empty command with the configured `GO` token at a throttled debug pace, and writes the same UI/status logs as the normal streamer. It does not send anything to real hardware.

## Image Prep Workflow

- Use the `Prep` workflow to open a JPG/JPEG or an existing sidecar and adjust:
  - DPI
  - target width/height in mm
  - exposure
  - Gaussian blur
  - source crop window
  - tonal levels (`2-8`)
  - threshold strategy (`banded` or `relative`)
  - auto/manual thresholds
- Preview can toggle between tonal and halftone views.
- `Crop source` can crop the loaded JPG before prep resizing; use `Edit crop` to drag the crop window, and `Edit masks` to return to local mask editing.
- `Local adjustments` can add rectangular masks that override exposure, contrast, and blur in selected image regions. Drag a mask in the prep preview to reposition it, then tune width, height, roundness, rotation, feathering, and image adjustments with sliders.
- `Export BMP + sidecar` writes deterministic files next to the source JPG:
  - `<image-stem>.plottrbot.processed.bmp`
  - `<image-stem>.plottrbot-edit.json`
- `Use in Place` loads the generated BMP into the Place Job workflow.
- If prep-linked settings are changed after using the image for a job, slicing from Place Job auto-refreshes the processed BMP first.

## Operator flow

1. `Prep`: convert a JPG/JPEG into deterministic Plottrbot-ready BMP output.
2. `Place Job`: position the job image, slice it, then use connected footprint tools to trace/check its bounding box on the canvas.
3. `Run`: connect USB, set motors/tool position, resume from a line if needed, then send/pause/stop.
4. `Advanced`: use retained-image overlays, raw serial, end GCODE, machine settings, and status logs.

## Streaming safety behaviors

- USB connect runs a preflight (`G92 H`) and requires a valid `GO` acknowledgement.
- Manual serial actions run asynchronously to avoid UI freezing on slow serial responses.
- Streaming includes `Pause` and `Stop`; optional stop recovery sends `G1 Z1` then `G28`.
- While paused, `Continue drawing` resumes the same stream position; `Draw from selected line` restarts from the selected preview line.
- While a stream is paused, manual controls remain available (`M17`, `M18`, tool up/down, home, raw serial).
- Out-of-bounds generated paths are blocked before streaming.

## Draw session logs

Every draw start creates a JSON session log with:

- image file/path, placement, size, and DPI
- optional image-prep metadata (source JPG + prep settings) when the draw came from `Prep`
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
