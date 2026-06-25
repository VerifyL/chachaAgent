# `read_file`

按行读取文件，支持多种定位方式。生产级安全措施：

- **Containment**：路径超出项目根目录拒绝
- **大小限制**：>10MB 拒绝
- **二进制检测**：40+ 扩展名黑名单
- **行对齐截断**：截断在最近换行符
- **元数据前缀**：`[文件] xxx.py | 243行 | 8KB | 行 1-100`

参数：
| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `path` | string | 必填 | 文件路径（相对或绝对） |
| `offset` | int | 1 | 起始行号（1-based） |
| `limit` | int | 100 | 最大读取行数 |
| `symbol` | string | — | 跳转到函数/类/变量定义（优先级高于 offset） |
| `context_lines` | int | 0 | symbol/行号前后各 N 行上下文 |
| `search` | string | — | 搜索关键词，自动定位并展开上下文 |
| `skip_first` | int | 0 | search 模式下跳过前 N 条匹配 |

示例：
```
read_file(path="main.py")                              # 默认行 1-100
read_file(path="main.py", offset=100, limit=50)        # 行 100-149
read_file(path="main.py", symbol="MyClass")             # 跳转到 MyClass 定义
read_file(path="main.py", search="TODO", context_lines=2)  # 搜索 + 上下文
```
