"""Persistent scheduling for the autonomous improvement loop.

Creates and manages a macOS launchd plist that runs `orchestrator improve`
on a configurable interval. Falls back to documenting cron usage on Linux.

The scheduler also supports Claude Code's /loop skill as an alternative
for session-scoped recurring runs.
"""
from pathlib import Path
from orchestrator.paths import CANOPY_DIR

PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.canopy.orchestrator.improve</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>-m</string>
        <string>orchestrator.cli</string>
        <string>improve</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{project_dir}</string>
    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>
    <key>StandardOutPath</key>
    <string>{log_dir}/orchestrator-improve.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/orchestrator-improve.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:{extra_path}</string>
    </dict>
</dict>
</plist>
"""

PLIST_NAME = "com.canopy.orchestrator.improve.plist"


def generate_plist(
    project_dir: Path,
    python_path: str = "python3",
    interval_hours: int = 8,
    log_dir: Path | None = None,
    extra_path: str = "",
) -> str:
    """Generate launchd plist content for scheduled improvement runs."""
    if log_dir is None:
        log_dir = CANOPY_DIR / "logs"

    return PLIST_TEMPLATE.format(
        python_path=python_path,
        project_dir=str(project_dir),
        interval_seconds=interval_hours * 3600,
        log_dir=str(log_dir),
        extra_path=extra_path,
    )


def install_schedule(
    project_dir: Path,
    python_path: str = "python3",
    interval_hours: int = 8,
) -> Path:
    """Install the launchd plist and load it."""
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    plist_path = plist_dir / PLIST_NAME

    log_dir = CANOPY_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    content = generate_plist(
        project_dir=project_dir,
        python_path=python_path,
        interval_hours=interval_hours,
        log_dir=log_dir,
    )
    plist_path.write_text(content, encoding="utf-8")
    return plist_path


def uninstall_schedule() -> bool:
    """Remove the launchd plist."""
    plist_path = Path.home() / "Library" / "LaunchAgents" / PLIST_NAME
    if plist_path.exists():
        plist_path.unlink()
        return True
    return False


def is_scheduled() -> bool:
    """Check if the launchd plist exists."""
    return (Path.home() / "Library" / "LaunchAgents" / PLIST_NAME).exists()
