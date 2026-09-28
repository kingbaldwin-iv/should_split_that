# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.32,<3", "pycryptodome>=3.20,<4"]
# ///
"""Compare one Tessera buy against split buys on a disposable Base fork.

    export BASE_RPC_URL='your archive Base RPC'
    uv run demo.py 3 2 2

Arguments: totalSell WETH, totalBuy WETH, pieces. Both strategies start from
the same snapshot, sell totalSell WETH, then buy totalBuy WETH. The first buys
in one swap; the second buys in pieces swaps. Each strategy is one transaction.

Execution prices come from Tessera's native swap logs, include applied penalties,
and exclude gas. Only the test contract's tokens and the test account's gas are
funded; historical Tessera configuration, helpers, custody and code are untouched.

Requires Anvil and solc 0.8.24+. Override paths with --anvil and --solc.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, getcontext
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from typing import Iterator
from urllib.parse import urlparse

from Crypto.Hash import keccak
import requests

getcontext().prec = 80

# This demo targets the Base WETH/USDC pool and its September 2026 implementation.
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
POOL = "0xf524c1bc1c64a2c99bc7eccf19ede9a1d89d5a7c"
IMPLEMENTATION = "0x6d9dd143e42b6338f4f6a7c0c26d124658f641cb"
IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
SWAP_EVENT_TOPIC = "0x56441808e0dc590c63862fb3c0c914bff286fc67b3983cd294eb33e21cca326e"
TEST_ACCOUNT = "0x76fa817e2d5b93fa3c0d21bca4547161923ec876"

BASE_CHAIN_ID = 8453
DEFAULT_FORK_BLOCK = 50_710_032
WETH_SCALE = 10**18
USDC_SCALE = 10**6
TRANSACTION_GAS_LIMIT = 16_000_000
POOL_STATE_SLOT_COUNT = 53

# Only these read methods may be sent directly to the supplied upstream RPC.
UPSTREAM_READ_METHODS = {"eth_chainId", "eth_getBlockByNumber", "eth_getStorageAt"}
REPLAY_HEADER_FIELDS = (
    "number", "timestamp", "baseFeePerGas", "miner", "gasLimit", "mixHash",
)


@dataclass
class Swap:
    """Raw token deltas from the pool's perspective: received (+), paid out (-)."""

    weth_delta: int
    usdc_delta: int


@dataclass
class ScenarioResult:
    pieces: int
    sold_weth: int
    sell_proceeds_usdc: int
    buys: list[Swap]
    gas_used: int

    @property
    def bought_weth(self) -> int:
        return sum(-swap.weth_delta for swap in self.buys)

    @property
    def spent_usdc(self) -> int:
        return sum(swap.usdc_delta for swap in self.buys)


# --- JSON-RPC and ABI encoding ------------------------------------------------

def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def rpc(url: str, method: str, params: list | None = None):
    """Send a JSON-RPC call; execution and state changes must stay on localhost."""
    if method not in UPSTREAM_READ_METHODS:
        parsed_url = urlparse(url)
        is_local_fork = parsed_url.scheme == "http" and parsed_url.hostname == "127.0.0.1"
        require(is_local_fork, f"{method} is only allowed on the local fork.")

    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    try:
        response = requests.post(url, json=payload, timeout=55)
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as error:
        # Do not expose a private RPC URL through a requests exception message.
        raise RuntimeError(f"{method}: {type(error).__name__}") from None

    if "error" in result:
        safe_error = json.dumps(result["error"]).replace(url, "[RPC]")
        raise RuntimeError(f"{method}: {safe_error}")
    return result["result"]


def keccak_hex(data: bytes) -> str:
    return keccak.new(digest_bits=256, data=data).hexdigest()


def encode_words(*values: int | str) -> str:
    """Encode uint256 values or hex addresses as consecutive 32-byte ABI words."""
    encoded = []
    for value in values:
        number = int(value, 16) if isinstance(value, str) else value
        encoded.append(f"{number:064x}")
    return "".join(encoded)


def encode_call(signature: str, *values: int | str) -> str:
    selector = keccak_hex(signature.encode())[:8]
    return "0x" + selector + encode_words(*values)


def decode_signed_word(word: str) -> int:
    value = int(word, 16)
    return value - 2**256 if value >= 2**255 else value


# --- Compile and prepare the local fork ---------------------------------------

def compile_contract(solc: str) -> str:
    source = Path(__file__).resolve().with_name("TesseraSplitDemo.sol")
    command = [
        solc, "--optimize", "--evm-version", "paris",
        "--combined-json", "bin", source.name,
    ]
    # A relative source name keeps the machine's directory out of compiler metadata.
    compiled = subprocess.run(command, cwd=source.parent, capture_output=True, text=True)
    require(compiled.returncode == 0, compiled.stderr)
    contracts = json.loads(compiled.stdout)["contracts"]
    return "0x" + contracts[f"{source.name}:TesseraSplitDemo"]["bin"]


def read_reference_headers(upstream_url: str, fork_block: int) -> list[dict]:
    chain_id = int(rpc(upstream_url, "eth_chainId"), 16)
    require(chain_id == BASE_CHAIN_ID, "Expected a Base RPC (chain ID 8453).")

    stored_implementation = rpc(
        upstream_url, "eth_getStorageAt", [POOL, IMPLEMENTATION_SLOT, hex(fork_block)]
    )
    implementation = "0x" + stored_implementation[-40:].lower()
    require(
        implementation == IMPLEMENTATION,
        "This block does not use the intended 0x6d9...41cb implementation.",
    )

    # Deploy in the first new block; execute each strategy in the second.
    headers = [
        rpc(upstream_url, "eth_getBlockByNumber", [hex(fork_block + offset), False])
        for offset in (1, 2)
    ]
    require(all(headers), "Choose a historical block with two subsequent headers.")
    return headers


@contextmanager
def anvil_fork(args: argparse.Namespace) -> Iterator[str]:
    """Start a disposable fork, yield its URL, and stop it even if a check fails."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    local_url = f"http://127.0.0.1:{port}"

    help_text = subprocess.check_output([args.anvil, "--help"], text=True)
    if "--base " in help_text:
        network_flags = ["--optimism", "--base", "beryl"]
    elif "--network" in help_text:
        network_flags = ["--network", "optimism"]
    else:
        network_flags = ["--optimism"]

    command = [
        args.anvil, *network_flags,
        "--fork-url", args.rpc,
        "--fork-block-number", str(args.block),
        "--chain-id", str(BASE_CHAIN_ID),
        "--accounts", "0",
        "--no-mining", "--order", "fifo",
        "--no-rate-limit", "--no-storage-caching", "--silent",
        "--host", "127.0.0.1", "--port", str(port),
    ]
    # A file avoids blocking Anvil on a full stdout pipe.
    with tempfile.TemporaryFile(mode="w+b") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    log.seek(0)
                    detail = log.read().decode(errors="replace").replace(args.rpc, "[RPC]")
                    raise RuntimeError(detail[-1500:])
                try:
                    block_number = int(rpc(local_url, "eth_blockNumber"), 16)
                    if block_number == args.block:
                        break
                except RuntimeError:
                    pass  # The process may still be opening its RPC listener.
                time.sleep(0.2)
            else:
                raise RuntimeError("Anvil did not start.")
            yield local_url
        finally:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def read_pool_state(local_url: str) -> list[str]:
    return [
        rpc(local_url, "eth_getStorageAt", [POOL, hex(slot), "latest"])
        for slot in range(POOL_STATE_SLOT_COUNT)
    ]


def mine_transaction(local_url: str, transaction: dict, header: dict) -> dict:
    """Mine one local transaction using the specified historical block context."""
    rpc(local_url, "evm_setNextBlockTimestamp", [int(header["timestamp"], 16)])
    rpc(local_url, "anvil_setNextBlockBaseFeePerGas", [header["baseFeePerGas"]])
    rpc(local_url, "anvil_setCoinbase", [header["miner"]])
    rpc(local_url, "evm_setBlockGasLimit", [header["gasLimit"]])
    rpc(local_url, "anvil_setNextBlockPrevRandao", [header["mixHash"]])

    transaction = {
        **transaction,
        "from": TEST_ACCOUNT,
        "gas": hex(TRANSACTION_GAS_LIMIT),
        "gasPrice": hex(int(header["baseFeePerGas"], 16) + 1),
    }
    transaction_hash = rpc(local_url, "eth_sendTransaction", [transaction])
    rpc(local_url, "evm_mine")
    receipt = rpc(local_url, "eth_getTransactionReceipt", [transaction_hash])

    if receipt is None or receipt["status"] != "0x1":
        trace = rpc(
            local_url, "debug_traceTransaction", [transaction_hash, {"tracer": "callTracer"}]
        )
        reason = trace.get("revertReason") or trace.get("output") or trace.get("error")
        raise RuntimeError(f"Local transaction reverted: {reason}")

    actual_header = rpc(local_url, "eth_getBlockByNumber", ["latest", False])
    require(actual_header["transactions"] == [transaction_hash], "Unexpected block contents.")
    for field in REPLAY_HEADER_FIELDS:
        require(actual_header[field] == header[field], f"Block header mismatch: {field}.")
    return receipt


def deploy_demo(local_url: str, creation_code: str, header: dict) -> str:
    account_code = rpc(local_url, "eth_getCode", [TEST_ACCOUNT, "latest"])
    require(account_code == "0x", "The test account must not already have code.")
    rpc(local_url, "anvil_setBalance", [TEST_ACCOUNT, hex(100 * WETH_SCALE)])
    rpc(local_url, "anvil_impersonateAccount", [TEST_ACCOUNT])

    receipt = mine_transaction(local_url, {"data": creation_code}, header)
    contract = receipt["contractAddress"]
    require(bool(contract), "Deployment did not return a contract address.")
    code = rpc(local_url, "eth_getCode", [contract, "latest"])
    require(code != "0x", "The deployed contract has no code.")
    return contract


def fund_demo(local_url: str, contract: str, total_sell: int) -> None:
    """Fund only the demo's token balances, using the known balance-mapping slots."""
    funding = [(WETH, 3, total_sell), (USDC, 9, 1_000_000 * USDC_SCALE)]
    for token, mapping_slot, amount in funding:
        mapping_key = bytes.fromhex(encode_words(contract, mapping_slot))
        storage_key = "0x" + keccak_hex(mapping_key)
        rpc(local_url, "anvil_setStorageAt", [token, storage_key, "0x" + encode_words(amount)])

        # This balance check verifies funding only; prices are calculated from logs.
        balance_call = {"to": token, "data": encode_call("balanceOf(address)", contract)}
        balance = int(rpc(local_url, "eth_call", [balance_call, "latest"]), 16)
        require(balance == amount, f"Test funding failed for token {token}.")


# --- Read Tessera's native swap events and report execution prices -------------

def decode_tessera_swaps(receipt: dict, pricing_account: str) -> list[Swap]:
    """Read pool-side deltas in log order, ignoring events from other emitters.

    Topics: signature, pricing account, signed WETH delta, signed USDC delta.
    Data: token addresses, anchor tag, and the two pre-swap flow accumulators.
    """
    swaps = []
    logs = sorted(receipt["logs"], key=lambda log: int(log["logIndex"], 16))
    for log in logs:
        topics = log["topics"]
        if log["address"].lower() != POOL or not topics or topics[0] != SWAP_EVENT_TOPIC:
            continue

        expected_data_length = 2 + 5 * 64  # "0x" followed by five 32-byte words.
        require(
            len(topics) == 4 and len(log["data"]) == expected_data_length,
            "Unexpected Tessera event layout.",
        )
        account = "0x" + topics[1][-40:].lower()
        require(account == pricing_account.lower(), "Unexpected swap pricing account.")

        data = log["data"][2:]
        fields = [int(data[offset:offset + 64], 16) for offset in range(0, len(data), 64)]
        require(fields[:2] == [int(WETH, 16), int(USDC, 16)], "Unexpected token pair.")

        swap = Swap(decode_signed_word(topics[2]), decode_signed_word(topics[3]))
        require(swap.weth_delta * swap.usdc_delta < 0, "Unexpected swap direction.")
        swaps.append(swap)
    return swaps


def read_scenario_result(
    receipt: dict, contract: str, total_sell: int, total_buy: int, pieces: int,
) -> ScenarioResult:
    swaps = decode_tessera_swaps(receipt, contract)
    expected_count = pieces + int(total_sell > 0)
    require(len(swaps) == expected_count, "Unexpected swap count.")

    sell_proceeds = 0
    if total_sell > 0:
        sell = swaps.pop(0)
        require(sell.weth_delta == total_sell and sell.usdc_delta < 0, "Unexpected sell.")
        sell_proceeds = -sell.usdc_delta

    buy_size, leftover_wei = divmod(total_buy, pieces)
    for index, buy in enumerate(swaps):
        expected_weth = buy_size + int(index < leftover_wei)
        require(
            -buy.weth_delta == expected_weth and buy.usdc_delta > 0,
            f"Unexpected amount for buy {index + 1}.",
        )

    result = ScenarioResult(
        pieces=pieces,
        sold_weth=total_sell,
        sell_proceeds_usdc=sell_proceeds,
        buys=swaps,
        gas_used=int(receipt["gasUsed"], 16),
    )
    require(result.bought_weth == total_buy, "Buys did not sum to totalBuy.")
    return result


def execution_price(usdc_amount: int, weth_amount: int) -> Decimal:
    """Convert raw token amounts into USDC per WETH."""
    return Decimal(usdc_amount) * WETH_SCALE / (USDC_SCALE * weth_amount)


def print_scenario(result: ScenarioResult) -> None:
    sold_weth = Decimal(result.sold_weth) / WETH_SCALE
    proceeds_usdc = Decimal(result.sell_proceeds_usdc) / USDC_SCALE
    print(f"{result.pieces} buy swap(s):")
    print(f"  Sell: {sold_weth} WETH -> {proceeds_usdc:.6f} USDC")
    if result.sold_weth:
        price = execution_price(result.sell_proceeds_usdc, result.sold_weth)
        print(f"  Sell execution price: {price:.12f} USDC/WETH")
    else:
        print("  Sell execution price: n/a (totalSell = 0)")

    for index, buy in enumerate(result.buys, start=1):
        weth_amount = Decimal(-buy.weth_delta) / WETH_SCALE
        usdc_cost = Decimal(buy.usdc_delta) / USDC_SCALE
        print(f"  Buy {index}: {weth_amount} WETH for {usdc_cost:.6f} USDC")

    total_cost = Decimal(result.spent_usdc) / USDC_SCALE
    price = execution_price(result.spent_usdc, result.bought_weth)
    print(f"  Total buy cost: {total_cost:.6f} USDC")
    print(f"  Buy execution price: {price:.12f} USDC/WETH")
    print(f"  Transaction gas used: {result.gas_used:,}\n", flush=True)


# --- Command-line entry point -------------------------------------------------

def parse_weth_amount(text: str) -> int:
    """Accept human WETH units on the CLI and convert them exactly to wei."""
    try:
        amount = Decimal(text) * WETH_SCALE
    except InvalidOperation:
        raise argparse.ArgumentTypeError("Use a numeric WETH amount.") from None
    if not amount.is_finite() or amount < 0 or amount != int(amount) or amount >= 2**255:
        raise argparse.ArgumentTypeError("Use a nonnegative WETH amount with at most 18 decimals.")
    return int(amount)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("total_sell", metavar="totalSell", type=parse_weth_amount)
    parser.add_argument("total_buy", metavar="totalBuy", type=parse_weth_amount)
    parser.add_argument("pieces", type=int)
    parser.add_argument("--rpc", default=os.environ.get("BASE_RPC_URL"))
    parser.add_argument("--block", type=int, default=DEFAULT_FORK_BLOCK)
    parser.add_argument("--anvil", default=shutil.which("anvil"))

    fallback_solc = Path.home() / ".solcx/solc-v0.8.24"
    default_solc = shutil.which("solc")
    if not default_solc and fallback_solc.exists():
        default_solc = str(fallback_solc)
    parser.add_argument("--solc", default=default_solc)
    args = parser.parse_args()

    if not args.rpc or not args.anvil or not args.solc:
        parser.error("Set BASE_RPC_URL and install Anvil and solc, or supply --rpc/--anvil/--solc.")
    if not (args.total_buy > 0 and 0 < args.pieces <= args.total_buy):
        parser.error("totalBuy and pieces must be positive; every piece must buy at least one wei.")
    return args


def main() -> None:
    args = parse_arguments()
    creation_code = compile_contract(args.solc)
    deployment_header, execution_header = read_reference_headers(args.rpc, args.block)

    with anvil_fork(args) as local_url:
        original_pool_state = read_pool_state(local_url)
        contract = deploy_demo(local_url, creation_code, deployment_header)
        fund_demo(local_url, contract, args.total_sell)
        require(read_pool_state(local_url) == original_pool_state, "Setup changed pool storage.")
        snapshot = rpc(local_url, "evm_snapshot")

        print(
            f"Compiled and deployed {contract} on local fork of Base block {args.block:,}.",
            flush=True,
        )
        print(f"Implementation: {IMPLEMENTATION}", flush=True)
        print(
            "Each scenario runs the sell and all buys in ONE transaction. "
            "Prices use Tessera swap logs and exclude gas.\n",
            flush=True,
        )

        results = []
        strategies = [1] if args.pieces == 1 else [1, args.pieces]
        for pieces in strategies:
            # Restore identical starting conditions before each complete strategy.
            require(rpc(local_url, "evm_revert", [snapshot]), "Could not restore the snapshot.")
            snapshot = rpc(local_url, "evm_snapshot")
            require(
                read_pool_state(local_url) == original_pool_state,
                "Pool state was not restored.",
            )

            transaction = {
                "to": contract,
                "data": encode_call(
                    "run(uint256,uint256,uint256)", args.total_sell, args.total_buy, pieces,
                ),
            }
            receipt = mine_transaction(local_url, transaction, execution_header)
            result = read_scenario_result(
                receipt, contract, args.total_sell, args.total_buy, pieces,
            )
            if results:
                require(
                    result.sell_proceeds_usdc == results[0].sell_proceeds_usdc,
                    "Preparatory sell differed between strategies.",
                )
            print_scenario(result)
            results.append(result)

        if len(results) == 2:
            single_buy, split_buys = results
            saving = single_buy.spent_usdc - split_buys.spent_usdc
            saving_usdc = Decimal(saving) / USDC_SCALE
            saving_bps = Decimal(saving) * 10_000 / single_buy.spent_usdc
            print(f"Split-buy saving before gas: {saving_usdc:.6f} USDC ({saving_bps:.6f} bps).")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as error:
        raise SystemExit(str(error)) from None
