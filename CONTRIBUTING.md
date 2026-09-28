# 贡献与开发

## 开发环境

使用 uv 管理 Python 和依赖；适用时使用 Homebrew 安装工具。Java 仅在选定的制作工具确实需要时使用，并用 jenv 管理。离线测试不需要 ffmpeg、Node.js、API key 或本地 AI 模型。

在仓库根目录执行：

```sh
uv sync --locked --group dev
uv run --locked python tools/check_repo.py
uv run --locked python -m unittest discover -s anime-pv/scripts/tests -v
```

跨 Python 版本验证可对 uv sync 添加 `--python 3.10` 或 `--python 3.13`。新增依赖须同步更新 pyproject.toml 与 uv.lock；技能脚本独立执行所需的依赖还需同步其 PEP 723 头。只改维护依赖时不扩大运行时依赖。

## 改动边界

先阅读 [架构约定](docs/ARCHITECTURE.md)。修改尽量对应一个真实问题，不为单个项目的创作偏好添加全局硬规则。保持 SKILL.md 简洁，把模式专属说明放在 references。维护说明留在 skill 文件夹外。

- 协议改动：核对官方接口，为请求、响应和失败恢复增加有意义的离线测试。
- 工作流改动：用具体场景检查阶段批准、新窗口恢复、局部回改是否仍保留用户意图；完整制作评测按 [评测方案](docs/skill-evaluation.md) 执行，区分走查和实际运行。
- 工程改动：保持 pathlib/subprocess 参数列表方式，避免 shell 专属语法进入跨平台脚本。
- 文档改动：更新相关链接与 CHANGELOG，不把未执行的检查写成通过。

## 测试数据与公开内容

使用合成图片、虚构 key 和模拟 HTTP 响应。不得提交真实 .env、密钥、签名下载链接、账户响应、私有角色素材或用户项目。不要把 API 真调用加入默认 CI；真实调用应由操作者明确选择后端，并记录模型、地区、操作、日期和实际结果，记录不含凭证。

## 提交与评审

提交说明描述具体问题、改后行为和验证结果。PR 说明列出实际通过的检查及未验证部分；行为变化要更新维护文档。新建 PR 不需要顺带发布版本或更改项目批准规则。
