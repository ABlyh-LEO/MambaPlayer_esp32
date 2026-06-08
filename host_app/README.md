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

USB Serial/JTAG uses the same MambaLink frame format as TCP. The GUI sends
control and audio upload over USB when a device is connected; if USB is not
connected, it uses the active TCP device connection.

The firmware uses Wi-Fi STA mode and connects back to this app at the DHCP
gateway IP. Configure hotspot credentials from the Wi-Fi provisioning panel over
USB once, then network-only control and audio upload can work after reconnect.

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

## Telemetry

- ADC samples are batched by the firmware at the configured telemetry interval,
  defaulting to `4 ms` for a 500 Hz ADC source.
- UART0 JustFloat is parsed as up to 16 little-endian float32 values followed by
  the VOFA+ tail `00 00 80 7F`, then forwarded in realtime batches.
- CAN raw and RoboMaster/DJI motor summaries are separate streams. The CAN panel
  can configure raw forwarding, the ID filter expression, and DJI parsing.
- UDP streams are best-effort. Sequence gaps are counted and displayed instead
  of retransmitted.

## Audio

- Upload accepts common desktop audio inputs such as WAV, MP3, FLAC, OGG, AIFF
  and M4A when the installed `soundfile` backend can decode them.
- The host converts uploads to `16 kHz` mono IMA ADPCM WAV and applies peak
  normalization before transfer.
- Power-on audio is limited to 10 seconds. Alarm audio uses the remaining
  storage budget reported by firmware status; without a status packet the app
  assumes the current `0x250000` SPIFFS partition.
- Speaker Mode streams `16 kHz` mono PCM to the firmware. On Windows it prefers
  VB-CABLE: set Windows or the target player output device to `CABLE Input
  (VB-Audio Virtual Cable)`, then the app captures `CABLE Output (VB-Audio
  Virtual Cable)`. This makes Mamba behave like the selected playback device
  instead of depending on the physical PC speakers. If VB-CABLE is unavailable,
  the app falls back to loopback-like capture inputs such as Stereo Mix.
- VB-CABLE provides the virtual audio device; the app only captures it and
  forwards PCM to the ESP32. It does not install a driver or create a Windows
  audio endpoint by itself.

## Diagnostics

```powershell
host_app\.venv\Scripts\python.exe -m host_app.probe tcp-status --host 127.0.0.1
host_app\.venv\Scripts\python.exe -m host_app.probe udp-once --port 37212
host_app\.venv\Scripts\python.exe -m host_app.probe usb-wifi --port COM8 --ssid MyHotspot --password password
host_app\.venv\Scripts\python.exe -m host_app.probe usb-upload-audio --port COM8 --kind alarm --file .\alarm.mp3
host_app\.venv\Scripts\python.exe -m host_app.probe snapshot
```

`probe snapshot` reads `host_app\runtime\state.json`, which the GUI writes for
scripted debugging. Runtime snapshots are intentionally ignored by git.
