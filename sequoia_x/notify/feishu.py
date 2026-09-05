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

    @staticmethod
    def _get_stock_names(symbols: list[str]) -> dict[str, str]:
        import baostock as bs
        bs.login()
        mapping = {}
        for code in symbols:
            prefix = "sh" if code.startswith(("6", "9")) else "sz"
            rs = bs.query_stock_basic(code=f"{prefix}.{code}")
            while rs.next():
                row = rs.get_row_data()
                mapping[code] = row[1]
        bs.logout()
        return mapping

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
