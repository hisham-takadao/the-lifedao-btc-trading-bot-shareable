"""The shareable version's own pieces: settings from the private key, local
file storage, and the optional trade-size cap. Offline."""

import json

import pytest

import run_bot
from bot.live_config import MAINNET
from bot.live_proposals import Proposal, STATUS_APPROVED, STATUS_EXECUTED
from bot.local_store import LocalStore
from bot.settings import SettingsError, load_settings

# Public Hardhat test key: well known, holds no funds, never use it for real.
HARDHAT_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
HARDHAT_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for k in ("WALLET_PRIVATE_KEY", "WALLET_ADDRESS", "MAX_TRADE_USD", "CANDLE_SOURCE", "DATA_DIR",
              "COINGECKO_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def test_address_comes_from_the_key(clean_env, monkeypatch):
    monkeypatch.setenv("WALLET_PRIVATE_KEY", HARDHAT_KEY)
    s = load_settings(clean_env / "none.env")
    assert s.wallet_address == HARDHAT_ADDR
    assert HARDHAT_KEY not in repr(s) and HARDHAT_KEY[2:] not in str(s)     # never shown


def test_settings_from_a_dotenv_file(clean_env):
    env = clean_env / ".env"
    env.write_text(f'# comment\nWALLET_PRIVATE_KEY="{HARDHAT_KEY}"\nMAX_TRADE_USD=25\nDATA_DIR={clean_env / "d"}\n')
    s = load_settings(env)
    assert s.wallet_address == HARDHAT_ADDR and s.max_trade_usd == 25.0 and s.data_dir == clean_env / "d"
    assert s.candle_source == "swaps"


def test_missing_or_placeholder_key(clean_env, monkeypatch):
    with pytest.raises(SettingsError, match="not set"):
        load_settings(clean_env / "none.env")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "your-private-key-here")
    with pytest.raises(SettingsError, match="not set"):
        load_settings(clean_env / "none.env")


def test_invalid_key_message_doesnt_echo_it(clean_env, monkeypatch):
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "0x1234notakey")
    with pytest.raises(SettingsError) as e:
        load_settings(clean_env / "none.env")
    assert "1234notakey" not in str(e.value)


def test_address_mismatch_is_refused(clean_env, monkeypatch):
    monkeypatch.setenv("WALLET_PRIVATE_KEY", HARDHAT_KEY)
    monkeypatch.setenv("WALLET_ADDRESS", "0x0000000000000000000000000000000000000001")
    with pytest.raises(SettingsError, match="doesn't match"):
        load_settings(clean_env / "none.env")


def test_bad_options_are_refused(clean_env, monkeypatch):
    monkeypatch.setenv("WALLET_PRIVATE_KEY", HARDHAT_KEY)
    monkeypatch.setenv("MAX_TRADE_USD", "-5")
    with pytest.raises(SettingsError):
        load_settings(clean_env / "none.env")
    monkeypatch.setenv("MAX_TRADE_USD", "")
    monkeypatch.setenv("CANDLE_SOURCE", "hourly")
    with pytest.raises(SettingsError):
        load_settings(clean_env / "none.env")


def proposal(pid, side="BUY", status=STATUS_APPROVED):
    return Proposal(proposal_id=pid, created_at=1_792_281_600, engine="RH", side=side, size_usd=130.65,
                    position_id=None if side == "BUY" else "pos1", reason="ENTRY", price_at_proposal=81_234.5,
                    status=status, meta={"adx_tier": 0})


def test_local_store_round_trips_proposals_and_state(tmp_path):
    st = LocalStore(tmp_path)
    st.write_new_proposal(proposal("a1"))
    st.write_new_proposal(proposal("b2", side="SELL"))
    st.update_status_by_id("a1", STATUS_EXECUTED, **{"Tx Hash": "0xabc", "Actual Fill Price": 81_300.12})
    ps = {p.proposal_id: p for p in st.get_all_proposals()}
    assert ps["a1"].status == STATUS_EXECUTED and ps["a1"].tx_hash == "0xabc" and ps["a1"].actual_fill_price == 81_300.12
    assert ps["a1"].created_at == 1_792_281_600 and ps["a1"].meta == {"adx_tier": 0}
    assert ps["b2"].status == STATUS_APPROVED and ps["b2"].position_id == "pos1"
    with pytest.raises(ValueError):
        st.update_status_by_id("zzz", STATUS_EXECUTED)

    assert st.load_state() is None
    st.save_state({"candle_index": 1})
    st.save_state({"candle_index": 2})
    assert st.load_state() == {"candle_index": 2}
    assert json.loads((tmp_path / "state.json.bak").read_text()) == {"candle_index": 1}
    st.save_partial_candle({"window_ts": 5})
    assert LocalStore(tmp_path).load_partial_candle() == {"window_ts": 5}     # survives a restart


def test_trade_size_cap(monkeypatch):
    v = run_bot.CappedVault(HARDHAT_ADDR, MAINNET, 25.0)
    v._cached_usdc_balance = 500.0
    assert v.max_affordable_size(300.0) == 25.0
    v.cap_usd = None
    assert v.max_affordable_size(300.0) == 300.0
    v._cached_usdc_balance = 60.0
    assert v.max_affordable_size(300.0) == pytest.approx(10.0)              # the $50 reserve still applies


def test_warm_up_drops_the_unfinished_candle():
    from bot.indicators import Candle
    w = 1_792_281_600
    cs = [Candle(w + i * 14400, 1, 1, 1, 1) for i in range(3)]
    assert [c.timestamp for c in run_bot.finished_candles(cs, w + 2 * 14400 + 3600)] == [w, w + 14400]
    assert len(run_bot.finished_candles(cs, w + 3 * 14400)) == 3
