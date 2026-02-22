// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title VibeGuard AI Sentinel
 * @dev Updated for BNB Chain Grant traction
 * Deployed on opBNB: 0x427398aa19D86d7df10Fa13D9b75e94c8a1a511b (old)
 * New version will be deployed soon
 */
contract VibeGuard {
    address public owner;
    string public currentVibe = "System Initialized";
    uint256 public trustScore = 100;

    // === НОВЫЕ МЕТРИКИ ДЛЯ ГРАНТА ===
    uint256 public totalScans;
    uint256 public totalShieldedWallets;
    mapping(address => bool) public isShielded;
    mapping(address => uint256) public walletVibeScore;

    event VibeUpdated(string newVibe, uint256 score, address updatedBy);
    event ScanPerformed(address contractAddr, uint256 vibeScore, bool isSafe, address user);
    event WalletShielded(address wallet, uint256 score);

    constructor() {
        owner = msg.sender;
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    function setVibe(string memory _newVibe, uint256 _score) public onlyOwner {
        currentVibe = _newVibe;
        trustScore = _score;
        emit VibeUpdated(_newVibe, _score, msg.sender);
    }

    function logScan(address _contract, uint256 _score, bool _isSafe, address _user) public onlyOwner {
        totalScans++;
        emit ScanPerformed(_contract, _score, _isSafe, _user);
    }

    function shieldWallet(address _wallet, uint256 _score) public onlyOwner {
        require(!isShielded[_wallet], "Already shielded");
        isShielded[_wallet] = true;
        walletVibeScore[_wallet] = _score;
        totalShieldedWallets++;
        emit WalletShielded(_wallet, _score);
    }

    function transferOwnership(address newOwner) public onlyOwner {
        owner = newOwner;
    }
}
