# Mamba Host App

Desktop-side monitor and configuration tool for the Mamba ESP32-C3 firmware.

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

## Diagnostics

```powershell
host_app\.venv\Scripts\python.exe -m host_app.probe tcp-status --host 127.0.0.1
host_app\.venv\Scripts\python.exe -m host_app.probe udp-once --port 37212
host_app\.venv\Scripts\python.exe -m host_app.probe usb-wifi --port COM8 --ssid MyHotspot --password password
```
