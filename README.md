# MambaPlayer ESP32-C3

ESP-IDF 6.0.1 firmware and Windows-first host data hub for the Mamba ESP32-C3
audio, battery, UART JustFloat and CAN monitor.

## Current Architecture

- Protocol is `MambaLink v2`; it is not compatible with v1 host/firmware.
  A single MambaLink frame carries up to `1536` bytes of payload.
- The ESP32-C3 firmware is a data gateway, not a web server. It does not embed
  HTML/CSS/JS resources and does not start a SoftAP configuration page.
- Wi-Fi runs in STA mode. Configure the computer hotspot SSID/password over USB;
  after DHCP, the firmware connects back to the gateway IP on TCP `37210`.
- TCP `37210` carries reliable MambaLink control, status, configuration, audio
  upload and Speaker Mode start/stop commands.
- UDP `37211` carries hello/heartbeat packets.
- UDP `37212` carries v2 telemetry: low-rate source catalog, selected 1 kHz
  values, and configured CAN latest-frame forwarding.
- USB Serial/JTAG also carries MambaLink frames for configuration, status and
  audio upload when Wi-Fi is not available. Unrequested low-rate catalog
  mirroring is disabled on USB; request status explicitly when using USB.
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

VOFA+ output defaults to sending JustFloat UDP to `127.0.0.1:1347` while the
host binds local UDP port `1346`.
Frames are JustFloat: little-endian `float32[]` followed by `00 00 80 7F`.

With VB-CABLE installed, Speaker Mode prefers `CABLE Output` capture while
Windows or an individual player outputs to `CABLE Input`, so the device behaves
like a virtual speaker target without relying on the PC's physical speakers.
Speaker Mode streams `32 kHz` mono PCM over UDP and uses a firmware jitter
buffer; uploaded power-on/alarm audio remains `16 kHz` mono IMA ADPCM WAV.

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

## 中文说明

MambaPlayer 当前由 ESP32-C3 固件和 Windows 优先的上位机数据中转中心组成。固件负责音频播放、电池/ADC 采样、UART0 JustFloat 输入、CAN 原始帧过滤转发和低压报警；上位机负责配置、音频转码上传、CAN 解析、VOFA+ JustFloat 转发和 Speaker Mode。

### 当前架构

- 协议为 `MambaLink v2`，与 v1 固件/上位机不兼容；单帧 payload 上限为 `1536` 字节。
- ESP32-C3 固件是数据网关，不再内嵌网页资源，也不启动 SoftAP 网页配置页。
- Wi-Fi 使用 STA 模式。首次通过 USB 配置电脑热点 SSID/password；固件拿到 DHCP 后会主动连接网关 IP 的 TCP `37210`。
- TCP `37210` 用于可靠控制、状态、配置、音频上传和 Speaker Mode 启停。
- UDP `37211` 用于 hello/心跳；UDP `37212` 用于 v2 遥测，包括低频 source catalog、选中通道的 1 kHz 数据和配置过的 CAN 最新帧转发。
- USB Serial/JTAG 同样使用 MambaLink 帧，可在 Wi-Fi 不可用时用于配置、状态查询和音频上传。USB 不会主动镜像低频 catalog，需要 USB 调试时请显式请求状态。
- UART0 作为外部 VOFA+ JustFloat 输入，配置为 `1000000 8N1`，不作为 ESP-IDF 日志口。
- CAN 默认 `1 Mbps`。固件默认不转发 CAN 帧；上位机最多配置 16 个标准帧 ID 后才会转发。

### 固件与存储

目标芯片为 `esp32c3`，使用 4 MB flash 分区：

```text
nvs      0x006000
phy_init 0x001000
factory  0x1A0000
storage  0x250000
```

SPIFFS 只保存音频文件：

- `/spiffs/poweron.wav`：上电提示音，最长 10 秒。
- `/spiffs/alarm.wav`：低压报警音，使用剩余音频预算。

上位机会把上传音频转成 `16 kHz`、单声道、IMA ADPCM WAV。当前块参数约为 `8111 bytes/s`；扣除 10 秒上电提示音和 32 KiB 安全空间后，报警音预算约 4 分钟，实际可用长度会受 SPIFFS 开销影响。

遥测 v2 行为：

- 低频 catalog/status 约每秒 2 次，列出 `adc.battery_mv`、`adc.raw`、`i2c.capacity`、`justfloat.0` 等 source key。
- 上位机最多选择 16 个 source key 作为固件侧 1 kHz UDP selected-values 流；低频源被选中时会重复最新值。
- CAN 转发为每个已配置 ID 保留一个“未转发最新帧”槽位，每 1 ms tick 只发送自上次 tick 后更新过的 ID。
- CAN 解析全部在上位机完成，v2 固件不再发送 DJI/RoboMaster 解析摘要流。

### 上位机

从仓库根目录启动：

```powershell
host_app\.venv\Scripts\python.exe host_app\app.py
```

上位机是浅色专业风格的数据中转中心，不再以内置波形图为核心。它管理 source catalog、固件 1 kHz 通道选择、CAN 转发、上位机侧 CAN 解析器、VOFA+ UDP JustFloat 输出、音频上传、Wi-Fi 配网和 Speaker Mode。

VOFA+ 输出默认发送 JustFloat UDP 到 `127.0.0.1:1347`，本地绑定 UDP `1346`。JustFloat 帧格式为小端 `float32[]` 后接 `00 00 80 7F`。

安装 VB-CABLE 后，Speaker Mode 会优先采集 `CABLE Output`，同时 Windows 或播放器把输出设备设为 `CABLE Input`，这样设备表现得更像一个虚拟音响目标。实时 Speaker Mode 使用 `32 kHz` 单声道 PCM 经 UDP 传输，并由固件 jitter buffer 播放；上传保存的上电/报警音频仍为 `16 kHz` 单声道 IMA ADPCM WAV。

更多上位机用法和 probe 调试命令见 [host_app/README.md](host_app/README.md)。
