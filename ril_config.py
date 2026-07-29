from copy import deepcopy
import ipaddress
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys


CONFIG_FILENAME = "ril_config.json"
LOCAL_CONFIG_FILENAME = "ril_config.local.json"
CONFIG_ENVIRONMENT_VARIABLE = "RIL_CONFIG_PATH"


class ConfigError(RuntimeError):
    pass


def application_directory():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundled_directory():
    bundle_path = getattr(sys, "_MEIPASS", None)
    if bundle_path:
        return Path(bundle_path).resolve()
    return Path(__file__).resolve().parent


def resource_path(filename):
    external = application_directory() / filename
    if external.exists():
        return external
    bundled = bundled_directory() / filename
    if bundled.exists():
        return bundled
    return external


def find_base_config_path():
    configured_path = os.environ.get(CONFIG_ENVIRONMENT_VARIABLE)
    candidates = []
    if configured_path:
        candidates.append(Path(configured_path).expanduser())
    candidates.extend(
        (
            application_directory() / CONFIG_FILENAME,
            bundled_directory() / CONFIG_FILENAME,
            Path(__file__).resolve().parent / CONFIG_FILENAME,
        )
    )

    checked = []
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in checked:
            continue
        checked.append(candidate)
        if candidate.is_file():
            return candidate

    raise ConfigError(
        f"{CONFIG_FILENAME}을 찾지 못했습니다: "
        + ", ".join(str(path) for path in checked)
    )


def find_local_config_path(base_path=None):
    base_path = Path(base_path or find_base_config_path()).resolve()
    beside_base = base_path.with_name(LOCAL_CONFIG_FILENAME)
    if beside_base.is_file():
        return beside_base.resolve()

    external_path = application_directory() / LOCAL_CONFIG_FILENAME
    if external_path.is_file():
        return external_path.resolve()
    return None


def _read_json(path):
    try:
        with Path(path).open("r", encoding="utf-8-sig") as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"설정 파일을 읽지 못했습니다: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ConfigError(f"설정 파일의 최상위 값은 객체여야 합니다: {path}")
    return value


def _merge_known(base, override, path=()):
    if not isinstance(base, dict) or not isinstance(override, dict):
        return deepcopy(override)

    unknown = sorted(set(override) - set(base))
    if unknown:
        location = ".".join(path) or "<root>"
        raise ConfigError(
            f"알 수 없는 로컬 설정 키가 있습니다: "
            f"{location}: {', '.join(unknown)}"
        )

    merged = deepcopy(base)
    for key, value in override.items():
        merged[key] = _merge_known(
            base[key],
            value,
            (*path, key),
        )
    return merged


def _require_mapping(config, key):
    value = config.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"필수 설정 객체가 없습니다: {key}")
    return value


def _positive_number(value, path, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{path} 값은 숫자여야 합니다.")
    if value < 0 or (not allow_zero and value == 0):
        raise ConfigError(f"{path} 값은 양수여야 합니다.")


def _positive_integer(value, path):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{path} 값은 양의 정수여야 합니다.")
    return value


def _non_empty_string(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{path} 값은 빈 문자열이 아니어야 합니다.")
    return value


def _safe_leaf_name(value, path):
    value = _non_empty_string(value, path)
    leaf = PureWindowsPath(value)
    if (
        leaf.name != value
        or value in (".", "..")
        or re.search(r'[\\/:*?"<>|\x00-\x1f]', value)
        or value.endswith((" ", "."))
    ):
        raise ConfigError(f"{path} 값은 안전한 단일 파일명이어야 합니다.")
    return value


def _safe_bootstrap_name(value, path):
    value = _non_empty_string(value, path)
    if (
        value != value.strip()
        or '"' in value
        or re.search(r"[\x00-\x1f]", value)
    ):
        raise ConfigError(
            f"{path} 값에는 제어 문자, 큰따옴표 또는 "
            "앞뒤 공백을 사용할 수 없습니다."
        )
    return value


def _string_list(value, path, allow_empty=False):
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ConfigError(f"{path} 값은 문자열 배열이어야 합니다.")
    return value


def _boolean(value, path):
    if not isinstance(value, bool):
        raise ConfigError(f"{path} 값은 true 또는 false여야 합니다.")
    return value


def legacy_version_from_version(version):
    match = re.fullmatch(
        r"(\d{6})(?:\.([1-9]\d*))?",
        str(version),
    )
    if not match:
        raise ConfigError(
            "release.version은 YYMMDD 또는 YYMMDD.hotfix 형식이어야 합니다."
        )
    sequence = int(match.group(2) or 0) + 1
    if sequence > 99:
        raise ConfigError("하루 릴리스 순번은 99개를 초과할 수 없습니다.")
    return f"{match.group(1)}{sequence:02d}"


def validate_config(config):
    if config.get("schema_version") != 1:
        raise ConfigError("지원하지 않는 설정 schema_version입니다.")

    for section in (
        "build",
        "release",
        "artifacts",
        "installation",
        "logging",
        "update",
        "network",
        "protocol",
        "client",
        "server",
        "interfaces",
        "devices",
    ):
        _require_mapping(config, section)

    build = config["build"]
    _non_empty_string(build.get("platform"), "build.platform")
    _non_empty_string(build.get("machine"), "build.machine")
    python_major_minor = _non_empty_string(
        build.get("python_major_minor"),
        "build.python_major_minor",
    )
    if not re.fullmatch(r"\d+\.\d+", python_major_minor):
        raise ConfigError(
            "build.python_major_minor는 major.minor 형식이어야 합니다."
        )
    architecture_bits = _positive_integer(
        build.get("architecture_bits"),
        "build.architecture_bits",
    )
    if architecture_bits not in (32, 64):
        raise ConfigError("build.architecture_bits는 32 또는 64여야 합니다.")

    release = config["release"]
    version = release.get("version")
    if not isinstance(version, str) or not re.fullmatch(
        r"\d{6}(?:\.[1-9]\d*)?",
        version,
    ):
        raise ConfigError(
            "release.version은 YYMMDD 또는 YYMMDD.hotfix 형식이어야 합니다."
        )
    _positive_integer(
        release.get("protocol_version"),
        "release.protocol_version",
    )
    _non_empty_string(release.get("repository"), "release.repository")
    _non_empty_string(release.get("tag_prefix"), "release.tag_prefix")
    _non_empty_string(release.get("branch"), "release.branch")
    _non_empty_string(
        release.get("publish_mutex_name"),
        "release.publish_mutex_name",
    )
    _positive_integer(
        release.get("stage_attempts"),
        "release.stage_attempts",
    )
    _positive_number(
        release.get("stage_retry_delay_seconds"),
        "release.stage_retry_delay_seconds",
    )
    _positive_integer(
        release.get("activation_attempts"),
        "release.activation_attempts",
    )
    _positive_number(
        release.get("activation_retry_delay_seconds"),
        "release.activation_retry_delay_seconds",
    )
    legacy_version_from_version(version)

    artifacts = config["artifacts"]
    for key in (
        "client_installer_filename",
        "server_installer_filename_template",
        "manifest_filename",
        "legacy_version_filename",
        "legacy_client_release_tag",
    ):
        _non_empty_string(artifacts.get(key), f"artifacts.{key}")
    if "{version}" not in artifacts["server_installer_filename_template"]:
        raise ConfigError(
            "artifacts.server_installer_filename_template에 "
            "{version}이 필요합니다."
        )

    installation = config["installation"]
    for key, value in installation.items():
        if key == "server_restarter_interval_hours":
            _positive_integer(value, f"installation.{key}")
        else:
            _non_empty_string(value, f"installation.{key}")
    for key in (
        "client_executable",
        "server_executable",
        "client_ui_file",
        "icon_file",
        "server_start_script",
        "server_start_power_shell_script",
        "server_restarter_script",
        "server_restarter_power_shell_script",
        "server_update_helper_script",
        "server_update_helper_ready_filename",
        "server_effective_config_filename",
        "client_startup_ready_filename",
        "client_startup_check_script",
        "server_health_filename",
        "shortcut_directory",
        "server_start_menu_shortcut",
        "server_desktop_shortcut",
    ):
        _safe_leaf_name(
            installation[key],
            f"installation.{key}",
        )
    for key in (
        "server_task_name",
        "server_restarter_task_name",
        "client_update_recovery_task_name",
        "client_install_registry_value",
        "server_install_registry_value",
        "legacy_install_registry_value",
        "server_version_registry_value",
        "registry_key",
    ):
        _safe_bootstrap_name(
            installation[key],
            f"installation.{key}",
        )
    task_names = [
        installation[key]
        for key in (
            "server_task_name",
            "server_restarter_task_name",
            "client_update_recovery_task_name",
        )
    ]
    if len({value.casefold() for value in task_names}) != len(task_names):
        raise ConfigError(
            "installation 예약 작업 이름은 Windows에서 "
            "대소문자를 구분하지 않고 서로 달라야 합니다."
        )
    runtime_directories = [
        installation[key]
        for key in (
            "client_runtime_directory",
            "server_runtime_directory",
            "legacy_runtime_directory",
        )
    ]
    if any(
        value in (".", "..")
        or re.search(r'[\\/:*?"<>|]', value)
        for value in runtime_directories
    ):
        raise ConfigError(
            "installation runtime directory는 단일 폴더명이어야 합니다."
        )
    if (
        len({value.casefold() for value in runtime_directories})
        != len(runtime_directories)
    ):
        raise ConfigError(
            "client/server/legacy runtime directory는 Windows에서 "
            "대소문자를 구분하지 않고 서로 달라야 합니다."
        )
    for key in (
        "server_update_state_relative_path",
        "server_update_stage_relative_directory",
        "server_manual_transaction_relative_directory",
    ):
        relative_path = PureWindowsPath(installation[key])
        if (
            not relative_path.parts
            or relative_path.is_absolute()
            or relative_path.drive
            or relative_path.root
            or any(part in (".", "..") for part in relative_path.parts)
            or any(
                re.search(r'[<>:"|?*\x00-\x1f]', part)
                or part.endswith((" ", "."))
                for part in relative_path.parts
            )
        ):
            raise ConfigError(
                f"installation.{key}는 안전한 상대 경로여야 합니다."
            )
    _non_empty_string(
        installation.get("update_mutex_name"),
        "installation.update_mutex_name",
    )

    logging_config = config["logging"]
    _non_empty_string(
        logging_config.get("directory"),
        "logging.directory",
    )

    network = config["network"]
    port = network.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("network.port 범위가 올바르지 않습니다.")
    for key, value in network.items():
        if key == "port":
            continue
        if key.endswith(("_attempts", "_bytes", "_backlog")):
            _positive_integer(value, f"network.{key}")
        elif key.endswith("_seconds"):
            _positive_number(value, f"network.{key}")

    protocol = config["protocol"]
    for key in (
        "general_command",
        "osmo_command",
        "nova_command",
        "capabilities_command",
        "busy_result_code",
        "legacy_result_command",
    ):
        _non_empty_string(protocol.get(key), f"protocol.{key}")
    _string_list(
        protocol.get("direct_result_features"),
        "protocol.direct_result_features",
    )
    _string_list(
        protocol.get("result_codes"),
        "protocol.result_codes",
    )
    _positive_integer(
        protocol.get("request_id_max_length"),
        "protocol.request_id_max_length",
    )
    current_au = protocol.get("current_au_commands")
    legacy_au = protocol.get("legacy_au_commands")
    if not isinstance(current_au, dict) or not isinstance(legacy_au, dict):
        raise ConfigError("AU 명령 매핑이 올바르지 않습니다.")
    for mapping_name, mapping in (
        ("current_au_commands", current_au),
        ("legacy_au_commands", legacy_au),
    ):
        if not mapping:
            raise ConfigError(f"protocol.{mapping_name}이 비어 있습니다.")
        for command, number in mapping.items():
            _non_empty_string(
                command,
                f"protocol.{mapping_name} command",
            )
            _positive_integer(
                number,
                f"protocol.{mapping_name}.{command}",
            )
    if len(set(current_au.values())) != len(current_au):
        raise ConfigError("현재 AU 장비 번호가 중복되었습니다.")
    overlapping_au_commands = set(current_au) & set(legacy_au)
    if overlapping_au_commands:
        raise ConfigError(
            "현재/구형 AU 명령이 중복되었습니다: "
            + ", ".join(sorted(overlapping_au_commands))
        )
    if not set(legacy_au.values()).issubset(current_au.values()):
        raise ConfigError(
            "구형 AU 명령의 장비 번호가 현재 AU 장비와 "
            "일치하지 않습니다."
        )
    legacy_client_commands = protocol.get("legacy_client_commands")
    if not isinstance(legacy_client_commands, dict):
        raise ConfigError("protocol.legacy_client_commands가 필요합니다.")
    for command, alias in legacy_client_commands.items():
        _non_empty_string(command, "protocol.legacy_client_commands command")
        _non_empty_string(
            alias,
            f"protocol.legacy_client_commands.{command}",
        )
        if command not in current_au or alias not in legacy_au:
            raise ConfigError(
                "구형 클라이언트 AU 명령 별칭이 현재/구형 "
                f"명령에 없습니다: {command} -> {alias}"
            )
        if current_au[command] != legacy_au[alias]:
            raise ConfigError(
                "구형 클라이언트 AU 명령 별칭은 같은 장비 번호를 "
                f"가리켜야 합니다: {command} -> {alias}"
            )

    devices = config["devices"]
    order = devices.get("order")
    definitions = devices.get("definitions")
    if not isinstance(order, list) or not isinstance(definitions, dict):
        raise ConfigError("devices.order/definitions 설정이 올바르지 않습니다.")
    _string_list(order, "devices.order")
    if len(order) != len(set(order)):
        raise ConfigError("devices.order에 중복 장비가 있습니다.")
    if set(order) != set(definitions):
        raise ConfigError("devices.order와 definitions 장비 목록이 일치하지 않습니다.")

    ips = []
    supported_commands = {
        protocol["general_command"],
        protocol["osmo_command"],
        protocol["nova_command"],
        *current_au,
    }
    for device_id in order:
        definition = definitions[device_id]
        if not isinstance(definition, dict):
            raise ConfigError(f"장비 설정이 객체가 아닙니다: {device_id}")
        ip = definition.get("ip")
        if not isinstance(ip, str) or not ip:
            raise ConfigError(f"장비 IP가 없습니다: {device_id}")
        try:
            parsed_ip = ipaddress.ip_address(ip)
        except ValueError as error:
            raise ConfigError(
                f"장비 IPv4 주소가 올바르지 않습니다: {device_id}"
            ) from error
        if parsed_ip.version != 4:
            raise ConfigError(
                f"장비 IP는 IPv4여야 합니다: {device_id}"
            )
        ips.append(ip)
        display_name = definition.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            raise ConfigError(f"장비 표시명이 없습니다: {device_id}")
        if definition.get("command") not in supported_commands:
            raise ConfigError(f"지원하지 않는 장비 명령입니다: {device_id}")
        if not isinstance(definition.get("groups"), list):
            raise ConfigError(f"장비 그룹이 배열이 아닙니다: {device_id}")
        _string_list(
            definition["groups"],
            f"devices.definitions.{device_id}.groups",
        )
    if len(ips) != len(set(ips)):
        raise ConfigError("장비 IP가 중복되었습니다.")

    interfaces = config["interfaces"]
    executable_names = _require_mapping(interfaces, "executable_names")
    for key in (
        "general",
        "osmo_1",
        "osmo_2",
        "nova_1",
        "nova_2",
        "au_result",
    ):
        _non_empty_string(
            executable_names.get(key),
            f"interfaces.executable_names.{key}",
        )

    general = _require_mapping(interfaces, "general")
    _string_list(
        general.get("configured_executable_paths"),
        "interfaces.general.configured_executable_paths",
    )
    _string_list(
        general.get("discovery_roots"),
        "interfaces.general.discovery_roots",
    )
    _non_empty_string(
        general.get("success_title_contains"),
        "interfaces.general.success_title_contains",
    )
    _string_list(
        general.get("rejected_title_markers"),
        "interfaces.general.rejected_title_markers",
    )

    for family_name in ("osmo", "nova"):
        family = _require_mapping(interfaces, family_name)
        for number in ("1", "2"):
            profile = _require_mapping(family, number)
            for key in ("directory", "title"):
                _non_empty_string(
                    profile.get(key),
                    f"interfaces.{family_name}.{number}.{key}",
                )

    au_config = _require_mapping(interfaces, "au")
    required_au_numbers = {str(number) for number in current_au.values()}
    if set(au_config) != required_au_numbers:
        raise ConfigError(
            "AU 실행 경로 설정과 명령 번호가 일치해야 합니다."
        )
    for number in required_au_numbers:
        profile = _require_mapping(au_config, number)
        for key in (
            "order_directory",
            "result_directory",
            "order_title",
            "result_title",
        ):
            _non_empty_string(
                profile.get(key),
                f"interfaces.au.{number}.{key}",
            )

    automation = _require_mapping(interfaces, "automation")
    _non_empty_string(
        automation.get("login_window_title"),
        "interfaces.automation.login_window_title",
    )
    for key, value in automation.items():
        if key.endswith(("_attempts", "_ms")):
            _positive_integer(value, f"interfaces.automation.{key}")
        elif key.endswith("_seconds"):
            _positive_number(value, f"interfaces.automation.{key}")
    click_offset = automation.get("activation_click_offset")
    if (
        not isinstance(click_offset, list)
        or len(click_offset) != 2
        or any(
            isinstance(item, bool) or not isinstance(item, int)
            for item in click_offset
        )
    ):
        raise ConfigError(
            "interfaces.automation.activation_click_offset은 "
            "숫자 2개 배열이어야 합니다."
        )
    _string_list(
        automation.get("special_focus_computer_names"),
        "interfaces.automation.special_focus_computer_names",
        allow_empty=True,
    )

    update = config["update"]
    for key in (
        "manifest_url",
        "legacy_version_url",
        "legacy_client_installer_url",
        "artifact_url_template",
    ):
        _non_empty_string(update.get(key), f"update.{key}")
    repository = release["repository"]
    branch = release["branch"]
    expected_manifest_url = (
        f"https://raw.githubusercontent.com/{repository}/{branch}/"
        f"{artifacts['manifest_filename']}"
    )
    expected_legacy_version_url = (
        f"https://raw.githubusercontent.com/{repository}/{branch}/"
        f"{artifacts['legacy_version_filename']}"
    )
    expected_legacy_installer_url = (
        f"https://github.com/{repository}/releases/download/"
        f"{artifacts['legacy_client_release_tag']}/"
        f"{artifacts['client_installer_filename']}"
    )
    expected_update_urls = {
        "manifest_url": expected_manifest_url,
        "legacy_version_url": expected_legacy_version_url,
        "legacy_client_installer_url": expected_legacy_installer_url,
    }
    for key, expected in expected_update_urls.items():
        if update[key] != expected:
            raise ConfigError(
                f"update.{key}은 release.repository/branch 및 "
                f"artifacts 설정과 일치해야 합니다: {expected}"
            )
    client_update = _require_mapping(update, "client")
    _boolean(client_update.get("automatic"), "update.client.automatic")
    _string_list(
        client_update.get("installer_arguments"),
        "update.client.installer_arguments",
        allow_empty=True,
    )
    _non_empty_string(
        client_update.get("temporary_filename_template"),
        "update.client.temporary_filename_template",
    )
    _positive_number(
        client_update.get("process_exit_timeout_seconds"),
        "update.client.process_exit_timeout_seconds",
    )
    _positive_number(
        client_update.get("startup_health_timeout_seconds"),
        "update.client.startup_health_timeout_seconds",
    )
    server_update = _require_mapping(update, "server")
    _boolean(server_update.get("automatic"), "update.server.automatic")
    _string_list(
        server_update.get("installer_arguments"),
        "update.server.installer_arguments",
    )
    for key in (
        "power_shell_executable",
        "temporary_filename_template",
    ):
        _non_empty_string(
            server_update.get(key),
            f"update.server.{key}",
        )
    for key in (
        "request_timeout_seconds",
        "download_connect_timeout_seconds",
        "download_read_timeout_seconds",
        "download_total_timeout_seconds",
        "mutex_wait_seconds",
    ):
        _positive_number(update.get(key), f"update.{key}")
    for key in (
        "initial_delay_seconds",
        "check_interval_seconds",
        "thread_join_timeout_seconds",
        "helper_start_timeout_seconds",
        "helper_start_poll_interval_seconds",
        "parent_exit_timeout_seconds",
        "installer_timeout_seconds",
        "health_timeout_seconds",
        "health_poll_interval_seconds",
        "state_stale_timeout_seconds",
        "drain_timeout_seconds",
        "failure_retry_delay_seconds",
    ):
        _positive_number(
            server_update.get(key),
            f"update.server.{key}",
            allow_zero=key == "initial_delay_seconds",
        )
    if (
        update["mutex_wait_seconds"]
        >= server_update["helper_start_timeout_seconds"]
    ):
        raise ConfigError(
            "update.mutex_wait_seconds는 "
            "update.server.helper_start_timeout_seconds보다 작아야 합니다."
        )

    client_config = config["client"]
    _non_empty_string(
        client_config.get("window_title_template"),
        "client.window_title_template",
    )
    _non_empty_string(
        client_config.get("log_filename_prefix"),
        "client.log_filename_prefix",
    )
    _positive_integer(
        client_config.get("credential_id_length"),
        "client.credential_id_length",
    )
    position = client_config.get("initial_window_position")
    if (
        not isinstance(position, list)
        or len(position) != 2
        or any(
            isinstance(item, bool) or not isinstance(item, int)
            for item in position
        )
    ):
        raise ConfigError(
            "client.initial_window_position은 숫자 2개 배열이어야 합니다."
        )

    server_config = config["server"]
    for key, value in server_config.items():
        if key.endswith("_seconds"):
            _positive_number(value, f"server.{key}")
        else:
            _non_empty_string(value, f"server.{key}")

    login_completion_budget = (
        server_config["login_execution_timeout_seconds"]
        + server_config["ial_child_join_timeout_seconds"]
    )
    for key in (
        "login_response_timeout_seconds",
        "legacy_total_timeout_seconds",
    ):
        if network[key] <= login_completion_budget:
            raise ConfigError(
                f"network.{key}는 server 로그인 실행·정리 제한시간"
                f"({login_completion_budget}초)보다 길어야 합니다."
            )
    if (
        server_update["drain_timeout_seconds"]
        <= login_completion_budget
    ):
        raise ConfigError(
            "update.server.drain_timeout_seconds는 server 로그인 "
            f"실행·정리 제한시간({login_completion_budget}초)보다 "
            "길어야 합니다."
        )
    return config


def load_base_config(path=None):
    config_path = Path(path).resolve() if path else find_base_config_path()
    return validate_config(_read_json(config_path))


def load_config(path=None, local_path=None):
    config_path = Path(path).resolve() if path else find_base_config_path()
    base = load_base_config(config_path)
    local_config_path = (
        Path(local_path).resolve()
        if local_path
        else find_local_config_path(config_path)
    )
    if local_config_path is None:
        return base

    override = _read_json(local_config_path)
    protected_sections = sorted(
        section
        for section in ("build", "release", "protocol", "installation")
        if section in override
    )
    if protected_sections:
        raise ConfigError(
            "빌드 환경, 통합 버전, 통신 프로토콜, 설치 bootstrap 설정은 "
            "ril_config.json에서만 관리합니다. "
            "ril_config.local.json에서 다음 항목을 "
            f"제거하세요: {', '.join(protected_sections)}"
        )
    return validate_config(_merge_known(base, override))


def expand_path(value):
    if not isinstance(value, str):
        raise ConfigError("경로 설정은 문자열이어야 합니다.")
    return os.path.expandvars(value)
