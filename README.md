[English](README.md) | [中文](README_zh.md)

# Orbbec camera

## BREAKING CHANGE: Forge 2.x / Dora 1.x migration

This migration from Forge 1.x / Dora 0.4.x requires coordinated deployment. Align the Forge packages, Dora CLI/coordinator/daemon, this node and its peer nodes before connecting a dataflow; replacing only one node in a Forge 1.x deployment is unsupported. Rebuild and redeploy executable artifacts with the matching environment. Keep `stable/1.x` for deployments that still use the previous stack.

The validated Python baseline is `forge-msgs==2.1.0`, `Pillow==13.0.0`, `forge-common==2.1.0`, the applicable `forge-robot` / `forge-policy` / `forge-kinematics` / `forge-tool` packages at `2.0.0`, and `dora-rs==1.0.1`. Respect this repository's Python and SDK platform requirements. Forge package families have independent versions. Rust nodes retain the published `forge_msgs` crate `2.0.0` with Dora `1.0.1`; Python Msgs `2.1.0` does not require a nonexistent Rust `2.1.0` release. The `forge_msgs.v1` message namespace remains unchanged.

This notice describes the deployment break from the previous stack; it does not declare hardware, real-model or sustained-run acceptance complete. See [this round's verification report](migration/msgs-2.1.0-report.json) for actual tests and outstanding gates.

Orbbec Gemini RGB, metric depth, infrared and optional organized point-cloud capture.

## Migration status

Target node version: **2.0.0**. These changes are candidates pending the platform,
hardware and sustained-stream acceptance gates below. A version declaration does not
mean a stable release has been accepted.

Startup failures now close the capture backend and observer, including failed Dora registration and encoder-thread startup. Encoder failures terminate the node instead of leaving a stalled stream.

## Runtime and platform

- First validation combination: Dora CLI/coordinator/daemon/node APIs 1.0.1,
  Forge Msgs 2.1.0 and Common 2.1.0; use only the families required by this node.
- Forge wire schemas remain `forge_msgs.v1`. Dora 0.x and 1.x cannot share a runtime.
- Use Python 3.12 for Python builds. Camera-only Python packages may also declare
  Python 3.11 support; test that interpreter separately before claiming it.
- Rust packages enable `arrow-v59`; the committed lock uses Arrow 59.3.0.
- Hardware camera/robot artifacts target Linux. macOS development tests do not
  qualify Linux binaries. GPU/rendering and vendor SDK prerequisites still apply.
- Keep a recoverable `stable/1.x` runtime and artifact combination.

## Setup and commands

Run from the repository root:

```sh
uv sync --frozen --python 3.12
uv run --frozen orbbec-camera --config config/sensor.example.yaml
```

Use `orbbec-camera --help` (or `uv run --frozen orbbec-camera --help`) for supported CLI options.
The example configuration is `config/sensor.example.yaml`; choose the actual device and supported
stream profiles before starting. This command requires an appropriate Dora dataflow.

## Capture readiness and failures

`init_timeout_sec` bounds each SDK initialization attempt, stream setup,
`prewarm_frames` and receipt of the first usable frame. Readiness requires
color (RGB or JPEG) plus all enabled depth/IR streams; optional point-cloud
conversion does not block it.
In `capture_process: isolated` mode, the worker reports ready only after queuing
that first frame. It can reopen the Pipeline twice after recoverable failures,
with a 1-second pause. The parent's total startup deadline covers all three
attempts plus another 5 seconds for process startup/IPC.

After readiness, 5 seconds without a complete usable frame triggers a Pipeline
reopen. Empty or incomplete frames do not refresh this deadline. Readers reject
old cached frames while recovery is in progress, and frame sequences remain
monotonic across reopens. The worker can retry twice within a short failure
period; its retry budget resets after 60 seconds of healthy delivery. Device
disconnects and exhausted retries remain errors. A short `wait_new_frame`
timeout may still return the current frame with its unchanged sequence before
the stall deadline.

Shutdown closes the camera before discarding pending image packets, so an unread
large RGBD packet cannot hold the worker open on a requested stop. Forced process
termination is logged; a backend that still cannot stop raises an error.

The Linux binary build uses the Python bindings from `pyorbbecsdk2==2.1.3`
and compiles the pinned native SDK 2.10.6 with a Gemini 2 bulk Y16 patch.
The patch checks UVC headers and frame sizes before handing frames to the SDK.
The SDK logs rejected payload and frame counts on shutdown.
Each tick publishes at most one RGBD set. The publisher retains two encoded
sets and drops the oldest if the encoder gets further ahead.

## Message contract

RGB: Image rgb8 or JPEG CompressedImage; depth: Image 32FC1 in metres; IR: mono8/16UC1. Point cloud is optional.

Keep bounded queues for high-rate image consumers and document whether old images
are dropped or producers experience backpressure. Preserve image dimensions, dtype,
encoding, units and capture metadata across the node boundary.

## Tests and build

```sh
uv run --frozen pytest tests -q
bash scripts/build_pyinstaller.sh
```

The binary build needs CMake, a C/C++ toolchain, pkg-config, and access to the
pinned Orbbec SDK source. To test the native patch from a Linux Python checkout,
run `bash scripts/build_native_sdk.sh build/native-sdk` and launch the camera
with `LD_PRELOAD` set to the absolute path to `build/native-sdk/lib/libOrbbecSDK.so.2`.
A regular `uv sync` installs the upstream wheel.

Use the committed `uv.lock` and frozen/locked installs. Python vendor SDK tests require
the supported SDK platform. For Linux headless MuJoCo rendering, install the required
EGL or OSMesa libraries and select the appropriate `MUJOCO_GL` backend.

Validate every delivered example or rendered template with:

```sh
dora validate path/to/dataflow.yaml
```

Schema validation is followed by a real start, message exchange and stop test.
Keep the existing detailed configuration and device setup reference in
[the Chinese guide](README_zh.md) and the repository's `config/`, `examples/` and
`scripts/` directories where present.

## Acceptance and release

1. Build candidate archives from the selected commit/tag and record actual embedded
   dependency versions, platform, archive SHA256 and unpacked executable SHA256.
2. Install the final archive into a clean supported environment. Verify the CLI,
   message contract, resource cleanup and error exits.
3. For image producers, run real supported RGB/depth configurations for at least
   four continuous hours after warmup. Exercise slow/multiple consumers, stalled
   consumption, failures and repeated start/stop. Define throughput, latency and
   memory limits before testing, and retain time series rather than frame payloads.
4. Exercise the relevant device; synthetic streams do not qualify hardware capture.
   Demonstrate rollback to the complete old node/runtime combination.
5. Publish the exact accepted archive. Rebuilding invalidates that artifact's
   acceptance; repeat it before publication. Never overwrite published tags/assets.

## License

Apache-2.0. See `LICENSE` and any bundled third-party notices.
