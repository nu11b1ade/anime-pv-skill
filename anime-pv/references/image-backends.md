# 生图途径与后端

仅首次需要生图时读取本文件。用户先明确选择内置生图或后端 API；选择写入交接，项目内沿用。内置工具不可用时报告，不自行切换 API；反之亦然。后端、端点或协议更换同样不能擅自进行。

## 配置查找

脚本依次读取项目根目录 `.env` → skill 根目录 `.env` → `~/.anime-pv/.env`（Windows 为用户主目录中的 `.anime-pv`）。首个含 `ANIME_PV_` 配置的文件为完整来源；缺项即报错，无关 `.env` 继续查找。不合并文件、不从进程环境补 key、不展开变量。复制 [配置模板](../assets/backend.env.example) 并填写，真实 `.env` 不进入版本库或交付包。

必填：`ANIME_PV_PROTOCOL`、`ANIME_PV_ENDPOINT`、`ANIME_PV_API_KEY`、`ANIME_PV_MODEL`。模型不默认写死；按用户账户和所需能力选定。端点是带版本的 API 基址，不是完整操作 URL，不可带密钥/查询参数。`ANIME_PV_PARAMS` 为可选 JSON 请求体参数对象，请求文件 params 在该对象上深度覆盖。两者都不能覆盖模型、提示词和图像输入。保留平台原生参数，不把尺寸、参考图能力强行统一。

| 协议 | 基址示例 | 适配范围及边界 |
| --- | --- | --- |
| openai | `https://api.openai.com/v1` 或用户兼容服务基址 | JSON 文生图；multipart 参考图/编辑、可选 mask；兼容服务须实际支持 Images 协议，不能只支持 chat/completions |
| gemini | `https://generativelanguage.googleapis.com/v1beta` | generateContent；本地图像 inlineData；文字指令编辑；不支持显式 mask、Vertex 鉴权或服务端多轮会话续接 |
| seedream | `https://ark.cn-beijing.volces.com/api/v3` | images/generations；参考图与指令编辑按所选模型能力；不支持显式 mask |
| dashscope | `https://dashscope.aliyuncs.com/api/v1` 或账户地域/业务空间基址 | 下表四个 API profile，地区与 key 必须匹配 |
| minimax | `https://api.minimax.io/v1` 或账户地域基址 | image_generation；文生图或单张角色主体参考；不承诺通用编辑，edit 明确拒绝 |

DashScope 用 `ANIME_PV_DASHSCOPE_API` 明确选择，不按模型名猜：

| profile | 请求类型 | 模型/能力示例（不是可用性保证） |
| --- | --- | --- |
| synthesis（默认） | 异步 text2image/image-synthesis | Wan 文生图 V2；仅 generate |
| edit | 异步 image2image/image-synthesis | wanx2.1-imageedit；单图指令编辑、mask；params.input.function 可选择支持的原生功能 |
| multimodal | 同步 multimodal-generation/generation | wan2.6-image 等对应模型；图像输入 messages |
| multimodal-async | 异步 image-generation/generation | 支持该异步 API 的模型；任务查询 |

接口支持不等于每个模型支持所有操作。尺寸、图数、格式、区域与配额限制按所选模型官方文档核对。脚本不支持流式或图文混排输出；不支持的模型能力应报告，不能降级或更换后端冒充成功。

## 调用

安装依赖由 `uv run` 根据脚本头完成，无需全局 pip。先只做配置检查，不联网，也不输出 key：

```sh
uv run <skill>/scripts/image_backend.py --project <项目> --check
```

用户已选 API 后，agent 在 `state/image-route.json` 记录真实选择依据（不可自行编造）：

```json
{"route":"backend","protocol":"openai","user_choice":"用户在本轮明确选择 OpenAI API；具体证据由 agent 填写"}
```

创建请求 JSON；保存到项目以便复现，不将 key 或带签名的 URL 写入请求。图像路径相对于项目根目录；命令行 request 文件路径相对于当前工作目录。

```json
{"operation":"generate","prompt":"具体创作提示词","params":{"size":"1024x1024"}}
```

参考/编辑示例：

```json
{"operation":"edit","prompt":"保持已批准角色身份，仅调整指定姿态","images":["assets/character-v1.png"],"mask":"assets/mask-v1.png","params":{}}
```

mask 仅用于 OpenAI 编辑与 DashScope edit；其余平台省略。MiniMax 用 reference。OpenAI/Gemini 的 images 接收本地文件；其余也接收 HTTPS URL 或 data URI。脚本不擅自上传素材到图床。

Gemini 原生参数示例为 `{"generationConfig":{"imageConfig":{"aspectRatio":"16:9"}}}`；DashScope 为 `{"parameters":{"size":"1024*1024","n":1}}`；Seedream 为 `{"size":"2K"}`；MiniMax 为 `{"aspect_ratio":"16:9","n":1}`。这些值需匹配具体模型。

```sh
uv run <skill>/scripts/image_backend.py --project <项目> --request <项目>/state/request-v1.json --job .image-jobs/character-v1
uv run <skill>/scripts/image_backend.py --project <项目> --job .image-jobs/character-v1 --resume
```

每次新提交使用全新 job 目录。轮询默认约 40 秒后返回 pending，agent 更新进展后用 resume 查询；单次网络请求仍可能超过此时长。同步生图可能耗时数分钟。将最终选用图片复制到版本化 assets 路径，记录提示词、非敏感模型参数及 job ID；交接不依赖缓存。已完成任务 resume 会校验输出哈希。

## 错误恢复与秘密边界

- POST 只提交一次。网络中断、超时或响应损坏时，任务记为 unknown；先查账户后台，不能自动重发。修复后确需重新提交，用新 job 并说明可能重复计费。
- 异步任务 ID 在开始查询前落盘。pending 可恢复；failed/canceled/unknown 不自动重生。GET 短暂错误最多三次有限重试。
- 下载失败保留私有结果缓存；resume 只重下结果，不再 POST。临时 URL 可能过期，过期后先查询任务；同步接口无任务查询则报告限制，不能承诺永久恢复。
- job 目录下 results.private.json 可能含有签名 URL，禁止展示、写交接或打包；下载完成即删除。默认 `.image-jobs/` 已忽略。HTTP 原始错误与鉴权头不写日志，CDN 下载使用不带平台鉴权的独立客户端。
- 有 key 仅表示配置齐备，不表示用户选择已完成、账号可用或模型支持。首次真实验证优先使用代表性任务，避免为了“测试连接”无必要生成素材。

## 官方协议依据

2026-09-26 核对。模型和服务可能变动，遇到能力或协议不符先查官方文档；不把这些链接整篇加载进上下文。

- [OpenAI Images 编辑接口](https://developers.openai.com/api/reference/resources/images/methods/edit)
- [Gemini 图像生成](https://ai.google.dev/gemini-api/docs/image-generation)
- [火山方舟图像生成 API](https://docs.volcengine.com/docs/ark/image-generation-api?lang=en)
- [万相文生图 V2](https://help.aliyun.com/zh/model-studio/text-to-image-v2-api-reference)、[图像编辑](https://help.aliyun.com/zh/model-studio/wanx-image-edit-api-reference)、[图像生成与编辑](https://help.aliyun.com/zh/model-studio/wan-image-generation-api-reference)
- [MiniMax 角色参考生图](https://platform.minimax.io/docs/api-reference/image-generation-i2i)

实际验证状态见 [验证记录](validation.md)。离线协议测试不能替代账户、地区、模型与真实输出质量验证。
