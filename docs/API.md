# Sequoia-X 数据服务

Sequoia-X V2 选股系统的对外数据接口。**只读**，底层复用 `data/sequoia_v2.db`，
不依赖 `main.py` / `DataEngine` / 飞书推送，可独立部署。

数据范围：666 只 A 股沪深京、~43 万行、后复权日 K（2024-01-01 ~ 当前）。

---

## 启动

### HTTP（FastAPI）

```bash
# 默认 0.0.0.0:8000
python serve.py

# 自定义端口
python serve.py --port 9000

# 等价：直接 uvicorn
uvicorn sequoia_x.api.app:app --host 0.0.0.0 --port 8000
```

启动后自动生成 OpenAPI 文档：

- Swagger UI：<http://localhost:8000/docs>
- ReDoc：<http://localhost:8000/redoc>
- OpenAPI JSON：<http://localhost:8000/openapi.json>

### MCP（stdio，给 Agent 用）

```bash
# 方式 1：直接 stdio
python -m sequoia_x.api.mcp_server

# 方式 2：mcp CLI
mcp run sequoia_x.api.mcp_server:mcp
```

外部 MCP client（Claude Desktop / Cursor / 自建 Agent）通过 stdio 子进程接入。

---

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活探针（含 DB stats） |
| GET | `/meta` | 数据库总览（股票数/行数/起止日期） |
| GET | `/symbols` | 全部股票代码（按字典序） |
| GET | `/symbols/{code}` | 单只股票元信息 |
| GET | `/symbols/{code}/ohlcv` | 单只 OHLCV（按日期） |
| GET | `/ohlcv` | 横截面：多只 × 单日（最多 200 只） |
| GET | `/ohlcv/latest` | 批量取每只股票最新一行（最多 500 只） |

`symbol` 接受 6 位数字或带前缀（`sh.` / `sz.` / `bj.`，大小写不敏感，含 `SH600000` 这种无点紧凑写法）。

### 示例

```bash
# 单只最近 5 天
curl 'http://localhost:8000/symbols/000034/ohlcv?limit=5'

# 单只日期范围
curl 'http://localhost:8000/symbols/600000/ohlcv?start=2026-09-01&end=2026-09-14'

# 横截面
curl 'http://localhost:8000/ohlcv?date=2026-09-11&symbols=000034,600000,sh.688981'

# 批量最新一行
curl 'http://localhost:8000/ohlcv/latest?symbols=000034,600000,300750'
```

错误码：

- `400` — symbol 格式错、limit/offset 越界、日期格式错
- `404` — symbol 不存在或无数据
- `503` — DB 不可读（健康检查失败）

---

## MCP 工具

跟 HTTP 端点一一对应：

| 工具 | 参数 | 说明 |
|---|---|---|
| `list_symbols` | — | 全部股票代码 |
| `get_db_stats` | — | 数据库总览 |
| `get_symbol_meta` | `symbol` | 单只元信息 |
| `get_ohlcv` | `symbol, start?, end?, limit?, order?` | 单只 OHLCV |
| `get_cross_section` | `symbols, date` | 横截面 |
| `get_latest` | `symbols` | 批量最新一行 |

---

## 设计要点

- **只读物理隔离**：用 SQLite `mode=ro` URI 打开，OS 级只读。即使代码 bug
  触发 INSERT 也会被 SQLite 直接拒绝，绝不污染 main.py 日常写入的数据。
- **旁路 DataEngine**：不复用 `engine._open_db`，避免 Py 3.14 下 with-block
  不 close fd 的路径；也不触发 `preload_all_ohlcv` 的 ~268MB 内存缓存。
- **WAL 并发**：main.py 用 WAL 模式写盘时，服务端读不阻塞。
- **符号归一化**：`sh.600000` / `SH600000` / `600000` 都映射成 `600000`，
  严格 6 位数字校验。港股、美股代码会直接 400。
- **无状态**：每请求开新连接，SQLite 进程内连接几乎零开销；多 worker 不开
  （fork 会触发 fd 继承问题，需要横向扩展时前面加 nginx / caddy）。

---

## systemd 部署模板

放 `/etc/systemd/system/sequoia-x-api.service`：

```ini
[Unit]
Description=Sequoia-X Data API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/Sequoia-X
EnvironmentFile=/opt/Sequoia-X/.env
ExecStart=/opt/Sequoia-X/.venv/bin/python serve.py --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
# fd 留够缓冲：8 策略 + 服务端并发 1 + 基础 20 = 100 足够
LimitNOFILE=1024

[Install]
WantedBy=multi-user.target
```

启用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sequoia-x-api
sudo systemctl status sequoia-x-api
```

---

## 测试

```bash
.venv/bin/python -m pytest tests/test_api.py -v

# 32 个用例：符号归一化 + 临时 DB 业务查询 + 真实 DB 冒烟
```

`tests/test_feishu.py` 里有 2 个跟本服务无关的旧失败（mock 路径问题，git
stash 验证过是 pre-existing），不影响数据服务。
