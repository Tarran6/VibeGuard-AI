// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "@openzeppelin/contracts/token/ERC721/ERC721.sol";
import "@openzeppelin/contracts/token/ERC721/extensions/ERC721URIStorage.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

contract VibeGuardGuardian is ERC721, ERC721URIStorage, Ownable, ReentrancyGuard {
    uint256 private _nextTokenId;
    mapping(uint256 => bytes32) public learningRoots;      // память агента (Merkle)
    mapping(uint256 => uint256) public protectedAmount;   // сколько спасли $
    mapping(uint256 => uint256) public scanCount;         // сколько сканов сделал

    event GuardianMinted(address owner, uint256 tokenId, string name);
    event LearningUpdated(uint256 tokenId, bytes32 newRoot, uint256 protected);
    event VibeAttested(uint256 tokenId, address wallet, uint8 riskScore, uint256 timestamp);

    constructor() ERC721("VibeGuard Guardian", "VGG") Ownable(msg.sender) {}

    // Минт Guardian'а (пользователь получит NFT-защитника)
    function mintGuardian(string calldata name, string calldata imageURI) external {
        uint256 tokenId = _nextTokenId++;
        _safeMint(msg.sender, tokenId);
        _setTokenURI(tokenId, imageURI);
        emit GuardianMinted(msg.sender, tokenId, name);
    }

    // Обновление памяти агента (вызываем после каждого успешного щита)
    function updateLearning(uint256 tokenId, bytes32 newMerkleRoot, uint256 _protectedAmount) 
        external onlyOwner {
        require(ownerOf(tokenId) == msg.sender || msg.sender == owner(), "Not authorized");
        learningRoots[tokenId] = newMerkleRoot;
        protectedAmount[tokenId] += _protectedAmount;
        scanCount[tokenId]++;
        emit LearningUpdated(tokenId, newMerkleRoot, protectedAmount[tokenId]);
    }

    // Аттестация защиты (on-chain proof для гранта и дашборда)
    function attestProtection(uint256 tokenId, address wallet, uint8 riskScore) external onlyOwner {
        emit VibeAttested(tokenId, wallet, riskScore, block.timestamp);
    }

    // Стандартные ERC721 функции
    function tokenURI(uint256 tokenId) public view override(ERC721, ERC721URIStorage) returns (string memory) {
        return super.tokenURI(tokenId);
    }

    function supportsInterface(bytes4 interfaceId) public view override(ERC721, ERC721URIStorage) returns (bool) {
        return super.supportsInterface(interfaceId);
    }
}
