"""Regression tests for cron log mirroring."""
import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "mirror_job_logs.sh"


def run_mirror(tmp_path, argv):
    """Run mirror_job_logs.sh with arbitrary argv and a redirected docker stream."""
    docker_stream = tmp_path / "docker.log"
    env = os.environ.copy()
    env["DOCKER_LOG_OUTPUT"] = str(docker_stream)
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result, docker_stream


def run_wrapper(tmp_path, command):
    """Run the wrapper against a small bash command."""
    log_file = tmp_path / "job.log"
    result, docker_stream = run_mirror(
        tmp_path,
        [str(log_file), "/bin/bash", "-lc", command],
    )
    return result, log_file, docker_stream


def test_wrapper_mirrors_combined_output_to_file_and_docker_stream(tmp_path):
    result, log_file, docker_stream = run_wrapper(
        tmp_path,
        'printf "stdout-line\\n"; printf "stderr-line\\n" >&2',
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert log_file.read_text() == "stdout-line\nstderr-line\n"
    assert docker_stream.read_text() == "stdout-line\nstderr-line\n"


def test_wrapper_preserves_wrapped_command_exit_code(tmp_path):
    result, log_file, docker_stream = run_wrapper(
        tmp_path,
        'printf "before-fail\\n"; exit 7',
    )

    assert result.returncode == 7
    assert log_file.read_text() == "before-fail\n"
    assert docker_stream.read_text() == "before-fail\n"


def test_wrapper_exits_nonzero_when_docker_stream_unwritable(tmp_path):
    log_file = tmp_path / "job.log"
    docker_stream = tmp_path / "docker.log"
    docker_stream.write_text("")
    docker_stream.chmod(0o000)
    result, _ = run_mirror(
        tmp_path,
        [str(log_file), "/bin/bash", "-c", "echo ok"],
    )

    assert result.returncode != 0


def test_wrapper_exits_64_when_called_with_no_args(tmp_path):
    result, _ = run_mirror(tmp_path, [])

    assert result.returncode == 64
    assert "Usage:" in result.stderr
