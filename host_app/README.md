# Mamba Host Data Hub

Desktop-side data hub and configuration tool for Mamba ESP32-C3 firmware.
The app focuses on routing and configuration; VOFA+ is responsible for waveform
visualization.

## Run

```powershell
python -m venv host_app\.venv
host_app\.venv\Scripts\python.exe -m pip install -r host_app\requirements.txt
host_app\.venv\Scripts\python.exe host_app\app.py
```

The app listens on:

- TCP `37210` for reliable MambaLink v2 control and audio upload.
- UDP `37211` for device hello packets.
- UDP `37212` for v2 realtime telemetry.

USB Serial/JTAG uses the same MambaLink frame format as TCP. The GUI sends
control and audio upload over USB when a device is connected; if USB is not
connected, it uses the active TCP device connection.

## Data Hub Workflow

- The firmware sends a low-rate source catalog. The left table shows available
  keys, latest values, units and nominal source rates.
- Select a row in the source table, add up to 16 source keys to the Firmware
  1 kHz table, then apply the stream configuration. These are the only values
  sampled into the firmware-side selected-values UDP stream.
- Select any source key and add it to the VOFA table. The host sends enabled
  VOFA rows at 1 kHz as JustFloat UDP frames.
- VOFA+ defaults: the host sends to `127.0.0.1:1347` and binds local UDP port
  `1346`. Configure VOFA+ to receive JustFloat over UDP on port `1347`.
- Project settings are saved as `host_app\runtime\last_project.mamba.json`.
  This stores VOFA address, firmware channels, CAN IDs, VOFA mappings and theme.

## CAN

- CAN raw forwarding is disabled by default in the firmware.
- Enter up to 16 standard CAN IDs, for example `0x201,0x202,0x200`, and apply.
- The firmware stores the latest unforwarded frame per configured ID and sends
  each ID at most once per 1 ms tick.
- The host table shows the latest frame, timestamp, DLC and data bytes.
- DJI/RoboMaster feedback parsing is host-side. For `0x201-0x208`, the parser
  exposes `angle`, `rpm`, `torque_current`, `temperature` and `error` as source
  keys such as `can.0x201.rpm`; map these to VOFA channels as needed.

## Audio

- Upload accepts common desktop audio inputs such as WAV, MP3, FLAC, OGG, AIFF
  and M4A when the installed `soundfile` backend can decode them.
- The host converts uploads to `16 kHz` mono IMA ADPCM WAV and applies peak
  normalization before transfer.
- Power-on audio is limited to 10 seconds. Alarm audio uses the remaining
  storage budget reported by the firmware partition assumptions.
- Speaker Mode streams `16 kHz` mono PCM to the firmware. On Windows it prefers
  VB-CABLE: set Windows or the target player output device to `CABLE Input
  (VB-Audio Virtual Cable)`, then the app captures `CABLE Output (VB-Audio
  Virtual Cable)`.

## Probe Commands

```powershell
host_app\.venv\Scripts\python.exe -m host_app.probe tcp-status --host 127.0.0.1
host_app\.venv\Scripts\python.exe -m host_app.probe udp-once --port 37212
host_app\.venv\Scripts\python.exe -m host_app.probe catalog --port 37212
host_app\.venv\Scripts\python.exe -m host_app.probe set-stream adc.battery_mv adc.raw justfloat.0
host_app\.venv\Scripts\python.exe -m host_app.probe can-filter 0x201 0x202
host_app\.venv\Scripts\python.exe -m host_app.probe vofa-test 1.0 -1.0 0.5
host_app\.venv\Scripts\python.exe -m host_app.probe usb-wifi --port COM8 --ssid MyHotspot --password password
host_app\.venv\Scripts\python.exe -m host_app.probe usb-upload-audio --port COM8 --kind alarm --file .\alarm.mp3
host_app\.venv\Scripts\python.exe -m host_app.probe snapshot
```

`probe snapshot` reads `host_app\runtime\state.json`, which the GUI writes for
scripted debugging. Runtime snapshots are intentionally ignored by git.

## Packaging Preparation

The repository includes `host_app\mamba_host.spec` for PyInstaller onedir
packaging. Build manually when needed:

```powershell
host_app\.venv\Scripts\pyinstaller.exe host_app\mamba_host.spec
```
