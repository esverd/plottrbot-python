# Plottrbot Full Testing Suite Procedure

This document is the repeatable Phase 1 validation procedure for `plottrbot-python`.

## 1. Environment Setup

```bash
cd /home/devsverd/development/plottrbot-python
source .venv/bin/activate
pip install -e ".[dev]"
```

## 2. Software-Only Validation

Run all automated tests, including UI button-click simulation:

```bash
pytest -q
```

Expected result:
- All tests pass.
- Includes full `MainWindow` clickthrough coverage in `tests/test_ui_clickthrough.py`.

## 3. Firmware/Board Prep (when using a bare Uno for hardware validation)

If the device under test is Arduino Uno Rev3 (not Nano), build/upload `plottrbot-uc` with a temporary Uno board override:

```bash
cp /home/devsverd/development/plottrbot-uc/platformio.ini /tmp/plottrbot-uc-uno.ini
sed -i 's/board = nanoatmega328/board = uno/' /tmp/plottrbot-uc-uno.ini
/home/devsverd/development/plottrbot-python/.venv/bin/pio run \
  -d /home/devsverd/development/plottrbot-uc \
  -e uno \
  --project-conf /tmp/plottrbot-uc-uno.ini \
  -t upload \
  --upload-port /dev/ttyACM0
```

## 4. Hardware Validation (Backend + UI)

Run the full hardware suite:

```bash
QT_QPA_PLATFORM=offscreen python scripts/run_hardware_validation.py --port /dev/ttyACM0
```

What this validates:
- serial connect/disconnect and preflight handshake
- manual command path (`M17`, `M18`, `G1 Z0`, `G1 Z1`, `G28`, `G92 H`)
- BMP conversion + full image stream completion
- pause/resume behavior mid-stream
- restart from line number
- bounding-box trace commands (pen up and pen down modes)
- sleep inhibitor active during streaming and released after
- real `MainWindow` workflow in Qt (slice, connect, send, pause/resume, finish, disconnect)

Expected result:
- Script exits with `Hardware validation suite passed.`

## 5. Optional Backend-Only Hardware Check

If Qt/offscreen is unavailable:

```bash
python scripts/run_hardware_validation.py --port /dev/ttyACM0 --skip-ui
```

## 6. Deep Operator Workflow

For a more operator-like pass that exercises DPI changes, image placement, hold/release, direct command probes, bounding-box traces, full BMP streaming, pause/resume, restart from line number, and stop recovery:

```bash
QT_QPA_PLATFORM=offscreen python scripts/run_deep_hardware_validation.py --port /dev/ttyACM0
```

Expected result:
- Script exits with `Deep hardware validation suite passed.`

## 7. Troubleshooting

- Serial permission error (`Permission denied`):
  - Ensure user is in `dialout` group.
- Upload sync error (`stk500_recv` on Uno):
  - Verify Uno board override was used for `plottrbot-uc`.
- Missing sleep inhibitor:
  - Install `systemd-inhibit` (package typically provided by `systemd` on Linux).
