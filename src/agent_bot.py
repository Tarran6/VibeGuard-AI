"""
VibeGuard Agent Bot — опциональный модуль для AI-анализа алертов.
Использует Google Gemini. Для работы: pip install google-genai
"""
import logging
import os
from web3 import Web3

# Network: opBNB Mainnet
# Verified Contract: 0x427398aa19D86d7df10Fa13D9b75e94c8a1a511b
CONTRACT_ADDRESS = "0x427398aa19D86d7df10Fa13D9b75e94c8a1a511b"
RPC_URL = "https://opbnb-mainnet-rpc.bnbchain.org"

logger = logging.getLogger("vibeguard.agent")


def analyze_event_ai(status: str, risk: int) -> str:
    """Генерирует отчёт по алерту через Gemini. Требует GEMINI_API_KEY и google-genai."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return "AI analysis skipped (GEMINI_API_KEY not set)."
    try:
        from google import genai
    except ImportError:
        logger.warning("google-genai не установлен — pip install google-genai")
        return "AI analysis failed (google-genai not installed)."
    try:
        client = genai.Client(api_key=key)
        prompt = (
            f"VibeGuard Alert: Status '{status}', Risk {risk}/5. "
            "Write a professional security report in English."
        )
        response = client.models.generate_content(
            model="gemini-2.0-flash", contents=prompt
        )
        return response.text or "AI analysis failed (empty response)."
    except Exception as e:
        logger.warning("Agent AI error: %s", e)
        return "AI analysis failed."


if __name__ == "__main__":
    print(f"VibeGuard Agent Bot monitoring contract: {CONTRACT_ADDRESS}")
