# MTK bring-up: getting Mali content on screen

[中文](MTK-ONSCREEN.zh-CN.md)

Status: verified on OPPO PHZ110 (MediaTek MT6989, Mali-G720-Immortalis MC12),
`6.1.157-android14-11`, Android 17, Droidspaces 7.2.0.

## Result

A Wayland client running inside the container, using libhybris + EGL + the
vendor Mali driver over `zwp_linux_dmabuf_v1`, renders and its output appears on
the Android display. Measured with a full-screen magenta draw:

| Phase | Magenta pixels | Region |
|---|---|---|
| client running | 48504 / 97904 (49.5%) | 1026 × 1686 |
| client exited | 0 / 97904 (0%) | — |

The compositor holds `/dev/mali0` open and has
`/vendor/lib64/egl/mt6989/libGLES_mali.so` mapped; no `llvmpipe`, `swrast` or
`softpipe` is loaded.

## What does not work on this device

### `zwp_linux_dmabuf_v1` buffers cannot be forged into `AHardwareBuffer`

`awl_ahb.cpp` wraps a client dma-buf into an `AHardwareBuffer` by fabricating the
`native_handle` layout that Qualcomm's snapalloc produces. That layout has two
file descriptors.

`mtk_alloc`-backed buffers report **three**:

```
anland-rd: donor handle layout unexpected (numFds=3) — not a snapalloc layout, refusing
anland-rd: blob offsets not calibrated — dmabuf import refused (no fallback)
```

The stride oracle returns 0 on this device, which is reported as "no verdict",
so the caller keeps the hardware-compositor path and retries the forge on every
frame. With `AWL_NO_SCANOUT=1` set, `awl_ahb_hwc_scanout_ok()` returns false and
those attempts are skipped.

### The SurfaceControl compositor does not present client content

With `sc_enabled: 1` (the default), `awl_sc.cpp` receives the client buffer,
fails the AHB forge, and its EGL fallback produces no visible output. The client
renders successfully — `eglInitialize` and `eglSwapBuffers` both succeed — but
nothing reaches the display.

### The GL renderer does present it

With `sc_enabled: 0`, `awl_renderer.cpp` takes the same failed-forge path, then
falls back to `awl_gl_tex_import_dmabuf()`, which imports the dma-buf through
`EGL_EXT_image_dma_buf_import` + `glEGLImageTargetTexture2DOES`. That path works
and the content is composited to the display.

## Required conditions

Three things must all hold, and each one fails in a way that looks like a
different bug.

| Condition | Without it |
|---|---|
| `mali_nsfix` loaded | `eglInitialize` returns `0x3001`; `dmesg` shows `mali 13000000.mali: Failed to get pid pointer for <comm>/<pid>` and `Common context initialization failed error = -3` |
| `AWL_NO_SCANOUT=1` | the AHB forge is retried every frame, logging two `anland-rd` errors per frame |
| `sc_enabled: 0` | the SurfaceControl compositor consumes the buffer and presents nothing |

`mali_nsfix` is the module from `misaka5202/droidspaces-lkm`.
`kbase_create_context()` calls `find_get_pid(current->tgid)`; `tgid` is always
an initial-namespace value while `find_get_pid()` resolves through the caller's
active namespace, so the lookup fails for a container process. The module
redirects that call. See `extra/mali_nsfix/README.md` in that repository.

## Configuration

`/data/adb/modules/anland-awl/config.json`:

```json
{
  "sc_enabled": 0,
  "runtime_dir": "/data/local/tmp/awl",
  "socket_listen": 1
}
```

The daemon must be started with `AWL_NO_SCANOUT=1` in its environment. It is
read at `awl_ahb_hwc_scanout_ok()` on each frame, so setting it only affects
processes started after the export.

## Reproducing

Inside the container:

```sh
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-anland
. /opt/hy/mali-gpu.env
export HYBRIS_EGLPLATFORM=wayland
export HYBRIS_WAYLAND_SWAP_RB=1
./wl_egl_probe
```

`wl_egl_probe` creates an `xdg_toplevel`, binds a `wl_egl_window`, renders
magenta, and holds the frame for 15 seconds. `eglSwapInterval(0)` is required:
with a non-zero interval `finishSwap` waits for a frame callback that this
compositor never delivers, and the process blocks indefinitely.

To verify, capture the screen while the client is running and again after it
exits. A correct result is a large area of colour in the first capture and none
in the second. Capturing only once is not sufficient — a window left behind by a
previously killed client produces a false positive.

## Open questions

- Why the SurfaceControl compositor presents nothing. Its EGL fallback runs the
  same `awl_gl_tex_import_dmabuf()` code as the renderer, yet produces no
  visible output. The difference is presumably downstream of the import.
- Whether `sc_enabled: 0` should be the MTK default, or whether the SurfaceControl
  path can be repaired. Switching the default changes behaviour for every MTK
  device, so it needs a reason that is understood, not just a symptom that is
  worked around.
- Whether a client using `wl_shm` rather than dma-buf ever exercises the GL
  path. The desktop session (`anland-session`, `Xwayland -rootless`,
  `anland-miniwm`) loads no GL library of its own, so its buffers reach the
  compositor through `wl_shm` and are uploaded with `glTexImage2D`; that path
  works on any GL backend and says nothing about dma-buf handling.