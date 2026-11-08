"""The easy-start pieces: saving the key, the data-folder wallet guard,
AUTO_APPROVE and the wizard's flow. Offline: chain reads and approvals are
faked; nothing is signed or sent."""

import os

import pytest

import run_bot
import start
from bot import easy_setup
from bot.settings import SettingsError, load_settings

HARDHAT_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
HARDHAT_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
OTHER_ADDR = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("WALLET_PRIVATE_KEY", "WALLET_ADDRESS", "AUTO_APPROVE", "TREND_CORE", "DATA_DIR"):
        monkeypatch.delenv(k, raising=False)


def test_write_env_key_starts_from_the_example_and_keeps_other_lines(tmp_path):
    example = tmp_path / ".env.example"
    example.write_text("# comment\nWALLET_PRIVATE_KEY=your-private-key-here\nMAX_TRADE_USD=25\n", encoding="utf-8")
    env = tmp_path / ".env"
    easy_setup.write_env_key(env, example, "abc")
    assert env.read_text(encoding="utf-8") == "# comment\nWALLET_PRIVATE_KEY=abc\nMAX_TRADE_USD=25\n"
    easy_setup.write_env_key(env, example, "def")          # replaced, not duplicated
    assert env.read_text(encoding="utf-8").count("WALLET_PRIVATE_KEY=") == 1
    assert "WALLET_PRIVATE_KEY=def" in env.read_text(encoding="utf-8")


def test_write_env_key_without_example(tmp_path):
    env = tmp_path / ".env"
    easy_setup.write_env_key(env, tmp_path / "missing", "abc")
    assert env.read_text(encoding="utf-8") == "WALLET_PRIVATE_KEY=abc\n"


def test_clean_key_strips_what_people_paste():
    assert easy_setup.clean_key('  "0xabc"\r\n') == "0xabc"


def test_wallet_mismatch_only_with_memory_of_another_wallet(tmp_path):
    d = tmp_path / "data"
    assert easy_setup.wallet_mismatch(d, HARDHAT_ADDR) is None              # nothing yet
    easy_setup.remember_wallet(d, OTHER_ADDR)
    assert easy_setup.wallet_mismatch(d, HARDHAT_ADDR) is None              # no memory to confuse
    (d / "state.json").write_text("{}", encoding="utf-8")
    assert easy_setup.wallet_mismatch(d, HARDHAT_ADDR) == OTHER_ADDR
    assert easy_setup.wallet_mismatch(d, OTHER_ADDR.lower()) is None        # same wallet, any case


def test_set_aside_renames_never_deletes(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "state.json").write_text("{}", encoding="utf-8")
    moved = easy_setup.set_aside(d, now=lambda: 0)
    assert not d.exists() and (moved / "state.json").exists() and moved.name.startswith("data-old-")


def test_funding_problems():
    ok = {"usdc": 200.0, "eth": 0.001}
    assert easy_setup.funding_problems(ok) == []
    assert len(easy_setup.funding_problems({"usdc": 10.0, "eth": 0.0})) == 2


def test_auto_approve_setting(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(f"WALLET_PRIVATE_KEY={HARDHAT_KEY}\n", encoding="utf-8")
    assert load_settings(env).auto_approve is False
    monkeypatch.setenv("AUTO_APPROVE", "yes")
    assert load_settings(env).auto_approve is True
    monkeypatch.setenv("AUTO_APPROVE", "sure")
    with pytest.raises(SettingsError):
        load_settings(env)


def _settings(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("WALLET_PRIVATE_KEY", HARDHAT_KEY)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return load_settings(tmp_path / "no.env")


def test_bot_refuses_another_wallets_memory(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    easy_setup.remember_wallet(s.data_dir, OTHER_ADDR)
    (s.data_dir / "state.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        run_bot.check_data_folder(s)


def test_bot_remembers_its_wallet(tmp_path, monkeypatch):
    s = _settings(tmp_path, monkeypatch)
    run_bot.check_data_folder(s)
    assert easy_setup.data_wallet(s.data_dir) == HARDHAT_ADDR


def test_auto_approve_agrees_without_asking(tmp_path, monkeypatch):
    from bot import approvals
    s = _settings(tmp_path, monkeypatch, AUTO_APPROVE="yes")
    seen = {}

    def fake_both(settings, confirm):
        seen["answer"] = confirm("Type YES: ")
        return True
    monkeypatch.setattr(approvals, "approve_both", fake_both)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not ask"))
    run_bot.auto_approve(s)
    assert seen["answer"] is True


def test_auto_approve_failure_never_stops_the_bot(tmp_path, monkeypatch):
    from bot import approvals
    s = _settings(tmp_path, monkeypatch, AUTO_APPROVE="yes")
    monkeypatch.setattr(approvals, "approve_both", lambda settings, confirm: (_ for _ in ()).throw(RuntimeError("rpc")))
    run_bot.auto_approve(s)      # logs, doesn't raise


# ---------------------------------------------------------------------------
# The wizard (start.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def wizard(tmp_path, monkeypatch):
    monkeypatch.setattr(start, "ENV", tmp_path / ".env")
    monkeypatch.setattr(start, "EXAMPLE", tmp_path / ".env.example")
    (tmp_path / ".env.example").write_text("WALLET_PRIVATE_KEY=your-private-key-here\nTREND_CORE=on\n",
                                           encoding="utf-8")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    answers = []
    monkeypatch.setattr(start, "ask", lambda q: answers.pop(0))
    return tmp_path, answers


def test_wizard_saves_a_confirmed_key(wizard, monkeypatch):
    tmp_path, answers = wizard
    keys = iter(["not a key", HARDHAT_KEY])
    monkeypatch.setattr(start.getpass, "getpass", lambda prompt="": next(keys))
    answers.append("y")
    assert start.key_is_set() is False
    start.enter_key()
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"WALLET_PRIVATE_KEY={HARDHAT_KEY}" in text and "TREND_CORE=on" in text
    assert load_settings(tmp_path / ".env").wallet_address == HARDHAT_ADDR


def test_wizard_asks_again_if_the_address_isnt_right(wizard, monkeypatch):
    tmp_path, answers = wizard
    other_key = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
    keys = iter([other_key, HARDHAT_KEY])
    monkeypatch.setattr(start.getpass, "getpass", lambda prompt="": next(keys))
    answers.extend(["n", "y"])
    start.enter_key()
    assert HARDHAT_KEY in (tmp_path / ".env").read_text(encoding="utf-8")


def _funded(approved):
    return {"price": 80_000.0, "usdc": 200.0, "wbtc": 0.0, "eth": 0.001, "approved": approved}


def test_wizard_offers_approvals_when_missing(wizard, monkeypatch):
    from bot import approvals
    tmp_path, answers = wizard
    monkeypatch.setenv("WALLET_PRIVATE_KEY", HARDHAT_KEY)
    monkeypatch.setattr(start, "wallet_status", lambda a: _funded(False))
    called = []
    monkeypatch.setattr(approvals, "approve_both", lambda s: called.append(s) or True)
    start.check_wallet(load_settings(start.ENV))
    assert len(called) == 1


def test_wizard_skips_approvals_when_in_place(wizard, monkeypatch):
    from bot import approvals
    tmp_path, answers = wizard
    monkeypatch.setenv("WALLET_PRIVATE_KEY", HARDHAT_KEY)
    monkeypatch.setattr(start, "wallet_status", lambda a: _funded(True))
    monkeypatch.setattr(approvals, "approve_both", lambda s: pytest.fail("no approvals needed"))
    start.check_wallet(load_settings(start.ENV))


def test_wizard_waits_for_funds_then_continues(wizard, monkeypatch):
    tmp_path, answers = wizard
    monkeypatch.setenv("WALLET_PRIVATE_KEY", HARDHAT_KEY)
    states = iter([{**_funded(True), "usdc": 0.0}, _funded(True)])
    monkeypatch.setattr(start, "wallet_status", lambda a: next(states))
    answers.append("")                       # press Enter to check again
    start.check_wallet(load_settings(start.ENV))
    assert answers == []


def test_wizard_moves_another_wallets_data_only_on_MOVE(wizard, monkeypatch):
    tmp_path, answers = wizard
    monkeypatch.setenv("WALLET_PRIVATE_KEY", HARDHAT_KEY)
    s = load_settings(start.ENV)
    easy_setup.remember_wallet(s.data_dir, OTHER_ADDR)
    (s.data_dir / "state.json").write_text("{}", encoding="utf-8")
    answers.append("no")
    with pytest.raises(SystemExit):
        start.check_data_folder(s)
    assert (s.data_dir / "state.json").exists()
    answers.append("MOVE")
    start.check_data_folder(s)
    assert not s.data_dir.exists() and any(p.name.startswith("data-old-") for p in tmp_path.iterdir())


def test_launchers_and_railway_config_exist():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("Start Bot (Windows).bat", "Start Bot (Mac).command", "start.sh", "railway.json"):
        assert os.path.exists(os.path.join(root, name)), name
    import json
    cfg = json.load(open(os.path.join(root, "railway.json"), encoding="utf-8"))["deploy"]
    assert cfg["startCommand"] == "python run_bot.py" and cfg["requiredMountPath"] == "/data"
    assert cfg["overlapSeconds"] == 0 and cfg["numReplicas"] == 1      # never two bots on one wallet
