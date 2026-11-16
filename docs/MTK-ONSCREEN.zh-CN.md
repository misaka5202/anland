# MTK 上屏排查记录

[English](MTK-ONSCREEN.md)

验证环境：OPPO PHZ110（MediaTek MT6989，Mali-G720-Immortalis MC12），
`6.1.157-android14-11`，Android 17，Droidspaces 7.2.0。

## 结果

容器内运行的 Wayland 客户端，经 libhybris + EGL + 厂商 Mali 驱动、
走 `zwp_linux_dmabuf_v1`，渲染结果出现在 Android 屏幕上。用全屏品红实测：

| 阶段 | 品红像素 | 区域 |
|---|---|---|
| 客户端运行中 | 48504 / 97904（49.5%） | 1026 × 1686 |
| 客户端退出后 | 0 / 97904（0%） | — |

合成器持有 `/dev/mali0`，映射了
`/vendor/lib64/egl/mt6989/libGLES_mali.so`；未加载 `llvmpipe`、`swrast` 或
`softpipe`。

## 本设备上不成立的部分

### `zwp_linux_dmabuf_v1` 的缓冲区无法伪造成 `AHardwareBuffer`

`awl_ahb.cpp` 把一个客户端 dma-buf 包装成 `AHardwareBuffer`，方式是伪造
Qualcomm snapalloc 产生的 `native_handle` 布局。那个布局是**两个** fd。

`mtk_alloc` 分配的缓冲区报**三个**：

```
anland-rd: donor handle layout unexpected (numFds=3) — not a snapalloc layout, refusing
anland-rd: blob offsets not calibrated — dmabuf import refused (no fallback)
```

本设备的 stride oracle 返回 0，被当作「无判定」，于是调用方保持硬件合成路径
并在每一帧重试伪造。设置 `AWL_NO_SCANOUT=1` 后 `awl_ahb_hwc_scanout_ok()`
返回 false，这些重试被跳过。

### SurfaceControl 合成器不呈现客户端内容

`sc_enabled: 1`（默认值）时，`awl_sc.cpp` 收到客户端缓冲区、伪造失败，
其 EGL 回退不产生任何可见输出。客户端渲染本身是成功的 ——
`eglInitialize` 与 `eglSwapBuffers` 都返回成功 —— 但屏幕上什么都没有。

### GL renderer 能呈现

`sc_enabled: 0` 时，`awl_renderer.cpp` 走同一条伪造失败的路径，然后回退到
`awl_gl_tex_import_dmabuf()`，用 `EGL_EXT_image_dma_buf_import` +
`glEGLImageTargetTexture2DOES` 导入 dma-buf。这条路径有效，内容被合成到屏幕。

## 必须同时满足的三个条件

三者缺一，表现各不相同，看起来像三个不同的问题。

| 条件 | 缺失时的表现 |
|---|---|
| 加载 `mali_nsfix` | `eglInitialize` 返回 `0x3001`；`dmesg` 出现 `mali 13000000.mali: Failed to get pid pointer for <comm>/<pid>` 与 `Common context initialization failed error = -3` |
| `AWL_NO_SCANOUT=1` | 每帧重试 AHB 伪造，每帧输出两条 `anland-rd` 错误 |
| `sc_enabled: 0` | SurfaceControl 合成器取走缓冲区但不呈现任何内容 |

`mali_nsfix` 来自 `misaka5202/droidspaces-lkm`。`kbase_create_context()` 调用
`find_get_pid(current->tgid)`；`tgid` 永远是初始命名空间的值，而 `find_get_pid()`
经由调用者的活动命名空间解析，容器进程的查找因此失败。该模块重定向这次调用。
详见该仓库 `extra/mali_nsfix/README.md`。

## 配置

`/data/adb/modules/anland-awl/config.json`：

```json
{
  "sc_enabled": 0,
  "runtime_dir": "/data/local/tmp/awl",
  "socket_listen": 1
}
```

守护进程必须在带 `AWL_NO_SCANOUT=1` 的环境下启动。该变量在每帧的
`awl_ahb_hwc_scanout_ok()` 中读取，因此只影响导出之后启动的进程。

## 复现

容器内：

```sh
export XDG_RUNTIME_DIR=/run/user/1000
export WAYLAND_DISPLAY=wayland-anland
. /opt/hy/mali-gpu.env
export HYBRIS_EGLPLATFORM=wayland
export HYBRIS_WAYLAND_SWAP_RB=1
./wl_egl_probe
```

`wl_egl_probe` 创建 `xdg_toplevel`、绑定 `wl_egl_window`、渲染品红并保持 15 秒。
必须使用 `eglSwapInterval(0)`：使用非零间隔时 `finishSwap` 会等待一个本合成器
从不发出的帧回调，进程无限阻塞。

验证方式：在客户端运行时截一次屏，退出后再截一次。正确结果是第一次有
大片颜色、第二次没有。**只截一次不够** —— 先前被强杀的客户端留下的窗口
会产生假阳性。

## 未解的问题

- SurfaceControl 合成器为何不呈现。它的 EGL 回退调用的是与 renderer 相同的
  `awl_gl_tex_import_dmabuf()`，却不产生任何可见输出。差异应该发生在导入之后。
- `sc_enabled: 0` 是否应作为 MTK 的默认值，或 SurfaceControl 路径能否修好。
  改默认值会影响所有 MTK 设备，需要有**被理解的原因**，而不只是一个被绕过的现象。
- 用 `wl_shm` 而非 dma-buf 的客户端是否会走 GL 路径。桌面会话
  （`anland-session`、`Xwayland -rootless`、`anland-miniwm`）自身不加载任何
  GL 库，其缓冲区经 `wl_shm` 到达合成器，由 `glTexImage2D` 上传；那条路径在
  任何 GL 后端上都有效，不能用来判断 dma-buf 处理是否正确。