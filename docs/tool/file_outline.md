# `file_outline`

文件骨架提取工具。按扩展名分发：

| 扩展名 | 策略 |
|--------|------|
| `.py` | AST 解析：类/函数签名、导入、常量 |
| `.md`, `.rst`, `.txt` | 提取标题结构 |
| `.json`, `.yaml`, `.toml` | 提取节标题 |
| 其他 | 前 20 行预览 |

输出格式（Python）：

```
[文件] db.py | 243行 | 3个类 | 15个函数
导入 (2): from sqlalchemy import create_engine, connect

class Database(Base):  # L10 "数据库连接管理器" [5方法, 2属性]
  async def connect(self, dsn: str) → Connection

class MigrationError(Exception):  # L120 [1方法]

常量 (3): MAX_RETRIES = 3, DEFAULT_POOL = 5
```
