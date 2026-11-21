"""Give CircuitPython filesystem write access for persistent logging."""

import storage

storage.remount("/", readonly=False)
print("Filesystem mode: persistent logging")
