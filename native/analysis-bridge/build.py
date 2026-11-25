"""Build a local VirtualDJ analysis bundle with user-supplied SDK headers."""
import argparse
from pathlib import Path
import plistlib
import subprocess

from afterhours.paths import analysis_reports

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--sdk", required=True, type=Path, help="Directory containing the official VirtualDJ SDK headers")
args = parser.parse_args()
sdk = args.sdk.expanduser().resolve()
if not all((sdk / name).is_file() for name in ("VdjPlugin8.h", "vdjDsp8.h")):
    parser.error("The SDK directory must contain VdjPlugin8.h and vdjDsp8.h.")

reports = analysis_reports()
report_value = str(reports).replace("\\", "\\\\").replace('"', '\\"')
bundle = root / "build/Afterhours Analysis.bundle"
exe = bundle / "Contents/MacOS/AfterhoursAnalysis"
exe.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(["clang++", "-std=c++17", "-O2", "-arch", "arm64", "-bundle", "-framework", "CoreFoundation",
                "-Wno-deprecated-declarations", "-I", str(sdk), f'-DAFTERHOURS_REPORT_DIR="{report_value}"',
                str(root / "AfterhoursAnalysis.cpp"), "-o", str(exe)], check=True)
(bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
    "CFBundleExecutable": "AfterhoursAnalysis", "CFBundleIdentifier": "local.afterhours.analysis",
    "CFBundleName": "Afterhours Analysis", "CFBundlePackageType": "BNDL",
    "CFBundleVersion": "6", "CFBundleShortVersionString": "0.3.0",
}))
subprocess.run(["codesign", "--force", "--sign", "-", str(bundle)], check=True)
print("Built and ad-hoc signed locally:", bundle)
