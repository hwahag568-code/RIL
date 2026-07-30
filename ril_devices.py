from dataclasses import dataclass

from ril_config import load_config


_CONFIG = load_config()
_PROTOCOL = _CONFIG["protocol"]
_DEVICES = _CONFIG["devices"]

GENERAL_COMMAND = _PROTOCOL["general_command"]
OSMO_COMMAND = _PROTOCOL["osmo_command"]
NOVA_COMMAND = _PROTOCOL["nova_command"]
CURRENT_AU_COMMANDS = dict(_PROTOCOL["current_au_commands"])
LEGACY_AU_COMMANDS = dict(_PROTOCOL["legacy_au_commands"])
AU_COMMAND_NUMBERS = {
    **CURRENT_AU_COMMANDS,
    **LEGACY_AU_COMMANDS,
}
LEGACY_CLIENT_COMMANDS = dict(_PROTOCOL["legacy_client_commands"])
SUPPORTED_LOGIN_COMMANDS = frozenset(
    {
        GENERAL_COMMAND,
        OSMO_COMMAND,
        NOVA_COMMAND,
        *CURRENT_AU_COMMANDS,
    }
)


@dataclass(frozen=True)
class Device:
    device_id: str
    display_name: str
    ip: str
    command: str
    groups: frozenset


def _device(device_id, definition):
    return Device(
        device_id,
        definition["display_name"],
        definition["ip"],
        definition["command"],
        frozenset(definition["groups"]),
    )


DEVICE_DEFINITIONS = tuple(
    _device(device_id, _DEVICES["definitions"][device_id])
    for device_id in _DEVICES["order"]
)

DEVICE_IDS = tuple(device.device_id for device in DEVICE_DEFINITIONS)
DEVICE_BY_ID = {
    device.device_id: device
    for device in DEVICE_DEFINITIONS
}
DEVICE_IPS = {
    device.device_id: device.ip
    for device in DEVICE_DEFINITIONS
}
DEVICE_DISPLAY_NAMES = {
    device.device_id: device.display_name
    for device in DEVICE_DEFINITIONS
}
DEVICE_COMMANDS = {
    device.device_id: device.command
    for device in DEVICE_DEFINITIONS
}


def devices_in_group(group):
    return tuple(
        device.device_id
        for device in DEVICE_DEFINITIONS
        if group in device.groups
    )


def validate_device_catalog():
    if len(DEVICE_BY_ID) != len(DEVICE_DEFINITIONS):
        raise RuntimeError("장비 ID가 중복되었습니다.")
    if len(set(DEVICE_IPS.values())) != len(DEVICE_DEFINITIONS):
        raise RuntimeError("장비 IP가 중복되었습니다.")

    unsupported = sorted(
        {
            device.command
            for device in DEVICE_DEFINITIONS
            if device.command not in SUPPORTED_LOGIN_COMMANDS
        }
    )
    if unsupported:
        raise RuntimeError(
            "서버가 지원하지 않는 장비 명령이 있습니다: "
            + ", ".join(unsupported)
        )


validate_device_catalog()
