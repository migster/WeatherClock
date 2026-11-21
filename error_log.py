"""Bounded persistent diagnostics for CircuitPython."""

import os
import time

LOG_PATH = "/weatherclock.log"
BACKUP_PATH = "/weatherclock.log.1"
MAX_LOG_BYTES = 32 * 1024
MAX_MESSAGE_BYTES = 512
_write_failed = False


def _file_size(path):
    try:
        return os.stat(path)[6]
    except OSError as error:
        if error.args[0] != 2:
            raise
        return 0


def log_event(message):
    """Print an event and append it to flash; stop writes after a storage error."""
    global _write_failed
    print(message)
    if _write_failed:
        return
    try:
        now = time.localtime()
        stamp = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(*now[:6])
        text = str(message).replace("\r", " ").replace("\n", " ")
        text = text.encode("utf-8")[:MAX_MESSAGE_BYTES].decode("utf-8", "ignore").encode("utf-8")
        entry = ("{} uptime={:.0f}s ".format(stamp, time.monotonic())).encode("utf-8")
        entry += text + b"\n"
        if _file_size(LOG_PATH) + len(entry) > MAX_LOG_BYTES:
            try:
                os.remove(BACKUP_PATH)
            except OSError as error:
                if error.args[0] != 2:
                    raise
            os.rename(LOG_PATH, BACKUP_PATH)
        with open(LOG_PATH, "ab") as log:
            log.write(entry)
    except OSError as error:
        _write_failed = True
        print("Persistent logging disabled until restart: {}. "
              "Check boot.py, filesystem ownership and free space.".format(error))
