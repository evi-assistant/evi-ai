import pytest
import json

import evi.users
from evi.users import User, add_user, login_dir_key, colliding_logins
from evi.apps.web import server as web_server
from evi import doctor


def test_login_dir_key():
    names = ['bob smith', 'bob_smith', 'bob/smith', 'Alice', 'alice', '..', 'owner.', ' x ']
    for name in names:
        assert login_dir_key(name) == web_server._login_dir_key(name)


def test_colliding_logins_logic():
    # Use a list of User objects
    users = [
        User("bob smith", "t1"),
        User("bob_smith", "t2"),
        User("alice", "t3"),
    ]
    result = colliding_logins(users)

    # The server's _colliding_logins expects a list of User-like objects (with .name)
    expected = web_server._colliding_logins(users)
    assert result == expected


def test_add_user_collisions(tmp_path):
    users_file = tmp_path / "users.json"

    # 1. add_user('bob_smith') then add_user('bob smith') raises ValueError naming 'bob_smith'
    add_user("bob_smith", path=users_file)
    with pytest.raises(ValueError) as excinfo:
        add_user("bob smith", path=users_file)
    assert "bob_smith" in str(excinfo.value)
    assert "users/bob_smith" in str(excinfo.value)

    # Verify users.json still holds only bob_smith
    users = evi.users.load_users(users_file)
    assert len(users) == 1
    assert users[0].name == "bob_smith"

    # 2. add_user('BOB_SMITH') after bob_smith re-issues (same name ignoring case)
    add_user("BOB_SMITH", path=users_file)
    users = evi.users.load_users(users_file)
    assert len(users) == 1
    assert users[0].name == "BOB_SMITH"

    # 3. add_user('alice') next to bob_smith works
    add_user("alice", path=users_file)
    users = evi.users.load_users(users_file)
    assert len(users) == 2
    assert {u.name for u in users} == {"BOB_SMITH", "alice"}


def test_doctor_web_collisions(tmp_path, monkeypatch):
    # Setup environment for doctor
    home = tmp_path / ".evi"
    home.mkdir()
    config_path = home / "config.toml"
    users_path = home / "users.json"

    # Config: multi_user = true, auth_token set
    config_content = '[web]\nmulti_user = true\nauth_token = "secret"\n'
    config_path.write_text(config_content)

    # Users: 'bob smith' and 'bob_smith' (collision)
    users_data = [
        {"name": "bob smith", "token": "t1"},
        {"name": "bob_smith", "token": "t2"},
    ]
    users_path.write_text(json.dumps(users_data))

    # Monkeypatch config and paths
    monkeypatch.setattr(evi.config, "HOME", home)
    monkeypatch.setattr(evi.config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(evi.users, "USERS_PATH", users_path)
    monkeypatch.delenv("EVI_WEB_AUTH_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)

    # Run the specific check
    checks = doctor._check_web_access()

    # Verify 'multi-user logins' warn exists
    collision_check = next((c for c in checks if c.name == "multi-user logins"), None)
    assert collision_check is not None
    assert collision_check.status == "warn"
    assert "bob smith" in collision_check.detail
    assert "bob_smith" in collision_check.detail
    assert any('remove "bob smith"' in fix for fix in collision_check.fix)
    assert any('remove "bob_smith"' in fix for fix in collision_check.fix)


def test_doctor_no_collisions(tmp_path, monkeypatch):
    home = tmp_path / ".evi"
    home.mkdir()
    config_path = home / "config.toml"
    users_path = home / "users.json"

    config_content = '[web]\nmulti_user = true\nauth_token = "secret"\n'
    config_path.write_text(config_content)

    users_data = [
        {"name": "alice", "token": "t1"},
        {"name": "bob", "token": "t2"},
    ]
    users_path.write_text(json.dumps(users_data))

    monkeypatch.setattr(evi.config, "HOME", home)
    monkeypatch.setattr(evi.config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(evi.users, "USERS_PATH", users_path)
    monkeypatch.delenv("EVI_WEB_AUTH_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)

    checks = doctor._check_web_access()
    collision_check = next((c for c in checks if c.name == "multi-user logins"), None)
    assert collision_check is None
