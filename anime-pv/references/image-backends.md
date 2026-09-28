# 生图途径与后端

仅首次需要生图时读取本文件。用户先明确选择内置生图或后端 API；选择写入交接，项目内沿用。内置工具不可用时报告，不自行切换 API；反之亦然。后端、端点或协议更换同样不能擅自进行。

## 选择之前

先读 [三方中转与最低能力建议](gateway-guidance.md)。完整创作路径至少需要文生图、单图参考、两张以上参考合成与指令编辑；协议名或模型名不能代替实测。精确尺寸、原生透明和独立mask按项目需求验证，不把内置同样存在的规格偏差当作模型画质不足。

已移除 MiniMax 主体参考适配，以及 DashScope synthesis/edit 旧 profile：这些旧路径只覆盖角色参考、纯文生图或单图编辑，不满足此skill完整创作路径的多参考与通用编辑要求。这是适配范围取舍，不是对整个厂商能力的评价。旧配置不会自动切换；MiniMax旧任务请用原版本检查/取回，已保存图片可继续使用。DashScope旧任务在原配置下仍可resume查询/下载，只有新建旧profile任务被拒绝；新项目明确选择新profile和对应模型。

## 配置查找

脚本依次读取项目根目录 `.env` → skill 根目录 `.env` → `~/.anime-pv/.env`（Windows 为用户主目录中的 `.anime-pv`）。首个含 `ANIME_PV_` 配置的文件为完整来源；缺项即报错，无关 `.env` 继续查找。不合并文件、不从进程环境补 key、不展开变量。复制 [配置模板](../assets/backend.env.example) 并填写，真实 `.env` 不进入版本库或交付包。

必填：`ANIME_PV_PROTOCOL`、`ANIME_PV_ENDPOINT`、`ANIME_PV_API_KEY`、`ANIME_PV_MODEL`。模型不默认写死；按用户账户和所需能力选定。端点是带版本的 API 基址，不是完整操作 URL，不可带密钥/查询参数。`ANIME_PV_PARAMS` 为可选 JSON 请求体参数对象，请求文件 params 在该对象上深度覆盖。两者都不能覆盖模型、提示词和图像输入。保留平台原生参数，不把尺寸、参考图能力强行统一。

| 协议 | 基址示例 | 适配范围及边界 |
| --- | --- | --- |
| openai | `https://api.openai.com/v1` 或用户兼容服务基址 | JSON 文生图；multipart 参考图/编辑、可选 mask；兼容服务须实际支持 Images 协议，不能只支持 chat/completions |
| gemini | `https://generativelanguage.googleapis.com/v1beta` | generateContent；本地图像 inlineData；文字指令编辑；不支持显式 mask、Vertex 鉴权或服务端多轮会话续接 |
| seedream | `https://ark.cn-beijing.volces.com/api/v3` | images/generations；参考图与指令编辑按所选模型能力；不支持显式 mask |
| dashscope | `https://dashscope.aliyuncs.com/api/v1` 或账户地域/业务空间基址 | 下表两个 API profile，地区与 key 必须匹配 |

DashScope 必须用 `ANIME_PV_DASHSCOPE_API` 明确选择，没有默认 profile，不按模型名猜：

| profile | 请求类型 | 模型/能力示例（不是可用性保证） |
| --- | --- | --- |
| multimodal | 同步 multimodal-generation/generation | wan2.6-image 仅支持1–4张参考图的生成/编辑，不能单独作为完整路径 |
| multimodal-async | 异步 image-generation/generation | wan2.6-image 支持纯文本混排生图及1–4张参考编辑；结果只保存图片 |

接口支持不等于每个模型支持所有操作。尺寸、图数、格式、区域与配额限制按所选模型官方文档核对。脚本不支持流式输出；仅为wan2.6-image异步接口支持混排结果的图片提取，文本不保存；不支持的模型能力应报告，不能降级或更换后端冒充成功。

对于`wan2.6-image + multimodal-async`，generate自动设置`enable_interleave=true,n=1,max_images=1`，用户显式参数优先但必须符合协议。`max_images`是数量上限，不是精确图数。reference/edit默认非混排、输入1–4张图；显式开启混排时最多1张图。同步profile的纯文本请求在本地拒绝，不自动切换profile或模型。选择完整万相路径时应明确配置multimodal-async；尚未真实账户验证。

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

mask 仅用于 OpenAI 编辑；其余平台省略。OpenAI/Gemini 的 images 接收本地文件；其余也接收 HTTPS URL 或 data URI。脚本不擅自上传素材到图床。

Gemini 原生参数示例为 `{"generationConfig":{"imageConfig":{"aspectRatio":"16:9"}}}`；DashScope 为 `{"parameters":{"size":"1024*1024","n":1}}`；Seedream 为 `{"size":"2K"}`。这些值需匹配具体模型。

```sh
uv run <skill>/scripts/image_backend.py --project <项目> --request <项目>/state/request-v1.json --job .image-jobs/character-v1
uv run <skill>/scripts/image_backend.py --project <项目> --job .image-jobs/character-v1 --resume
```

OpenAI 多参考图使用标准重复 `image[]` 字段，上传文件自动分配唯一ASCII名称，避免不同目录同名素材在中转层冲突；不会改动本地原图。

每次新提交使用全新 job 目录。轮询默认约 40 秒后返回 pending，agent 更新进展后用 resume 查询；单次网络请求仍可能超过此时长。同步生图可能耗时数分钟。将最终选用图片复制到版本化 assets 路径，记录提示词、非敏感模型参数及 job ID；交接不依赖缓存。已完成任务 resume 会校验输出哈希。

## 输出验收

`status=complete` 只表示图片已落盘。新任务另存 `validation`：`checks_passed` 表示已知机械检查通过，`needs_review` 表示图数、精确尺寸或要求的透明度未满足；无论哪种都需视觉验收。`files` 记录实际宽高、格式、alpha范围、透明像素比例和哈希。脚本只比较可识别的原生参数（size的像素格式、n、background），不会从提示词推断尺寸，也不将2K等档位误当精确像素。

规格偏差不触发重新付费生成，不自动裁切、拉伸或强制修改alpha。agent应报告实际规格，在项目合成层按构图选择等比适配、留边或用户认可的裁切，保留原图。alpha最高254本身不是失败；局部改色和mask仍需核对角色与保护区域，不能只凭像素不相同就判画质不合格。旧已完成任务resume保留原状态，没有validation时不能宣称它通过新检查；旧待下载/异步任务没有原始expected，完成后标为not_checked，不猜测原始规格。

## 错误恢复与秘密边界

- `job.json` 的 `diagnostic` 记录脱敏错误类别、POST/GET、poll-response或download阶段和可用的HTTP状态；区分读超时、连接异常、无效JSON与服务报错，不记录原始异常、响应体、鉴权头或端点。`first_diagnostic`保留首次诊断，`diagnostic`更新为最近一次实际请求错误；本地拒绝resume不会覆盖已有诊断。
- POST 只提交一次。网络中断、超时或响应损坏时，任务记为 unknown；先查账户后台，不能自动重发。修复后确需重新提交，用新 job 并说明可能重复计费。
- 异步任务 ID 在开始查询前落盘。pending 可恢复；failed/canceled/unknown 不自动重生。GET 短暂错误最多三次有限重试。
- 下载失败保留私有结果缓存；resume 只重下结果，不再 POST。临时 URL 可能过期，过期后先查询任务；同步接口无任务查询则报告限制，不能承诺永久恢复。
- job 目录下 results.private.json 可能含有签名 URL，禁止展示、写交接或打包；下载完成即删除。默认 `.image-jobs/` 已忽略。HTTP 原始错误与鉴权头不写日志，CDN 下载使用不带平台鉴权的独立客户端。
- 有 key 仅表示配置齐备，不表示用户选择已完成、账号可用或模型支持。首次真实验证优先使用代表性任务，避免为了“测试连接”无必要生成素材。

异步 DashScope 任务已到 `download`，但缓存的下载链接疑似失效时，显式刷新同一任务的结果：

```sh
uv run <skill>/scripts/image_backend.py --project <项目> --job .image-jobs/character-v1 --resume --refresh-results
```

此操作只用已有 task ID 发起 GET 查询，不重新生成。查询前保存 `pending` 状态，取得有效结果后才替换私有结果缓存；查询失败或仍未完成时，之后用普通 `--resume` 继续。同步任务、缺 task ID、已完成、failed 或 unknown 的任务拒绝刷新；更换配置仍会被后端绑定检查拒绝。刷新后可能仍返回过期链接，脚本不循环刷新或承诺服务端结果永久有效。

图片先写入同一 job 内的 `image-NNN.ext.tmp`，完成后原子替换为正式文件。写入中断时，普通 resume 可重写临时文件；已完成文件内容不同则拒绝覆盖。旧版本遗留的损坏正式文件不能自动区分于人工改动：核对并另存到项目内后，移走冲突文件，再恢复同一 job，无需新建付费任务。不要并发操作同一个 job。

## 官方协议依据

2026-09-27 复核 OpenAI、Gemini、万相与 MiniMax 文档；Seedream 文档本轮页面未完整加载，保留既有适配，未新增真实验证。模型和服务可能变动，遇到能力或协议不符先查官方文档；不把这些链接整篇加载进上下文。

- [OpenAI Images 编辑接口](https://developers.openai.com/api/reference/resources/images/methods/edit)
- [Gemini 图像生成](https://ai.google.dev/gemini-api/docs/image-generation)
- [火山方舟图像生成 API](https://docs.volcengine.com/docs/ark/image-generation-api?lang=en)
- [万相文生图 V2](https://help.aliyun.com/zh/model-studio/text-to-image-v2-api-reference)、[图像编辑](https://help.aliyun.com/zh/model-studio/wanx-image-edit-api-reference)、[图像生成与编辑](https://help.aliyun.com/zh/model-studio/wan-image-generation-api-reference)

实际验证状态见 [验证记录](validation.md)。离线协议测试不能替代账户、地区、模型与真实输出质量验证。
