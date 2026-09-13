# 🍳 美食短片滤镜实验台

用户上传做菜视频 → 选择 **暖色 / 油亮 / 柔光 / 清晰食材** 模板 → 微调 **饱和度、对比度**
→ 页面给出 3 帧预览（可拖动对比原图）和后端实际使用的 ffmpeg 命令，可整片渲染下载。

## 核心安全模型（重点）

**用户永远不能直接提交 shell 或 ffmpeg 滤镜字符串。**

| 攻击面 | 防御 |
|---|---|
| 用户提交 `-vf` / `filter_complex` | 请求体只接受 `id/preset/frame/saturation/contrast`，**多一个字段直接 400** |
| 模板名/数值注入 shell | 模板必须命中服务端枚举；数值强制 `float` 并按白名单区间 clamp |
| curves 表达式注入 | 只允许 ffmpeg 命名预设枚举（`vintage` 等），不接受任意控制点 |
| shell 元字符（`;`、`$()`、反引号） | argv 列表 + `shell=False` 调 ffmpeg，元字符只是普通字符串 |
| 文件名/路径穿越 | 服务端用 `secrets.token_hex` 重命名；所有取数路径用 `^[0-9a-f]{8,32}$` 校验 |
| 伪造文件 | 扩展白名单 + ffprobe 实测能否解析 |
| 资源耗尽 | ≤200MB、≤10 分钟；抽帧 60s、渲染 300s 超时 |

滤镜参数白名单见 `core/filters.py` 的 `SPEC` / `INT_SPEC`，模板内置值同样受其约束。

## 目录

```
app.py              Flask 路由 + 严格入参校验
core/filters.py     白名单引擎：模板、区间校验、filterchain 拼装
core/runner.py      ffmpeg/ffprobe 封装（argv 调用、超时）
templates/ static/  前端（原生 HTML/CSS/JS，无构建）
bin/                ffmpeg / ffprobe 静态可执行文件
uploads/ tmp/       原片与渲染产物 / 抽帧与预览（运行时生成）
```

滤镜链固定为 5 段，顺序恒定：
`curves(可选) → eq → colorbalance → unsharp → hqdn3d`

## 运行

```bash
# bin/ 下需有 ffmpeg、ffprobe（已放入静态构建）
python3 -m venv --without-pip venv        # 本机 Debian 精简镜像缺 ensurepip
python get-pip.py && pip install flask     # 常规环境直接 pip install -r requirements.txt
./venv/bin/python app.py
# 打开 http://127.0.0.1:8000
```

## API

- `POST /api/upload` (multipart `video`) → 抽 3 帧
- `POST /api/preview` JSON `{id, frame(0-2), preset, saturation, contrast}`
- `POST /api/render`  JSON `{id, preset, saturation, contrast}` → MP4 下载地址
- `GET /api/presets` 模板与滑杆取值域

饱和度用户域 0.5–1.8、对比度 0.7–1.5（在模板基准上相乘后再按 eq 白名单收敛）。
