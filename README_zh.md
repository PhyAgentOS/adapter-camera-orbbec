[English](README.md) | 中文

# forge-devices-orbbec-camera

Orbbec Gemini 2 深度相机的 Python 采集包与 Dora 节点。Python 包使用标准
src layout：`src/forge_devices_orbbec_camera`。

## 支持范围

- Linux + USB；运行时需要 libusb 和 Orbbec udev 规则
- Color：`forge_msgs.Image(rgb8)` 或 `CompressedImage(jpeg)`
- Depth：`forge_msgs.Image(32FC1)`，float32，单位米，零值表示无效深度
- PointCloud：可选 Forge `PointCloud` v1 organized XYZ/XYZRGB 输出，默认关闭
- IR：`forge_msgs.Image(mono8)`（兼容后端返回 uint16 时为 `16UC1`）


## 安装与环境检查

运行时依赖从公开 PyPI 解析，其中 `dora-rs>=1.0.1,<2`、`forge-common>=2.1.0,<3`、
`forge-msgs>=2.0.0,<3`；发布与部署使用仓库中的 `uv.lock` 固定实际版本。

```bash
uv sync
uv run python scripts/check_environment.py
sudo bash scripts/install_permissions.sh  # 仅限管理员审阅脚本与规则后的源码部署
```

普通 Dora 节点和 `snapshot` 启动时会检查 `libusb`、内嵌 udev 规则版本、`video`
用户组、冲突规则和当前 Orbbec USB 节点权限；检查失败会输出处理建议，但不会自动弹出
提权窗口。

`init-device` 是部署后二进制的短检查和初始化命令。环境已就绪时直接退出且不提权；需要
安装规则或配置用户组时才请求授权。为避免 `pkexec` 以 root 重新执行普通用户
可修改的代码，它只允许 **frozen 二进制位于 root-owned、且整条父目录链均不可由
组/其他用户写入的安装路径** 时请求 PolicyKit。不要直接对构建目录中的
`dist/orbbec_camera` 使用自动提权；先由管理员安装，再由实际相机用户运行：

```bash
sudo install -o root -g root -m 0755 dist/orbbec_camera /usr/local/bin/orbbec-camera
orbbec-camera init-device
```

privileged helper 只原子安装固定内嵌规则、将 `PKEXEC_UID` 对应的普通用户加入 `video`
组并 reload udev；不会调用包管理器，也不会删除检测到的其他 Orbbec udev 规则。完成后
需重新插拔设备，并注销后重新登录以刷新用户组。

`sudo bash scripts/install_permissions.sh` 是源码部署的显式管理员入口。运行前必须审阅
脚本和固定 udev 规则，且不得为用户可写路径配置免密 sudo。该入口固定系统命令搜索路径、
校验规则 SHA-256、拒绝可执行 udev 指令并安装同一份规则，然后根据 `SUDO_USER` 将实际
调用用户加入 `video` 组；它不会调用包管理器，系统必须事先安装 libusb 运行时。
源码构建 SDK 时才需要 `libusb-1.0-0-dev`。

官方工具验证应先使用 Orbbec Viewer 或 SDK 官方示例确认设备、固件、流 profile
和深度工作模式，再运行本项目。

## 基础命令

```bash
# CLI 帮助
uv run orbbec-camera --help
# 源码首次初始化见上方 install_permissions.sh；已安装的 frozen 二进制使用 init-device

# 列举设备
uv run orbbec-list-devices
uv run orbbec-camera list-devices --json

# 单帧采集
uv run python examples/python_capture_sample/run_capture_sample.py \
  --config config/sensor.example.yaml --all-streams

# Dora 节点
uv run orbbec-camera --config config/sensor.example.yaml
```

Python 独立示例见 `examples/python_list_devices` 和
`examples/python_capture_sample`。Dora 的可消费链路见
`examples/dora_sensor_stream`，其中 sink 会用 `PointCloudView` 解码点云、用
`Image`/`CompressedImage` 解码图像，并打印流信息和 UTC 接收时间。旧的
`examples/orbbec_camera_viewer` 仍保留用于图像可视化联调。

## 配置

完整示例位于 `config/sensor.example.yaml`。主要字段：

- `device_serial`/`device_index`：设备选择，优先使用 serial
- `color/depth/ir`：流开关、分辨率、帧率、传输格式和控制参数
- `align_mode`：`disable` 保持原始坐标系；`sw` 使用 SDK `AlignFilter`；
  `hw` 使用 SDK Config 硬件对齐，不支持时启动会明确失败
- `frame_sync`：多设备硬件同步
- `prewarm_frames`、`connect_delay_ms`、`init_timeout_sec`：启动行为
- `capture_process`：`isolated` 将 pyorbbecsdk 放入独立子进程，避免与
  Dora/Zenoh 共享文件描述符；Dora 节点推荐使用。`direct` 仅用于独立工具调试
- `point_cloud`：Forge PointCloud v1 开关、RGB 着色和可选 `frame_id`
- `output_color`、`output_depth`、`output_ir`、`output_point_cloud`：稳定的 Dora topic

当前不支持运行时配置更新。流 profile、设备、对齐和绝大多数控制参数需修改 YAML
后重启节点。

## 输出语义

| topic | 消息 | 编码/单位 |
| --- | --- | --- |
| `image/color` | `Image` 或 `CompressedImage` | `rgb8` 或 JPEG |
| `image/depth` | `Image` | `32FC1`，米 |
| `image/ir` | `Image` | `mono8`，兼容 `16UC1` |
| `point_cloud` | Forge `PointCloud` v1 | organized float32 XYZ（米）及可选 uint8 RGB |

点云输出默认关闭；默认 topic 和配置契约如下：

```yaml
output_point_cloud: point_cloud
point_cloud:
  enabled: false
  colorize: true
  frame_id: null
```

启用 `point_cloud.enabled` 必须同时启用 Depth。`align_mode=disable` 配合
`colorize:false` 输出 XYZ-only，坐标位于 Depth optical frame；`colorize:true` 还必须
启用 Color，并要求 `align_mode=sw` 或 `hw` D2C，XYZ 坐标位于 Color optical frame。
彩色点云建议同时设置 `frame_sync:true`；否则空间对齐仍有效，但动态场景中的 Color/Depth
时间对应仅为 best-effort。点云是 organized 网格，XYZ 数组均为 float32 米制坐标；无效槽位的 X、Y、Z 全部为
NaN。着色时 R/G/B 数组为 uint8，无效点为黑色；不着色时 RGB 数组为空。

消费端可使用 `PointCloudView.from_arrow(event["value"])`。`capture_process=isolated`
会通过 multiprocessing IPC/pickle 将帧从采集子进程拷贝到父进程；即使消费端本地
NumPy/PyArrow 视图可保持低拷贝，也不表示 Dora 链路端到端 zero-copy。

Dora 图像与点云输出会尽力附加用户 metadata `capture_timestamp_ns`：它是采集后端取得
该 FrameSet 时记录的 Unix epoch 纳秒时间，用于关联同一次采集的 Color / Depth / IR /
PointCloud。该字段是可选的最佳估计，不替代 Dora 管理的消息时间戳；缓存、编码和发送
过程不会重新生成它。`point_cloud.frame_id` 非 `null` 时，点云还会附加可选用户
metadata `frame_id`。后端日志中的 `timestamp_ms` 是 SDK 帧时间戳，仅用于诊断。

## 可选发布可观测性

仅当节点启动时环境变量 **`FORGE_OBSERVABILITY=1`** 才启用；未设置、`0`、`true`
或其他值均关闭。默认路径不采样可观测时钟，Color / Depth / IR / PointCloud 的 payload、
业务 metadata 和既有发送失败行为保持不变。

启用后，每个实际输出都严格通过 Forge Common 2.1 公共
`Observer.publish(..., new_origin=True, ...)` 创建独立 producer origin。Color、Depth、IR、
PointCloud 分别使用稳定低基数 origin ID：`orbbec_camera.color`、
`orbbec_camera.depth`、`orbbec_camera.ir`、`orbbec_camera.point_cloud`。每个 payload 的
`forge_origin_time_ns` 和 `forge_publish_time_ns` 都在该 payload 的发布边界生成，不表示整个
FrameSet 的共同生产时间；同一 FrameSet 的跨输出关联继续由业务 metadata
`capture_timestamp_ns` 表达。点云的可选 `frame_id` 也保持不变。相机不会观察或继承 Dora
tick 中的 origin，因此 tick 只负责触发发送。

观测时钟、指标记账或指标日志异常会降级到业务发送，不阻断相机输出。Dora 发送失败仍按原
语义处理：图像发送异常继续传播，点云发送异常仍告警并允许图像流继续；不会因观测而重试。
进程内使用固定 series 上限的聚合器，每 5 秒输出并清空一次
`observability interval` JSON，只以稳定 output topic 作为 label，不保留帧 ID、origin ID、
设备信息或逐事件记录。关闭节点时会 best-effort 尝试输出最后一个区间快照；短超时退出或
reporter 异常时不保证该快照一定写出。

```bash
FORGE_OBSERVABILITY=1 uv run orbbec-camera --config config/sensor.example.yaml
```

确保 Dora runner 将变量传入相机进程；不设置即可保持兼容模式。日志仅保存在当前运行环境，
本项目不会自行上传指标。跨机器使用 wall-clock 时间计算链路延迟时，需要部署方保证时钟同步。

SDK 提供的 Depth 原始像素会结合 `get_depth_scale()` 归一为毫米，公共消息层再除以
1000 转为米。快照 PNG 仍保存 uint16 毫米值，不能与 Dora `32FC1` 直接混用。

## 打包与部署

```bash
bash scripts/build_pyinstaller.sh
```

产物为 `dist/orbbec_camera`。公开归档只包含该二进制；项目及第三方许可证已嵌入，
可通过 `orbbec_camera licenses` 查看。spec 会从已安装的 wheel 收集
`libOrbbecSDK.so*`、Python 扩展、`extensions/` 和固定 udev 规则，运行时 hook 设置
bundle 内动态库搜索路径。安装到上述可信系统路径后可执行 `init-device`；构建目录中的
用户自有产物会拒绝自动提权，这是预期安全行为。构建脚本
使用 `uv.lock`、公开 PyPI 依赖和隔离的 `.venv_build` 同步依赖；锁文件过期会直接失败。构建机和目标机必须使用兼容架构与 libc。

## 常见问题

- 未发现设备：运行已安装二进制的 `orbbec-camera init-device`，或直接启动相机查看 preflight；根据 ACTION 安装规则/配置用户组，重新插拔并重新登录。
- 检测到其他包含 vendor `2bc5` 的 udev 规则：由管理员核对规则优先级和权限；工具只报告，
  不会自动删除系统规则。
- `libusb`/动态库加载失败：运行 `scripts/check_environment.py`，确认系统运行时和
  wheel 架构匹配。
- `uvc_open` 失败或设备忙：确认没有残留采集进程；必要时正常结束旧进程并重新插拔。
- profile 不支持：用官方 Viewer 确认当前型号/固件提供的分辨率、帧率和格式。
- 深度全零：检查工作模式、曝光、有效范围和 `depth_unit`，并用已知距离实测。

## 数据与隐私

本项目默认不提供遥测，也不会自行上传相机数据。Color、Depth、IR 和点云帧会发送到
当前 Dora dataflow 配置的接收方；部署者需要自行确认 Dora/Zenoh 的网络边界和访问控制。
`snapshot` 会将图像写入调用者指定的位置，设备枚举和日志可能包含设备序列号、固件和
USB 信息。

相机图像、红外图、深度图和设备序列号都应按敏感数据处理。采集包含人员、屏幕、文档
或私人场所的数据前，应获得适用的授权并制定保留、访问和删除策略。

不要把个人绝对路径、设备录包、真实场景图像、私有 SDK、设备序列号或密钥提交到仓库。


## Forge 2 / Dora 1 迁移

目标版本：`2.0.0`，当前为待验收改动。首轮验证组合使用 Dora 1.0.1、Forge Msgs 2.0.0、Common 2.1.0；依赖与构建工具以提交的锁文件为准。原有消息规范继续使用 `forge_msgs.v1`。

本轮要求：检查所有示例最终 YAML，使用 `dora validate <dataflow.yaml>` 验证，并实际执行消息往返。相机取流、资源释放和异常退出须分别验收；高吞吐图像链预热后连续测试至少 4 小时，覆盖慢消费者、扇出、退出与重启，记录实际内存占用。无硬件单测不能替代设备验收。

最终发布归档须先在声明支持的平台完成验收，记录源码与归档、可执行文件校验值；通过后发布同一份包，重建后重新验收。Linux 发布包仍需 Linux 验证，其他平台的开发测试不代表其已通过。

图像消费示例新增 `min_frames` 与 `expected_inputs` 配置；无帧、坏帧、缺少必需流或 Dora 错误均判失败。

节点初始化失败时也会关闭采集后端和观察器；编码线程异常会向主循环报告失败，避免流静默停滞。
