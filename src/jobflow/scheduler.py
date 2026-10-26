"""Daily scheduling helpers for macOS launchd and catch-up runs."""

from datetime import datetime
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape


RUN_HOURS = (8,)


def _run_slot(moment: datetime) -> Optional[int]:
    """Return the most recent scheduled hour, or none before the first run."""
    due = [hour for hour in RUN_HOURS if moment.hour >= hour]
    return max(due) if due else None


def should_run(now: datetime, latest_started_at: Optional[str]) -> bool:
    slot = _run_slot(now)
    if slot is None:
        return False
    if not latest_started_at:
        return True
    try:
        latest = datetime.fromisoformat(latest_started_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    latest_local = latest.astimezone(now.tzinfo)
    return latest_local.date() != now.date() or _run_slot(latest_local) != slot


def launchd_plist(project_dir: Path, hours=RUN_HOURS, minute: int = 0) -> str:
    """Generate a daily launch agent; launchd coalesces missed runs after wake."""
    project_dir = Path(project_dir).resolve()
    executable = project_dir / ".venv" / "bin" / "jobflow"
    db = project_dir / "data" / "jobflow.db"
    calendars = "\n".join(
        """      <dict><key>Weekday</key><integer>{}</integer><key>Hour</key><integer>{}</integer><key>Minute</key><integer>{}</integer></dict>""".format(
            weekday, hour, minute
        )
        for weekday in range(7) for hour in hours
    )
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.jobflow.discovery</string>
  <key>ProgramArguments</key><array>
    <string>{executable}</string><string>--db</string><string>{db}</string><string>daily-run</string>
  </array>
  <key>WorkingDirectory</key><string>{project}</string>
  <key>StartCalendarInterval</key><array>
{calendars}
  </array>
  <key>StandardOutPath</key><string>{project}/data/scheduler.log</string>
  <key>StandardErrorPath</key><string>{project}/data/scheduler-error.log</string>
</dict></plist>
""".format(
        executable=escape(str(executable)), db=escape(str(db)), project=escape(str(project_dir)), calendars=calendars
    )
