# MambaPlayer ESP32-C3

ESP-IDF 6.0.1 firmware and Windows-first host data hub for the Mamba ESP32-C3
audio, battery, UART JustFloat and CAN monitor.

## Current Architecture

- Protocol is `MambaLink v2`; it is not compatible with v1 host/firmware.
- The ESP32-C3 firmware is a data gateway, not a web server. It does not embed
  HTML/CSS/JS resources and does not start a SoftAP configuration page.
- Wi-Fi runs in STA mode. Configure the computer hotspot SSID/password over USB;
  after DHCP, the firmware connects back to the gateway IP on TCP `37210`.
- TCP `37210` carries reliable MambaLink control, status, configuration, audio
  upload and speaker PCM streaming.
- UDP `37211` carries hello/heartbeat packets.
- UDP `37212` carries v2 telemetry: low-rate source catalog, selected 1 kHz
  values, and configured CAN latest-frame forwarding.
- USB Serial/JTAG also carries MambaLink frames for configuration, status and
  audio upload when Wi-Fi is not available.
- UART0 is reserved for external VOFA+ JustFloat input at `1000000 8N1`; it is
  not used for ESP-IDF console logs.
- CAN defaults to `1 Mbps`. CAN frames are not forwarded by default; the host
  must configure up to 16 standard IDs to forward.

## Firmware

The firmware target is `esp32c3` with a 4 MB flash layout:

```text
nvs      0x006000
phy_init 0x001000
factory  0x1A0000
storage  0x250000
```

The storage partition keeps only audio files:

- `/spiffs/poweron.wav`: power-on prompt, limited to 10 seconds.
- `/spiffs/alarm.wav`: low-voltage alarm prompt, using the remaining audio
  budget.

Audio uploaded by the host is converted to `16 kHz`, mono, IMA ADPCM WAV. At the
current block size this is about `8111 bytes/s`; after reserving the 10 second
power-on prompt and 32 KiB safety space, the alarm file budget is roughly
4 minutes before SPIFFS overhead.

Telemetry v2 behavior:

- Low-rate catalog/status is sent about twice per second and lists available
  source keys such as `adc.battery_mv`, `adc.raw`, `i2c.capacity` and
  `justfloat.0`.
- The host selects up to 16 source keys for firmware-side 1 kHz UDP selected
  values. Low-rate values repeat their latest known value when selected.
- CAN forwarding keeps a dirty latest-frame slot per configured ID. Every 1 ms
  tick sends only IDs that received a new frame since the previous tick.
- CAN parsing is host-side. The firmware does not emit DJI/RoboMaster summary
  streams in v2.

## Host App

Run the desktop tool from the repository root:

```powershell
host_app\.venv\Scripts\python.exe host_app\app.py
```

The host app is now a light professional data hub rather than a waveform tool.
It manages source catalog, firmware 1 kHz channel selection, CAN forwarding,
host-side CAN parsers, VOFA+ UDP JustFloat output, audio upload, Wi-Fi
provisioning and Speaker Mode.

VOFA+ output defaults to remote `127.0.0.1:1346` with local UDP port `1347`.
Frames are JustFloat: little-endian `float32[]` followed by `00 00 80 7F`.

With VB-CABLE installed, Speaker Mode prefers `CABLE Output` capture while
Windows or an individual player outputs to `CABLE Input`, so the device behaves
like a virtual speaker target without relying on the PC's physical speakers.

See [host_app/README.md](host_app/README.md) for details and probe commands.

## Verification

Host-side tests:

```powershell
host_app\.venv\Scripts\python.exe -m unittest host_app.test_mamba_link host_app.test_telemetry_store
```

Firmware build:

```powershell
eim --log-file D:\Code\stm32\proj_mamba_v3\eim-build.log run "idf.py build"
```
