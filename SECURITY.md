# Security Policy & Self-Audit

## Current Security Practices
- No private keys in repository (only .env.example)
- Rate limiting on all LLM and RPC calls
- Input sanitization for LLM prompts (anti-injection protection)
- OnlyOwner modifier on all sensitive contract functions
- On-chain events for full transparency

## Self-Audit Checklist
- [x] Private keys never committed
- [x] WebSocket reconnection logic implemented
- [x] Contract verified on opBNB
- [x] Open-source under MIT license
- [ ] Full professional audit planned after BNB Chain Grant

We take security extremely seriously.
