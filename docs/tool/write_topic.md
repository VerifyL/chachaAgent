# `write_topic`

写入长期主题记忆（session 级别 `topics/` 目录）。

支持的主题：
- `user-preferences` — 用户偏好
- `project-decisions` — 项目技术决策
- `lessons-learned` — 踩坑教训
- `errors-fixed` — 成功修复的 bug
- `project-progress` — 项目进度

当用户要求"记住"时，必须同时调用 `remember` + `write_topic`。
