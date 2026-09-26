# anime-pv-skill

用于 AI agent 的平面二次元 PV 制作技能。通过二维素材合成、动态图形、排版和镜头运动制作视频，完全不使用视频生成模型。

支持两种模式：**角色替换复刻**（保持原片画面与时间轴，仅替换指定角色，原音轨直接复制）和 **自主创作**（可参考风格；用户未提供音频时输出无音轨视频）。

## 使用入口

技能包位于 [`anime-pv/`](anime-pv/SKILL.md)，仓库根目录的文档和工具用于持续维护，不属于运行时技能包。将整个 `anime-pv` 文件夹放到所用 agent 支持的技能目录，或让具备文件、命令执行与视觉能力的 agent 直接读取 `anime-pv/SKILL.md`。内置生图是否可用取决于 agent。

示例请求：

> 使用 anime-pv，根据参考视频替换指定角色。先完成项目确认与预检，并在阶段批准后交接。

已有项目：

> 读取项目目录中的 HANDOFF.md，核对批准项和证据版本，继续下一阶段。

默认阶段：项目确认与预检 → 视觉与动态定案 → 全片预演 → 正式制作与验收。阶段内自主推进；每阶段达成共识后保存交接，默认停在边界，支持新窗口恢复。用户可以明确合并、跳过或连续执行阶段。

## 工具与生图

Python 使用 [uv](https://docs.astral.sh/uv/)。先运行只读环境检查：

```sh
uv run anime-pv/scripts/project.py preflight
```

生图途径需由用户明确选择：agent 内置生图或后端 API。首批适配 OpenAI Images 原生及兼容协议、Gemini、火山方舟 Seedream、阿里百炼/万相、MiniMax。能力因协议及模型而异，详见 [后端配置与限制](anime-pv/references/image-backends.md)。

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

## 许可状态

尚未指定开源许可证。仓库公开不等于授予开源许可；许可证选择由维护者另行决定。
