"""
Комплексные тесты VibeGuard AI для грантовой оценки.
Покрывают реальную логику работы системы.
"""

import pytest
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch
from web3 import Web3
import sys

# Добавляем src в путь для импорта
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

# Импортируем тестируемые модули
from bot import (
    verify_wallet, check_scam, process_bnb_tx, process_erc20_log,
    require_multisig, confirm_action, is_owner, _pending_actions,
    bnb_to_usd, token_to_usd, get_decimals, rpc
)
from agent_bot import analyze_event_ai


class TestWalletVerification:
    """Тесты верификации кошельков"""
    
    @pytest.mark.asyncio
    async def test_valid_wallet_verification(self):
        """Тест успешной верификации валидного кошелька"""
        user_id = 12345
        address = "0x742d35Cc6634C0532925a3b8D4C9db96C4b4Db45"
        signature = "0x" + "a" * 130  # Мок подписи
        
        with patch('bot.db') as mock_db, \
             patch('bot.save_db') as mock_save, \
             patch('bot.Web3.is_address', return_value=True), \
             patch('bot.encode_defunct'), \
             patch('bot.Web3().eth.account.recover_message', return_value=address):
            
            mock_db.__getitem__ = MagicMock(return_value={
                "pending_verifications": {
                    str(user_id): {"nonce": "test_nonce", "ts": asyncio.get_event_loop().time()}
                }
            })
            mock_db.__setitem__ = MagicMock()
            mock_db.get = MagicMock(return_value=[])
            
            result, message = await verify_wallet(user_id, address, signature)
            
            assert result is True
            assert "✅ Кошелёк подключён" in message
            mock_save.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_invalid_wallet_address(self):
        """Тест невалидного адреса кошелька"""
        user_id = 12345
        address = "invalid_address"
        signature = "0x" + "a" * 130
        
        result, message = await verify_wallet(user_id, address, signature)
        
        assert result is False
        assert "Невалидный формат адреса" in message
    
    @pytest.mark.asyncio
    async def test_invalid_signature_format(self):
        """Тест невалидного формата подписи"""
        user_id = 12345
        address = "0x742d35Cc6634C0532925a3b8D4C9db96C4b4Db45"
        signature = "short_signature"
        
        result, message = await verify_wallet(user_id, address, signature)
        
        assert result is False
        assert "Невалидный формат подписи" in message


class TestScamDetection:
    """Тесты определения скам-контрактов"""
    
    @pytest.mark.asyncio
    async def test_honeypot_detection(self):
        """Тест определения honeypot"""
        addr = "0x742d35Cc6634C0532925a3b8D4C9db96C4b4Db45"
        
        mock_response = {
            "result": {
                addr.lower(): {
                    "is_honeypot": "1",
                    "is_open_source": "0",
                    "is_proxy": "0",
                    "can_take_back_ownership": "0",
                    "hidden_owner": "0"
                }
            }
        }
        
        with patch('bot.Web3.is_address', return_value=True), \
             patch('bot.http_session.get') as mock_get:
            
            mock_get.return_value.__aenter__.return_value.status = 200
            mock_get.return_value.__aenter__.return_value.json = AsyncMock(return_value=mock_response)
            
            risks = await check_scam(addr)
            
            assert "🍯 HONEYPOT" in risks
            assert "🔐 ЗАКРЫТЫЙ КОД" in risks
    
    @pytest.mark.asyncio
    async def test_safe_contract(self):
        """Тест безопасного контракта"""
        addr = "0x742d35Cc6634C0532925a3b8D4C9db96C4b4Db45"
        
        mock_response = {
            "result": {
                addr.lower(): {
                    "is_honeypot": "0",
                    "is_open_source": "1",
                    "is_proxy": "0",
                    "can_take_back_ownership": "0",
                    "hidden_owner": "0"
                }
            }
        }
        
        with patch('bot.Web3.is_address', return_value=True), \
             patch('bot.http_session.get') as mock_get:
            
            mock_get.return_value.__aenter__.return_value.status = 200
            mock_get.return_value.__aenter__.return_value.json = AsyncMock(return_value=mock_response)
            
            risks = await check_scam(addr)
            
            assert len(risks) == 0


class TestTransactionProcessing:
    """Тесты обработки транзакций"""
    
    @pytest.mark.asyncio
    async def test_whale_detection_bnb(self):
        """Тест определения кита (крупной транзакции BNB)"""
        tx = {
            "value": "0xDE0B6B3A7640000",  # 1000 BNB в hex
            "from": "0x742d35Cc6634C0532925a3b8D4C9db96C4b4Db45",
            "to": "0x8ba1f109551bD432803012645Hac136c"
        }
        
        with patch('bot.db_lock') as mock_lock, \
             patch('bot.bnb_to_usd', return_value=500000),  # $500K
             patch('bot._wallet_watchers', return_value=[]), \
             patch('bot.call_ai', return_value="Крупная транзакция"), \
             patch('bot.check_scam', return_value=[]), \
             patch('bot.notify_owners') as mock_notify, \
             patch('bot.log_onchain') as mock_log:
            
            mock_lock.__aenter__ = AsyncMock()
            mock_db = {
                "cfg": {"limit_usd": 10000, "ignore": [], "watch": []},
                "stats": {"whales": 0}
            }
            mock_lock.__aenter__.return_value = mock_db
            
            await process_bnb_tx(tx)
            
            # Проверяем, что статистика обновилась
            assert mock_db["stats"]["whales"] == 1
            # Проверяем, что владельцы уведомлены
            mock_notify.assert_called()
            # Проверяем, что транзакция залогирована в блокчейн
            mock_log.assert_called()
    
    @pytest.mark.asyncio
    async def test_erc20_whale_detection(self):
        """Тест определения кита (ERC20 токен)"""
        log = {
            "address": "0x742d35Cc6634C0532925a3b8D4C9db96C4b4Db45",
            "topics": [
                "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                "0x000000000000000000000000742d35cc6634c0532925a3b8d4c9db96c4b4db45",
                "0x0000000000000000000000008ba1f109551bd432803012645hac136c"
            ],
            "data": "0x152D02C7E14AF6800000"  # 1000000 токенов
        }
        
        with patch('bot.db_lock') as mock_lock, \
             patch('bot.get_decimals', return_value=18), \
             patch('bot.token_to_usd', return_value=75000),  # $75K
             patch('bot._wallet_watchers', return_value=[]), \
             patch('bot.call_ai', return_value="Крупный перевод токенов"), \
             patch('bot.check_scam', return_value=[]), \
             patch('bot.notify_owners') as mock_notify:
            
            mock_lock.__aenter__ = AsyncMock()
            mock_db = {
                "cfg": {"limit_usd": 10000, "ignore": [], "watch": []},
                "stats": {"whales": 0}
            }
            mock_lock.__aenter__.return_value = mock_db
            
            await process_erc20_log(log)
            
            assert mock_db["stats"]["whales"] == 1
            mock_notify.assert_called()


class TestMultisig:
    """Тесты мультиподписей"""
    
    @pytest.mark.asyncio
    async def test_single_owner_action(self):
        """Тест действия с одним владельцем"""
        with patch('bot.OWNERS', {12345}):
            result, message = await require_multisig("test_action", "target", 12345)
            
            assert result is True
            assert "Одиночный владелец" in message
    
    @pytest.mark.asyncio
    async def test_multisig_require_confirmation(self):
        """Тест требования мультиподписи"""
        with patch('bot.OWNERS', {12345, 67890}), \
             patch('bot.MULTISIG_THRESHOLD', 2), \
             patch('bot.save_db') as mock_save:
            
            result, message = await require_multisig("test_action", "target", 12345)
            
            assert result is False
            assert "2 подтверждений" in message
            assert "Получено: 1/2" in message
            mock_save.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_multisig_confirmation(self):
        """Тест подтверждения мультиподписи"""
        action_id = "test_action:target:1234567890"
        
        with patch('bot.MULTISIG_THRESHOLD', 2), \
             patch('bot.save_db') as mock_save:
            
            _pending_actions[action_id] = {
                "type": "test_action",
                "target": "target",
                "initiator": 12345,
                "confirmations": {12345},
                "required": 2,
                "ts": asyncio.get_event_loop().time()
            }
            
            result, message = await confirm_action(action_id, 67890)
            
            assert result is True
            assert "Действие подтверждено" in message
            assert action_id not in _pending_actions
            mock_save.assert_called_once()


class TestAIAnalysis:
    """Тесты AI-анализа"""
    
    def test_ai_analysis_no_key(self):
        """Тест AI-анализа без API ключа"""
        with patch.dict(os.environ, {'GEMINI_API_KEY': ''}):
            result = analyze_event_ai("high", 5)
            
            assert "AI analysis skipped" in result
    
    def test_ai_analysis_success(self):
        """Тест успешного AI-анализа"""
        mock_response = MagicMock()
        mock_response.text = "Professional security analysis report"
        
        with patch.dict(os.environ, {'GEMINI_API_KEY': 'test_key'}), \
             patch('agent_bot.genai.Client') as mock_client, \
             patch('agent_bot.genai.Client.models.generate_content', return_value=mock_response):
            
            result = analyze_event_ai("medium", 3)
            
            assert "Professional security analysis report" in result


class TestSecurityValidation:
    """Тесты безопасности и валидации"""
    
    def test_is_owner_validation(self):
        """Тест проверки владельца"""
        with patch('bot.OWNERS', {12345, 67890}):
            assert is_owner(12345) is True
            assert is_owner(99999) is False
    
    def test_web3_address_validation(self):
        """Тест валидации адресов Web3"""
        valid_addr = "0x742d35Cc6634C0532925a3b8D4C9db96C4b4Db45"
        invalid_addr = "invalid_address"
        
        assert Web3.is_address(valid_addr) is True
        assert Web3.is_address(invalid_addr) is False


class TestPriceCalculations:
    """Тесты расчета цен"""
    
    @pytest.mark.asyncio
    async def test_bnb_to_usd_conversion(self):
        """Тест конвертации BNB в USD"""
        with patch('bot.refresh_bnb_price') as mock_refresh, \
             patch('bot._price_cache', {'BNB': 600.0}):
            
            mock_refresh.return_value = None
            
            result = await bnb_to_usd(1.5)
            
            assert result == 900.0
    
    @pytest.mark.asyncio
    async def test_token_to_usd_conversion(self):
        """Тест конвертации токенов в USD"""
        token_addr = "0x742d35Cc6634C0532925a3b8D4C9db96C4b4Db45"
        raw_amount = 1000000000000000000000  # 1000 токенов с 18 decimals
        decimals = 18
        
        with patch('bot._token_price_cache', {token_addr: (0.5, asyncio.get_event_loop().time())}):
            result = await token_to_usd(token_addr, raw_amount, decimals)
            
            assert result == 500.0  # 1000 * $0.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
