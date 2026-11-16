#!/usr/bin/env python3
"""
给 anland 的 waylandbridge 加一条「原生 dma-buf → 纹理」路径，绕过 QCOM 专属的
AHardwareBuffer 伪造。

背景：
  awl_ahb.cpp 的 awl_ahb_wrap() 要求 donor AHardwareBuffer 的 handle 恰好 2 个 fd
  （高通 snapalloc 布局：fd[0]=像素 dma-buf，fd[1]=28KB metadata blob）。MTK 的
  gralloc handle 是 3 个 fd，donor_blob_fd() 直接判定「不是 snapalloc 布局」返回 -1，
  然后 "no fallback" 拒绝导入 —— 于是客户端的 dma-buf 永远进不来，Mali 渲染的东西
  到不了屏幕。

  但渲染器的**输出**是普通的 ANativeWindow（MTK 本来就支持），伪造的 AHB 只用于把
  客户端的 dma-buf 采样成纹理。所以这里不需要伪造：用标准的
  EGL_EXT_image_dma_buf_import + zwp_linux_dmabuf 单 plane 参数直接建 EGLImage 即可，
  对任何 SoC 都通用。

改动三处：
  1. awl_gl.hpp  —— 声明 awl_gl_tex_import_dmabuf
  2. awl_gl.cpp  —— 用 EGL_LINUX_DMA_BUF_EXT 实现它
  3. awl_renderer.cpp —— AHB 路径失败时回退到它
"""
import re
import sys
from pathlib import Path

WB = Path(__file__).resolve().parent
if not (WB / "awl_gl.cpp").exists():
    # 允许从仓库根运行
    WB = WB / "services" / "waylandbridge"
assert (WB / "awl_gl.cpp").exists(), f"找不到 waylandbridge 源目录：{WB}"

changed = []


def read(p):
    return p.read_text(encoding="utf-8", errors="surrogateescape")


def write(p, s):
    p.write_text(s, encoding="utf-8", errors="surrogateescape")


# ---------------------------------------------------------------------------
# 1. awl_gl.hpp —— 声明新函数
# ---------------------------------------------------------------------------
hpp = WB / "awl_gl.hpp"
s = read(hpp)
if "awl_gl_tex_import_dmabuf" not in s:
    anchor = "bool awl_gl_tex_import(AHardwareBuffer* ahb, awl_gl_tex* out);"
    assert anchor in s, "awl_gl.hpp 锚点不在"
    add = anchor + """

/* 直接从客户端 dma-buf 建 EGLImage + 纹理（EGL_EXT_image_dma_buf_import）。
 * 不经过 AHardwareBuffer，因此不依赖高通的 snapalloc 伪造，MTK 等平台可用。
 * fd 由调用方持有；本函数只在创建期间使用，不接管所有权。 */
bool awl_gl_tex_import_dmabuf(int fd, uint32_t width, uint32_t height,
                              uint32_t stride, uint32_t drm_format,
                              uint64_t modifier, awl_gl_tex* out);"""
    s = s.replace(anchor, add, 1)
    write(hpp, s)
    changed.append("awl_gl.hpp: 声明 awl_gl_tex_import_dmabuf")

# ---------------------------------------------------------------------------
# 2. awl_gl.cpp —— 实现
# ---------------------------------------------------------------------------
cpp = WB / "awl_gl.cpp"
s = read(cpp)
if "awl_gl_tex_import_dmabuf" not in s:
    anchor = "void awl_gl_tex_release(awl_gl_tex* t) {"
    assert anchor in s, "awl_gl.cpp 锚点不在"
    impl = r"""
/* ---------------- dma-buf → texture（不经过 AHB） ---------------- */

bool awl_gl_tex_import_dmabuf(int fd, uint32_t width, uint32_t height,
                              uint32_t stride, uint32_t drm_format,
                              uint64_t modifier, awl_gl_tex* out) {
    if (fd < 0 || width == 0 || height == 0) return false;
    if (!g.eglCreateImageKHR || !g.glEGLImageTargetTexture2DOES) {
        LOGE("dmabuf import: EGL/KHR image entry points missing");
        return false;
    }

    /* DRM fourcc 直接就是 EGL_LINUX_DRM_FOURCC_EXT 要的值 */
    EGLint attrs[] = {
        EGL_WIDTH,                          (EGLint)width,
        EGL_HEIGHT,                         (EGLint)height,
        EGL_LINUX_DRM_FOURCC_EXT,           (EGLint)drm_format,
        EGL_DMA_BUF_PLANE0_FD_EXT,          (EGLint)fd,
        EGL_DMA_BUF_PLANE0_OFFSET_EXT,      0,
        EGL_DMA_BUF_PLANE0_PITCH_EXT,       (EGLint)stride,
        /* 修饰符只在非 INVALID 时给；LINEAR 也要给，否则部分驱动当成 tiled */
        EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT, (EGLint)(modifier & 0xffffffffu),
        EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT, (EGLint)(modifier >> 32),
        EGL_NONE
    };

    EGLImageKHR img = g.eglCreateImageKHR(g.display, EGL_NO_CONTEXT,
                                          EGL_LINUX_DMA_BUF_EXT, (EGLClientBuffer)NULL,
                                          attrs);
    if (img == EGL_NO_IMAGE_KHR) {
        LOGE("eglCreateImageKHR(dma_buf) failed 0x%x (fmt=0x%08x %ux%u stride=%u mod=0x%llx)",
             eglGetError(), drm_format, width, height, stride,
             (unsigned long long)modifier);
        return false;
    }

    GLuint tex = 0;
    glGenTextures(1, &tex);
    glBindTexture(GL_TEXTURE_2D, tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    g.glEGLImageTargetTexture2DOES(GL_TEXTURE_2D, img);
    GLenum terr = glGetError();
    if (terr != GL_NO_ERROR) {
        LOGE("EGLImageTargetTexture(dma_buf): 0x%x", terr);
        glDeleteTextures(1, &tex);
        g.eglDestroyImageKHR(g.display, img);
        return false;
    }
    out->image = img;
    out->texture = tex;
    return true;
}

"""
    s = s.replace(anchor, impl + anchor, 1)
    write(cpp, s)
    changed.append("awl_gl.cpp: 实现 awl_gl_tex_import_dmabuf")

# ---------------------------------------------------------------------------
# 3. awl_renderer.cpp —— AHB 失败时回退
# ---------------------------------------------------------------------------
rnd = WB / "awl_renderer.cpp"
s = read(rnd)

if "g_dmtex" not in s:
    # 3a. 在文件里加一个按 surface 缓存直连纹理的容器（只在这个翻译单元用）
    anchor = "static void render_thread_loop(wl_window* w);   /* defined at end of file */"
    assert anchor in s, "awl_renderer.cpp 锚点 1 不在"
    s = s.replace(anchor, anchor + """

/* 直连 dma-buf 纹理的缓存：surface_id → awl_gl_tex。
 * 只在高通专属的 AHB 伪造失败时使用（MTK 等平台）。渲染线程独占访问。 */
#include <unordered_map>
static std::unordered_map<uint64_t, awl_gl_tex> g_dmtex;
static std::unordered_map<uint64_t, uint64_t>   g_dmtex_ino;""", 1)

    # 3b. 在 AHB 取纹理失败处插入回退
    old = """            struct awl_ahb_slot* s = l.cache
                ? awl_ahb_cache_get(l.cache, b, AHARDWAREBUFFER_USAGE_GPU_SAMPLED_IMAGE, w->frame_no)
                : nullptr;
            if (s) tex = awl_gl_slot_texture(s);   /* 0 = forge/import refused — retry next frame */"""
    assert old in s, "awl_renderer.cpp 锚点 2 不在"
    new = old + """
                if (!tex) {
                    /* AHB 伪造被拒（非高通平台）→ 直接用 EGL_EXT_image_dma_buf_import。
                     * 同一 inode 命中缓存，避免每帧重建 EGLImage。 */
                    awl_gl_tex& dt = g_dmtex[lay[i].surface_id];
                    bool need = (dt.texture == 0) || (g_dmtex_ino[lay[i].surface_id] != b->ino);
                    if (dt.texture && need) { awl_gl_tex_release(&dt); }
                    if (need && awl_gl_tex_import_dmabuf(b->dmabuf_fd, b->width, b->height,
                                                         b->stride, b->format,
                                                         DRM_FORMAT_MOD_LINEAR, &dt)) {
                        g_dmtex_ino[lay[i].surface_id] = b->ino;
                        static bool logged = false;
                        if (!logged) { logged = true;
                            LOGI("dmabuf 直连导入生效（绕过 AHB 伪造，fmt=0x%08x %ux%u）",
                                 b->format, b->width, b->height); }
                    }
                    if (dt.texture) tex = dt.texture;
                }"""
    s = s.replace(old, new, 1)
    write(rnd, s)
    changed.append("awl_renderer.cpp: AHB 失败时回退到直连 dmabuf")

print("改动：")
for c in changed:
    print("  -", c)
if not changed:
    print("  (无，已打过)")