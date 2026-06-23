# `remember`

写入短期会话记忆（7天后自动清理）。写入当前 session 的 `YYYY-MM-DD.md`。

参数：
- `content` (必填)：要记住的内容摘要

当用户要求"记住"时，必须同时调用 `remember` + `write_topic`。
