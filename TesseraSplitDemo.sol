// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
}

interface ITessera {
    function tesseraSwapWithAllowances(
        address tokenIn,
        address tokenOut,
        int256 amountSpecified,
        uint256 amountCheck,
        address recipient,
        bytes calldata swapData
    ) external;
}

/// @notice Execute a sell followed by split buys on a local Base fork.
/// @dev Python funds this contract and reads Tessera's native swap events.
contract TesseraSplitDemo {
    address constant WETH = 0x4200000000000000000000000000000000000006;
    address constant USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address constant TESSERA = 0x55555522005BcAE1c2424D474BfD5ed477749E3e;

    /// @param totalSell WETH to sell first, in wei. Zero skips the sell.
    /// @param totalBuy Total WETH to buy back, in wei.
    /// @param pieces Number of exact-output swaps used to buy totalBuy.
    function run(uint256 totalSell, uint256 totalBuy, uint256 pieces) external {
        require(totalBuy > 0 && pieces > 0 && pieces <= totalBuy, "bad size");
        require(totalSell <= uint256(type(int256).max), "sell too large");
        require(totalBuy <= uint256(type(int256).max), "buy too large");
        require(IERC20(WETH).approve(TESSERA, type(uint256).max), "WETH approval");
        require(IERC20(USDC).approve(TESSERA, type(uint256).max), "USDC approval");

        if (totalSell > 0) {
            // A positive amountSpecified means exact input: sell this much WETH.
            ITessera(TESSERA).tesseraSwapWithAllowances({
                tokenIn: WETH,
                tokenOut: USDC,
                amountSpecified: int256(totalSell),
                amountCheck: 1, // Minimum USDC output, in raw token units.
                recipient: address(this),
                swapData: ""
            });
        }

        uint256 buySize = totalBuy / pieces;
        uint256 leftoverWei = totalBuy % pieces;

        for (uint256 i; i < pieces; ++i) {
            // Give the first few buys one extra wei when totalBuy is not divisible.
            uint256 wethToBuy = buySize + (i < leftoverWei ? 1 : 0);

            // A negative amountSpecified means exact output: receive this much WETH.
            ITessera(TESSERA).tesseraSwapWithAllowances({
                tokenIn: USDC,
                tokenOut: WETH,
                amountSpecified: -int256(wethToBuy),
                amountCheck: type(uint256).max, // No USDC input cap in this demo.
                recipient: address(this),
                swapData: ""
            });
        }
    }
}
