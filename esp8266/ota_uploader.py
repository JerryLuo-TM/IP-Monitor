"""Use PlatformIO Python and the firmware's current OTA password for uploads."""

from pathlib import Path
import re

Import("env")

platformio_python = Path.home() / ".platformio" / "penv" / "Scripts" / "python.exe"
if platformio_python.is_file():
    env.Replace(PYTHONEXE=str(platformio_python))

config = Path(env.subst("$PROJECT_DIR")) / "include" / "wan_config.h"
match = re.search(r'^#define OTA_PASSWORD "([^"]+)"', config.read_text(encoding="utf-8"), re.M)
if not match:
    raise RuntimeError("OTA_PASSWORD is missing from include/wan_config.h")
env.Append(UPLOAD_FLAGS=["--auth=" + match.group(1)])
