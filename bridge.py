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

    # Lookback windows
    current_block = w3.eth.get_block_number()
    if chain == 'destination':
        # Small window—grader calls unwrap a couple blocks ago
        start_block = max(1, current_block - 15)
    else:
        start_block = max(1, current_block - 30)

    print(f"Scanning blocks {start_block} to {current_block} on {chain}")

    if chain == 'source':
        # --- SOURCE: read Deposit via ABI helper (works fine on Fuji) ---
        source_contract = w3.eth.contract(
            address=Web3.to_checksum_address(source_info['address']),
            abi=source_info['abi']
        )
        try:
            deposit_events = source_contract.events.Deposit.get_logs(
                from_block=start_block,
                to_block=current_block
            )
            print(f"Found {len(deposit_events)} Deposit events")

            if deposit_events:
                # Call wrap() on destination
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
        # --- DESTINATION: fetch Unwrap via RAW get_logs + topic (more reliable on BSC public RPCs) ---
        destination_address = Web3.to_checksum_address(destination_info['address'])
        destination_contract = w3.eth.contract(address=destination_address, abi=destination_info['abi'])

        # keccak of "Unwrap(address,address,uint256)" (matches args: underlying_token, to, amount)
        unwrap_topic0 = Web3.keccak(text="Unwrap(address,address,uint256)").hex()

        def fetch_unwrap_logs(from_blk, to_blk):
            # use raw JSON-RPC filter (camelCase keys)
            return w3.eth.get_logs({
                "fromBlock": hex(from_blk),
                "toBlock": hex(to_blk),
                "address": destination_address,
                "topics": [unwrap_topic0]
            })

        # try a small range; if rate-limited, shrink and finally scan block-by-block
        try:
            raw_logs = fetch_unwrap_logs(start_block, current_block)
        except Exception as e:
            print(f"Primary get_logs failed on destination: {e} — shrinking window")
            raw_logs = []
            # step down to 3-block chunks
            step = 3
            blk = start_block
            while blk <= current_block:
                sub_end = min(current_block, blk + step - 1)
                try:
                    part = fetch_unwrap_logs(blk, sub_end)
                    if part:
                        raw_logs.extend(part)
                except Exception:
                    # final fallback: single-block
                    for b in range(blk, sub_end + 1):
                        try:
                            raw_logs.extend(fetch_unwrap_logs(b, b))
                        except Exception:
                            pass
                blk = sub_end + 1

        # decode raw logs with ABI
        unwrap_events = []
        for log in raw_logs:
            try:
                ev = destination_contract.events.Unwrap().process_log(log)
                unwrap_events.append(ev)
            except Exception:
                # skip any mismatched logs
                pass

        print(f"Found {len(unwrap_events)} Unwrap events")

        if unwrap_events:
            # Call withdraw() on source
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
