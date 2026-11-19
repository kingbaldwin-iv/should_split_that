Thank you @evmknows for independently verifying the claim.
Alright, today we are going to talk about a relatively interesting optimization mainly relevant for aggregators and searchers. The case in study is Base deployment of Tessera, the main propAMM on Base by historical volume. 
TL;DR: In certain pool states, buying a fixed amount of ETH through several smaller swaps within one transaction can require less USDC than buying it in a single swap. 
This can be done simply via help of one contract that just batches the Tessera swap request into multiple Tessera swaps. This is one of the differentiating factor of Tessera to traditional AMMs as we know it given their pricing does not let this happen. 
Probably no need to go into specific implementation details but the rough idea is that Tessera keep tracks of historical flow through its pool for both tokens. A swap can have 2 different effects. Either it reduces the liquidity on the corresponding side or it replenishes on the opposite side (or both). The latter happens e.g. when the swap wants to buy but previous transactions have been net sell. So for buy swap to actually have a “liquidity reduction” effect first it needs to offset the previous sell flow. 

<img width="1200" height="571" alt="image" src="https://github.com/user-attachments/assets/a303ca4d-c7fe-440e-babd-d7a5cec2c9f4" />
A simple abstracted example is that imagine most flow has been selling into pool previously, leaving a sufficiently large net selling imbalance. Now imagine ask side is 1 ETH at 100 and another 1 ETH at 101. If you then come and buy 2 ETH in one swap you execute at 100.5. But if you batch into 2 swaps of 1 ETH in single transaction what happens is after first swap the bid side gets replenished instead of eating the ask side. Then in the second buy you can execute at 100 again giving you execution price of 100 rather than 100.5.
A historically reproduced example is top of block 50,199,36: 

<img width="1200" height="571" alt="image" src="https://github.com/user-attachments/assets/b9807e1a-ab98-40d3-9ff0-7c201a291c92" />
