# Mamba Host App

Desktop-side monitor and configuration tool for the Mamba ESP32-C3 firmware.
The main window is a dark VOFA+-style dock workspace: waveform in the center,
channels on the left, connection/measurement properties on the right, and
CAN/status/log data at the bottom.

## Run

```powershell
python -m venv host_app\.venv
host_app\.venv\Scripts\python.exe -m pip install -r host_app\requirements.txt
host_app\.venv\Scripts\python.exe host_app\app.py
```

The app listens on:

- TCP `37210` for reliable MambaLink control and audio upload.
- UDP `37211` for device hello packets.
- UDP `37212` for best-effort realtime telemetry.

USB Serial/JTAG uses the same MambaLink frame format as TCP.

## Wave Workspace

- The center wave panel binds enabled channels against the time axis `T`.
- Toolbar controls include Run/Pause, Clear, Auto Y, Auto X/Y, Follow Tail,
  visible window `dt`, cursors, and PNG export.
- The channel panel controls visibility and color, and shows latest/min/max
  values plus drop counts.
- Enable cursors, then double-click the wave area twice to place vertical
  cursors; the property panel reports `dt` and per-channel `dY`.
- Mock mode generates battery, JustFloat and RoboMaster-like channels for
  no-hardware UI checks.

## Diagnostics

```powershell
host_app\.venv\Scripts\python.exe -m host_app.probe tcp-status --host 127.0.0.1
host_app\.venv\Scripts\python.exe -m host_app.probe udp-once --port 37212
host_app\.venv\Scripts\python.exe -m host_app.probe usb-wifi --port COM8 --ssid MyHotspot --password password
host_app\.venv\Scripts\python.exe -m host_app.probe snapshot
```

`probe snapshot` reads `host_app\runtime\state.json`, which the GUI writes for
scripted debugging. Runtime snapshots are intentionally ignored by git.
