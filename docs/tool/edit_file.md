# `edit_file`

精确替换文件内容。AtomicWriter 临时文件 + fsync + 原子 rename，无截断风险。

参数：
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `path` | string | 必填 | 目标文件路径 |
| `old_string` | string | 必填 | 被替换的内容（必须精确匹配） |
| `new_string` | string | 必填 | 替换后的内容 |
| `replace_all` | bool | false | 是否替换所有匹配项 |

匹配失败时返回文件头部 15 行帮助修正。替换成功时返回备份路径。备份在 `.chacha_agent/backups/`。
