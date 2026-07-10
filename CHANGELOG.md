# Changelog

All notable changes to ChachaAgent will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **轮次压缩** — `compression_round_interval` 配置项（默认 30），每 N 轮对话后 `force` 压缩一次（跳过 token 阈值检查），设为 `0` 禁用。与阈值压缩共享计数器，避免刚压完又压。

### Changed

- **子 Agent 超时分档** — `TaskTool` 默认 timeout 从统一 300s 改为按类型分档：`explore` 600s、`plan` 600s、`worker` 900s，匹配各自的 `max_rounds`，减少长任务误杀。

### Fixed

- *(nothing yet)*

### Removed

- *(nothing yet)*

## [3.1.6] — 2026-06-30

> **Initial documented release.** Covers the project's current state as a general-purpose AI Agent framework with a microkernel architecture.

### Added

- **CLI Frontend** — Full-featured terminal interface built on `prompt_toolkit` + `Rich`, with keyboard shortcuts (`Ctrl+N` new session, `Ctrl+S` save, `Ctrl+F` debug, `Ctrl+B` session list, `Ctrl+X` compress, `Ctrl+L` clear, `Ctrl+R` reasoning, `Ctrl+T` telemetry, `Ctrl+C` interrupt, `Ctrl+J` newline, `Ctrl+D` exit, `Ctrl+\` force quit).
- **Orchestrator (`run_stream`)** — Unified 13-step pipeline: Hook → Policy → Gateway → concurrent tool execution.
- **10 Built-in Tools** — `read`, `write`, `edit`, `bash`, `grep`, `glob`, `task`, `memory`, `approval_control`, `cache_read`.
- **Security Policy Engine** — Weighted risk assessment, command blacklist, cost circuit-breaker, interactive CLI approval, four-tier permission levels.
- **Hook System** — Responsibility-chain engine supporting built-in Python hooks and external `ShellCommand` hooks; YAML-based declarative rules via `RuleEngine`.
- **Memory System** — Multi-layer memory: daily session memory, permanent memory, topic-based memory, session isolation, `DreamPipeline` (project-level consolidation) and `GlobalDream` (cross-project permanent memory).
- **JSON-RPC 2.0 Gateway** — Asynchronous message bus with backpressure control and global event listeners.
- **Telemetry System** — Structured logging, metrics collection (counter / gauge / histogram), span tracing, Prometheus export.
- **Model Router** — Three strategies (priority / cost / random), fault isolation, degradation chain.
- **Model Factory** — Unified factory for OpenAI, DeepSeek, and Ollama clients.
- **Usage Tracker** — Per-model token consumption and cost statistics.
- **SubAgent Spawner** — Three sub-agent types (`explore`, `plan`, `worker`) with isolated contexts.
- **Context Compression** — Four-layer progressive compression: `FROZEN` → `TRIMMED` → `SUMMARIZED` → `CONSOLIDATED`.
- **Static Rule Loader** — Layered loading from `~/.chacha/CHACHA.md` and per-project `CHACHA.md`.
- **Sandbox Executor** — Subprocess isolation, environment variable whitelist, resource limits, process-group isolation.
- **Output Governor** — Streaming JSON repair (4 strategies) + illegal content interception.
- **Atomic Writer** — Safe file write utility using atomic rename.
- **Retry Handler** — Exponential backoff for LLM API calls.
- **Configuration System** — Auto-generates `~/.chacha/config.toml` and `~/.chacha/CHACHA.md` on first launch.
- **Project Initializer** — Scaffolds a new ChachaAgent project.
- **Environment Validator** — Pre-flight environment checks.
- **Session Service** — Session orchestration and lifecycle management.
- **Checkpoint Manager** — Session checkpoint persistence.
- **CLI Theme** — Configurable terminal color theme.
- **CLI documentation** — Added missing keyboard shortcuts and debug commands to `docs/cli.md`.

### Changed

- **Documentation** — Updated `docs/architecture.md` with current version number and component status markers (✅ / 🚧).
- **README** — Updated to v3.1.6 reflecting all implemented features and current project structure.

### Fixed

- **README tool count** — Corrected the tool count to accurately reflect all 10 built-in tools.
- **ModelRouter status marker** — Marked as ✅ (fully implemented) in the architecture diagram.
- **ModelFactory status marker** — Marked as ✅ (fully implemented) in the architecture diagram.
- **UsageTracker status marker** — Marked as ✅ (fully implemented) in the architecture diagram.

### Removed

- *(nothing yet)*

---

[Unreleased]: https://github.com/VerifyL/chachaAgent/compare/v3.1.6...HEAD
[3.1.6]: https://github.com/VerifyL/chachaAgent/releases/tag/v3.1.6
