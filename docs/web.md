# ChachaAgent Web 前端

基于 FastAPI + React 的 Web 管理界面，与 CLI 共享底层 `Orchestrator.run_stream()`。

## 快速启动

```bash
# 直接启动（whl 包内置前端）
chacha web

# 自定义端口
chacha web --port 3000

# 开发模式热重载（仅后端）
chacha web --reload

# 浏览器打开
# http://localhost:8100
```

## 开发模式

```bash
# 终端 1 — 后端
chacha web

# 终端 2 — 前端（热更新，改代码秒级生效）
cd interface/web/frontend
npm install
npm run dev

# 浏览器打开 http://localhost:5173
```

Vite 已配置代理，`/api/*` 和 WebSocket 自动转发到后端 `:8100`。

## 架构

```
浏览器 ←WebSocket→ FastAPI ←WebBridge→ AgentBridge → Orchestrator → LLM
       ←REST API→          (会话管理)
```

| 组件 | 文件 | 说明 |
|------|------|------|
| FastAPI 服务端 | `interface/web/server.py` | 入口、CORS、静态文件托管、lifespan |
| WebSocket 桥接 | `interface/web/web_bridge.py` | WebSocket ↔ AgentBridge，单例 |
| 聊天路由 | `interface/web/routes/chat.py` | `/api/ws/chat` 流式对话 |
| 会话路由 | `interface/web/routes/sessions.py` | `/api/sessions` CRUD |
| React 前端 | `interface/web/frontend/src/` | Vite + React + Tailwind + Zustand |

## WebSocket 协议

### Client → Server

| 消息 | 说明 |
|------|------|
| `{"type":"chat","content":"..."}` | 发送对话消息 |
| `{"type":"new_session"}` | 创建新会话 |
| `{"type":"stop"}` | 中断当前回答 |
| `{"type":"ping"}` | 心跳检测 |

### Server → Client（9 种 StreamEvent）

| 事件 | 说明 |
|------|------|
| `{"type":"text","content":"..."}` | 流式文本增量 |
| `{"type":"reasoning","content":"..."}` | 推理过程（DeepSeek-R1 think） |
| `{"type":"tool_call_start","tool_name":"..."}` | 工具调用开始 |
| `{"type":"tool_call_args","tool_name":"...","args":"..."}` | 工具参数 |
| `{"type":"tool_exec_start","tool_name":"..."}` | 工具执行开始 |
| `{"type":"tool_exec_end","tool_name":"...","preview":"...","truncated":false}` | 工具执行结束 |
| `{"type":"tool_error","tool_name":"...","error":"..."}` | 工具执行错误 |
| `{"type":"done","tokens":1234}` | 回答完成 |
| `{"type":"error","message":"..."}` | 系统错误 |
| `{"type":"session_created","session_id":"..."}` | 会话已创建 |
| `{"type":"pong"}` | 心跳响应 |

### 连接示例

```javascript
const ws = new WebSocket("ws://localhost:8100/api/ws/chat");

// 发送消息
ws.send(JSON.stringify({ type: "chat", content: "你好" }));

// 恢复已有会话
const ws = new WebSocket("ws://localhost:8100/api/ws/chat?session_id=20260708-180144");

// 接收流式事件
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  switch (msg.type) {
    case "text":       /* 追加文本 */ break;
    case "reasoning":  /* 追加推理 */ break;
    case "done":       /* 回答完成 */ break;
    case "error":      /* 显示错误 */ break;
  }
};
```

## REST API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/sessions` | 会话列表 |
| `GET` | `/api/sessions/{id}` | 会话消息历史 |
| `DELETE` | `/api/sessions/{id}` | 删除会话 |
| `POST` | `/api/sessions` | 新建会话 |
| `GET` | `/api/health` | 健康检查 |

## 前端功能

| 功能 | 说明 |
|------|------|
| 流式对话 | 打字机效果，实时增量渲染 |
| 代码高亮 | `rehype-highlight`，亮/暗双主题，语言标签 |
| 代码复制 | 每段代码块右上角一键复制按钮 |
| 工具调用卡片 | 展开显示参数和结果，截断标记 |
| 思考过程折叠 | 默认展开，显示行数，折叠后显示预览 |
| 会话管理 | 侧边栏列表、历史加载、切换/删除 |
| 错误横幅 | 红色警告 + 重试按钮 + 关闭 |
| 暗/亮主题 | 跟随系统自动切换，可手动覆盖 |
| 响应式 | 移动端侧边栏浮层 + 遮罩 |
| 快捷键 | Enter 发送 / Shift+Enter 换行 |

## 前端技术栈

| 技术 | 用途 |
|------|------|
| React 18 + TypeScript | UI 框架 |
| Vite | 构建工具（热更新） |
| Tailwind CSS 4 | 原子化样式 |
| Zustand | 轻量状态管理 |
| react-markdown | Markdown 渲染 |
| remark-gfm | 表格/删除线/任务列表 |
| rehype-highlight | 代码语法高亮 |

## 会话持久化

- 前端刷新后自动恢复最近会话（`localStorage.lastSessionId`）
- WebSocket 重连时附带 `?session_id=xxx` 恢复会话
- 会话存储与 CLI 共享（`~/.chacha/projects/{hash}/`），CLI 创建的会话在 Web 侧边栏可见

## 构建与发布

```bash
# 开发（无需构建）
cd interface/web/frontend && npm run dev

# 生产构建
cd interface/web/frontend && npm run build

# 打包 whl（CI 自动执行）
pip install build && python -m build --wheel
```

构建产物 `frontend/dist/` 通过 `MANIFEST.in` 打入 whl，用户 `pip install` 后直接 `chacha web` 可用，**无需 Node.js**。
