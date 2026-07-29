import socket
import pickle
import time
import threading

from ril_config import load_config


_NETWORK = load_config()["network"]

MAX_MESSAGE_SIZE = _NETWORK["max_message_size_bytes"]
DEFAULT_MESSAGE_CHUNK_SIZE = _NETWORK["message_chunk_size_bytes"]
DEFAULT_SERVER_CLIENT_TIMEOUT = _NETWORK["server_client_timeout_seconds"]
DEFAULT_CONNECT_TIMEOUT = _NETWORK["connect_timeout_seconds"]
DEFAULT_SEND_TIMEOUT = _NETWORK["send_timeout_seconds"]
DEFAULT_REQUEST_RESPONSE_TIMEOUT = _NETWORK[
    "request_response_timeout_seconds"
]
DEFAULT_CANCEL_POLL_INTERVAL = _NETWORK[
    "cancel_poll_interval_seconds"
]
DEFAULT_RESULT_CONNECT_TIMEOUT = _NETWORK["result_connect_timeout_seconds"]
DEFAULT_RESULT_READ_TIMEOUT = _NETWORK["result_read_timeout_seconds"]
DEFAULT_SERVER_BACKLOG = _NETWORK["server_backlog"]
DEFAULT_SERVER_ACCEPT_POLL_INTERVAL = _NETWORK[
    "server_accept_poll_interval_seconds"
]
DEFAULT_CONNECT_ATTEMPTS = _NETWORK["connect_attempts"]
DEFAULT_CONNECT_RETRY_DELAY = _NETWORK["connect_retry_delay_seconds"]


class MessageTooLargeError(ValueError):
    pass


class IncompleteMessageError(ValueError):
    pass


class OperationCancelledError(RuntimeError):
    pass


def _raise_if_cancelled(is_cancelled):
    if is_cancelled is not None and is_cancelled():
        raise OperationCancelledError("작업이 취소되었습니다.")


# =============================================================================
# 서버 코드
# =============================================================================
def run_server(
    port,
    do_work_server,
    s_count=1,
    client_timeout=DEFAULT_SERVER_CLIENT_TIMEOUT,
    backlog=DEFAULT_SERVER_BACKLOG,
    on_listening=None,
    stop_event=None,
    accept_poll_interval=DEFAULT_SERVER_ACCEPT_POLL_INTERVAL,
    concurrent_handlers=False,
):
    handler_condition = threading.Condition()
    active_handlers = set()

    def run_handler(client, addr):
        try:
            do_work_server(client, addr)
        finally:
            try:
                client.close()
            finally:
                with handler_condition:
                    active_handlers.discard(threading.current_thread())
                    handler_condition.notify_all()

    # 1. 초기화
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows의 SO_REUSEADDR는 다른 프로세스가 같은 포트를
            # 함께 bind할 수 있게 하므로 서버 단일 소유권에 적합하지 않다.
            server.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        else:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 2. bind
        server.bind(('',port))

        # 3. listen
        server.listen(backlog)
        if stop_event is not None:
            server.settimeout(accept_poll_interval)
        if on_listening is not None:
            on_listening()

        # 4. accept
        remaining = s_count
        while remaining is None or remaining > 0:
            if stop_event is not None and stop_event.is_set():
                break
            print('클라이언트 접속을 기다립니당나귀')
            client = None
            try:
                try:
                    client,addr = server.accept()
                except socket.timeout:
                    if stop_event is not None:
                        continue
                    raise
                client.settimeout(client_timeout)
                if concurrent_handlers:
                    handler = threading.Thread(
                        target=run_handler,
                        args=(client, addr),
                        name="RIL_Client_Handler",
                        daemon=False,
                    )
                    with handler_condition:
                        active_handlers.add(handler)
                    try:
                        handler.start()
                    except Exception:
                        with handler_condition:
                            active_handlers.discard(handler)
                        raise
                    client = None
                else:
                    do_work_server(client, addr)
            finally:
                if client is not None:
                    client.close()
            if remaining is not None:
                remaining -= 1
    finally:
        server.close()
        if concurrent_handlers:
            with handler_condition:
                while active_handlers:
                    handler_condition.wait()
# =============================================================================
# 클라이언트 코드
# =============================================================================
def run_client(
    ip,
    port,
    do_work_client,
    int_id,
    int_pw,
    prg,
    connect_timeout=DEFAULT_CONNECT_TIMEOUT,
    send_timeout=DEFAULT_SEND_TIMEOUT,
    is_cancelled=None,
    connect_attempts=DEFAULT_CONNECT_ATTEMPTS,
    connect_retry_delay=DEFAULT_CONNECT_RETRY_DELAY,
):
    if connect_attempts < 1:
        raise ValueError("connect_attempts는 1 이상이어야 합니다.")

    for attempt in range(connect_attempts):
        # 연결에 실패한 소켓은 재사용하지 않는다.
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _raise_if_cancelled(is_cancelled)
            client.settimeout(connect_timeout)
            print('로그인을 위해 서버에 접속을 시도중 !')
            try:
                client.connect((ip,port))
            except OSError:
                # connect가 끝나기 전의 실패만 재시도한다. 연결 후
                # 전송 실패는 서버가 명령을 받았을 수 있어 재전송하지 않는다.
                if attempt + 1 >= connect_attempts:
                    raise
                _raise_if_cancelled(is_cancelled)
                time.sleep(connect_retry_delay)
                continue

            _raise_if_cancelled(is_cancelled)
            client.settimeout(send_timeout)
            print('로그인을 위한 서버 접속 완료!')
            do_work_client(client,int_id,int_pw, prg)
            print('로그인을 위한 서버 접속 종료 *^^*')
            return
        finally:
            client.close()
    
def recv_result(
    ip,
    port,
    do_recv_result,
    int_id,
    int_pw,
    prg,
    connect_timeout=DEFAULT_RESULT_CONNECT_TIMEOUT,
    response_timeout=DEFAULT_RESULT_READ_TIMEOUT,
    deadline=None,
):
    # 1. 초기화
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    response_deadline = time.monotonic() + response_timeout
    if deadline is None:
        deadline = response_deadline
    else:
        deadline = min(deadline, response_deadline)

    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("전체 결과 대기 시간이 초과되었습니다.")
        client.settimeout(min(connect_timeout, remaining))
        # 2. connect
        print('결과를 받기 위해 서버에 접속을 시도중 !')
        client.connect((ip,port))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("전체 결과 대기 시간이 초과되었습니다.")
        client.settimeout(remaining)
        print('결과를 받기 위한 서버 접속 완료!')
        result = do_recv_result(
            client,
            int_id,
            int_pw,
            prg,
            deadline,
        )  # 결과 받아오기
        print('결과를 받기 위한 서버 접속 종료 *^^*')
        return result   #  결과 반환
    
    except socket.timeout:
        print("접속 시간 초과 (Timeout) - 재시도 예정")
        return None  # None을 반환해야 listen_server가 루프를 돕니다.
        
    except ConnectionRefusedError:
        print("서버 연결 거부 (서버가 바쁨) - 재시도 예정")
        return None

    except MessageTooLargeError:
        raise

    except IncompleteMessageError:
        print("불완전한 결과 응답 - 재시도 예정")
        return None

    except OperationCancelledError:
        raise

    except Exception as e:
        print(f"그 외 접속 에러 발생: {e}")
        return None
    finally:
        client.close()

def request_response(
    ip,
    port,
    cmd,
    connect_timeout=DEFAULT_CONNECT_TIMEOUT,
    send_timeout=DEFAULT_SEND_TIMEOUT,
    response_timeout=DEFAULT_REQUEST_RESPONSE_TIMEOUT,
    is_cancelled=None,
):
    """요청을 보내고 같은 연결에서 해당 요청의 응답을 받는다."""
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    deadline = time.monotonic() + response_timeout
    try:
        _raise_if_cancelled(is_cancelled)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("전체 요청 시간이 초과되었습니다.")
        client.settimeout(min(connect_timeout, remaining))
        client.connect((ip, port))

        _raise_if_cancelled(is_cancelled)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("전체 요청 시간이 초과되었습니다.")
        client.settimeout(min(send_timeout, remaining))
        my_send(cmd, client)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("전체 요청 시간이 초과되었습니다.")
        client.settimeout(remaining)
        return my_recv(
            DEFAULT_MESSAGE_CHUNK_SIZE,
            client,
            total_timeout=remaining,
            is_cancelled=is_cancelled,
        )
    finally:
        client.close()
    
    
# =============================================================================
# 공통 코드
# =============================================================================

def my_recv(
    B_SIZE,
    client,
    max_bytes=MAX_MESSAGE_SIZE,
    total_timeout=None,
    is_cancelled=None,
    cancel_poll_interval=DEFAULT_CANCEL_POLL_INTERVAL,
):
    data = b""
    deadline = (
        time.monotonic() + total_timeout
        if total_timeout is not None
        else None
    )

    while True:
        _raise_if_cancelled(is_cancelled)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout(
                    "메시지 전체 수신 시간이 초과되었습니다."
                )
            receive_timeout = remaining
            if is_cancelled is not None:
                receive_timeout = min(
                    receive_timeout,
                    cancel_poll_interval,
                )
            client.settimeout(receive_timeout)

        try:
            packet = client.recv(B_SIZE)
        except socket.timeout:
            if (
                is_cancelled is not None
                and deadline is not None
                and time.monotonic() < deadline
            ):
                continue
            raise
        if not packet:
            if data:
                raise IncompleteMessageError(
                    "연결이 종료되기 전에 메시지 수신이 완료되지 않았습니다."
                )
            return None

        data += packet
        if len(data) > max_bytes:
            raise MessageTooLargeError(
                f"메시지 크기가 제한을 초과했습니다: "
                f"{len(data)} > {max_bytes}"
            )

        try:
            return pickle.loads(data)
        except Exception:
            # 아직 덜 받은 pickle일 수 있으므로 다음 패킷을 기다린다.
            # socket timeout 또는 연결 종료 시에는 위 예외로 빠져나간다.
            continue
# def my_recv(B_SIZE,client):
#     data = client.recv(B_SIZE)
#     if not data:
#         return data
#     cmd = pickle.loads(data)
#     return cmd

def my_send(cmd, client):
    data = pickle.dumps(cmd) # 직렬화
    if len(data) > MAX_MESSAGE_SIZE:
        raise MessageTooLargeError(
            f"메시지 크기가 제한을 초과했습니다: "
            f"{len(data)} > {MAX_MESSAGE_SIZE}"
        )
    client.sendall(data)
    
    return 0
