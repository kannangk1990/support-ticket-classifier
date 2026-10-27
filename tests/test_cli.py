import pytest

from ticket_classifier.cli import main


def test_reprocess_rejects_a_zero_limit(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["reprocess", "--limit", "0"]) == 2
    assert "--limit must be at least 1" in capsys.readouterr().err
