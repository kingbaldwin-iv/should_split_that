# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.32,<3", "pycryptodome>=3.20,<4"]
# ///
"""Compile, DEPLOY, and execute TesseraSplitDemo.sol on a disposable Base fork.

    export BASE_RPC_URL='your archive Base RPC'
    uv run demo.py 3 2 2

Arguments: totalSell WETH, totalBuy WETH, pieces. The script compares one buy
against `pieces` buys from the same snapshot; BOTH first sell totalSell WETH.
Requires Anvil and solc 0.8.24+. Override paths with --anvil and --solc.
Only the test contract's token balances and the test EOA's gas are funded.
Historical Tessera configuration, helpers, custody and code are untouched.
"""
import argparse
from decimal import Decimal, InvalidOperation, getcontext
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlparse

from Crypto.Hash import keccak
import requests

getcontext().prec = 80
WETH = '0x4200000000000000000000000000000000000006'
USDC = '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'
POOL = '0xf524c1bc1c64a2c99bc7eccf19ede9a1d89d5a7c'
IMPLEMENTATION = '0x6d9dd143e42b6338f4f6a7c0c26d124658f641cb'
IMPL_SLOT = '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'
USER = '0x76fa817e2d5b93fa3c0d21bca4547161923ec876'
TREASURY = '0x3dbe077e7986657e95e1cc50089f17a5a4af0aae'
W, M = 10**18, 10**6


def hash_bytes(value):
    return keccak.new(digest_bits=256, data=value).hexdigest()


def words(*values):
    return ''.join(f'{(int(x,16) if isinstance(x,str) else x):064x}' for x in values)


def calldata(signature, *values):
    return '0x' + hash_bytes(signature.encode())[:8] + words(*values)


def rpc(url, method, params=None):
    # State changes and transaction submission can ONLY reach our localhost fork.
    if method not in {'eth_chainId', 'eth_getBlockByNumber', 'eth_getStorageAt'}:
        assert urlparse(url).hostname == '127.0.0.1' and urlparse(url).scheme == 'http'
    try:
        response = requests.post(url, json={'jsonrpc':'2.0', 'id':1,
                                  'method':method, 'params':params or []}, timeout=55)
        response.raise_for_status()
        result = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f'{method}: {type(exc).__name__}') from None
    if 'error' in result:
        raise RuntimeError(method + ': ' + json.dumps(result['error']).replace(url, '[RPC]'))
    return result['result']


def token_amount(text):
    try:
        value = Decimal(text) * W
    except InvalidOperation:
        raise argparse.ArgumentTypeError('Use a numeric WETH amount.') from None
    if not value.is_finite() or value < 0 or value != int(value) or value >= 2**255:
        raise argparse.ArgumentTypeError('Use a nonnegative WETH amount with at most 18 decimals.')
    return int(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('totalSell', type=token_amount)
    parser.add_argument('totalBuy', type=token_amount)
    parser.add_argument('pieces', type=int)
    parser.add_argument('--rpc', default=os.environ.get('BASE_RPC_URL'))
    parser.add_argument('--block', type=int, default=50710032)
    parser.add_argument('--anvil', default=shutil.which('anvil'))
    fallback_solc = Path.home()/'.solcx/solc-v0.8.24'
    parser.add_argument('--solc', default=shutil.which('solc') or (str(fallback_solc) if fallback_solc.exists() else None))
    args = parser.parse_args()
    if not args.rpc or not args.anvil or not args.solc:
        parser.error('Set BASE_RPC_URL and install Anvil and solc, or supply --rpc/--anvil/--solc.')
    if not (args.totalBuy > 0 and 0 < args.pieces <= args.totalBuy):
        parser.error('totalBuy and pieces must be positive; every piece must buy at least one wei.')

    source = Path(__file__).resolve().with_name('TesseraSplitDemo.sol')
    compiled = subprocess.run([args.solc, '--optimize', '--evm-version', 'paris',
                               '--combined-json', 'bin', source.name], cwd=source.parent,
                              capture_output=True, text=True)
    if compiled.returncode:
        raise RuntimeError(compiled.stderr)
    contracts = json.loads(compiled.stdout)['contracts']
    creation_code = '0x' + next(c['bin'] for name,c in contracts.items() if name.endswith(':TesseraSplitDemo'))
    if int(rpc(args.rpc, 'eth_chainId'), 16) != 8453:
        raise RuntimeError('Expected a Base RPC (chain ID 8453).')
    impl = rpc(args.rpc, 'eth_getStorageAt', [POOL, IMPL_SLOT, hex(args.block)])
    if '0x'+impl[-40:] != IMPLEMENTATION:
        raise RuntimeError('This block does not use the intended 0x6d9...41cb implementation.')
    # Deployment uses the first new block; the scenario uses the second.
    headers = [rpc(args.rpc, 'eth_getBlockByNumber', [hex(args.block+i), False]) for i in [1,2]]
    if not all(headers):
        raise RuntimeError('Choose a historical block with two subsequent headers.')

    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
    local = f'http://127.0.0.1:{port}'
    def call(method, params=None): return rpc(local, method, params)
    help_text = subprocess.check_output([args.anvil, '--help'], text=True)
    network = (['--optimism','--base','beryl'] if '--base ' in help_text else
               ['--network','optimism'] if '--network' in help_text else ['--optimism'])
    command = [args.anvil, *network, '--fork-url', args.rpc, '--fork-block-number', str(args.block),
               '--chain-id','8453', '--accounts','0', '--no-mining', '--order','fifo',
               '--no-rate-limit', '--no-storage-caching', '--silent', '--host','127.0.0.1', '--port',str(port)]
    # Keep logs in a temporary file so Anvil can never block on a full stdout pipe.
    with tempfile.TemporaryFile(mode='w+b') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(log.read().decode(errors='replace').replace(args.rpc, '[RPC]')[-1500:])
                try:
                    if int(call('eth_blockNumber'),16) == args.block: break
                except RuntimeError: time.sleep(.2)
            else: raise RuntimeError('Anvil did not start.')

            def pool_state():
                return [call('eth_getStorageAt',[POOL,hex(i),'latest']) for i in range(53)]
            original_state = pool_state()
            def mine_transaction(tx, header):
                call('evm_setNextBlockTimestamp',[int(header['timestamp'],16)])
                call('anvil_setNextBlockBaseFeePerGas',[header['baseFeePerGas']])
                call('anvil_setCoinbase',[header['miner']])
                call('evm_setBlockGasLimit',[header['gasLimit']])
                call('anvil_setNextBlockPrevRandao',[header['mixHash']])
                tx.update({'from':USER, 'gas':hex(16_000_000),
                           'gasPrice':hex(int(header['baseFeePerGas'],16)+1)})
                txhash = call('eth_sendTransaction',[tx])
                call('evm_mine')
                receipt = call('eth_getTransactionReceipt',[txhash])
                if receipt is None or receipt['status'] != '0x1':
                    trace = call('debug_traceTransaction',[txhash,{'tracer':'callTracer'}])
                    raise RuntimeError('Local transaction reverted: '+str(trace.get('revertReason') or trace.get('output') or trace.get('error')))
                actual = call('eth_getBlockByNumber',['latest',False])
                assert actual['transactions'] == [txhash]
                assert all(actual[k] == header[k] for k in ['number','timestamp','baseFeePerGas','miner','gasLimit','mixHash'])
                return receipt

            assert call('eth_getCode',[USER,'latest']) == '0x'
            call('anvil_setBalance',[USER,hex(100*W)])
            call('anvil_impersonateAccount',[USER])
            deployed = mine_transaction({'data':creation_code},headers[0])
            contract = deployed['contractAddress']
            assert contract and call('eth_getCode',[contract,'latest']) != '0x'

            def balance(token, account=contract):
                return int(call('eth_call',[{'to':token,'data':calldata('balanceOf(address)',account)},'latest']),16)
            # Fund only the newly deployed test contract. Verify the known token
            # balance mappings with each original token's balanceOf getter.
            for token,slot,amount in [(WETH,3,args.totalSell),(USDC,9,1_000_000*M)]:
                key = '0x'+hash_bytes(bytes.fromhex(words(contract,slot)))
                call('anvil_setStorageAt',[token,key,'0x'+words(amount)])
                assert balance(token) == amount
            assert pool_state() == original_state
            initial_balances = {t:balance(t) for t in [WETH,USDC]}
            initial_custody = {t:balance(t,TREASURY) for t in [WETH,USDC]}
            snapshot = call('evm_snapshot')
            result_topic = '0x'+hash_bytes(b'Result(uint256,uint256,uint256,uint256,uint256,uint256)')
            buy_topic = '0x'+hash_bytes(b'Buy(uint256,uint256,uint256)')
            print(f'Compiled and deployed {contract} on local fork of Base block {args.block:,}.', flush=True)
            print(f'Implementation: {IMPLEMENTATION}', flush=True)
            print('Each scenario runs the sell and all buys in ONE transaction; prices exclude gas.\n',flush=True)
            results = []
            for pieces in dict.fromkeys([1,args.pieces]):
                assert call('evm_revert',[snapshot]); snapshot = call('evm_snapshot')
                assert pool_state() == original_state
                receipt = mine_transaction({'to':contract,'data':calldata('run(uint256,uint256,uint256)',args.totalSell,args.totalBuy,pieces)},headers[1])
                events = [l for l in receipt['logs'] if l['address'] == contract]
                result_event = next(l for l in events if l['topics'][0] == result_topic)
                values = [int(result_event['data'][i:i+64],16) for i in range(2,len(result_event['data']),64)]
                sold, proceeds, bought, spent, sell_price, buy_price = values
                assert (sold,bought) == (args.totalSell,args.totalBuy)
                assert balance(WETH)-initial_balances[WETH] == bought-sold
                assert balance(USDC)-initial_balances[USDC] == proceeds-spent
                assert balance(WETH,TREASURY)-initial_custody[WETH] == sold-bought
                assert balance(USDC,TREASURY)-initial_custody[USDC] == spent-proceeds
                buys = [[int(l['data'][i:i+64],16) for i in range(2,len(l['data']),64)]
                        for l in events if l['topics'][0] == buy_topic]
                assert len(buys) == pieces and sum(b[1] for b in buys) == bought and sum(b[2] for b in buys) == spent
                if results: assert proceeds == results[0]['proceeds'], 'preparatory sell differed between strategies'
                print(f'{pieces} buy swap(s):')
                print(f'  Sell: {Decimal(sold)/W} WETH -> {Decimal(proceeds)/M:.6f} USDC')
                print(f'  Sell execution price: {Decimal(sell_price)/W:.12f} USDC/WETH' if sold else '  Sell execution price: n/a (totalSell = 0)')
                for i,b,q in buys:
                    print(f'  Buy {i+1}: {Decimal(b)/W} WETH for {Decimal(q)/M:.6f} USDC')
                print(f'  Total buy cost: {Decimal(spent)/M:.6f} USDC')
                print(f'  Buy execution price: {Decimal(buy_price)/W:.12f} USDC/WETH')
                print(f'  Transaction gas used: {int(receipt["gasUsed"],16):,}\n',flush=True)
                results.append({'pieces':pieces,'proceeds':proceeds,'spent':spent})
            if len(results)==2:
                saving = results[0]['spent']-results[1]['spent']
                print(f'Split-buy saving before gas: {Decimal(saving)/M:.6f} USDC '
                      f'({Decimal(saving)*10000/results[0]["spent"]:.6f} bps).')
        finally:
            process.terminate()
            try: process.wait(timeout=8)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


if __name__ == '__main__':
    try: main()
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
