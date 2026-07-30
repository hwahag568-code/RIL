import time
import threading
import uuid

import mynetlib
from ril_devices import LEGACY_CLIENT_COMMANDS
from ril_config import load_config
from ril_version import PROTOCOL_VERSION


_CONFIG = load_config()
_NETWORK = _CONFIG["network"]
_PROTOCOL = _CONFIG["protocol"]

PORT = _NETWORK["port"]
CAPABILITIES_COMMAND = _PROTOCOL["capabilities_command"]
CAPABILITY_TIMEOUT = _NETWORK["capability_timeout_seconds"]
CAPABILITY_CACHE_TTL = _NETWORK["capability_cache_ttl_seconds"]
LOGIN_RESPONSE_TIMEOUT = _NETWORK["login_response_timeout_seconds"]
DIRECT_RESULT_MAX_ATTEMPTS = _NETWORK["direct_result_max_attempts"]
LEGACY_TOTAL_TIMEOUT = _NETWORK["legacy_total_timeout_seconds"]
LEGACY_CONNECT_TIMEOUT = _NETWORK["legacy_connect_timeout_seconds"]
LEGACY_READ_TIMEOUT = _NETWORK["legacy_read_timeout_seconds"]
LEGACY_RETRY_DELAY = _NETWORK["legacy_retry_delay_seconds"]
LEGACY_COMMAND_CONNECT_ATTEMPTS = _NETWORK[
    "legacy_command_connect_attempts"
]
LEGACY_COMMAND_CONNECT_RETRY_DELAY = _NETWORK[
    "legacy_command_connect_retry_delay_seconds"
]
DIRECT_RESULT_FEATURES = set(_PROTOCOL["direct_result_features"])
LEGACY_RESULT_COMMAND = _PROTOCOL["legacy_result_command"]
MESSAGE_CHUNK_SIZE = _NETWORK["message_chunk_size_bytes"]
_capability_cache = {}
_capability_locks = {}
_capability_locks_guard = threading.Lock()


class CapabilityProbeError(RuntimeError):
    pass


class LoginResultTimeoutError(TimeoutError):
    pass


class LoginResultUncertainError(RuntimeError):
    """명령 전송 가능성은 있지만 최종 결과를 확인하지 못한 경우."""

    def __init__(self, message, request_id):
        super().__init__(message)
        self.request_id = request_id


def _get_capability_lock(ip):
    with _capability_locks_guard:
        return _capability_locks.setdefault(ip, threading.Lock())


def _invalidate_server_capabilities(ip):
    capability_lock = _get_capability_lock(ip)
    with capability_lock:
        _capability_cache.pop(ip, None)


def do_recv_result(
    client,
    int_id,
    int_pw,
    prg,
    deadline,
    is_cancelled=None,
):
    print('클라이언트 결과확인 위해 열일중')

    if prg != LEGACY_RESULT_COMMAND:
        return None

    # 결과 요청
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("전체 결과 대기 시간이 초과되었습니다.")
    client.settimeout(min(mynetlib.DEFAULT_SEND_TIMEOUT, remaining))
    cmd_r = [int_id, int_pw, LEGACY_RESULT_COMMAND]
    mynetlib.my_send(cmd_r, client)

    # 서버 응답 한 번 대기
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("전체 결과 대기 시간이 초과되었습니다.")
    client.settimeout(remaining)
    cmd_v = mynetlib.my_recv(
        MESSAGE_CHUNK_SIZE,
        client,
        total_timeout=remaining,
        is_cancelled=is_cancelled,
    )
    print("서버에서 받은 응답:", cmd_v, type(cmd_v))

    return cmd_v


def listen_server(
    ips,
    int_id,
    int_pw,
    prg,
    is_cancelled=None,
):
    deadline = time.monotonic() + LEGACY_TOTAL_TIMEOUT
    attempt = 0

    while True:
        mynetlib._raise_if_cancelled(is_cancelled)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        attempt += 1
        print(
            f"[listen_server] 결과 요청 시도 {attempt} "
            f"(남은 시간: {remaining:.1f}초)"
        )
        def receive_result(
            result_client,
            result_id,
            result_pw,
            result_program,
            result_deadline,
        ):
            return do_recv_result(
                result_client,
                result_id,
                result_pw,
                result_program,
                result_deadline,
                is_cancelled=is_cancelled,
            )

        result = mynetlib.recv_result(
            ips,
            PORT,
            receive_result,
            int_id,
            int_pw,
            prg,
            connect_timeout=min(LEGACY_CONNECT_TIMEOUT, remaining),
            response_timeout=min(LEGACY_READ_TIMEOUT, remaining),
            deadline=deadline,
        )
        if result is not None:
            return result

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(LEGACY_RETRY_DELAY, remaining))

    raise LoginResultTimeoutError(
        f"{LEGACY_TOTAL_TIMEOUT}초 안에 서버 결과를 받지 못했습니다."
    )

def do_work_client(client, int_id, int_pw, prg):
    print('클라이언트 로그인 위해 열일중')
    cmd = [int_id, int_pw, prg]
    mynetlib.my_send(cmd, client)


def run_client2(
    ips,
    int_id,
    int_pw,
    prg,
    is_cancelled=None,
):
    mynetlib.run_client(
        ips,
        PORT,
        do_work_client,
        int_id,
        int_pw,
        prg,
        is_cancelled=is_cancelled,
        connect_attempts=LEGACY_COMMAND_CONNECT_ATTEMPTS,
        connect_retry_delay=LEGACY_COMMAND_CONNECT_RETRY_DELAY,
    )


def get_server_capabilities(ip, is_cancelled=None):
    capability_lock = _get_capability_lock(ip)

    with capability_lock:
        now = time.monotonic()
        cached = _capability_cache.get(ip)
        if cached is not None:
            expires_at, capabilities = cached
            if now < expires_at:
                return capabilities
            _capability_cache.pop(ip, None)

        try:
            request_options = {
                "response_timeout": CAPABILITY_TIMEOUT,
            }
            if is_cancelled is not None:
                request_options["is_cancelled"] = is_cancelled
            response = mynetlib.request_response(
                ip,
                PORT,
                ["", "", CAPABILITIES_COMMAND],
                **request_options,
            )
        except mynetlib.OperationCancelledError:
            raise
        except Exception as e:
            raise CapabilityProbeError(
                f"서버 기능 확인에 실패했습니다: {e}"
            ) from e

        if (
            isinstance(response, dict)
            and response.get("type") == "capabilities"
        ):
            _capability_cache[ip] = (
                now + CAPABILITY_CACHE_TTL,
                response,
            )
            return response

        if (
            isinstance(response, (list, tuple))
            and len(response) >= 2
            and response[1] == "int_failed"
        ):
            legacy_capabilities = {}
            _capability_cache[ip] = (
                now + CAPABILITY_CACHE_TTL,
                legacy_capabilities,
            )
            return legacy_capabilities

        raise CapabilityProbeError(
            f"서버 기능 확인 응답이 없습니다: {response!r}"
        )


def _supports_direct_result(capabilities):
    try:
        protocol_version = int(capabilities.get("protocol_version", 0))
    except (TypeError, ValueError):
        return False

    features = set(capabilities.get("features", []))
    return (
        protocol_version >= PROTOCOL_VERSION
        and DIRECT_RESULT_FEATURES.issubset(features)
    )


def run_login(ip, int_id, int_pw, prg, is_cancelled=None):
    capabilities = get_server_capabilities(
        ip,
        is_cancelled=is_cancelled,
    )
    if not _supports_direct_result(capabilities):
        legacy_prg = LEGACY_CLIENT_COMMANDS.get(prg, prg)
        if is_cancelled is None:
            run_client2(ip, int_id, int_pw, legacy_prg)
            return listen_server(
                ip,
                int_id,
                int_pw,
                LEGACY_RESULT_COMMAND,
            )
        run_client2(
            ip, int_id, int_pw, legacy_prg,
            is_cancelled=is_cancelled,
        )
        return listen_server(
            ip, int_id, int_pw, LEGACY_RESULT_COMMAND,
            is_cancelled=is_cancelled,
        )

    request_id = uuid.uuid4().hex
    direct_deadline = time.monotonic() + LOGIN_RESPONSE_TIMEOUT
    try:
        direct_command = [int_id, int_pw, prg, request_id]
        for attempt in range(DIRECT_RESULT_MAX_ATTEMPTS):
            remaining = (
                LOGIN_RESPONSE_TIMEOUT
                if attempt == 0
                else direct_deadline - time.monotonic()
            )
            if remaining <= 0:
                raise LoginResultTimeoutError(
                    f"{LOGIN_RESPONSE_TIMEOUT}초 안에 "
                    "서버 결과를 받지 못했습니다."
                )
            request_options = {
                "response_timeout": remaining,
            }
            if is_cancelled is not None:
                request_options["is_cancelled"] = is_cancelled
            try:
                response = mynetlib.request_response(
                    ip,
                    PORT,
                    direct_command,
                    **request_options,
                )
                if response is None:
                    raise mynetlib.IncompleteMessageError(
                        "서버가 직접 응답 없이 연결을 종료했습니다."
                    )
                break
            except (
                ConnectionError,
                TimeoutError,
                mynetlib.IncompleteMessageError,
            ):
                _invalidate_server_capabilities(ip)
                if attempt + 1 >= DIRECT_RESULT_MAX_ATTEMPTS:
                    raise

        if not isinstance(response, (list, tuple)) or len(response) < 3:
            raise RuntimeError(
                f"직접 응답 형식이 올바르지 않습니다: {response!r}"
            )

        server_ip, status, response_request_id = response[:3]
        if response_request_id != request_id:
            raise RuntimeError(
                "서버 응답의 요청 ID가 일치하지 않습니다: "
                f"expected={request_id!r}, actual={response_request_id!r}"
            )
    except mynetlib.OperationCancelledError:
        _invalidate_server_capabilities(ip)
        raise
    except Exception as error:
        _invalidate_server_capabilities(ip)
        raise LoginResultUncertainError(
            "서버의 최종 로그인 결과를 확인하지 못했습니다. "
            "인터페이스 상태를 먼저 확인한 뒤 다시 실행하세요. "
            f"(request_id={request_id}, 원인={error})",
            request_id,
        ) from error

    return server_ip, status
