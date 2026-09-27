# anime-pv-skill

用于 AI agent 的平面二次元 PV 制作技能。通过二维素材合成、动态图形、排版和镜头运动制作视频，完全不使用视频生成模型。

支持两种模式：**角色替换复刻**（保持原片画面与时间轴，仅替换指定角色，原音轨直接复制）和 **自主创作**（可参考风格；用户未提供音频时输出无音轨视频）。

## 使用入口

技能包位于 [`anime-pv/`](anime-pv/SKILL.md)，仓库根目录的文档和工具用于持续维护，不属于运行时技能包。将整个 `anime-pv` 文件夹放到所用 agent 支持的技能目录，或让具备文件、命令执行与视觉能力的 agent 直接读取 `anime-pv/SKILL.md`。内置生图是否可用取决于 agent。

示例请求：

> 使用 anime-pv，根据参考视频替换指定角色。先完成项目确认与预检，并在阶段批准后交接。

已有项目：

> 请使用 anime-pv，读取 /实际项目路径/HANDOFF.md。我确认阶段 1 的成果 design/preflight-v1.md（v1），请直接开始阶段 2 视觉与动态定案。

默认阶段：项目确认与预检 → 视觉与动态定案 → 全片预演 → 正式制作与验收。阶段内自主推进；每阶段成果完成后保存交接并停在边界。修改在当前窗口进行；确认成果后直接在新窗口发送续接提示词，即批准所列成果并启动下一阶段，无需在旧窗口先批准或在新窗口重复审批。用户可以明确合并、跳过或连续执行阶段。

阶段结束或暂停时，最终回复会附带可直接复制到新对话的简短提示词，仅包含技能与交接入口、本次成果确认（完成时）和目标阶段；已有规则、决定和任务细节从技能及交接文档读取。生成文件默认放在项目内，临时内容集中到 `tmp/`、缓存集中到 `.cache/`，方便管理磁盘空间。

## 工具与生图

Python 使用 [uv](https://docs.astral.sh/uv/)。先运行只读环境检查：

```sh
uv run anime-pv/scripts/project.py preflight
```

生图途径需由用户明确选择：agent 内置生图或后端 API。适配 OpenAI Images 原生及兼容协议、Gemini、火山方舟 Seedream、阿里百炼/万相多模态路径。三方中转应具备文生图、单图参考、多参考合成和指令编辑；详情见 [中转选择建议](anime-pv/references/gateway-guidance.md)。能力因协议及模型而异，详见 [后端配置与限制](anime-pv/references/image-backends.md)。

默认不运行本地 AI 模型，不固定唯一渲染引擎。仅有 API key 不代表已经获得生图途径选择。项目素材和真实配置应放在独立项目目录。

## 维护

- [贡献和本地检查](CONTRIBUTING.md)
- [设计约定与代码地图](docs/ARCHITECTURE.md)
- [维护、兼容性和发布流程](docs/MAINTENANCE.md)
- [已知限制与待办](docs/ROADMAP.md)
- [变更记录](CHANGELOG.md)
- [技能初始验证记录](anime-pv/references/validation.md)

```sh
uv sync --locked --group dev
uv run --locked python tools/check_repo.py
uv run --locked python -m unittest discover -s anime-pv/scripts/tests -v
```

仓库 CI 在 macOS、Linux、Windows 上执行结构检查和离线测试；以具体运行结果为准。离线测试不代表真实 API 调用成功或成片画质已验收。

## 开源许可

本项目的代码、技能指令、模板和文档采用 [MIT License](LICENSE)，允许使用、修改、分发及商业使用，分发时保留版权和许可声明。可独立安装的技能目录内也包含 [许可证副本](anime-pv/LICENSE)。第三方依赖及用户提供的素材遵循各自的许可。
