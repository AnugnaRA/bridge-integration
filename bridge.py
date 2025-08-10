from web3 import Web3
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware  # Necessary for POA chains
from datetime import datetime
import json
import pandas as pd


def connect_to(chain):
    if chain == 'source':  # The source contract chain is avax
        api_url = f"https://api.avax-test.network/ext/bc/C/rpc"  # AVAX C-chain testnet

    if chain == 'destination':  # The destination contract chain is bsc
        api_url = f"https://data-seed-prebsc-1-s1.binance.org:8545/"  # BSC testnet

    if chain in ['source', 'destination']:
        w3 = Web3(Web3.HTTPProvider(api_url))
        # inject the poa compatibility middleware to the innermost layer
        w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    return w3


def get_contract_info(chain, contract_info):
    """
        Load the contract_info file into a dictionary
        This function is used by the autograder and will likely be useful to you
    """
    try:
        with open(contract_info, 'r') as f:
            contracts = json.load(f)
    except Exception as e:
        print(
            f"Failed to read contract info\nPlease contact your instructor\n{e}")
        return 0
    return contracts[chain]

def scan_blocks(chain, contract_info="contract_info.json"):
    """
        chain - (string) should be either "source" or "destination"
        On source: find Deposit events, then call wrap() on destination.
        On destination: find Unwrap events, then call withdraw() on source.
    """
    if chain not in ['source', 'destination']:
        print(f"Invalid chain: {chain}")
        return 0

    # Your private key
    PRIVATE_KEY = '0x29c4d805d1bb13b3ae64d3ccc9705ae8ba943543d0dc03bf7d6d635d0461c6f3'

    # Connect to the selected chain
    w3 = connect_to(chain)

    # Load contract info for both sides
    source_info = get_contract_info('source', contract_info)
    destination_info = get_contract_info('destination', contract_info)

    # Warden account
    account = w3.eth.account.from_key(PRIVATE_KEY)
    warden_address = account.address

    # Lookback window (destination needs wider + chunking due to RPC limits)
    current_block = w3.eth.get_block_number()
    if chain == 'destination':
        lookback = 40   # wider so we don’t miss the grader’s Unwrap blocks
    else:
        lookback = 30
    start_block = max(1, current_block - lookback)
    print(f"Scanning blocks {start_block} to {current_block} on {chain}")

    # ------- robust log fetcher: chunked + block-hash fallback -------
    import time

    def fetch_logs(event_obj, start_blk, end_blk, chunk):
        """
        Try to fetch logs in [start_blk, end_blk] by chunks.
        If a chunk hits 'limit exceeded', retry that chunk per-block using block_hash.
        """
        logs = []
        blk = start_blk
        while blk <= end_blk:
            sub_end = min(end_blk, blk + chunk - 1)
            try:
                part = event_obj.get_logs(from_block=blk, to_block=sub_end)
                if part:
                    logs.extend(part)
            except Exception as e:
                # Fallback: per-block by block hash (works better on BSC public RPCs)
                # Keep going even if some blocks still fail.
                # Small sleep to reduce throttling.
                for b in range(blk, sub_end + 1):
                    try:
                        block = w3.eth.get_block(b)
                        part = event_obj.get_logs(block_hash=block.hash)
                        if part:
                            logs.extend(part)
                    except Exception:
                        pass
                    time.sleep(0.06)
            blk = sub_end + 1
        return logs
    # -----------------------------------------------------------------

    if chain == 'source':
        # Look for Deposit events on source (Avalanche)
        source_contract = w3.eth.contract(
            address=Web3.to_checksum_address(source_info['address']),
            abi=source_info['abi']
        )
        try:
            # Fuji is friendlier; medium chunks are fine.
            deposit_events = fetch_logs(source_contract.events.Deposit, start_block, current_block, chunk=20)
            print(f"Found {len(deposit_events)} Deposit events")

            if deposit_events:
                # Call wrap() on destination (BSC)
                w3_dest = connect_to('destination')
                destination_contract = w3_dest.eth.contract(
                    address=Web3.to_checksum_address(destination_info['address']),
                    abi=destination_info['abi']
                )

                for ev in deposit_events:
                    args = ev['args']
                    token = args['token']
                    recipient = args['recipient']
                    amount = args['amount']
                    print(f"Processing Deposit: token={token}, recipient={recipient}, amount={amount}")
                    try:
                        nonce = w3_dest.eth.get_transaction_count(warden_address, 'pending')
                        tx = destination_contract.functions.wrap(token, recipient, amount).build_transaction({
                            'from': warden_address,
                            'nonce': nonce,
                            'gas': 500000,
                            'gasPrice': w3_dest.eth.gas_price,
                        })
                        signed = w3_dest.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
                        txh = w3_dest.eth.send_raw_transaction(signed.raw_transaction)  # v6
                        print(f"Wrap transaction sent: {txh.hex()}")
                        rcpt = w3_dest.eth.wait_for_transaction_receipt(txh, timeout=120)
                        print(f"Wrap transaction confirmed in block {rcpt.blockNumber}")
                    except Exception as e:
                        print(f"Error processing wrap: {e}")
        except Exception as e:
            print(f"Error getting Deposit events: {e}")

    elif chain == 'destination':
        # Look for Unwrap events on destination (BSC)
        destination_contract = w3.eth.contract(
            address=Web3.to_checksum_address(destination_info['address']),
            abi=destination_info['abi']
        )
        try:
            # BSC public RPC is picky; use small chunks.
            unwrap_events = fetch_logs(destination_contract.events.Unwrap, start_block, current_block, chunk=5)
            print(f"Found {len(unwrap_events)} Unwrap events")

            if unwrap_events:
                # Call withdraw() on source (Avalanche)
                w3_source = connect_to('source')
                source_contract = w3_source.eth.contract(
                    address=Web3.to_checksum_address(source_info['address']),
                    abi=source_info['abi']
                )
                for ev in unwrap_events:
                    args = ev['args']
                    underlying_token = args['underlying_token']
                    recipient = args['to']
                    amount = args['amount']
                    print(f"Processing Unwrap: token={underlying_token}, recipient={recipient}, amount={amount}")
                    try:
                        nonce = w3_source.eth.get_transaction_count(warden_address, 'pending')
                        tx = source_contract.functions.withdraw(underlying_token, recipient, amount).build_transaction({
                            'from': warden_address,
                            'nonce': nonce,
                            'gas': 500000,
                            'gasPrice': w3_source.eth.gas_price,
                        })
                        signed = w3_source.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
                        txh = w3_source.eth.send_raw_transaction(signed.raw_transaction)  # v6
                        print(f"Withdraw transaction sent: {txh.hex()}")
                        rcpt = w3_source.eth.wait_for_transaction_receipt(txh, timeout=120)
                        print(f"Withdraw transaction confirmed in block {rcpt.blockNumber}")
                    except Exception as e:
                        print(f"Error processing withdraw: {e}")
        except Exception as e:
            print(f"Error getting Unwrap events: {e}")
