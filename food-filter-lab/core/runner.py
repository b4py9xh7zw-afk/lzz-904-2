"""ffmpeg 执行器：argv 列表调用、绝对路径二进制、限时、限大小。"""
from __future__ import annotations

import os
import shutil
import subprocess

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BIN = os.path.join(BASE_DIR, "bin")
FFMPEG = os.path.join(_BIN, "ffmpeg")
FFPROBE = os.path.join(_BIN, "ffprobe")

# 允许上传的容器（ffprobe 仍会再验证文件是否真的是视频）
ALLOWED_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024     # 200MB
MAX_DURATION_SEC = 600                    # 最长 10 分钟
PREVIEW_WIDTH = 960


class FFmpegError(RuntimeError):
    pass


def check_tools() -> None:
    for tool in (FFMPEG, FFPROBE):
        if not (os.path.isfile(tool) and os.access(tool, os.X_OK)):
            raise RuntimeError(f"找不到可执行文件: {tool}")


def probe_duration(path: str) -> float:
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, timeout=30)
    if out.returncode != 0:
        raise FFmpegError("ffprobe 无法解析该文件（可能不是有效视频）")
    try:
        return float(out.stdout.strip())
    except ValueError:
        raise FFmpegError("无法读取视频时长")


def extract_frames(src: str, duration: float, out_dir: str,
                   n: int = 3) -> list[float]:
    """等间距抽取 n 帧原图（缩到 PREVIEW_WIDTH），返回对应的时间点。"""
    os.makedirs(out_dir, exist_ok=True)
    # 在 10%~90% 之间取点，避开纯黑片头/片尾
    if duration <= 0:
        times = [0.0] * n
    else:
        times = [duration * (0.1 + 0.8 * i / max(1, n - 1)) for i in range(n)]

    for i, t in enumerate(times):
        out = os.path.join(out_dir, f"orig_{i}.jpg")
        cmd = [
            FFMPEG, "-nostdin", "-y", "-ss", f"{t:.3f}", "-i", src,
            "-frames:v", "1",
            "-vf", f"scale={PREVIEW_WIDTH}:-2",
            "-q:v", "3", out,
        ]
        _run(cmd, timeout=60)
    return times


def make_preview(src_frame: str, filterchain: str, out_path: str) -> None:
    """对单帧原图施加白名单滤镜链。filterchain 由 filters.py 生成。"""
    cmd = [
        FFMPEG, "-nostdin", "-y", "-i", src_frame,
        "-vf", filterchain, "-frames:v", "1", "-q:v", "3", out_path,
    ]
    _run(cmd, timeout=60)


def render(src: str, filterchain: str, out_path: str,
           preset_name: str) -> None:
    """整片渲染。filterchain 只作用于视频流，音频直接流复制/转 AAC。"""
    tmp = out_path + ".part.mp4"
    cmd = [
        FFMPEG, "-nostdin", "-y", "-i", src,
        "-filter_complex", f"[0:v]{filterchain}[v]",
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "128k",
        "-t", str(MAX_DURATION_SEC),
        tmp,
    ]
    try:
        _run(cmd, timeout=300)
        shutil.move(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    # 关键安全点：shell=False（默认），参数为 argv 列表，不经任何 shell 解析
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, shell=False,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-8:]
        raise FFmpegError("ffmpeg 执行失败:\n" + "\n".join(tail))
    return proc
