# 🛡️ VibeGuard AI — Neural Security Layer for opBNB

**AI-powered on-chain/off-chain security infrastructure protecting users from scams, honeypots, and malicious contracts in real time.**

[![Deployed on opBNB](https://img.shields.io/badge/Deployed_on-opBNB-00BFFF)](https://opbnbscan.com/address/0x6D32BA27Cb51292F71C0Ee602366e7BFE586c9F6)
[![BAP-578 NFA](https://img.shields.io/badge/BAP--578_NFA-8A2BE2)](https://github.com/bnb-chain/BAP-578)
[![Multi-LLM](https://img.shields.io/badge/AI-Gemini_2.0_×_Grok_4_×_DeepSeek-FF4500)](https://deepmind.google/technologies/gemini/)
[![Live](https://img.shields.io/badge/Status-Live-brightgreen)](https://t.me/VibeGuard_AI_bot)

---

## 📊 Live Metrics (as of February 27, 2026)

- **Blocks processed:** 5,095,332
- **Whales detected:** 187
- **Threats identified:** 4
- **Value analyzed:** $1.1M+
- **Active Guardian NFTs:** 1
- **Shielded wallets:** 1

**[Telegram Bot](https://t.me/VibeGuard_AI_bot)** | **[Live Dashboard](https://vibe-guard-dashboard.vercel.app)** | **[Pitch Deck](https://vibe-guard-presentation.vercel.app)** | **[Contract](https://opbnbscan.com/address/0x6D32BA27Cb51292F71C0Ee602366e7BFE586c9F6)**

---

## 🎯 What is VibeGuard AI?

VibeGuard AI is a **neural security layer** for the opBNB blockchain. It continuously monitors every block, analyzes transactions using a ensemble of large language models (Gemini 2.0 Flash, Grok 4, DeepSeek), and provides **real‑time risk assessments** to users.

Unlike traditional security tools that only detect threats after they happen, VibeGuard operates **proactively**:
- It intercepts suspicious transaction patterns before user confirmation.
- It audits smart contract code on‑demand.
- It issues **on‑chain attestations** via Guardian NFTs (BAP‑578) to record protection history.

---

## 🧠 Architecture Overview
┌─────────────┐ ┌─────────────────┐ ┌──────────────────┐
│ User │────▶│ Telegram Bot │────▶│ Security Layer │
│ (Telegram) │ │ (Interface) │ │ (AI + GoPlus) │
└─────────────┘ └─────────────────┘ └────────┬─────────┘
│
▼
┌─────────────┐ ┌─────────────────┐ ┌──────────────────┐
│ opBNB │◀────│ Guardian NFT │◀────│ On‑chain Attest. │
│ Blockchain │ │ (BAP‑578 Agent) │ │ (LogScan events) │
└─────────────┘ └─────────────────┘ └──────────────────┘

**Key components:**
- **Security Layer:** Python + asyncio, real‑time block scanner, multi‑LLM intent analysis, GoPlus pre‑filter.
- **Guardian NFT (BAP‑578):** Non‑fungible agent minted by the protocol for each user. Stores `protectedAmount`, `scanCount`, and a Merkle root of its "memory" – all on‑chain.
- **On‑chain Attestations:** Every threat detection is logged immutably on opBNB via `logScan` events, creating verifiable proof of protection.
- **User Interfaces:** Telegram bot (primary), WebApp for wallet connection, and a live dashboard with real‑time metrics.

---

## 🛠 Tech Stack

| Component          | Technology |
|--------------------|------------|
| Blockchain         | opBNB Mainnet, Solidity, BAP‑578 |
| AI Models          | Gemini 2.0 Flash, Grok 4, DeepSeek |
| Backend            | Python 3.12, asyncio, web3.py, PostgreSQL |
| Frontend (Bot)     | Telegram Bot API, Reown AppKit |
| Frontend (Dashboard)| Next.js, Tailwind, Recharts |
| Infrastructure     | Docker, Railway, Vercel |

---

## 🚀 How It Works (End‑to‑End Flow)

1. **User connects wallet** via the Telegram WebApp (Reown AppKit).
2. **Guardian NFT is minted** by the protocol (gas paid by the owner – frictionless onboarding).
3. **Real‑time monitoring starts**: every new block is scanned; transactions are filtered through GoPlus and then analyzed by the AI ensemble.
4. **If a threat is detected** (e.g., honeypot, drainer contract), an instant alert is sent to the user with a structured risk report (`verdict`, `confidence`, `risk_factors`).
5. **Every scan is attested on‑chain** via `logScan` event, creating an immutable audit trail.
6. **Users can query their Guardian** via `/guardian` to see protected amount and scan count.

---

## � Why Blockchain? (The BAP‑578 Narrative)

Guardian NFTs are **protocol‑owned security agents**, not user‑collectibles. They are minted by the VibeGuard protocol to users **for free**, eliminating gas friction and preventing speculation. The on‑chain state (`protectedAmount`, `scanCount`) serves as **verifiable proof** of the agent's activity, while the actual user binding is kept off‑chain for scalability. This design allows:

- Gasless onboarding for mass adoption.
- Immutable protection history.
- Future composability with other DeFi protocols (e.g., proof of protection for lending).

---

## Smart Contracts

- **Guardian NFT (BAP-578):** [`0x6D32BA27Cb51292F71C0Ee602366e7BFE586c9F6`](https://opbnbscan.com/address/0x6D32BA27Cb51292F71C0Ee602366e7BFE586c9F6)  
  ERC-721 токен, представляющий персонального защитника пользователя. Хранит `protectedAmount`, `scanCount` и историю обучения.

- **VibeGuard Logging Contract:** [`0x6e5e4e9e9c4f5e498393c4b6216781a28e15902f`](https://opbnbscan.com/address/0x6e5e4e9e9c4f5e498393c4b6216781a28e15902f)  
  Используется для on-chain записи всех событий сканирования (`logScan`) и защиты кошельков (`shieldWallet`). Обеспечивает прозрачность и неизменяемость логов.

---

## 📈 Roadmap

- **Q1 2026** — MVP on opBNB, Guardian NFT minting, structured AI output.
- **Q2 2026** — Chrome extension with real‑time transaction interception.
- **Q3 2026** — Mobile app (iOS/Android) and B2B API for wallets.
- **Q4 2026** — $VIBE token for governance and staking, multisig owner.

---

## 🤝 Contacts

- **Telegram:** [@tarran6](https://t.me/tarran6)
- **X:** [@Tarran6](https://x.com/Tarran6)
- **GitHub:** [Tarran6/VibeGuard-AI](https://github.com/Tarran6/VibeGuard-AI)

---

**Built with ❤️ on opBNB + Grok 4 + BAP‑578**
