# Tessera: sell 100 WETH, buy 80 WETH in one or five swaps

Both self-contained requests were executed successfully with `debug_traceCall`
against the supplied Base archive RPC. These are simulations, not broadcast
transactions.

## Requests and outputs

- `sell100_buy80_once.request.json`: sell 100 WETH, then buy 80 WETH once.
- `sell100_buy80_in5.request.json`: sell 100 WETH, then buy 16 WETH five times.
- Matching `.response.json` files contain the full JSON-RPC responses.
- Matching `.calltrace.json` files contain just the callTracer result.

Both use Base block **50,710,033** (`0x305c611`), **txIndex 0**, and identical
explicit block overrides copied from that block's header. The opening Tessera
swap sees the parent-state accumulators: 0.747117458585022290 WETH and 0 USDC.

The requests embed the compiled helper **runtime** bytecode. No deployment,
local Anvil state, preparatory transaction, private key, or implicit funding
state is needed to replay them.

## State overrides and funding

There are exactly two account overrides:

1. Sender: ETH balance = 101 ETH.
2. Helper: runtime code = `EthOnlySplitHelper.sol` compiled with solc 0.8.24,
   optimizer enabled and EVM target `paris`.

There are no token-storage, Tessera-storage, manager, receiver, nonce, or
classification overrides. The helper's code belongs under `code` inside
`stateOverrides`; `stateDiff` is for storage words and is not needed here.

Each call sends 100 ETH to the helper. It wraps that ETH through WETH's normal
`deposit()`, sells all 100 WETH through Tessera, and uses the USDC proceeds to buy
80 WETH. The buys send WETH to the separate receiver; unused USDC stays in the
helper. There is no external funding swap.

## Results from native Tessera swap logs

| Strategy | Sell proceeds (USDC) | Buy cost (USDC) | Average buy price (USDC/WETH) |
|---|---:|---:|---:|
| One 80-WETH buy | 247,178.815642 | 197,793.577879 | 2,472.4197234875 |
| Five 16-WETH buys | 247,178.815642 | 197,786.582885 | 2,472.3322860625 |

Splitting saves **6.994994 USDC before gas**. Each 16-WETH buy costs
39,557.316577 USDC. Both traces transfer exactly 80 WETH to the receiver.

## Address checks

The sender, helper, and receiver are three randomly generated addresses. Before
overrides, historical reads at the parent block found zero ETH balance, nonce,
code, WETH balance and USDC balance for all three, and manager status zero.
Current-state account proofs also show zero balance/nonce and empty code/storage.

The RPC cannot provide account proofs this far back, so historical checks are
ordinary archive RPC reads rather than historical Merkle proofs. Their exact
results are included in `historical_address_checks.json`; current proofs are
included separately. These are checks at the specified states, not a claim
about every external contract's mappings or every point in chain history.

In the actual traces, every sender/pricing-account manager status is **0**
(ordinary), and every Tessera address-helper predicate returns **false**.
No address classification is overridden.

## Replay

Set your archive Base RPC in `BASE_RPC_URL`, then run from this directory:

```bash
curl --fail-with-body -sS "$BASE_RPC_URL" \
  -H 'Content-Type: application/json' \
  --data-binary @sell100_buy80_once.request.json

curl --fail-with-body -sS "$BASE_RPC_URL" \
  -H 'Content-Type: application/json' \
  --data-binary @sell100_buy80_in5.request.json
```

The provider must support historical `debug_traceCall`, `txIndex`,
`stateOverrides`, `blockOverrides`, and `callTracer` with logs. The complete
requests contain no RPC URL or credentials. Request options follow the
[debug_traceCall schema](https://geth.ethereum.org/docs/interacting-with-geth/rpc/ns-debug#debug_tracecall).
