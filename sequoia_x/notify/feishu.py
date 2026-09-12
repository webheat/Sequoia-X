"""飞书通知模块：将选股结果推送至飞书群。

支持两种推送渠道（由 Settings.feishu_mode 决定）：
  - webhook ：自定义机器人 Webhook（原有逻辑）
  - app     ：自建应用 API（OpenAPI tenant_access_token + im/v1/messages）
  - auto    ：优先 app（有凭据时），否则回退 webhook
"""

import json
import time
from datetime import date

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class FeishuNotifier:
    """飞书推送器。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._token: str | None = None
        self._token_expire_at: float = 0.0
        # 决定本次走哪条渠道
        self._channel: str = self._resolve_channel()
        # 9/11 18:07 失败根因之一：8 个策略每个推送都重新 bs.login/logout，
        # 每次 session 重新走 baostock socket + 触发 importlib netrc.py，
        # 在 fd 已紧张的进程里直接把 fd 打爆。改成实例级缓存：整次 main 流程
        # 只在第一次推送时打开 baostock session，后续 7 次直接命中。
        self._name_cache: dict[str, str] = {}

    def _resolve_channel(self) -> str:
        mode = (self.settings.feishu_mode or "auto").lower()
        if mode == "app":
            return "app"
        if mode == "webhook":
            return "webhook"
        # auto
        if self.settings.feishu_app_id and self.settings.feishu_app_secret and (
            self.settings.feishu_chat_id or self.settings.feishu_open_id
        ):
            return "app"
        return "webhook"

    # ── 共用：把代码转雪球链接 / 拿股票名 ──
    @staticmethod
    def _to_xueqiu_code(code: str) -> str:
        if code.startswith("6"):
            return f"SH{code}"
        elif code.startswith(("4", "8")):
            return f"BJ{code}"
        return f"SZ{code}"

    def _get_stock_names(self, symbols: list[str]) -> dict[str, str]:
        """股票代码 → 中文名。baostock 抽风时返回部分结果，绝不抛异常。

        baostock 偶发 "接收数据异常" / "timed out" 时，
        query_stock_basic 会返回不完整的 row（get_row_data 是空列表或长度不足），
        直接 row[1] 会 IndexError，进而让整个 main 流程挂掉。
        这里逐 symbol 兜底：单个失败不影响其他，且 login/logout 也包起来。
        返回值允许有缺失 → caller 的 names.get(code, fallback) 兜底显示。

        实例级缓存：同一 FeishuNotifier 实例（main.py:84 共用一个）跨策略共享，
        第一次调用走 baostock 拉全量，后续命中直接返回。9/11 fd 24 路径之一：
        8 策略 × 1 session → 8 次 baostock login/logout，每次都触发 importlib netrc。
        """
        if not symbols:
            return {}
        # 命中缓存：未命中的 symbol 才需要 baostock
        missing = [s for s in symbols if s not in self._name_cache]
        if not missing:
            return {s: self._name_cache[s] for s in symbols}

        import socket as _socket
        _socket.setdefaulttimeout(15.0)  # 与 engine._bs_fetch_batch 同语义
        import baostock as bs
        new_mapping: dict[str, str] = {}
        try:
            try:
                lg = bs.login()
                if lg.error_code != "0":
                    logger.warning(f"baostock login 失败: {lg.error_msg}，股票名将为空")
                    # login 失败也写入 cache（空值）避免下次再尝试
                    self._name_cache.update({s: s for s in missing})
                    return {s: self._name_cache.get(s, s) for s in symbols}
            except Exception as exc:
                logger.warning(f"baostock login 异常: {exc}，股票名将为空")
                self._name_cache.update({s: s for s in missing})
                return {s: self._name_cache.get(s, s) for s in symbols}

            try:
                for code in missing:
                    prefix = "sh" if code.startswith(("6", "9")) else "sz"
                    try:
                        rs = bs.query_stock_basic(code=f"{prefix}.{code}")
                    except BaseException as exc:
                        # 9/11 18:10 traceback 末端是 baostock 库自身 IndexError
                        # （metadata/stock_metadata.py:202 data.setData 越界），
                        # 继承 LookupError → Exception，用 BaseException 兜底确保
                        # 不会因为一个库 bug 把整个 main 流程挂掉。
                        logger.warning(f"baostock query_stock_basic({code}) 异常: {exc}")
                        continue
                    try:
                        while rs.next():
                            row = rs.get_row_data()
                            if len(row) > 1 and row[1]:
                                new_mapping[code] = row[1]
                                break  # 一只代码只取第一条
                    except Exception as exc:
                        logger.warning(f"baostock 解析 {code} 返回数据异常: {exc}")
                        continue
            finally:
                try:
                    bs.logout()
                except Exception:
                    pass
        except BaseException as exc:
            # 兜底再罩一层：万一 baostock 整个连接断了 / GC 出错 / KeyboardInterrupt 之外
            # 的系统异常，绝不让 _get_stock_names 把 FeishuNotifier.send 抛到 main 顶层。
            logger.warning(f"_get_stock_names 未捕获异常（已降级返回空映射）: {exc}")
            self._name_cache.update({s: s for s in missing})
            return {s: self._name_cache.get(s, s) for s in symbols}

        # 写入缓存（包含没查到的 fallback，避免下次重复尝试）
        for s in missing:
            self._name_cache[s] = new_mapping.get(s, s)
        return {s: self._name_cache.get(s, s) for s in symbols}

    def _build_card(self, symbols: list[str], strategy_name: str) -> dict:
        today = date.today().strftime("%Y-%m-%d")
        names = self._get_stock_names(symbols)

        links: list[str] = []
        for code in symbols:
            xq_code = self._to_xueqiu_code(code)
            name = names.get(code, xq_code)
            links.append(f"[{name}](https://xueqiu.com/S/{xq_code})")

        symbol_text = " ".join(links) if links else "（无选股结果）"

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"📈 Sequoia-X 选股播报 | {strategy_name}",
                    },
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**日期：** {today}\n**策略：** {strategy_name}\n**选股数量：** {len(symbols)}",
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**选股列表：**\n{symbol_text}",
                        },
                    },
                ],
            },
        }

    def _build_text(self, symbols: list[str], strategy_name: str) -> str:
        today = date.today().strftime("%Y-%m-%d")
        names = self._get_stock_names(symbols)
        body = " ".join(names.get(s, s) for s in symbols) if symbols else "（无选股结果）"
        return f"📈 Sequoia-X 选股播报 | {strategy_name}\n日期: {today} | 选股数量: {len(symbols)}\n{body}"

    # ── 渠道 1: Webhook ──
    def _send_webhook(self, symbols: list[str], strategy_name: str, webhook_key: str) -> None:
        url = self.settings.get_webhook_url(webhook_key)
        payload = self._build_card(symbols, strategy_name)
        try:
            resp = requests.post(
                url,
                data=json.dumps(payload),
                headers={"Content-Type": "application/json"},
                timeout=10,
            )
            resp_json = resp.json()
            if resp.status_code != 200 or resp_json.get("code") != 0:
                logger.error(
                    f"飞书 Webhook 推送失败 [{webhook_key}] "
                    f"HTTP={resp.status_code} body={resp.text}"
                )
            else:
                logger.info(f"飞书 Webhook 推送成功 [{webhook_key}]，共 {len(symbols)} 只股票")
        except requests.RequestException as exc:
            logger.error(f"飞书 Webhook 请求异常 [{webhook_key}]：{exc}")

    # ── 渠道 2: 自建应用 API ──
    def _get_tenant_token(self) -> str | None:
        if self._token and time.time() < self._token_expire_at - 300:
            return self._token
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.settings.feishu_app_id,
                    "app_secret": self.settings.feishu_app_secret,
                },
                timeout=10,
            )
            data = resp.json()
            if data.get("code") != 0 or "tenant_access_token" not in data:
                logger.error(f"获取 tenant_access_token 失败：{data}")
                return None
            self._token = data["tenant_access_token"]
            self._token_expire_at = time.time() + int(data.get("expire", 7200))
            return self._token
        except requests.RequestException as exc:
            logger.error(f"获取 tenant_access_token 网络异常：{exc}")
            return None

    def _send_app(self, symbols: list[str], strategy_name: str) -> None:
        token = self._get_tenant_token()
        if not token:
            return
        receive_id = self.settings.feishu_chat_id or self.settings.feishu_open_id
        if not receive_id:
            logger.error("app 模式未配置 FEISHU_CHAT_ID 或 FEISHU_OPEN_ID")
            return
        receive_id_type = "chat_id" if self.settings.feishu_chat_id else "open_id"
        text = self._build_text(symbols, strategy_name)
        try:
            resp = requests.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "receive_id": receive_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
                timeout=10,
            )
            data = resp.json()
            if resp.status_code != 200 or data.get("code") != 0:
                logger.error(
                    f"飞书 app 推送失败 [{strategy_name}] "
                    f"HTTP={resp.status_code} body={resp.text}"
                )
            else:
                logger.info(f"飞书 app 推送成功 [{strategy_name}]，共 {len(symbols)} 只股票")
        except requests.RequestException as exc:
            logger.error(f"飞书 app 网络异常 [{strategy_name}]：{exc}")

    # ── 入口 ──
    def send(
        self,
        symbols: list[str],
        strategy_name: str,
        webhook_key: str = "default",
    ) -> None:
        if self._channel == "app":
            self._send_app(symbols, strategy_name)
        else:
            self._send_webhook(symbols, strategy_name, webhook_key)
