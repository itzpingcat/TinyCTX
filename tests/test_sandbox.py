from sandbox.__main__ import _format_output


def test_sandbox_output_marks_truncation_and_exit_status():
    output = _format_output(b"out\n", b"err\n", 3, True, False)
    assert "out" in output
    assert "[stderr]" in output
    assert "output truncated" in output
    assert "[exit 3]" in output
