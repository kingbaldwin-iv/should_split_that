// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
}

interface IWETH {
    function deposit() external payable;
}

interface ITessera {
    function tesseraSwapWithAllowances(
        address tokenIn, address tokenOut, int256 amountSpecified,
        uint256 amountCheck, address recipient, bytes calldata swapData
    ) external;
}

/// @notice Wrap ETH, sell WETH once, then buy WETH in one or more pieces.
/// @dev Simulated through debug_traceCall with code at a verified unused address.
contract EthOnlySplitHelper {
    address constant WETH = 0x4200000000000000000000000000000000000006;
    address constant USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address constant TESSERA = 0x55555522005BcAE1c2424D474BfD5ed477749E3e;

    function run(uint256 totalSell, uint256 totalBuy, uint256 pieces, address receiver)
        external payable
    {
        require(msg.value == totalSell, "ETH funding");
        require(totalSell > 0 && totalBuy > 0 && pieces > 0 && pieces <= totalBuy, "size");
        require(totalSell <= uint256(type(int256).max), "sell too large");
        require(totalBuy <= uint256(type(int256).max), "buy too large");

        // This normal WETH deposit is the only token funding step.
        IWETH(WETH).deposit{value: msg.value}();
        require(IERC20(WETH).approve(TESSERA, type(uint256).max), "WETH approval");
        require(IERC20(USDC).approve(TESSERA, type(uint256).max), "USDC approval");

        ITessera(TESSERA).tesseraSwapWithAllowances(
            WETH, USDC, int256(totalSell), 1, address(this), ""
        );

        uint256 buySize = totalBuy / pieces;
        uint256 leftoverWei = totalBuy % pieces;
        for (uint256 i; i < pieces; ++i) {
            uint256 wethToBuy = buySize + (i < leftoverWei ? 1 : 0);
            ITessera(TESSERA).tesseraSwapWithAllowances(
                USDC, WETH, -int256(wethToBuy), type(uint256).max, receiver, ""
            );
        }
    }
}
