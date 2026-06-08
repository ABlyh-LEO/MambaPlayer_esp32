# MambaPlayer ESP32-C3

ESP-IDF 6.0.1 firmware and Windows-first host tool for the Mamba ESP32-C3
audio, battery and CAN monitor.

## Current Architecture

- The ESP32-C3 firmware is a data gateway, not a web server. It does not embed
  HTML/CSS/JS resources and does not start a SoftAP configuration page.
- Wi-Fi runs in STA mode. Configure the computer hotspot SSID/password over USB;
  after DHCP, the firmware connects back to the gateway IP on TCP `37210`.
  If the host app is not running yet, the firmware keeps retrying the TCP
  connection while Wi-Fi stays connected.
- TCP `37210` carries reliable MambaLink control, status, config, audio upload
  and speaker PCM streaming.
- UDP `37211` carries hello/heartbeat packets.
- UDP `37212` carries best-effort high-rate telemetry batches for ADC, CAN raw,
  RoboMaster/DJI motor summaries and UART JustFloat.
- USB Serial/JTAG also carries MambaLink frames for configuration, status and
  audio upload when Wi-Fi is not available.
- UART0 is reserved for external VOFA+ JustFloat input at `1000000 8N1`; it is
  not used for ESP-IDF console logs.

## Firmware

The firmware target is `esp32c3` with a 4 MB flash layout:

```text
nvs      0x006000
phy_init 0x001000
factory  0x1A0000
storage  0x250000
```

The storage partition keeps only audio files, currently:

- `/spiffs/poweron.wav`: power-on prompt, limited to 10 seconds.
- `/spiffs/alarm.wav`: low-voltage alarm prompt, using the remaining audio
  budget.

Audio uploaded by the host is converted to `16 kHz`, mono, IMA ADPCM WAV. At the
current block size this is about `8111 bytes/s`; after reserving the 10 second
power-on prompt and 32 KiB safety space, the alarm file budget is roughly
4 minutes before SPIFFS overhead.

## Host App

Run the desktop tool from the repository root:

```powershell
host_app\.venv\Scripts\python.exe host_app\app.py
```

The host app provides a VOFA+-style dark dock workspace, channel store, CAN raw
table, status view, audio upload controls, Wi-Fi provisioning and speaker mode.
See [host_app/README.md](host_app/README.md) for details and probe commands.

## Verification

Host-side tests:

```powershell
host_app\.venv\Scripts\python.exe -m unittest host_app.test_mamba_link host_app.test_telemetry_store
```

Firmware build, when the ESP-IDF environment is available:

```powershell
eim run "idf.py build"
```
