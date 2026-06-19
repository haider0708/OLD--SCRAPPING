import io
import sys

import scrape


def test_print_header_handles_non_utf_console(monkeypatch):
    """
    Regression: Windows cp1252 consoles can crash on emoji output.
    """
    buffer = io.BytesIO()
    cp1252_stdout = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", cp1252_stdout)

    # Should not raise UnicodeEncodeError
    scrape.print_header("🧪 TESTING")
    cp1252_stdout.flush()

    output = buffer.getvalue().decode("cp1252", errors="strict")
    assert "TESTING" in output
