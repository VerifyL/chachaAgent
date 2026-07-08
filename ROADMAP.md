# Roadmap

以下是 ChaChaAgent 的待实现功能清单，按优先级排列。欢迎贡献！

## 高优先级 🔴

### Anthropic 客户端
- **目标**：新增 Anthropic 原生 API 客户端适配器，当前仅支持 OpenAI 兼容 API
- **涉及**：`core/llm_clients/anthropic_client.py`
- **依赖**：`anthropic` SDK（已安装）

## 中优先级 🟡

### Code-RAG 引擎
- **目标**：基于代码语义的检索增强生成，含符号解析器 + 向量存储
- **涉及**：`capabilities/rag/`（骨架：`symbol_parser`、`vector_store`）
- **依赖**：`lancedb`、`tree-sitter`（已安装）

### 多模态支持
- **目标**：图片/音频/视频输入支持
- **涉及**：`capabilities/multimodal/`（目录已创建）
- **配置**：`[multimodal]` 段已预留

## 低优先级 🟢

### OpenClaw 加载器
- **目标**：加载 OpenClaw 插件生态
- **涉及**：`capabilities/openclaw_loader.py`（骨架已创建）

### 插件安装器
- **目标**：社区插件的一键安装与管理
- **涉及**：`capabilities/plugin_installer.py`（骨架已创建）

### 测试基础设施
- **涉及**：`tests/benchmark/`、`tests/evaluation/`、`tests/fuzz/`、`tests/mocks/`（目录已创建）
- **内容**：基准测试、标准评测、模糊测试、Mock 工具

### 调试工具
- **涉及**：`core/debug/`（占位）

---

> ⚠️ 以上为 Roadmap，按需认领。贡献前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。
