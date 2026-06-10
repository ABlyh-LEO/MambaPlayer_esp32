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
- UDP `37213` for low-latency Speaker Mode PCM packets.

USB Serial/JTAG uses the same MambaLink frame format as TCP. When both links
are available, the GUI prefers TCP for control and audio upload. Speaker Mode
uses TCP only to start/stop the stream, then sends `32 kHz` mono PCM over UDP
`37213` in 5 ms packets. USB remains the fallback path for configuration and is
still used for first-time Wi-Fi setup, but low-latency Speaker Mode requires a
TCP/Wi-Fi connection so the host knows the ESP32 address.
MambaLink v2 accepts up to `1536` bytes of payload in one frame.

## Data Hub Workflow

- The firmware sends a low-rate source catalog. The left table shows available
  keys, latest values, units and nominal source rates.
- If UDP telemetry is unavailable, the GUI can still populate fallback source
  values from requested TCP/USB status frames.
- Select a row in the source table, add up to 16 source keys to the Firmware
  1 kHz table, then apply the stream configuration. These are the only values
  sampled into the firmware-side selected-values UDP stream.
- Select any source key and add it to the VOFA table. The host sends enabled
  VOFA rows at 1 kHz as JustFloat UDP frames. The firmware may batch several
  1 kHz samples into one UDP packet for efficiency; the host expands the batch
  back into individual 1 kHz JustFloat frames before forwarding to VOFA+.
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
- Speaker Mode streams `32 kHz` mono PCM to the firmware through UDP `37213`;
  start/stop ACKs still use TCP. The firmware uses a jitter buffer with
  Low Latency, Balanced and Quality modes; Balanced is the default. On Windows
  it prefers VB-CABLE: set Windows or the target player output device to
  `CABLE Input (VB-Audio Virtual Cable)`,
  then the app captures `CABLE Output (VB-Audio Virtual Cable)`.
- Closing the host app sends a stop command first. If TCP is closed abruptly,
  firmware stops Speaker Mode on disconnect without playing the power-on sound;
  a normal stop command resumes alarm behavior only when the alarm state is
  active.
- Firmware creates the Speaker UDP receiver only after `AUDIO_STREAM_START`.
  This keeps boot stable before Wi-Fi/lwIP is ready and avoids opening the audio
  data socket while the device is still initializing.
- `probe snapshot` includes host-side Speaker UDP target and packet counters.
  Firmware status includes `audio.diag.speaker_udp_*`, buffer watermarks,
  underrun/overrun, PLC and drift counters for checking whether packets reach
  the ESP32 and are being played smoothly.

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

## 中文说明

Mamba Host 当前定位为“数据中转中心”和配置工具，波形显示交给 VOFA+。上位机负责连接 ESP32-C3、选择高频通道、转发 JustFloat、配置 CAN 转发、上传音频、Speaker Mode 和 Wi-Fi 配网。

### 启动

```powershell
python -m venv host_app\.venv
host_app\.venv\Scripts\python.exe -m pip install -r host_app\requirements.txt
host_app\.venv\Scripts\python.exe host_app\app.py
```

程序监听以下端口：

- TCP `37210`：可靠的 MambaLink v2 控制与音频上传。
- UDP `37211`：设备 hello 包。
- UDP `37212`：v2 实时遥测。
- UDP `37213`：低延迟 Speaker Mode PCM 音频包。

USB Serial/JTAG 与 TCP 使用同一种 MambaLink 帧格式。TCP 和 USB 同时可用时，GUI 优先用 TCP 做控制和音频上传；USB 仍用于首次 Wi-Fi 配网和兜底配置。Speaker Mode 只用 TCP 发送 start/stop ACK 命令，实时音频通过 UDP `37213` 以 `32 kHz` 单声道 PCM、每包 5 ms 的方式发送。低延迟 Speaker Mode 需要 TCP/Wi-Fi 连接，因为上位机必须知道 ESP32 的网络地址。MambaLink v2 单帧 payload 上限为 `1536` 字节。

### 数据中转流程

- 固件会低频发送 source catalog，左侧表格显示可用 key、最新值、单位和名义速率。
- 如果 UDP 遥测不可用，GUI 仍可从显式请求的 TCP/USB status 帧生成兜底 source 值。
- 在 source 表中选择一行，最多加入 16 个 key 到 Firmware 1 kHz 表，再应用 stream 配置；只有这些值会进入固件侧 selected-values UDP 流。
- 任意 source key 都可以加入 VOFA 表。上位机以 1 kHz 发送启用的 VOFA 行，格式为 JustFloat UDP。固件为了效率可能把多个 1 kHz 样本打进一个 UDP 包，上位机会再拆回逐个 1 kHz JustFloat 帧发给 VOFA+。
- VOFA+ 默认配置：上位机发送到 `127.0.0.1:1347`，本地绑定 UDP `1346`；请在 VOFA+ 中配置 UDP JustFloat 接收端口为 `1347`。
- 工程配置保存到 `host_app\runtime\last_project.mamba.json`，包含 VOFA 地址、固件通道、CAN ID、VOFA 映射和主题。

### CAN

- 固件默认不转发 CAN raw。
- 在上位机输入最多 16 个标准 CAN ID，例如 `0x201,0x202,0x200`，然后应用。
- 固件为每个配置过的 ID 保存“未转发最新帧”，每个 1 ms tick 每个 ID 最多发送一次。
- 上位机表格显示最新帧、时间戳、DLC 和 data bytes。
- DJI/RoboMaster 反馈解析在上位机完成。对 `0x201-0x208`，解析器会导出 `angle`、`rpm`、`torque_current`、`temperature` 和 `error`，形成 `can.0x201.rpm` 这类 source key，可按需映射到 VOFA 通道。

### 音频

- 上传接受常见桌面音频输入，例如 WAV、MP3、FLAC、OGG、AIFF、M4A，前提是已安装的 `soundfile` 后端能够解码。
- 上位机上传前会转成 `16 kHz` 单声道 IMA ADPCM WAV，并做 peak normalize。
- 上电提示音最长 10 秒；报警音使用固件分区假设给出的剩余存储预算。
- Speaker Mode 通过 UDP `37213` 向固件发送 `32 kHz` 单声道 PCM；start/stop ACK 仍走 TCP。固件使用 jitter buffer，支持 Low Latency、Balanced、Quality 三种延迟模式，默认 Balanced。
- Windows 下优先使用 VB-CABLE：将 Windows 或目标播放器输出设备设为 `CABLE Input (VB-Audio Virtual Cable)`，上位机采集 `CABLE Output (VB-Audio Virtual Cable)`。
- 关闭上位机时会先发送 stop 命令。如果 TCP 异常断开，固件会停止 Speaker Mode，但不会播放上电音；正常 stop 后仅在报警状态仍然有效时恢复报警逻辑。
- 固件只在收到 `AUDIO_STREAM_START` 后创建 Speaker UDP 接收器，以免设备初始化期间过早打开音频数据 socket。
- `probe snapshot` 会输出上位机侧 Speaker UDP 目标和包计数。固件 status 会包含 `audio.diag.speaker_udp_*`、buffer 水位、underrun/overrun、PLC 和 drift 计数，用于判断 ESP32 是否收到音频包并平滑播放。

### Probe 命令

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

`probe snapshot` 读取 `host_app\runtime\state.json`。该文件由 GUI 写入，便于脚本化调试，并且会被 git 忽略。

### 打包准备

仓库包含 `host_app\mamba_host.spec`，用于 PyInstaller onedir 打包。需要发布时手动执行：

```powershell
host_app\.venv\Scripts\pyinstaller.exe host_app\mamba_host.spec
```
