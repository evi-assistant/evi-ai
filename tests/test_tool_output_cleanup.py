import os
import time
from unittest.mock import patch

from evi import tool_output
from evi.apps.cli import main as cli_main
from evi.config import Config


def test_tool_output_cleanup(monkeypatch, tmp_path):
    # Setup
    monkeypatch.setattr(cli_main, "_CLEANUP_DONE", False)
    monkeypatch.setattr("evi.config.HOME", tmp_path)

    # Create a spill folder that is 30 days old
    # tool_output.spill creates a file in a session folder
    old_spill_file = tool_output.spill(tmp_path, "old_session", "old_call", "text")
    old_spill = old_spill_file.parent
    thirty_days_ago = time.time() - (30 * 24 * 60 * 60)
    os.utime(old_spill, (thirty_days_ago, thirty_days_ago))
    for f in old_spill.iterdir():
        os.utime(f, (thirty_days_ago, thirty_days_ago))

    # Create a spill folder that is new
    new_spill_file = tool_output.spill(tmp_path, "new_session", "new_call", "text")
    new_spill = new_spill_file.parent

    # Mock TranscriptStore to verify it's not called when days=0
    with patch("evi.apps.cli.main.TranscriptStore") as mock_store_cls:
        mock_store = mock_store_cls.return_value

        # Case 1: cleanup_period_days = 0
        # Spill files should use DEFAULT_KEEP_DAYS (14), so 30-day old should be gone.
        cfg = Config()
        cfg.tools.cleanup_period_days = 0

        cli_main._maybe_cleanup_transcripts(cfg)

        assert not os.path.exists(old_spill), "30-day old spill should be deleted"
        assert os.path.exists(new_spill), "New spill should be kept"
        mock_store.prune.assert_not_called()

        # Reset for next case
        monkeypatch.setattr(cli_main, "_CLEANUP_DONE", False)
        tool_output.spill(tmp_path, "new_session_2", "new_call_2", "text")
        old_spill_2_file = tool_output.spill(tmp_path, "old_session_2", "old_call_2", "text")
        old_spill_2 = old_spill_2_file.parent
        thirty_days_ago = time.time() - (30 * 24 * 60 * 60)
        os.utime(old_spill_2, (thirty_days_ago, thirty_days_ago))
        for f in old_spill_2.iterdir():
            os.utime(f, (thirty_days_ago, thirty_days_ago))

        # Case 2a: cleanup_period_days = 60
        # 30-day old spill should be kept.
        cfg.tools.cleanup_period_days = 60
        cli_main._maybe_cleanup_transcripts(cfg)
        assert os.path.exists(old_spill_2), "30-day old spill should be kept when cleanup_period_days=60"

        # Reset for next case
        monkeypatch.setattr(cli_main, "_CLEANUP_DONE", False)
        new_spill_3_file = tool_output.spill(tmp_path, "new_session_3", "new_call_3", "text")
        new_spill_3 = new_spill_3_file.parent
        ten_day_old_file = tool_output.spill(tmp_path, "ten_day_old_session", "ten_day_old_call", "text")
        ten_day_old = ten_day_old_file.parent
        ten_days_ago = time.time() - (10 * 24 * 60 * 60)
        os.utime(ten_day_old, (ten_days_ago, ten_days_ago))
        for f in ten_day_old.iterdir():
            os.utime(f, (ten_days_ago, ten_days_ago))

        # Case 2b: cleanup_period_days = 7
        # 10-day old spill should be deleted when cleanup_period_days=7
        cfg.tools.cleanup_period_days = 7
        cli_main._maybe_cleanup_transcripts(cfg)
        assert not os.path.exists(ten_day_old), "10-day old spill should be deleted when cleanup_period_days=7"
        assert os.path.exists(new_spill_3), "New spill should be kept"
