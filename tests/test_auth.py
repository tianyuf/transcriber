import getpass
import json
import stat

import pytest

from transcriber import auth, cli


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, key):
        self.values[(service, account)] = key

    def delete_password(self, service, account):
        del self.values[(service, account)]


def test_native_keychain_storage_and_environment_precedence(monkeypatch):
    keyring = FakeKeyring()
    monkeypatch.setattr(auth, "_keyring", lambda: keyring)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert auth.save_key("stored-openai", "openai") == "the system keychain"
    assert auth.stored_key("openai") == ("stored-openai", "the system keychain")
    monkeypatch.setenv("OPENAI_API_KEY", "environment-openai")
    assert auth.stored_key("openai") == ("environment-openai", "the OPENAI_API_KEY environment variable")
    monkeypatch.delenv("OPENAI_API_KEY")
    assert keyring.values[(auth.SERVICE, "openai")] == "stored-openai"


def test_fallback_file_preserves_multiple_provider_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "FALLBACK_FILE", tmp_path / "credentials.json")
    for provider in ("openai", "grok", "gemini", "anthropic"):
        monkeypatch.delenv(provider == "grok" and "XAI_API_KEY" or f"{provider.upper()}_API_KEY", raising=False)
    auth.save_key("openai-secret", "openai")
    auth.FALLBACK_FILE.chmod(0o644)  # existing files must be tightened too
    auth.save_key("grok-secret", "grok")
    data = json.loads(auth.FALLBACK_FILE.read_text())
    assert data == {"openai_api_key": "openai-secret", "grok_api_key": "grok-secret"}
    assert stat.S_IMODE(auth.FALLBACK_FILE.stat().st_mode) == 0o600
    assert auth.stored_key("grok")[0] == "grok-secret"
    removed = auth.delete_key("openai")
    assert removed and auth.stored_key("openai")[0] is None
    assert auth.stored_key("grok")[0] == "grok-secret"
    auth.delete_key("grok")
    assert not auth.FALLBACK_FILE.exists()


def test_native_login_uses_hidden_prompt_and_does_not_print_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(auth, "FALLBACK_FILE", tmp_path / "credentials.json")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "hidden-anthropic-key")
    auth.login_native("anthropic")
    output = capsys.readouterr().out
    assert "hidden-anthropic-key" not in output
    assert "Saved the Anthropic API key" in output and "when first used" in output
    assert auth.stored_key("anthropic")[0] == "hidden-anthropic-key"


def test_native_status_and_logout(monkeypatch, capsys):
    keyring = FakeKeyring()
    monkeypatch.setattr(auth, "_keyring", lambda: keyring)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert auth.native_status("gemini") == 1
    assert "No Google Gemini key found" in capsys.readouterr().out
    auth.save_key("gemini-secret", "gemini")
    assert auth.native_status("gemini") == 0
    output = capsys.readouterr().out
    assert "not been validated" in output and "gemini-secret" not in output
    auth.logout_native("gemini")
    assert "Removed the Google Gemini key" in capsys.readouterr().out
    assert auth.stored_key("gemini")[0] is None


def test_auth_cli_dispatches_native_provider(monkeypatch):
    called = []
    monkeypatch.setattr(auth, "login_native", lambda provider: called.append(provider))
    args = cli.build_parser().parse_args(["auth", "login", "--provider", "openai"])
    assert cli.cmd_auth(args) == 0 and called == ["openai"]
    args = cli.build_parser().parse_args(["auth", "login", "--provider", "openai", "--headless"])
    with pytest.raises(SystemExit, match="OpenRouter options"):
        cli.cmd_auth(args)


def test_terminal_runs_sign_in_when_no_key_is_saved(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    monkeypatch.setattr(auth, "FALLBACK_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TRANSCRIBER_NO_AUTO_LOGIN", raising=False)
    monkeypatch.setattr(auth, "interactive", lambda: True)
    monkeypatch.setattr(auth, "login", lambda: auth.save_key("from-browser"))
    monkeypatch.setattr(auth, "AUTO_LOGIN", True)
    assert auth.require_key() == "from-browser"
    assert "not signed in to OpenRouter yet" in capsys.readouterr().out


def test_no_automatic_sign_in_for_scripts_libraries_or_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    monkeypatch.setattr(auth, "FALLBACK_FILE", tmp_path / "credentials.json")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(auth, "login", lambda: pytest.fail("must not open a browser"))
    monkeypatch.setattr(auth, "AUTO_LOGIN", False)  # the library and the app
    monkeypatch.setattr(auth, "interactive", lambda: True)
    with pytest.raises(SystemExit, match="transcriber app"):
        auth.require_key()
    monkeypatch.setattr(auth, "AUTO_LOGIN", True)
    monkeypatch.setattr(auth, "interactive", lambda: False)  # output redirected to a log, or an agent
    with pytest.raises(SystemExit, match="auth login"):
        auth.require_key()
    monkeypatch.setattr(auth, "interactive", lambda: True)
    monkeypatch.setenv("TRANSCRIBER_NO_AUTO_LOGIN", "1")
    with pytest.raises(SystemExit):
        auth.require_key()


def test_terminal_asks_for_a_missing_native_key(monkeypatch, tmp_path):
    from transcriber.providers import credentials_for
    monkeypatch.setattr(auth, "_keyring", lambda: None)
    monkeypatch.setattr(auth, "FALLBACK_FILE", tmp_path / "credentials.json")
    monkeypatch.setattr(auth, "CONFIG_DIR", tmp_path)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("TRANSCRIBER_NO_AUTO_LOGIN", raising=False)
    monkeypatch.setattr(auth, "interactive", lambda: True)
    monkeypatch.setattr(auth, "AUTO_LOGIN", True)
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "typed-grok-key")
    assert credentials_for(["grok:grok-4.3"]) == {"grok": "typed-grok-key"}


def test_bare_command_opens_the_app_only_at_a_terminal(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(cli, "cmd_app", lambda a: opened.append(a.port) or 0)
    cli.build_parser()  # the parser binds cmd_app at build time, so rebuild after patching
    monkeypatch.setattr(auth, "interactive", lambda: True)
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 0 and opened == [0]
    monkeypatch.setattr(auth, "interactive", lambda: False)
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2 and "transcriber app" in capsys.readouterr().out
