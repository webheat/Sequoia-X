"""对外数据服务：FastAPI HTTP + MCP stdio。

两条路共用同一个只读 SQLite helper，互不干扰。
- HTTP：见 ``app.py``，跑 uvicorn 起常驻进程
- MCP：见 ``mcp_server.py``，stdio 传输，给外部 Agent 直接调

设计原则：
- 不复用 DataEngine：避免 preload 缓存 + Py 3.14 fd 泄漏路径
- OS 级只读 (mode=ro URI)：写不进 DB，物理上不可能污染 main.py 的数据
- 符号输入归一化：``000001`` / ``sh.000001`` / ``SH000001`` 都接受
"""
