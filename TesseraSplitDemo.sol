// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
}

interface ITessera {
    function tesseraSwapWithAllowances(
        address tokenIn, address tokenOut, int256 amountSpecified,
        uint256 amountCheck, address recipient, bytes calldata swapData
    ) external;
}

contract TesseraSplitDemo {
    address constant WETH = 0x4200000000000000000000000000000000000006;
    address constant USDC = 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913;
    address constant TESSERA = 0x55555522005BcAE1c2424D474BfD5ed477749E3e;

    /// totalSell and totalBuy are WETH amounts in wei (18 decimals).
    /// Sell once, then buy totalBuy through `pieces` exact-output swaps.
    /// Python computes prices from Tessera's own swap events in the receipt.
    function run(uint256 totalSell, uint256 totalBuy, uint256 pieces) external {
        require(totalBuy > 0 && pieces > 0 && pieces <= totalBuy, "bad size");
        require(totalSell <= uint256(type(int256).max), "sell too large");
        require(totalBuy <= uint256(type(int256).max), "buy too large");
        require(IERC20(WETH).approve(TESSERA, type(uint256).max), "WETH approval");
        require(IERC20(USDC).approve(TESSERA, type(uint256).max), "USDC approval");

        if (totalSell > 0) {
            // Positive amountSpecified: exact input. Sell totalSell WETH.
            ITessera(TESSERA).tesseraSwapWithAllowances(
                WETH, USDC, int256(totalSell), 1, address(this), ""
            );
        }

        uint256 each = totalBuy / pieces;
        uint256 remainder = totalBuy % pieces;
        for (uint256 i; i < pieces; ++i) {
            // Distribute leftover wei so the purchases sum to exactly totalBuy.
            uint256 amount = each + (i < remainder ? 1 : 0);
            // Negative amountSpecified: exact output. Buy `amount` WETH.
            ITessera(TESSERA).tesseraSwapWithAllowances(
                USDC, WETH, -int256(amount), type(uint256).max, address(this), ""
            );
        }
    }
}
