"""
Network configuration for the live executor. Kept separate from bot/config.py
(which is the paper bot's config) since live trading needs to switch between
testnet and mainnet, with genuinely different contract addresses on each --
confirmed during development that Uniswap's contracts are NOT deployed at
the same addresses on Arbitrum Sepolia as they are on Arbitrum One, and
Arbitrum Sepolia has no meaningfully-liquid WBTC/USDC pool at all. So
testnet here is used only for rehearsing the transaction-signing pipeline
(an ERC20 approve), never for a real swap rehearsal.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NetworkConfig:
    name: str
    chain_id: int
    rpc_urls: tuple
    usdc_address: str
    wbtc_address: str | None       # None on testnet -- no real WBTC token there
    pool_address: str | None       # None on testnet -- no real pool there
    router_address: str | None     # None on testnet -- SwapRouter02 isn't deployed at the mainnet address there
    usdc_decimals: int = 6
    wbtc_decimals: int = 8
    pool_fee_units: int = 500


MAINNET = NetworkConfig(
    name="Arbitrum One (mainnet -- real funds)",
    chain_id=42161,
    rpc_urls=(
        "https://arb1.arbitrum.io/rpc",
        "https://arbitrum-one.publicnode.com",
        "https://rpc.ankr.com/arbitrum",
    ),
    usdc_address="0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    wbtc_address="0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
    pool_address="0x0E4831319A50228B9e450861297aB92dee15B44F",
    router_address="0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45",   # Uniswap V3 SwapRouter02, verified on-chain
)

TESTNET = NetworkConfig(
    name="Arbitrum Sepolia (testnet -- rehearsal only, no real swap possible)",
    chain_id=421614,
    rpc_urls=(
        "https://sepolia-rollup.arbitrum.io/rpc",
        "https://arbitrum-sepolia.publicnode.com",
    ),
    usdc_address="0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d",   # confirmed via Blockscout token balance check
    wbtc_address=None,
    pool_address=None,
    router_address=None,
)


def get_network(name: str) -> NetworkConfig:
    if name.lower() in ("mainnet", "arbitrum-one", "arbitrum_one"):
        return MAINNET
    if name.lower() in ("testnet", "arbitrum-sepolia", "arbitrum_sepolia"):
        return TESTNET
    raise ValueError(f"Unknown network {name!r} -- expected 'mainnet' or 'testnet'")


# Slippage tolerance for the amountOutMinimum floor on live swaps. Wider
# than the paper bot's modeled max slippage (0.10%) since a real swap can
# genuinely execute worse than that under real market conditions -- this
# protects against an outlier fill, not a guarantee of matching the
# simulation exactly.
LIVE_SLIPPAGE_TOLERANCE_PCT = 0.5

# If the current price has moved more than this much from the price a
# proposal was written against, refuse to execute it rather than blindly
# trading on stale data -- relevant once approvals can lag behind proposals.
MAX_PRICE_STALENESS_PCT = 1.0

# A proposal still PENDING or APPROVED this long after it was written is
# marked EXPIRED_STALE instead of blocking its engine (BUY) or its position
# (SELL) indefinitely -- P2: a manual-mode RH BUY sat PENDING from
# 2026-07-28 to 2026-10-13 and blocked every RH entry. In auto mode
# proposals execute in the cycle they're written, so this never fires there.
# 24h = 6 cycles for a human to decide; a fresh signal re-proposes at the
# current price.
PROPOSAL_MAX_AGE_HOURS = 24

# TWAP guard (2026-11-08): before any swap, the pool's spot price must be
# within this % of its own 30-minute time-weighted average price, or the
# trade is skipped and re-checked next cycle. Protects against trading on a
# momentary spike (e.g. the 2025-10-20 single-transaction wick to $110).
TWAP_WINDOW_SECONDS = 1800
MAX_TWAP_DEVIATION_PCT = 1.0
