# anime-pv-skill

用于 AI agent 的平面二次元 PV 制作技能。通过二维素材合成、动态图形、排版和镜头运动制作视频，完全不使用视频生成模型。

支持两种模式：**角色替换复刻**（保持原片画面与时间轴，仅替换指定角色，原音轨直接复制）和 **自主创作**（可参考风格；用户未提供音频时输出无音轨视频）。

## 使用入口

技能包位于 [`anime-pv/`](anime-pv/SKILL.md)，仓库根目录的文档和工具用于持续维护，不属于运行时技能包。将整个 `anime-pv` 文件夹放到所用 agent 支持的技能目录，或让具备文件、命令执行与视觉能力的 agent 直接读取 `anime-pv/SKILL.md`。内置生图是否可用取决于 agent。

示例请求：

> 使用 anime-pv，根据参考视频替换指定角色。先问清必要事项，再做视觉与动态定案样片。

已有项目：

> 请使用 anime-pv，读取 /实际项目路径/HANDOFF.md。我确认阶段 2 的成果 design/stage2-review-v1.md（v1），请直接开始阶段 3 全片预演。

默认阶段：项目确认与预检 → 视觉与动态定案 → 全片预演 → 正式制作与验收。阶段 1 问清只能由用户决定的事项后直接进入阶段 2，用样片而非文字方案确认方向；阶段 2、3 成果完成后保存交接并停在边界，阶段 4 以用户验收结束。修改在当前窗口进行；确认成果后直接在新窗口发送续接提示词，即批准所列成果并启动下一阶段，无需在旧窗口先批准或在新窗口重复审批。用户可以明确合并、跳过或连续执行阶段。

质量要求覆盖首次样片自检、顶级化打磨方向、镜头设计、素材一致性、最终渲染链路验证及缺陷复验，详见 [制作质量与审片](anime-pv/references/quality.md)。[制作手法与工具](anime-pv/references/craft.md) 列出渲染路线取舍、二次元撮影与有限动画手法、画质技术要点和动态自检手段，均为可选手段；不固定画风、镜头数量或特效套路。

阶段结束或暂停时，最终回复会附带可直接复制到新对话的简短提示词，仅包含技能与交接入口、本次成果确认（完成时）和目标阶段；已有规则、决定和任务细节从技能及交接文档读取。生成文件默认放在项目内，临时内容集中到 `tmp/`、缓存集中到 `.cache/`，方便管理磁盘空间。

## 工具与生图

Python 使用 [uv](https://docs.astral.sh/uv/)。先运行只读环境检查，它会报告工具版本、ffmpeg 关键编码器与滤镜、中日文字体和可选渲染引擎：

```sh
uv run anime-pv/scripts/project.py preflight
```

审片证据由 `anime-pv/scripts/media.py` 生成：带时间戳的胶片条、与原片同时间点的并排/差分图、切点检测与对齐，以及包括音轨逐包比对在内的工程核查。这些是自检证据，不替代连续播放审阅与用户验收。

生图途径需由用户明确授权：agent 可用的内置或 MCP 生图工具，以及一个或多个后端 API profile（`.env.<profile>`，调用时加 `--profile`），可一次授权多个，由模型在授权范围内按素材择优。适配 OpenAI Images 原生及兼容协议、Gemini、火山方舟 Seedream、阿里百炼/万相多模态路径。三方中转应具备文生图、单图参考、多参考合成和指令编辑；详情见 [中转选择建议](anime-pv/references/gateway-guidance.md)。能力因协议及模型而异，详见 [后端配置与限制](anime-pv/references/image-backends.md)。

不使用视频生成模型（含插帧）。本地 AI 工具默认不运行；抠像/分割、修补、超分、OCR 等非生成用途能明显提升质量时，由模型提出具体方案，用户同意后使用。不固定唯一渲染引擎。仅有 API key 不代表已经获得生图授权。项目素材和真实配置应放在独立项目目录。

## 维护

- [贡献和本地检查](CONTRIBUTING.md)
- [设计约定与代码地图](docs/ARCHITECTURE.md)
- [维护、兼容性和发布流程](docs/MAINTENANCE.md)
- [已知限制与待办](docs/ROADMAP.md)
- [完整 PV 与工作流评测方案](docs/skill-evaluation.md)
- [变更记录](CHANGELOG.md)
- [技能验证历史](docs/validation-history.md)

```sh
uv sync --locked --group dev
uv run --locked python tools/check_repo.py
uv run --locked python -m unittest discover -s anime-pv/scripts/tests -v
```

仓库 CI 在 macOS、Linux、Windows 上执行结构检查和离线测试，Linux 作业另装 ffmpeg 运行媒体集成测试；以具体运行结果为准。离线测试不代表真实 API 调用成功或成片画质已验收。

## 开源许可

本项目的代码、技能指令、模板和文档采用 [MIT License](LICENSE)，允许使用、修改、分发及商业使用，分发时保留版权和许可声明。可独立安装的技能目录内也包含 [许可证副本](anime-pv/LICENSE)。第三方依赖及用户提供的素材遵循各自的许可。
