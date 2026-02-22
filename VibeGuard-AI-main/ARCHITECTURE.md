# VibeGuard AI Architecture

## High-Level Overview
Real-time Neural Security Sentinel for opBNB Mainnet.

### Core Components
- **Sentinel Engine**: Python + web3.py + NodeReal WebSocket
- **Intent Analysis**: Multi-LLM consensus (Gemini 2.0 Flash + Grok 4 + Claude 3.5)
- **On-chain Logging**: VibeGuard.sol (verified, with totalScans & shieldWallet)
- **Dashboard**: Next.js with live metrics and particles
- **Future**: Flutter mobile + Google Auth + AI-Firewall

## Data Flow
1. New block via WebSocket
2. GoPlus pre-filter
3. LLM intent analysis (<1.8s)
4. Vibe-Check score → Telegram alert + on-chain event + dashboard

Built with ❤️ by Taran V.S + Grok (xAI) — AI Co-Founder & Neural Architect
