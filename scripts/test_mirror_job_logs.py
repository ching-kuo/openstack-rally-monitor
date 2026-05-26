"""Regression tests for cron log mirroring."""
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "mirror_job_logs.sh"


def run_mirror(tmp_path, argv):
    """Run mirror_job_logs.sh with arbitrary argv."""
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        check=False,
    )
    return result


def run_wrapper(tmp_path, command):
    """Run the wrapper against a small bash command."""
    log_file = tmp_path / "job.log"
    result = run_mirror(
        tmp_path,
        [str(log_file), "/bin/bash", "-lc", command],
    )
    return result, log_file


def test_wrapper_appends_combined_output_to_log_file(tmp_path):
    result, log_file = run_wrapper(
        tmp_path,
        'printf "stdout-line\\n"; printf "stderr-line\\n" >&2',
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert log_file.read_text() == "stdout-line\nstderr-line\n"


def test_wrapper_preserves_wrapped_command_exit_code(tmp_path):
    result, log_file = run_wrapper(
        tmp_path,
        'printf "before-fail\\n"; exit 7',
    )

    assert result.returncode == 7
    assert log_file.read_text() == "before-fail\n"


def test_wrapper_exits_64_when_called_with_no_args(tmp_path):
    result = run_mirror(tmp_path, [])

    assert result.returncode == 64
    assert "Usage:" in result.stderr
