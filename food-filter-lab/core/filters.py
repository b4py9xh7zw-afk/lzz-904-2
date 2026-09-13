"""
滤镜白名单引擎 —— 整个项目的安全核心。

设计原则：
1. 用户永远不能直接提交滤镜字符串或 shell 命令，只能提交「模板名 + 两个数值」。
2. 所有参数（包括模板内置参数）都必须先通过 SPEC 白名单的类型 / 范围校验。
3. ffmpeg 的 -filter_complex / -vf 一律由本模块拼装，值经 Python 格式化输出，
   并对唯一的字符串型参数 curves（预设名）做严格枚举校验，从源头杜绝命令注入。
4. 启动子进程时使用 argv 列表（不经 shell），文件名只允许服务端生成的十六进制 token。
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# 1. 参数白名单：键名、类型、闭区间。任何不在这里的键一律拒绝。
#    （取值范围刻意收窄到“食物调色”合理域，例如对比度不允许拉到极端值。）
# ---------------------------------------------------------------------------
SPEC: Dict[str, Tuple[type, float, float]] = {
    # eq 滤镜
    "contrast": (float, 0.5, 2.0),
    "brightness": (float, -0.3, 0.3),
    "saturation": (float, 0.0, 3.0),
    "gamma": (float, 0.6, 1.8),
    "gamma_r": (float, 0.6, 1.8),
    "gamma_g": (float, 0.6, 1.8),
    "gamma_b": (float, 0.6, 1.8),
    # colorbalance 滤镜（高/中/低光的 rgb 偏移）
    "rh": (float, -1.0, 1.0), "gh": (float, -1.0, 1.0), "bh": (float, -1.0, 1.0),
    "rm": (float, -1.0, 1.0), "gm": (float, -1.0, 1.0), "bm": (float, -1.0, 1.0),
    "rs": (float, -1.0, 1.0), "gs": (float, -1.0, 1.0), "bs": (float, -1.0, 1.0),
    # unsharp 锐化（尺寸必须是奇数，单独处理）
    "unsharp_lamount": (float, 0.0, 2.0),
    "unsharp_camount": (float, -1.0, 1.0),
    # hqdn3d 柔光降噪
    "hqdn3d_luma": (float, 0.0, 20.0),
    "hqdn3d_chroma": (float, 0.0, 20.0),
}

# 整数型参数（unsharp 矩阵尺寸），单独的奇整数白名单
INT_SPEC: Dict[str, Tuple[int, int, int]] = {
    "unsharp_lsize": (3, 23),   # 闭区间，拼装时强制取奇数
    "unsharp_csize": (3, 23),
}

# curves 允许使用的「命名预设」白名单（取值来自本机 ffmpeg curves 文档；
# 不允许用户给任意控制点表达式，从根源杜绝表达式注入）
ALLOWED_CURVES = {
    "none", "color_negative", "cross_process", "darker",
    "increase_contrast", "lighter", "medium_contrast",
    "negative", "vintage",
}

# 滑杆允许的用户输入域（前端也会限制，后端再强制一遍）
SATURATION_USER = (0.5, 1.8)
CONTRAST_USER = (0.7, 1.5)

PRESET_ORDER = ["warm", "oily", "soft", "clear"]

# ---------------------------------------------------------------------------
# 2. 四套模板。每个值都落在上面的白名单内；curves 只能是白名单预设。
#    base_saturation / base_contrast 会与用户滑杆相乘后再 clamp。
# ---------------------------------------------------------------------------
PRESETS: Dict[str, Dict] = {
    "warm": {
        "label": "暖色",
        "desc": "红黄基调，营造温热烟火气",
        "base_saturation": 1.18,
        "base_contrast": 1.06,
        "params": {
            "contrast": 1.06, "brightness": 0.02, "saturation": 1.18,
            "gamma": 1.0, "gamma_r": 1.06, "gamma_g": 1.0, "gamma_b": 0.95,
            "rh": 0.12, "gh": 0.0, "bh": -0.10,
            "rm": 0.08, "gm": 0.02, "bm": -0.06,
            "rs": 0.05, "gs": 0.0, "bs": -0.03,
            "unsharp_lamount": 0.25, "unsharp_camount": 0.08,
            "unsharp_lsize": 5, "unsharp_csize": 5,
            "hqdn3d_luma": 1.5, "hqdn3d_chroma": 2.5,
        },
        "curves": "none",
    },
    "oily": {
        "label": "油亮",
        "desc": "压暗背景、提高对比与饱和，突出油润反光",
        "base_saturation": 1.32,
        "base_contrast": 1.22,
        "params": {
            "contrast": 1.22, "brightness": -0.01, "saturation": 1.32,
            "gamma": 0.97, "gamma_r": 1.03, "gamma_g": 1.0, "gamma_b": 0.96,
            "rh": 0.10, "gh": 0.03, "bh": -0.08,
            "rm": 0.06, "gm": 0.0, "bm": -0.05,
            "rs": 0.0, "gs": 0.0, "bs": 0.0,
            # 强锐化 + 高对比，高光更“跳”
            "unsharp_lamount": 0.75, "unsharp_camount": 0.35,
            "unsharp_lsize": 5, "unsharp_csize": 5,
            "hqdn3d_luma": 0.6, "hqdn3d_chroma": 1.2,
        },
        "curves": "medium_contrast",
    },
    "soft": {
        "label": "柔光",
        "desc": "提亮、低反差、轻微降噪，奶油般柔焦",
        "base_saturation": 1.05,
        "base_contrast": 0.92,
        "params": {
            "contrast": 0.92, "brightness": 0.06, "saturation": 1.05,
            "gamma": 1.08, "gamma_r": 1.03, "gamma_g": 1.02, "gamma_b": 1.04,
            "rh": 0.04, "gh": 0.02, "bh": 0.03,
            "rm": 0.03, "gm": 0.02, "bm": 0.02,
            "rs": 0.02, "gs": 0.01, "bs": 0.02,
            "unsharp_lamount": 0.12, "unsharp_camount": 0.0,
            "unsharp_lsize": 3, "unsharp_csize": 3,
            "hqdn3d_luma": 6.0, "hqdn3d_chroma": 8.0,
        },
        "curves": "lighter",
    },
    "clear": {
        "label": "清晰食材",
        "desc": "中性色彩 + 强锐化，纹理边缘干净利落",
        "base_saturation": 1.12,
        "base_contrast": 1.12,
        "params": {
            "contrast": 1.12, "brightness": 0.01, "saturation": 1.12,
            "gamma": 1.0, "gamma_r": 1.0, "gamma_g": 1.0, "gamma_b": 1.0,
            "rh": 0.0, "gh": 0.0, "bh": 0.0,
            "rm": 0.0, "gm": 0.0, "bm": 0.0,
            "rs": 0.0, "gs": 0.0, "bs": 0.0,
            "unsharp_lamount": 1.0, "unsharp_camount": 0.45,
            "unsharp_lsize": 7, "unsharp_csize": 7,
            "hqdn3d_luma": 0.4, "hqdn3d_chroma": 0.8,
        },
        "curves": "none",
    },
}


class FilterError(ValueError):
    """参数不在白名单 / 不合法时抛出，调用方返回 400。"""


def _clamp_float(name: str, value) -> float:
    if name not in SPEC:
        raise FilterError(f"未授权参数: {name}")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise FilterError(f"参数 {name} 必须是数字")
    if math.isnan(v) or math.isinf(v):
        raise FilterError(f"参数 {name} 非法数值")
    _, lo, hi = SPEC[name]
    if not (lo <= v <= hi):
        raise FilterError(f"参数 {name}={v:g} 越出白名单区间 [{lo:g},{hi:g}]")
    return v


def _clamp_int_odd(name: str, value) -> int:
    if name not in INT_SPEC:
        raise FilterError(f"未授权参数: {name}")
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise FilterError(f"参数 {name} 必须是整数")
    lo, hi = INT_SPEC[name]
    if not (lo <= v <= hi):
        raise FilterError(f"参数 {name}={v} 越出白名单区间 [{lo},{hi}]")
    if v % 2 == 0:          # unsharp 矩阵尺寸必须为奇数
        v += 1 if v < hi else -1
    return min(hi, max(lo, v))


def resolve_params(preset: str, saturation_user: float,
                   contrast_user: float) -> Dict:
    """
    模板内置参数 × 用户微调 → 最终白名单参数集。
    用户唯一能影响的只有 saturation / contrast 两个数值。
    """
    if preset not in PRESETS:
        raise FilterError(f"未知模板: {preset!r}，允许: {', '.join(PRESET_ORDER)}")

    # 用户滑杆先 clamp 到用户域
    su = _clamp_user(saturation_user, SATURATION_USER, "饱和度微调")
    cu = _clamp_user(contrast_user, CONTRAST_USER, "对比度微调")

    p = PRESETS[preset]
    out: Dict = {}
    for name, raw in p["params"].items():
        if name in INT_SPEC:
            out[name] = _clamp_int_odd(name, raw)
        else:
            out[name] = _clamp_float(name, raw)

    # 用户微调与模板基准相乘，再按 eq 的白名单区间 clamp
    out["saturation"] = _clamp_float("saturation", p["base_saturation"] * su)
    out["contrast"] = _clamp_float("contrast", p["base_contrast"] * cu)

    curves = p["curves"]
    if curves not in ALLOWED_CURVES:
        raise FilterError(f"curves 预设不在白名单: {curves!r}")
    out["_curves"] = curves
    return out


def _clamp_user(value, bounds, label) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise FilterError(f"{label}必须是数字")
    if math.isnan(v) or math.isinf(v):
        raise FilterError(f"{label}非法")
    lo, hi = bounds
    return min(hi, max(lo, v))


# ---------------------------------------------------------------------------
# 3. 把白名单参数拼成固定结构的 filter chain（顺序恒定，无任何用户字符串拼接）
# ---------------------------------------------------------------------------
def build_filterchain(p: Dict, append_scale: int | None = None) -> str:
    stages: List[str] = []

    if p["_curves"] != "none":
        # 预设名已经过枚举白名单校验，这里再断言一次
        assert p["_curves"] in ALLOWED_CURVES
        stages.append(f"curves=preset={p['_curves']}")

    stages.append(
        "eq="
        f"contrast={p['contrast']:.3f}:"
        f"brightness={p['brightness']:.3f}:"
        f"saturation={p['saturation']:.3f}:"
        f"gamma={p['gamma']:.3f}:"
        f"gamma_r={p['gamma_r']:.3f}:"
        f"gamma_g={p['gamma_g']:.3f}:"
        f"gamma_b={p['gamma_b']:.3f}"
    )

    cb_keys = [("rs", "rs"), ("gs", "gs"), ("bs", "bs"),
               ("rm", "rm"), ("gm", "gm"), ("bm", "bm"),
               ("rh", "rh"), ("gh", "gh"), ("bh", "bh")]
    cb = ":".join(f"{k}={p[n]:.3f}" for n, k in cb_keys)
    stages.append(f"colorbalance={cb}")

    stages.append(
        "unsharp="
        f"lx={p['unsharp_lsize']}:ly={p['unsharp_lsize']}:"
        f"la={p['unsharp_lamount']:.3f}:"
        f"cx={p['unsharp_csize']}:cy={p['unsharp_csize']}:"
        f"ca={p['unsharp_camount']:.3f}"
    )

    stages.append(
        f"hqdn3d={p['hqdn3d_luma']:.2f}:{p['hqdn3d_chroma']:.2f}"
    )

    if append_scale:
        # 预览专用：统一缩到指定宽度（偶数高度，yuv420 兼容）
        stages.append(f"scale={int(append_scale)}:-2")

    return ",".join(stages)
