"""Exception hierarchy shared by every DevReader layer.

All errors carry an optional ``command``/``output``/``serial`` so the UI and log
can show exactly what went wrong without parsing message strings.
"""

from __future__ import annotations


class DevReaderError(Exception):
    def __init__(self, message: str, *, command: str = "", output: str = "", serial: str | None = None):
        super().__init__(message)
        self.message = message
        self.command = command
        self.output = output
        self.serial = serial

    def __str__(self) -> str:
        return self.message


# -- ADB / transport ------------------------------------------------------
class AdbError(DevReaderError):
    """Base class for adb related failures."""


class AdbNotFoundError(AdbError):
    """adb executable could not be located."""


class AdbCommandError(AdbError):
    """adb returned a non-zero exit code or reported an error."""


class TimeoutError_(AdbError):
    """An adb command (typically wait-for-device) exceeded its timeout."""


class CancelledError(AdbError):
    """The user cancelled the running operation."""


class DeviceNotFoundError(AdbError):
    """The device is not connected."""


class DeviceOfflineError(AdbError):
    """The device is listed as offline."""


class DeviceUnauthorizedError(AdbError):
    """The device has not authorized this host for USB debugging."""


# -- Device discovery -----------------------------------------------------
class DiscoveryError(DevReaderError):
    """Problem while discovering devices (e.g. no environment variables)."""


# -- Preparation ----------------------------------------------------------
class PreparationError(DevReaderError):
    """A preparation step (root/remount/...) failed."""


class RootError(PreparationError):
    pass


class RemountError(PreparationError):
    pass


# -- Configuration --------------------------------------------------------
class ConfigError(DevReaderError):
    """Base class for configuration read/write failures."""


class ConfigReadError(ConfigError):
    pass


class ConfigParseError(ConfigError):
    pass


class ConfigValidationError(ConfigError):
    pass


class ConfigWriteError(ConfigError):
    pass


class ConfigPushError(ConfigWriteError):
    pass


class ConfigVerificationError(ConfigWriteError):
    pass
