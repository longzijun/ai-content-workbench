"""The interactive initializer retries mismatch without storing or printing it."""
import sys

from scripts import manage_access


def test_mismatch_and_short_password_retry_until_confirmed(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["manage_access.py"])
    monkeypatch.setattr(manage_access.db, "init_db", lambda: None)
    monkeypatch.setattr(manage_access.access, "configured", lambda: False)
    answers = iter(["short", "valid-test-password", "different-test-password", "confirmed-test-password", "confirmed-test-password"])
    monkeypatch.setattr(manage_access.getpass, "getpass", lambda *_: next(answers))
    saved = []
    monkeypatch.setattr(manage_access.access, "setup_password", lambda password, **_: saved.append(password))
    manage_access.main()
    assert saved == ["confirmed-test-password"]
    output = capsys.readouterr().out
    assert "输入不一致" in output and "密码已配置" in output
    assert all(secret not in output for secret in ("valid-test-password", "different-test-password", "confirmed-test-password"))


def test_initialized_owner_is_not_accidentally_reset(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["manage_access.py"])
    monkeypatch.setattr(manage_access.db, "init_db", lambda: None)
    monkeypatch.setattr(manage_access.access, "configured", lambda: True)
    monkeypatch.setattr(manage_access.getpass, "getpass", lambda *_: (_ for _ in ()).throw(AssertionError("should not prompt")))
    manage_access.main()
