"""
텔레그램 알림 모듈
"""
import requests
from utils.logger import get_logger
from config import Config

logger = get_logger("Telegram")


class TelegramNotifier:
    def __init__(self):
        self.token   = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)

    def send(self, message: str) -> bool:
        if not self.enabled:
            return False
        url  = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
        try:
            resp = requests.post(url, data=data, timeout=10)
            return resp.status_code == 200
        except Exception as e:
            logger.warning(f"텔레그램 전송 실패: {e}")
            return False

    def notify_buy(self, name, code, price, qty, reason):
        msg = (
            f"🟢 <b>매수 체결</b>\n"
            f"종목: {name} ({code})\n"
            f"가격: {price:,}원 × {qty}주\n"
            f"금액: {price*qty:,}원\n"
            f"사유: {reason[:100]}"
        )
        self.send(msg)

    def notify_sell(self, name, code, price, qty, profit, reason):
        emoji = "💰" if profit >= 0 else "🔴"
        msg = (
            f"{emoji} <b>매도 체결</b>\n"
            f"종목: {name} ({code})\n"
            f"가격: {price:,}원 × {qty}주\n"
            f"손익: {profit:+,.0f}원\n"
            f"사유: {reason[:100]}"
        )
        self.send(msg)

    def notify_system(self, msg_text: str):
        self.send(f"⚙️ <b>시스템</b>: {msg_text}")
