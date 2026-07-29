import sys

from ril_build_version import (
    PROTOCOL_VERSION as BUILD_PROTOCOL_VERSION,
    VERSION as BUILD_VERSION,
)
from ril_config import legacy_version_from_version, load_base_config


if getattr(sys, "frozen", False):
    VERSION = BUILD_VERSION
    PROTOCOL_VERSION = BUILD_PROTOCOL_VERSION
else:
    _RELEASE = load_base_config()["release"]
    VERSION = _RELEASE["version"]
    PROTOCOL_VERSION = _RELEASE["protocol_version"]
LEGACY_UPDATE_VERSION = legacy_version_from_version(VERSION)
