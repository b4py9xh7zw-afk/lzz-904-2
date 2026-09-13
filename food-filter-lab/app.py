"""美食短片滤镜实验台 —— Flask 后端。

安全边界速览：
- 上传：扩展白名单 + 大小上限 + ffprobe 实测，文件名由服务端 token 重写。
- 入参：只接受 id / frame / preset / saturation / contrast，多一个字段都 400。
- 滤镜：core/filters.py 白名单引擎生成，用户无法提交 vf/filter_complex/shell。
- 执行：argv 列表 + shell=False + 绝对路径 ffmpeg + 超时。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex

from flask import (Flask, jsonify, request, send_from_directory, abort,
                   render_template)

from core.filters import (PRESETS, PRESET_ORDER, SATURATION_USER,
                          CONTRAST_USER, resolve_params, build_filterchain,
                          FilterError)
from core import runner

BASE = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE, "uploads")
TMP_DIR = os.path.join(BASE, "tmp")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = runner.MAX_UPLOAD_BYTES

ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
ALLOWED_BODY = {"id", "frame", "preset", "saturation", "contrast"}
N_FRAMES = 3


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/presets")
def list_presets():
    return jsonify({
        "presets": [
            {"id": k, "label": PRESETS[k]["label"], "desc": PRESETS[k]["desc"]}
            for k in PRESET_ORDER
        ],
        "bounds": {
            "saturation": SATURATION_USER,
            "contrast": CONTRAST_USER,
        },
    })


# --------------------------------------------------------------------------
# 上传：保存 → ffprobe → 抽 3 帧预览原图
# --------------------------------------------------------------------------
@app.post("/api/upload")
def upload():
    f = request.files.get("video")
    if f is None or not f.filename:
        return jsonify({"error": "请选择视频文件"}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in runner.ALLOWED_EXT:
        return jsonify({"error": f"不支持的格式 {ext}，允许: "
                                 f"{', '.join(sorted(runner.ALLOWED_EXT))}"}), 400

    token = secrets.token_hex(8)
    if not ID_RE.fullmatch(token):           # 双保险：后续所有路径拼接只认 token
        abort(500)
    src = os.path.join(UPLOAD_DIR, token + ext)
    work = os.path.join(TMP_DIR, token)
    os.makedirs(work, exist_ok=True)
    f.save(src)

    size = os.path.getsize(src)
    if size > runner.MAX_UPLOAD_BYTES:
        _cleanup(token, ext)
        return jsonify({"error": "文件超过 200MB 上限"}), 413

    try:
        duration = runner.probe_duration(src)
    except runner.FFmpegError as e:
        _cleanup(token, ext)
        return jsonify({"error": str(e)}), 400

    if duration > runner.MAX_DURATION_SEC + 1:
        _cleanup(token, ext)
        return jsonify({"error": "视频超过 10 分钟上限"}), 400

    try:
        times = runner.extract_frames(src, duration, work, n=N_FRAMES)
    except runner.FFmpegError as e:
        _cleanup(token, ext)
        return jsonify({"error": "抽帧失败: " + str(e)}), 500

    with open(os.path.join(work, "meta.json"), "w") as fh:
        json.dump({"src": src, "duration": duration, "times": times,
                   "size": size}, fh)

    return jsonify({
        "id": token,
        "duration": round(duration, 2),
        "size_mb": round(size / 1024 / 1024, 1),
        "frames": [
            {"index": i, "time": round(t, 2),
             "url": f"/media/{token}/orig_{i}.jpg"}
            for i, t in enumerate(times)
        ],
    })


# --------------------------------------------------------------------------
# 预览：对某一帧施加白名单滤镜
# --------------------------------------------------------------------------
@app.post("/api/preview")
def preview():
    body, err = _parse_filter_body(require_frame=True)
    if err:
        return err

    token = body["id"]
    work = os.path.join(TMP_DIR, token)
    meta = _load_meta(token)
    if meta is None or not os.path.isfile(
            os.path.join(work, f"orig_{body['frame']}.jpg")):
        return jsonify({"error": "会话不存在或已过期，请重新上传"}), 404

    try:
        params = resolve_params(body["preset"],
                                body["saturation"], body["contrast"])
    except FilterError as e:
        return jsonify({"error": str(e)}), 400

    chain = build_filterchain(params)                       # 输入帧已是 960 宽
    h = _sig(body["preset"], body["saturation"], body["contrast"])
    frame = body["frame"]
    out_name = f"out_{frame}_{h}.jpg"
    out_path = os.path.join(work, out_name)

    if not os.path.isfile(out_path):                        # 同参数直接命中缓存
        try:
            runner.make_preview(
                os.path.join(work, f"orig_{frame}.jpg"), chain, out_path)
        except runner.FFmpegError as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({
        "ok": True,
        "url": f"/media/{token}/{out_name}",
        "resolved": _public_params(params),
        "filter_chain": chain,
        "commands": _commands(meta["src"], chain,
                              os.path.join(work, f"orig_{frame}.jpg"),
                              os.path.join(UPLOAD_DIR, f"{token}_{h}.mp4")),
    })


# --------------------------------------------------------------------------
# 整片渲染（白名单参数 + 固定编码），用于检验后端真的能跑通
# --------------------------------------------------------------------------
@app.post("/api/render")
def render_video():
    body, err = _parse_filter_body(require_frame=False)
    if err:
        return err

    token = body["id"]
    meta = _load_meta(token)
    if meta is None:
        return jsonify({"error": "会话不存在或已过期，请重新上传"}), 404

    try:
        params = resolve_params(body["preset"],
                                body["saturation"], body["contrast"])
    except FilterError as e:
        return jsonify({"error": str(e)}), 400

    chain = build_filterchain(params)
    h = _sig(body["preset"], body["saturation"], body["contrast"])
    out_path = os.path.join(UPLOAD_DIR, f"{token}_{h}.mp4")

    if not os.path.isfile(out_path):
        try:
            runner.render(meta["src"], chain, out_path, body["preset"])
        except runner.FFmpegError as e:
            return jsonify({"error": str(e)}), 500
        except TimeoutError:
            return jsonify({"error": "渲染超时（>300s），请换更短的素材"}), 504

    return jsonify({
        "ok": True,
        "download": f"/download/{token}_{h}.mp4",
        "commands": _commands(meta["src"], chain, None, out_path),
    })


# --------------------------------------------------------------------------
# 静态媒体 / 下载（路径受 token 正则约束）
# --------------------------------------------------------------------------
@app.get("/media/<token>/<path:name>")
def media(token, name):
    if not ID_RE.fullmatch(token):
        abort(404)
    if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", name):
        abort(404)
    return send_from_directory(os.path.join(TMP_DIR, token), name)


@app.get("/download/<path:name>")
def download(name):
    if not re.fullmatch(r"[0-9a-f]{8,32}_[A-Za-z0-9]{10}\.mp4", name):
        abort(404)
    return send_from_directory(UPLOAD_DIR, name, as_attachment=True)


# --------------------------------------------------------------------------
# 入参严格校验：多余字段直接拒绝 —— 用户无法塞 filter/vf/cmd 等任何键
# --------------------------------------------------------------------------
def _parse_filter_body(require_frame: bool):
    if not request.is_json:
        return None, (jsonify({"error": "请求体必须是 JSON"}), 400)
    raw = request.get_json(silent=True)
    if not isinstance(raw, dict):
        return None, (jsonify({"error": "请求体必须是 JSON 对象"}), 400)

    extra = set(raw) - ALLOWED_BODY
    if extra:
        return None, (jsonify({"error":
                      f"非法字段 {sorted(extra)}，本接口仅接受 "
                      f"{sorted(ALLOWED_BODY)}"}), 400)

    try:
        token = str(raw["id"])
        if not ID_RE.fullmatch(token):
            raise ValueError("id 非法")
        preset = str(raw["preset"])
        sat = float(raw["saturation"])
        con = float(raw["contrast"])
    except (KeyError, TypeError, ValueError):
        return None, (jsonify({"error":
                      "缺少或非法字段：id / preset / saturation / contrast"}), 400)

    frame = 0
    if require_frame:
        try:
            frame = int(raw["frame"])
        except (KeyError, TypeError, ValueError):
            return None, (jsonify({"error": "frame 必须是 0/1/2"}), 400)
        if not 0 <= frame < N_FRAMES:
            return None, (jsonify({"error": "frame 只允许 0、1、2"}), 400)

    return ({"id": token, "preset": preset, "saturation": sat,
             "contrast": con, "frame": frame}), None


def _load_meta(token: str):
    if not ID_RE.fullmatch(token):
        return None
    path = os.path.join(TMP_DIR, token, "meta.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def _sig(preset: str, sat: float, con: float) -> str:
    key = f"{preset}|{sat:.3f}|{con:.3f}"
    return hashlib.sha1(key.encode()).hexdigest()[:10]


def _public_params(p: dict) -> dict:
    return {k: (round(v, 3) if isinstance(v, float) else v)
            for k, v in p.items() if k != "_curves"}


def _commands(src: str, chain: str, frame_in: str | None, out: str) -> dict:
    """返回「服务端实际会执行」的命令字符串，仅用于展示（使用 shlex 转义）。"""
    rendered = " ".join(shlex.quote(x) for x in [
        runner.FFMPEG, "-nostdin", "-y", "-i", src,
        "-filter_complex", f"[0:v]{chain}[v]",
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "128k", out])
    preview_cmd = None
    if frame_in:
        preview_cmd = " ".join(shlex.quote(x) for x in [
            runner.FFMPEG, "-nostdin", "-y", "-i", frame_in,
            "-vf", chain, "-frames:v", "1", "-q:v", "3", out])
    return {"preview": preview_cmd, "render": rendered}


def _cleanup(token: str, ext: str):
    p = os.path.join(UPLOAD_DIR, token + ext)
    if os.path.isfile(p):
        os.remove(p)


if __name__ == "__main__":
    runner.check_tools()
    app.run(host="127.0.0.1", port=8000, debug=False)
