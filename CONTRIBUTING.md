# 贡献指南

感谢你对 ChachaAgent 的关注！我们欢迎任何形式的贡献。

## 提交 Issue

- **Bug 报告**：描述复现步骤、期望行为、实际行为，附上环境信息（Python 版本、操作系统、依赖版本）。
- **功能请求**：说明使用场景和期望效果，可附上伪代码或 API 设想。
- 在 [Issues](https://github.com/VerifyL/chachaAgent/issues) 页面提交，尽量使用清晰的标题。

## 提交 Pull Request

1. **Fork** 仓库并创建功能分支：`git checkout -b feat/your-feature`
2. 编写代码，确保通过现有测试。
3. 如果添加了新功能，请补充对应的测试用例。
4. 运行代码检查和格式化：

   ```bash
   ruff check .
   black --check .
   ```

5. 提交前运行测试：

   ```bash
   pytest
   ```

6. 提交 PR 时填写清晰的描述（做了什么、为什么、如何测试）。
7. 保持 PR 范围小而聚焦，一个 PR 只做一件事。

## 开发环境搭建

```bash
# 克隆仓库
git clone https://github.com/VerifyL/chachaAgent.git
cd chachaAgent

# 创建虚拟环境（推荐）
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 安装开发依赖
pip install -e ".[dev]"
```

`[dev]` 包含：pytest、pytest-asyncio、black、ruff、watchdog。

## 代码风格

项目使用 **black** 和 **ruff** 确保代码风格一致：

| 工具 | 配置 | 规则 |
|------|------|------|
| **black** | `line-length = 120` | 自动格式化 |
| **ruff** | `line-length = 120` | `E` `F` `I` `N` `W` |
| **引号** | 双引号（`ruff.format`） | — |
| **目标版本** | Python 3.10+ | — |

建议在编辑器中开启保存时自动格式化（black/ruff）。

## 项目结构

```
chachaAgent/
├── core/           # 核心执行引擎与调度
├── protocol/       # 协议与类型定义
├── capabilities/   # 工具、记忆、RAG 等能力
├── interface/      # CLI、Web 等前端入口
├── scripts/        # 辅助脚本
├── tests/          # 测试用例
└── docs/           # 文档
```

## License

ChachaAgent 采用 [MIT License](LICENSE)。提交贡献即表示你同意将你的代码以 MIT 协议发布。
