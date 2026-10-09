"""Plugin/skill remove: names are validated and deletion is confined to the
plugins/skills dir (1.0.53 hotfix — `evi plugin remove ..` used to rmtree the
whole eVi home, and `skills.remove('..')` every user skill).

Everything runs against a tmp eVi home; nothing touches the real ~/.evi.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from evi import plugins, skills

# Names that must be refused outright — never mapped onto some other installed
# plugin/skill, never resolved to the root, its parent, a drive, or a share.
BAD_NAMES = [
    "..", ".", "", "../x", "a/../..", "/abs", "C:\\", "\\\\server\\share",
    "..\\..", "x\x00", ".git", "evil.git/..",
    "x/..", "./x", "x/", "x.git", "../plugins", "../skills", "x\n", " x",
]

# Installed plugins/skills a lenient slugging of the bad names would land on
# (../x -> x, /abs -> abs, .git -> git, …) — they must all survive.
_VICTIMS = ["x", "abs", "share", "git", "a", "evil", "keep"]


def _make_plugin(pdir: Path, name: str) -> Path:
    d = pdir / name
    d.mkdir(parents=True)
    (d / "plugin.toml").write_text(f'name = "{name}"\n', encoding="utf-8")
    return d


def _make_skill(sdir: Path, name: str) -> Path:
    d = sdir / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    return d


def _layout(tmp_path: Path) -> Path:
    """A tmp eVi home with sentinels at every level a bad name could reach."""
    home = tmp_path / "home"
    for name in _VICTIMS:
        _make_plugin(home / "plugins", name)
        _make_skill(home / "skills", name)
    (home / "x").mkdir()
    (home / "x" / "keep.txt").write_text("sibling of plugins/", encoding="utf-8")
    (home / "config.toml").write_text("# eVi config\n", encoding="utf-8")
    (home / "plugins" / "sentinel.txt").write_text("root", encoding="utf-8")
    (home / "skills" / "sentinel.txt").write_text("root", encoding="utf-8")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "keep.txt").write_text("outside", encoding="utf-8")
    (tmp_path / "sentinel.txt").write_text("parent of home", encoding="utf-8")
    return home


def _snapshot(base: Path) -> set[str]:
    return {p.relative_to(base).as_posix() for p in base.rglob("*")}


# --- plugins -------------------------------------------------------------


@pytest.mark.parametrize("name", BAD_NAMES)
def test_plugin_remove_refuses_bad_name(tmp_path: Path, name: str) -> None:
    home = _layout(tmp_path)
    before = _snapshot(tmp_path)
    assert plugins.remove(name, root=home) is False
    assert _snapshot(tmp_path) == before  # nothing deleted anywhere


def test_plugin_remove_refuses_absolute_paths(tmp_path: Path) -> None:
    home = _layout(tmp_path)
    before = _snapshot(tmp_path)
    for target in (tmp_path / "outside", home, home / "plugins", home / "plugins" / "keep"):
        assert plugins.remove(str(target), root=home) is False
    assert _snapshot(tmp_path) == before


def test_plugin_remove_still_removes_a_real_plugin(tmp_path: Path) -> None:
    home = _layout(tmp_path)
    before = _snapshot(tmp_path)
    assert plugins.remove("keep", root=home) is True
    assert not (home / "plugins" / "keep").exists()
    after = _snapshot(tmp_path)
    assert before - after == {"home/plugins/keep", "home/plugins/keep/plugin.toml"}
    assert plugins.remove("keep", root=home) is False  # already gone
    assert plugins.remove("ghost", root=home) is False


@pytest.mark.parametrize(
    "name", ["..", ".", "", "a/../..", "C:\\", "\\\\server\\share", "..\\..",
             "x\x00", ".git", "evil.git/..", "x\n"],
)
def test_set_enabled_refuses_escaping_name(tmp_path: Path, name: str) -> None:
    home = _layout(tmp_path)
    # What `..` / `''` used to address: make them look like plugins.
    (home / "plugin.toml").write_text('name = "home"\n', encoding="utf-8")
    (home / "plugins" / "plugin.toml").write_text('name = "root"\n', encoding="utf-8")
    assert plugins.set_enabled(name, False, root=home) is False
    assert not (home / "plugins" / ".state.json").exists()


def test_set_enabled_still_works(tmp_path: Path) -> None:
    home = _layout(tmp_path)
    assert plugins.set_enabled("keep", False, root=home) is True
    assert plugins.is_enabled("keep", root=home) is False
    assert plugins.set_enabled("keep.git", True, root=home) is True  # slug as before
    assert plugins.is_enabled("keep", root=home) is True


def test_install_rejects_escaping_names(tmp_path: Path) -> None:
    home = _layout(tmp_path)
    src = _make_plugin(tmp_path / "src", "fine")
    before = _snapshot(home)
    for bad in ("..", ".git", "evil.git/..", "x\n", "a/../.."):
        with pytest.raises(plugins.PluginError):
            plugins.install(str(src), name=bad, root=home)
    assert _snapshot(home) == before


# --- skills --------------------------------------------------------------


@pytest.mark.parametrize("name", BAD_NAMES)
def test_skill_remove_refuses_bad_name(tmp_path: Path, name: str) -> None:
    home = _layout(tmp_path)
    before = _snapshot(tmp_path)
    with pytest.raises(skills.SkillError):
        skills.remove(name, root=home / "skills")
    assert _snapshot(tmp_path) == before


def test_skill_remove_refuses_absolute_paths(tmp_path: Path) -> None:
    home = _layout(tmp_path)
    before = _snapshot(tmp_path)
    for target in (tmp_path / "outside", home, home / "skills", home / "skills" / "keep"):
        with pytest.raises(skills.SkillError):
            skills.remove(str(target), root=home / "skills")
    assert _snapshot(tmp_path) == before


def test_skill_remove_still_removes_a_real_skill(tmp_path: Path) -> None:
    home = _layout(tmp_path)
    before = _snapshot(tmp_path)
    assert skills.remove("keep", root=home / "skills") is True
    assert before - _snapshot(tmp_path) == {"home/skills/keep", "home/skills/keep/SKILL.md"}
    assert skills.remove("keep", root=home / "skills") is False
    with pytest.raises(skills.SkillError):  # plugin skills are still refused
        skills.remove("keep:keep", root=home / "skills")


# --- symlinked / junctioned installs --------------------------------------


def _symlink(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # no symlink privilege on Windows
        pytest.skip(f"can't create a directory symlink here: {exc}")


def _junction(link: Path, target: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("junctions are Windows-only")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


_LINKERS = {"symlink": _symlink, "junction": _junction}


@pytest.mark.parametrize("kind", sorted(_LINKERS))
def test_plugin_remove_unlinks_linked_dir_keeps_target(tmp_path: Path, kind: str) -> None:
    home = _layout(tmp_path)
    target = _make_plugin(tmp_path / "dev", "linked")
    (target / "commands").mkdir()
    (target / "commands" / "hi.md").write_text("hi", encoding="utf-8")
    link = home / "plugins" / "linked"
    _LINKERS[kind](link, target)
    assert [p.name for p in plugins.list_plugins(root=home)].count("linked") == 1

    assert plugins.remove("linked", root=home) is True
    assert not os.path.lexists(link)
    assert (target / "plugin.toml").is_file()
    assert (target / "commands" / "hi.md").is_file()


@pytest.mark.parametrize("kind", sorted(_LINKERS))
def test_plugin_link_to_evi_home_only_loses_the_link(tmp_path: Path, kind: str) -> None:
    home = _layout(tmp_path)
    link = home / "plugins" / "loop"
    _LINKERS[kind](link, home)  # plugins/loop -> the eVi home itself
    skills_before = _snapshot(home / "skills")

    assert plugins.remove("loop", root=home) is True
    assert not os.path.lexists(link)
    assert (home / "config.toml").is_file()
    assert (home / "x" / "keep.txt").is_file()
    assert (home / "plugins" / "keep" / "plugin.toml").is_file()
    assert _snapshot(home / "skills") == skills_before
    assert (tmp_path / "sentinel.txt").is_file()


@pytest.mark.parametrize("kind", sorted(_LINKERS))
def test_skill_remove_unlinks_linked_dir_keeps_target(tmp_path: Path, kind: str) -> None:
    home = _layout(tmp_path)
    target = _make_skill(tmp_path / "dev", "linked")
    (target / "reference.md").write_text("ref", encoding="utf-8")
    link = home / "skills" / "linked"
    _LINKERS[kind](link, target)

    assert skills.remove("linked", root=home / "skills") is True
    assert not os.path.lexists(link)
    assert (target / "SKILL.md").is_file() and (target / "reference.md").is_file()


@pytest.mark.parametrize("kind", sorted(_LINKERS))
def test_install_over_linked_plugin_replaces_only_the_link(tmp_path: Path, kind: str) -> None:
    home = _layout(tmp_path)
    target = _make_plugin(tmp_path / "dev", "linked")
    (target / "notes.md").write_text("dev copy", encoding="utf-8")
    link = home / "plugins" / "linked"
    _LINKERS[kind](link, target)
    src = _make_plugin(tmp_path / "src", "linked")
    (src / "new.md").write_text("new", encoding="utf-8")

    # Used to be a raw OSError (rmtree refuses links), not a PluginError.
    assert plugins.install(str(src), root=home) == "linked"
    assert not plugins._is_link(link) and (link / "new.md").is_file()
    assert (target / "notes.md").is_file() and not (target / "new.md").exists()
    assert (home / "config.toml").is_file()


@pytest.mark.parametrize("kind", sorted(_LINKERS))
def test_import_skill_over_linked_skill_replaces_only_the_link(
    tmp_path: Path, kind: str
) -> None:
    home = _layout(tmp_path)
    target = _make_skill(tmp_path / "dev", "linked")
    (target / "reference.md").write_text("dev copy", encoding="utf-8")
    link = home / "skills" / "linked"
    _LINKERS[kind](link, target)
    src = _make_skill(tmp_path / "src", "linked")
    (src / "new.md").write_text("new", encoding="utf-8")

    with pytest.raises(skills.SkillError, match="skill exists"):
        skills.import_skill(str(src), root=home / "skills")
    assert skills.import_skill(str(src), overwrite=True, root=home / "skills") == "linked"
    assert not plugins._is_link(link) and (link / "new.md").is_file()
    assert (target / "reference.md").is_file() and not (target / "new.md").exists()


def _lock_unlink(monkeypatch: pytest.MonkeyPatch, link: Path) -> None:
    """Make unlinking ``link`` fail as a locked/in-use link would."""
    real_unlink = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == link:
            raise PermissionError(13, "in use", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)


@pytest.mark.parametrize("kind", sorted(_LINKERS))
def test_failed_unlink_is_a_plugin_or_skill_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # A raw OSError here was an uncaught CLI traceback / web 500.
    home = _layout(tmp_path)
    for sub, make in (("plugins", _make_plugin), ("skills", _make_skill)):
        _LINKERS[kind](home / sub / "linked", make(tmp_path / "dev" / sub, "linked"))
        _lock_unlink(monkeypatch, home / sub / "linked")
        if sub == "plugins":
            with pytest.raises(plugins.PluginError, match="couldn't remove linked"):
                plugins.remove("linked", root=home)
        else:
            with pytest.raises(skills.SkillError, match="couldn't remove linked"):
                skills.remove("linked", root=home / "skills")
        assert os.path.lexists(home / sub / "linked")


# --- hand-copied dirs -----------------------------------------------------


@pytest.mark.parametrize("dirname", ["my.plugin", "My Plugin"])
def test_hand_copied_plugin_with_non_slug_name_can_be_disabled_and_removed(
    tmp_path: Path, dirname: str
) -> None:
    # install() only writes slug names, but a dir copied in by hand is still
    # listed and loaded (hooks/MCP/bin) — it must stay disable-able/removable
    # by its exact name, and only by that name.
    home = _layout(tmp_path)
    _make_plugin(home / "plugins", dirname)
    assert dirname in [p.name for p in plugins.list_plugins(root=home)]

    assert plugins.set_enabled(dirname, False, root=home) is True
    assert dirname not in [p.name for p in plugins.plugin_dirs(root=home)]

    before = _snapshot(tmp_path)
    for near in (dirname.upper(), dirname + "/", "./" + dirname, dirname + " "):
        assert plugins.remove(near, root=home) is False
    assert _snapshot(tmp_path) == before
    assert plugins.remove(dirname, root=home) is True
    assert before - _snapshot(tmp_path) == {
        f"home/plugins/{dirname}", f"home/plugins/{dirname}/plugin.toml",
    }


def test_hand_made_skill_dir_removed_by_exact_name(tmp_path: Path) -> None:
    home = _layout(tmp_path)
    _make_skill(home / "skills", "My Skill")
    before = _snapshot(tmp_path)
    assert skills.remove("My Skill", root=home / "skills") is True
    assert before - _snapshot(tmp_path) == {"home/skills/My Skill", "home/skills/My Skill/SKILL.md"}


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 path normalization")
def test_exact_name_cannot_alias_another_entry_on_windows(tmp_path: Path) -> None:
    # A literal "x." entry (only creatable via \\?\) is listed as-is, but Win32
    # reads "plugins\x." as "plugins\x" — accepting it would delete plugin x.
    home = _layout(tmp_path)
    aliases = [
        "\\\\?\\" + str((home / sub).resolve()) + "\\x."
        for sub in ("plugins", "skills")
    ]
    for a in aliases:
        os.mkdir(a)
    try:
        assert "x." in os.listdir(home / "plugins")
        assert plugins.remove("x.", root=home) is False
        assert plugins.set_enabled("x.", False, root=home) is False
        with pytest.raises(skills.SkillError):
            skills.remove("x.", root=home / "skills")
        assert (home / "plugins" / "x" / "plugin.toml").is_file()
        assert (home / "skills" / "x" / "SKILL.md").is_file()
        assert not (home / "plugins" / ".state.json").exists()
    finally:
        for a in aliases:
            os.rmdir(a)


# --- entry points (CLI + web) ---------------------------------------------


@pytest.fixture
def evi_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import evi.config as config_mod

    home = _layout(tmp_path)
    monkeypatch.setattr(config_mod, "HOME", home)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr(skills, "SKILL_DIR", home / "skills")
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # CLI installs its own
    return home


@pytest.mark.parametrize("name", ["..", ".git", "a/../.."])
def test_cli_plugin_remove_refuses(evi_home: Path, tmp_path: Path, name: str) -> None:
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    before = _snapshot(tmp_path)
    r = CliRunner().invoke(cli_main.app, ["plugin", "remove", name])
    assert r.exit_code == 1 and "no such plugin" in r.stdout
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("name", ["..", ".git", "a/../.."])
def test_cli_skill_remove_refuses(evi_home: Path, tmp_path: Path, name: str) -> None:
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    before = _snapshot(tmp_path)
    r = CliRunner().invoke(cli_main.app, ["skill", "remove", name])
    assert r.exit_code == 1 and "invalid skill name" in r.stdout
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", sorted(_LINKERS))
@pytest.mark.parametrize("group", ["plugin", "skill"])
def test_cli_remove_reports_failed_unlink(
    evi_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, group: str, kind: str
) -> None:
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    make = _make_plugin if group == "plugin" else _make_skill
    link = evi_home / f"{group}s" / "linked"
    _LINKERS[kind](link, make(tmp_path / "dev", "linked"))
    _lock_unlink(monkeypatch, link)
    r = CliRunner().invoke(cli_main.app, [group, "remove", "linked"])
    assert r.exit_code == 1 and isinstance(r.exception, SystemExit)  # no traceback
    assert "couldn't remove linked" in r.stdout
    assert os.path.lexists(link)


@pytest.mark.parametrize("group", ["plugin", "skill"])
def test_cli_remove_name_with_markup_is_refused_cleanly(evi_home: Path, group: str) -> None:
    # A refused name is echoed back; it must not be parsed as Rich markup.
    from typer.testing import CliRunner

    import evi.apps.cli.main as cli_main

    r = CliRunner().invoke(cli_main.app, [group, "remove", "[/x]"])
    assert r.exit_code == 1 and isinstance(r.exception, SystemExit)
    assert "[/x]" in r.stdout


def test_web_plugin_remove_refuses(
    evi_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    import evi.apps.web.server as server_mod

    monkeypatch.setattr("evi.marketplace.MARKETPLACE_PATH", tmp_path / "marketplace.json")
    monkeypatch.setattr(server_mod, "make_client", lambda *_: None)
    (tmp_path / "images").mkdir()
    monkeypatch.setattr(server_mod, "IMAGE_DIR", tmp_path / "images")
    client = TestClient(server_mod.create_app())

    before = _snapshot(tmp_path)
    for name in ("..", ".git", "evil.git/..", "C:\\", "../x"):
        r = client.post("/api/plugins/remove", json={"name": name})
        assert r.status_code == 404, (name, r.status_code)
    assert _snapshot(tmp_path) == before
    assert client.post("/api/plugins/remove", json={"name": "keep"}).status_code == 200
    assert not (evi_home / "plugins" / "keep").exists()
