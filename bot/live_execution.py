"""
Turns an APPROVED proposal into a real, on-chain swap -- and only an
approved one. Nothing in this module ever runs without a human having
already changed a Status cell from PENDING to APPROVED.

LiveFillResult is deliberately shaped to match the paper bot's FillResult
(same field names: fill_price, wbtc_qty, usd_value, slippage_pct,
fees_usd) so it can be handed directly to TrendBotEngine.open_position()
and RangeHarvesterEngine.open_position() completely unmodified -- the same
"genuine drop-in" approach already proven for LiveVault against
check_entry(). fees_usd is 0.0 here rather than omitted: real costs are
paid in ETH (a separate balance, reported separately as gas_cost_eth), not
as a USD deduction the way the simulated cost model works.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .live_config import (NetworkConfig, LIVE_SLIPPAGE_TOLERANCE_PCT, MAX_PRICE_STALENESS_PCT,
                          TWAP_WINDOW_SECONDS, MAX_TWAP_DEVIATION_PCT)
from .onchain import get_live_price, get_twap_price
from .live_wallet import (
    encode_exact_input_single, compute_min_amount_out, build_and_sign_transaction,
    broadcast_transaction, wait_for_receipt, receipt_succeeded, decode_swap_event,
    LiveWalletError,
)


class StalePriceError(LiveWalletError):
    """Raised when the current price has moved too far from the price a
    proposal was written against -- refuses to execute on stale data
    rather than silently trading at a materially different price than what
    was actually approved."""
    pass


class ExecutionRevertedError(LiveWalletError):
    """The transaction was mined but reverted -- distinct from a network
    failure, since gas was spent for a real (if failed) attempt."""
    def __init__(self, message: str, tx_hash: str):
        super().__init__(message)
        self.tx_hash = tx_hash


class TwapDeviationError(StalePriceError):
    """The pool's spot price is too far from its own 30-minute average (a
    momentary spike), or the average couldn't be read. Handled exactly like a
    stale price: nothing is sent, and the strategy re-checks next cycle."""
    pass


def check_twap(pool_address: str, spot_price: float, max_deviation_pct: float = MAX_TWAP_DEVIATION_PCT,
               seconds: int = TWAP_WINDOW_SECONDS, twap_reader=get_twap_price) -> float:
    """Raise TwapDeviationError unless the spot price is within
    max_deviation_pct of the pool's TWAP. Fails closed: if the TWAP can't be
    read, the trade is skipped. Returns the TWAP."""
    try:
        twap = twap_reader(pool_address, seconds)
    except Exception as e:  # noqa: BLE001
        raise TwapDeviationError(f"Couldn't read the pool's {seconds // 60}-minute average price ({e}); "
                                 f"refusing to trade without it.") from e
    if twap <= 0:
        raise TwapDeviationError(f"The pool's {seconds // 60}-minute average price is invalid ({twap}).")
    deviation = abs(spot_price - twap) / twap * 100
    if deviation > max_deviation_pct:
        raise TwapDeviationError(
            f"Spot price ${spot_price:,.2f} is {deviation:.2f}% from the pool's {seconds // 60}-minute average "
            f"${twap:,.2f} (limit {max_deviation_pct}%) -- possible temporary spike. Refusing to execute.")
    return twap


@dataclass
class LiveFillResult:
    fill_price: float
    wbtc_qty: float
    usd_value: float
    slippage_pct: float
    fees_usd: float
    tx_hash: str
    gas_used: int
    gas_cost_eth: float
    block_number: int


def check_price_staleness(current_price: float, price_at_proposal: float,
                           max_staleness_pct: float = MAX_PRICE_STALENESS_PCT) -> tuple[bool, float]:
    if price_at_proposal <= 0:
        return True, float("inf")
    staleness_pct = abs(current_price - price_at_proposal) / price_at_proposal * 100
    return staleness_pct > max_staleness_pct, staleness_pct


def _gas_cost_eth(receipt: dict) -> float:
    gas_used = int(receipt["gasUsed"], 16)
    effective_gas_price = int(receipt.get("effectiveGasPrice", "0x0"), 16)
    return gas_used * effective_gas_price / 10 ** 18


def execute_buy(network: NetworkConfig, private_key_hex: str, wallet_address: str,
                 size_usd: float, price_at_proposal: float,
                 slippage_tolerance_pct: float = LIVE_SLIPPAGE_TOLERANCE_PCT,
                 max_staleness_pct: float = MAX_PRICE_STALENESS_PCT) -> LiveFillResult:
    """USDC -> WBTC. Raises StalePriceError before sending anything if the
    price has moved too far since the proposal was written."""
    rpc_urls = list(network.rpc_urls)

    price_info = get_live_price(network.pool_address)
    current_price = price_info["price_usdc_per_wbtc"]
    is_stale, staleness_pct = check_price_staleness(current_price, price_at_proposal, max_staleness_pct)
    if is_stale:
        raise StalePriceError(
            f"Price moved {staleness_pct:.2f}% since proposal (limit {max_staleness_pct}%) -- "
            f"proposal price ${price_at_proposal:,.2f}, current ${current_price:,.2f}. Refusing to execute."
        )
    check_twap(network.pool_address, current_price)

    amount_in_raw = int(size_usd * 10 ** network.usdc_decimals)
    expected_wbtc = size_usd / current_price
    expected_out_raw = int(expected_wbtc * 10 ** network.wbtc_decimals)
    min_out_raw = compute_min_amount_out(expected_out_raw, slippage_tolerance_pct)

    data = encode_exact_input_single(
        token_in=network.usdc_address, token_out=network.wbtc_address, fee=network.pool_fee_units,
        recipient=wallet_address, amount_in=amount_in_raw, amount_out_minimum=min_out_raw,
    )
    signed = build_and_sign_transaction(rpc_urls, network.chain_id, private_key_hex, network.router_address, data)
    tx_hash = broadcast_transaction(rpc_urls, signed.raw_hex)
    receipt = wait_for_receipt(rpc_urls, tx_hash)

    if not receipt_succeeded(receipt):
        raise ExecutionRevertedError(f"Buy transaction {tx_hash} was mined but reverted", tx_hash)

    swap = decode_swap_event(receipt, network.pool_address)
    # token0 = WBTC, token1 = USDC for this pool -- pool pays WBTC out (negative amount0),
    # receives USDC in (positive amount1).
    actual_wbtc = abs(swap.amount0) / 10 ** network.wbtc_decimals
    actual_usdc = abs(swap.amount1) / 10 ** network.usdc_decimals
    actual_fill_price = actual_usdc / actual_wbtc if actual_wbtc > 0 else 0.0
    slippage_pct = abs(actual_fill_price - current_price) / current_price * 100 if current_price > 0 else 0.0

    return LiveFillResult(
        fill_price=actual_fill_price, wbtc_qty=actual_wbtc, usd_value=actual_usdc,
        slippage_pct=slippage_pct, fees_usd=0.0, tx_hash=tx_hash,
        gas_used=int(receipt["gasUsed"], 16), gas_cost_eth=_gas_cost_eth(receipt),
        block_number=int(receipt["blockNumber"], 16),
    )


def execute_sell(network: NetworkConfig, private_key_hex: str, wallet_address: str,
                  wbtc_qty: float, price_at_proposal: float,
                  slippage_tolerance_pct: float = LIVE_SLIPPAGE_TOLERANCE_PCT,
                  max_staleness_pct: float = MAX_PRICE_STALENESS_PCT) -> LiveFillResult:
    """WBTC -> USDC. Same staleness guard as execute_buy."""
    rpc_urls = list(network.rpc_urls)

    price_info = get_live_price(network.pool_address)
    current_price = price_info["price_usdc_per_wbtc"]
    is_stale, staleness_pct = check_price_staleness(current_price, price_at_proposal, max_staleness_pct)
    if is_stale:
        raise StalePriceError(
            f"Price moved {staleness_pct:.2f}% since proposal (limit {max_staleness_pct}%) -- "
            f"proposal price ${price_at_proposal:,.2f}, current ${current_price:,.2f}. Refusing to execute."
        )
    check_twap(network.pool_address, current_price)

    amount_in_raw = int(wbtc_qty * 10 ** network.wbtc_decimals)
    expected_usdc = wbtc_qty * current_price
    expected_out_raw = int(expected_usdc * 10 ** network.usdc_decimals)
    min_out_raw = compute_min_amount_out(expected_out_raw, slippage_tolerance_pct)

    data = encode_exact_input_single(
        token_in=network.wbtc_address, token_out=network.usdc_address, fee=network.pool_fee_units,
        recipient=wallet_address, amount_in=amount_in_raw, amount_out_minimum=min_out_raw,
    )
    signed = build_and_sign_transaction(rpc_urls, network.chain_id, private_key_hex, network.router_address, data)
    tx_hash = broadcast_transaction(rpc_urls, signed.raw_hex)
    receipt = wait_for_receipt(rpc_urls, tx_hash)

    if not receipt_succeeded(receipt):
        raise ExecutionRevertedError(f"Sell transaction {tx_hash} was mined but reverted", tx_hash)

    swap = decode_swap_event(receipt, network.pool_address)
    # This time the pool receives WBTC in (positive amount0) and pays USDC out (negative amount1).
    actual_wbtc = abs(swap.amount0) / 10 ** network.wbtc_decimals
    actual_usdc = abs(swap.amount1) / 10 ** network.usdc_decimals
    actual_fill_price = actual_usdc / actual_wbtc if actual_wbtc > 0 else 0.0
    slippage_pct = abs(actual_fill_price - current_price) / current_price * 100 if current_price > 0 else 0.0

    return LiveFillResult(
        fill_price=actual_fill_price, wbtc_qty=actual_wbtc, usd_value=actual_usdc,
        slippage_pct=slippage_pct, fees_usd=0.0, tx_hash=tx_hash,
        gas_used=int(receipt["gasUsed"], 16), gas_cost_eth=_gas_cost_eth(receipt),
        block_number=int(receipt["blockNumber"], 16),
    )
