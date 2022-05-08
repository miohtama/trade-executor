import logging
import datetime
import time
from collections import Counter
from decimal import Decimal
from typing import List, Dict, Set

from eth_account.datastructures import SignedTransaction
from eth_typing import HexAddress
from hexbytes import HexBytes
from web3 import Web3
from web3.contract import Contract

from eth_defi.abi import get_deployed_contract
from eth_defi.gas import GasPriceSuggestion, apply_gas, estimate_gas_fees
from eth_defi.hotwallet import HotWallet
from eth_defi.token import fetch_erc20_details, TokenDetails
from eth_defi.txmonitor import wait_transactions_to_complete, \
    broadcast_and_wait_transactions_to_complete, broadcast_transactions
from eth_defi.uniswap_v2.deployment import UniswapV2Deployment, FOREVER_DEADLINE
from eth_defi.uniswap_v2.fees import estimate_sell_price, estimate_sell_price_decimals
from eth_defi.uniswap_v2.analysis import analyse_trade, TradeSuccess
from tradeexecutor.state.state import State
from tradeexecutor.state.trade import TradeExecution
from tradeexecutor.state.blockhain_transaction import BlockchainTransaction
from tradeexecutor.state.identifier import AssetIdentifier, TradingPairIdentifier

logger = logging.getLogger(__name__)


class TradeExecutionFailed(Exception):
    """Our Uniswap trade reverted"""


def translate_to_naive_swap(
        web3: Web3,
        deployment: UniswapV2Deployment,
        hot_wallet: HotWallet,
        t: TradeExecution,
        gas_fees: GasPriceSuggestion,
        base_token_details: TokenDetails,
        quote_token_details: TokenDetails,
    ):
    """Creates an AMM swap tranasction out of buy/sell.

    If buy tries to do the best execution for given `planned_reserve`.

    If sell tries to do the best execution for given `planned_quantity`.

    Route only between two pools - stablecoin reserve and target buy/sell.

    Any gas price is set by `web3` instance gas price strategy.

    :param t:
    :return: Unsigned transaction
    """

    if t.is_buy():
        amount0_in = int(t.planned_reserve * 10**quote_token_details.decimals)
        path = [quote_token_details.address, base_token_details.address]
        t.reserve_currency_allocated = t.planned_reserve
    else:
        # Reverse swap
        amount0_in = int(-t.planned_quantity * 10**base_token_details.decimals)
        path = [base_token_details.address, quote_token_details.address]
        t.reserve_currency_allocated = 0

    args = [
        amount0_in,
        0,
        path,
        hot_wallet.address,
        FOREVER_DEADLINE,
    ]

    # https://docs.uniswap.org/protocol/V2/reference/smart-contracts/router-02#swapexacttokensfortokens
    # https://web3py.readthedocs.io/en/stable/web3.eth.account.html#sign-a-contract-transaction
    tx = deployment.router.functions.swapExactTokensForTokens(
        *args,
    ).buildTransaction({
        'chainId': web3.eth.chain_id,
        'gas': 350_000,  # Estimate max 350k gas per swap
        'from': hot_wallet.address,
    })

    apply_gas(tx, gas_fees)

    signed = hot_wallet.sign_transaction_with_new_nonce(tx)
    selector = deployment.router.functions.swapExactTokensForTokens

    # Create record of this transaction
    tx_info = t.tx_info = BlockchainTransaction()
    tx_info.set_target_information(
        web3.eth.chain_id,
        deployment.router.address,
        selector.fn_name,
        args,
        tx,
    )

    tx_info.set_broadcast_information(tx["nonce"], signed.hash.hex(), signed.rawTransaction.hex())


def prepare_swaps(
        web3: Web3,
        hot_wallet: HotWallet,
        uniswap: UniswapV2Deployment,
        ts: datetime.datetime,
        state: State,
        instructions: List[TradeExecution],
        underflow_check=True) -> Dict[HexAddress, int]:
    """Prepare multiple swaps to be breoadcasted parallel from the hot wallet.

    :param underflow_check: Do we check we have enough cash in hand before trying to prepare trades.
        Note that because when executing sell orders first, we will have more cash in hand to make buys.

    :return: Token approvals we need to execute the trades
    """

    # Get our starting nonce
    gas_fees = estimate_gas_fees(web3)

    for idx, t in enumerate(instructions):

        base_token_details = fetch_erc20_details(web3, t.pair.base.checksum_address)
        quote_token_details = fetch_erc20_details(web3, t.pair.quote.checksum_address)

        assert base_token_details.decimals is not None, f"Bad token at {t.pair.base.address}"
        assert quote_token_details.decimals is not None, f"Bad token at {t.pair.quote.address}"

        state.portfolio.check_for_nonce_reuse(hot_wallet.current_nonce)

        translate_to_naive_swap(
            web3,
            uniswap,
            hot_wallet,
            t,
            gas_fees,
            base_token_details,
            quote_token_details,
        )

        if t.is_buy():
            state.portfolio.move_capital_from_reserves_to_trade(t, underflow_check=underflow_check)

        t.started_at = ts


def approve_tokens(
        web3: Web3,
        deployment: UniswapV2Deployment,
        hot_wallet: HotWallet,
        instructions: List[TradeExecution],
    ) -> List[SignedTransaction]:
    """Approve multiple ERC-20 token allowances for the trades needed.

    Each token is approved only once. E.g. if you have 4 trades using USDC,
    you will get 1 USDC approval.
    """

    signed = []

    approvals = Counter()

    for idx, t in enumerate(instructions):

        base_token_details = fetch_erc20_details(web3, t.pair.base.checksum_address)
        quote_token_details = fetch_erc20_details(web3, t.pair.quote.checksum_address)

        # Update approval counters for the whole batch
        if t.is_buy():
            approvals[quote_token_details.address] += int(t.planned_reserve * 10**quote_token_details.decimals)
        else:
            approvals[base_token_details.address] += int(-t.planned_quantity * 10**base_token_details.decimals)

    for idx, tpl in enumerate(approvals.items()):
        token_address, amount = tpl

        assert amount > 0, f"Got a non-positive approval {token_address}: {amount}"

        token = get_deployed_contract(web3, "IERC20.json", token_address)
        tx = token.functions.approve(
            deployment.router.address,
            amount,
        ).buildTransaction({
            'chainId': web3.eth.chain_id,
            'gas': 100_000,  # Estimate max 100k per approval
            'from': hot_wallet.address,
        })
        signed.append(hot_wallet.sign_transaction_with_new_nonce(tx))

    return signed


def approve_infinity(
        web3: Web3,
        deployment: UniswapV2Deployment,
        hot_wallet: HotWallet,
        instructions: List[TradeExecution],
    ) -> List[SignedTransaction]:
    """Approve multiple ERC-20 token allowances for the trades needed.

    Each token is approved only once. E.g. if you have 4 trades using USDC,
    you will get 1 USDC approval.
    """

    signed = []

    approvals = Counter()

    for idx, t in enumerate(instructions):

        base_token_details = fetch_erc20_details(web3, t.pair.base.checksum_address)
        quote_token_details = fetch_erc20_details(web3, t.pair.quote.checksum_address)

        # Update approval counters for the whole batch
        if t.is_buy():
            approvals[quote_token_details.address] += int(t.planned_reserve * 10**quote_token_details.decimals)
        else:
            approvals[base_token_details.address] += int(-t.planned_quantity * 10**base_token_details.decimals)

    for idx, tpl in enumerate(approvals.items()):
        token_address, amount = tpl

        assert amount > 0, f"Got a non-positive approval {token_address}: {amount}"

        token = get_deployed_contract(web3, "IERC20.json", token_address)
        tx = token.functions.approve(
            deployment.router.address,
            amount,
        ).buildTransaction({
            'chainId': web3.eth.chain_id,
            'gas': 100_000,  # Estimate max 100k per approval
            'from': hot_wallet.address,
        })
        signed.append(hot_wallet.sign_transaction_with_new_nonce(tx))

    return signed


def confirm_approvals(
        web3: Web3,
        txs: List[SignedTransaction],
        confirmation_block_count=0,
        max_timeout=datetime.timedelta(minutes=5),
    ):
    """Wait until all transactions are confirmed.

    :param confirmation_block_count: How many blocks to wait for the transaction to settle

    :raise: If any of the transactions fail
    """
    logger.info("Confirming %d approvals, confirmation_block_count is %d", len(txs), confirmation_block_count)
    receipts = broadcast_and_wait_transactions_to_complete(
        web3,
        txs,
        confirmation_block_count=confirmation_block_count,
        max_timeout=max_timeout)
    return receipts


def broadcast(
        web3: Web3,
        ts: datetime.datetime,
        instructions: List[TradeExecution],
        confirmation_block_count: int=0,
        ganache_sleep=0.5) -> Dict[HexBytes, TradeExecution]:
    """Broadcast multiple transations and manage the trade executor state for them.

    :return: Map of transaction hashes to watch
    """

    logger.info("Broadcasting %d trades", len(instructions))

    res = {}
    # Another nonce guard
    nonces: Set[int] = set()

    broadcast_batch: List[SignedTransaction] = []

    for t in instructions:
        assert isinstance(t.tx_info.signed_bytes, str), f"Got signed transaction: {t.tx_info.signed_bytes}"
        assert t.tx_info.nonce not in nonces, "Nonce already used"
        nonces.add(t.tx_info.nonce)
        t.broadcasted_at = ts
        res[t.tx_info.tx_hash] = t
        # Only SignedTransaction.rawTransaction attribute is intresting in this point
        tx = SignedTransaction(rawTransaction=t.tx_info.signed_bytes, hash=None, r=0, s=0, v=0)
        broadcast_batch.append(tx)

    hashes = broadcast_transactions(web3, broadcast_batch, confirmation_block_count=confirmation_block_count)
    assert len(hashes) == len(instructions)

    return res


def wait_trades_to_complete(
        web3: Web3,
        trades: List[TradeExecution],
        confirmation_block_count=0,
        max_timeout=datetime.timedelta(minutes=5),
        poll_delay=datetime.timedelta(seconds=1)) -> Dict[HexBytes, dict]:
    """Watch multiple transactions executed at parallel.

    :return: Map of transaction hashes -> receipt
    """
    logger.info("Waiting %d trades to confirm", len(trades))
    assert isinstance(confirmation_block_count, int)
    tx_hashes = [t.tx_info.tx_hash for t in trades]
    receipts = wait_transactions_to_complete(web3, tx_hashes, confirmation_block_count, max_timeout, poll_delay)
    return receipts


def resolve_trades(
        web3: Web3,
        uniswap: UniswapV2Deployment,
        ts: datetime.datetime,
        state: State,
        trades: Dict[HexBytes, TradeExecution],
        receipts: Dict[HexBytes, dict],
        stop_on_execution_failure=True):
    """Resolve trade outcome.

    Read on-chain Uniswap swap data from the transaction receipt and record how it went.

    Mutates the trade objects in-place.

    :stop_on_execution_failure: Raise an exception if any of the trades failed
    """

    for tx_hash, receipt in receipts.items():
        trade = trades[tx_hash.hex()]

        logger.info("Resolved trade %s", trade)

        base_token_details = fetch_erc20_details(web3, trade.pair.base.checksum_address)
        quote_token_details = fetch_erc20_details(web3, trade.pair.quote.checksum_address)

        # Update the transaction confirmation status
        status = receipt["status"] == 1
        trade.tx_info.set_confirmation_information(
            ts,
            receipt["blockNumber"],
            receipt["blockHash"].hex(),
            receipt.get("effectiveGasPrice", 0),
            receipt["gasUsed"],
            status
        )

        result = analyse_trade(web3, uniswap, tx_hash)
        if isinstance(result, TradeSuccess):

            # TODO: Assumes stablecoin trades only ATM

            if trade.is_buy():
                assert result.path[0] == quote_token_details.address
                price = 1 / result.price
                executed_reserve = result.amount_in / Decimal(10**quote_token_details.decimals)
                executed_amount = result.amount_out / Decimal(10**base_token_details.decimals)
            else:
                # Ordered other way around
                assert result.path[0] == base_token_details.address
                assert result.path[-1] == quote_token_details.address
                price = result.price
                executed_amount = -result.amount_in / Decimal(10**base_token_details.decimals)
                executed_reserve = result.amount_out / Decimal(10**quote_token_details.decimals)

            assert (executed_reserve > 0) and (executed_amount != 0) and (price > 0), f"Executed amount {executed_amount}, executed_reserve: {executed_reserve}, price: {price}, tx info {trade.tx_info}"

            # Mark as success
            state.mark_trade_success(
                ts,
                trade,
                executed_price=float(price),
                executed_amount=executed_amount,
                executed_reserve=executed_reserve,
                lp_fees=0,
                native_token_price=1.0,
            )
        else:
            logger.error("Trade failed %s: %s", ts, trade)
            state.mark_trade_failed(
                ts,
                trade,
            )

            if stop_on_execution_failure:
                tx_hash = trade.tx_info.tx_hash
                raise TradeExecutionFailed(f"Could not execute a trade: {trade}, transaction was {tx_hash}")

            trade.tx_info.revert_reason = result.revert_reason


def get_current_price(web3: Web3, uniswap: UniswapV2Deployment, pair: TradingPairIdentifier, quantity=Decimal(1)) -> float:
    """Get a price from Uniswap v2 pool, assuming you are selling 1 unit of base token.

    Does decimal adjustment.

    :return: Price in quote token.
    """
    price = estimate_sell_price_decimals(uniswap, pair.base.checksum_address, pair.quote.checksum_address, quantity)
    return float(price)


def get_held_assets(web3: Web3, address: HexAddress, assets: List[AssetIdentifier]) -> Dict[HexAddress, Decimal]:
    """Get list of assets hold by the a wallet."""

    result = {}
    for asset in assets:
        token_details = fetch_erc20_details(web3, asset.checksum_address)
        balance = token_details.contract.functions.balanceOf(address).call()
        result[token_details.address] = Decimal(balance) / Decimal(10 ** token_details.decimals)
    return result


def get_token_for_asset(web3: Web3, asset: AssetIdentifier) -> Contract:
    """Get ERC-20 contract proxy."""
    erc_20 = get_deployed_contract(web3, "ERC20MockDecimals.json", asset.address)
    return erc_20
