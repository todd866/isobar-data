"""The install script copies a plist and does not start the agent."""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_install_script_writes_a_plist_and_does_not_load_it(tmp_path):
    install_dir = tmp_path / "agents"
    log_dir = tmp_path / "logs"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "if [ \"$1\" = \"sync\" ]; then\n"
        "  mkdir -p .venv/bin\n"
        "  printf '%s\\n' '#!/bin/sh' 'exit 0' > .venv/bin/python\n"
        "  printf '%s\\n' '#!/bin/sh' 'exit 0' > .venv/bin/isobar-data\n"
        "  chmod +x .venv/bin/python .venv/bin/isobar-data\n"
        "fi\n"
        "exit 0\n"
    )
    stub.chmod(0o755)
    env = os.environ.copy()
    env["INSTALL_DIR"] = str(install_dir)
    env["LOG_DIR"] = str(log_dir)
    env["HOME"] = str(tmp_path)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    completed = subprocess.run(
        ["sh", str(ROOT / "scripts" / "install-launchd.sh")],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "not loaded" in completed.stdout
    script = (ROOT / "scripts" / "install-launchd.sh").read_text()
    code = "\n".join(line for line in script.splitlines() if not line.strip().startswith("#"))
    assert "launchctl" not in code
    plist = install_dir / "com.isobar.data.plist"
    text = plist.read_text()
    assert "<key>Label</key>" in text
    assert "<string>com.isobar.data</string>" in text
    assert "<key>StartInterval</key>" in text
    assert "<integer>300</integer>" in text
    assert "<key>RunAtLoad</key>" in text
    assert "<false/>" in text
    assert "isobar-data" in text
    assert str(ROOT) not in text
    assert ".cursor/worktrees" not in text
    assert "Application Support/isobar-data" in text
    app = tmp_path / "Library" / "Application Support" / "isobar-data"
    assert (app / "app" / "isobar_data").is_dir()
    assert (app / "bin" / "isobar-data-launchd").is_file()
    wrapper = (app / "bin" / "isobar-data-launchd").read_text()
    assert "rotate_log" in wrapper
    assert "launchctl" not in wrapper
    # Parse and validate the generated plist on Linux CI as well as macOS.
    config = plistlib.loads(plist.read_bytes())
    assert config["Label"] == "com.isobar.data"
    assert config["StartInterval"] == 300
    assert config["RunAtLoad"] is False
    assert config["ProgramArguments"] == [str(app / "bin" / "isobar-data-launchd")]
    if sys.platform == "darwin":
        lint = subprocess.run(["plutil", "-lint", str(plist)], check=True, capture_output=True, text=True)
        assert "OK" in lint.stdout

    again = subprocess.run(
        ["sh", str(ROOT / "scripts" / "install-launchd.sh")],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "not loaded" in again.stdout
    uninstall = ROOT / "scripts" / "uninstall-launchd.sh"
    assert uninstall.is_file()
    removed = subprocess.run(
        ["sh", str(uninstall)],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "not loaded" in removed.stdout
    assert "launchctl" not in uninstall.read_text()
    assert not app.exists()
    assert not plist.exists()
