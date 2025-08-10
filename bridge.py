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
        Scan the last 5 blocks of the source and destination chains
        Look for 'Deposit' events on the source chain and 'Unwrap' events on the destination chain
        When Deposit events are found on the source chain, call the 'wrap' function the destination chain
        When Unwrap events are found on the destination chain, call the 'withdraw' function on the source chain
    """

    # This is different from Bridge IV where chain was "avax" or "bsc"
    if chain not in ['source', 'destination']:
        print(f"Invalid chain: {chain}")
        return 0

    # Your private key
    PRIVATE_KEY = '0x29c4d805d1bb13b3ae64d3ccc9705ae8ba943543d0dc03bf7d6d635d0461c6f3'

    # Connect to the chain
    w3 = connect_to(chain)

    # Get contract information
    source_info = get_contract_info('source', contract_info)
    destination_info = get_contract_info('destination', contract_info)

    # Get the account from private key
    account = w3.eth.account.from_key(PRIVATE_KEY)
    warden_address = account.address

    # Get current block number and calculate range
    current_block = w3.eth.get_block_number()
    start_block = max(1, current_block - 5)  # Scan last 5 blocks

    print(f"Scanning blocks {start_block} to {current_block} on {chain}")

    if chain == 'source':
        # We're on source chain, look for Deposit events
        source_contract = w3.eth.contract(
            address=Web3.to_checksum_address(source_info['address']),
            abi=source_info['abi']
        )

        # Get Deposit events
        try:
            events = source_contract.events.Deposit.get_logs(
                from_block=start_block,
                to_block=current_block
            )

            print(f"Found {len(events)} Deposit events")

            if len(events) > 0:
                # Connect to destination chain
                w3_dest = connect_to('destination')
                destination_contract = w3_dest.eth.contract(
                    address=Web3.to_checksum_address(
                        destination_info['address']),
                    abi=destination_info['abi']
                )

                # Process each Deposit event
                for event in events:
                    token = event.args['token']
                    recipient = event.args['recipient']
                    amount = event.args['amount']

                    print(
                        f"Processing Deposit: token={token}, recipient={recipient}, amount={amount}")

                    try:
                        # Call wrap on destination chain
                        nonce = w3_dest.eth.get_transaction_count(warden_address, 'pending')

                        wrap_txn = destination_contract.functions.wrap(
                            token,
                            recipient,
                            amount
                        ).build_transaction({
                            'from': warden_address,
                            'nonce': nonce,
                            'gas': 500000,
                            'gasPrice': w3_dest.eth.gas_price,
                        })

                        signed_txn = w3_dest.eth.account.sign_transaction(
                            wrap_txn, private_key=PRIVATE_KEY)
                        tx_hash = w3_dest.eth.send_raw_transaction(signed_txn.raw_transaction)

                        print(f"Wrap transaction sent: {tx_hash.hex()}")

                        # Wait for confirmation
                        receipt = w3_dest.eth.wait_for_transaction_receipt(
                            tx_hash, timeout=120)
                        print(
                            f"Wrap transaction confirmed in block {receipt.blockNumber}")

                    except Exception as e:
                        print(f"Error processing wrap: {e}")

        except Exception as e:
            print(f"Error getting Deposit events: {e}")

    elif chain == 'destination':
        # We're on destination chain, look for Unwrap events
        destination_contract = w3.eth.contract(
            address=Web3.to_checksum_address(destination_info['address']),
            abi=destination_info['abi']
        )

        # Get Unwrap events
        try:
            events = destination_contract.events.Unwrap.get_logs(
                from_block=start_block,
                to_block=current_block
            )

            print(f"Found {len(events)} Unwrap events")

            if len(events) > 0:
                # Connect to source chain
                w3_source = connect_to('source')
                source_contract = w3_source.eth.contract(
                    address=Web3.to_checksum_address(source_info['address']),
                    abi=source_info['abi']
                )

                # Process each Unwrap event
                for event in events:
                    underlying_token = event.args['underlying_token']
                    recipient = event.args['to']
                    amount = event.args['amount']

                    print(
                        f"Processing Unwrap: token={underlying_token}, recipient={recipient}, amount={amount}")

                    try:
                        # Call withdraw on source chain
                        nonce = w3_source.eth.get_transaction_count(warden_address, 'pending')

                        withdraw_txn = source_contract.functions.withdraw(
                            underlying_token,
                            recipient,
                            amount
                        ).build_transaction({
                            'from': warden_address,
                            'nonce': nonce,
                            'gas': 500000,
                            'gasPrice': w3_source.eth.gas_price,
                        })

                        signed_txn = w3_source.eth.account.sign_transaction(
                            withdraw_txn, private_key=PRIVATE_KEY)
                        tx_hash = w3_source.eth.send_raw_transaction(signed_txn.raw_transaction)

                        print(f"Withdraw transaction sent: {tx_hash.hex()}")

                        # Wait for confirmation
                        receipt = w3_source.eth.wait_for_transaction_receipt(
                            tx_hash, timeout=120)
                        print(
                            f"Withdraw transaction confirmed in block {receipt.blockNumber}")

                    except Exception as e:
                        print(f"Error processing withdraw: {e}")

        except Exception as e:
            print(f"Error getting Unwrap events: {e}")
