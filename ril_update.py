import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

import requests


class DownloadCancelled(Exception):
    pass


class UpdateManifestError(RuntimeError):
    pass


SUPPORTED_MANIFEST_SCHEMA_VERSION = 2


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_key(version):
    try:
        return tuple(
            int(part)
            for part in str(version).removeprefix("v").split(".")
        )
    except (TypeError, ValueError) as error:
        raise UpdateManifestError(
            f"업데이트 버전 형식이 올바르지 않습니다: {version!r}"
        ) from error


def validate_sha256(value):
    normalized = str(value or "").strip().lower()
    if (
        len(normalized) != 64
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise UpdateManifestError("업데이트 SHA-256 값이 올바르지 않습니다.")
    return normalized


def fetch_manifest(
    url,
    timeout,
    requests_module=requests,
):
    requests_module.packages.urllib3.disable_warnings()
    response = requests_module.get(
        url,
        timeout=timeout,
        verify=False,
    )
    response.raise_for_status()
    try:
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)):
            manifest = json.loads(bytes(content).decode("utf-8-sig"))
        else:
            manifest = response.json()
    except (
        AttributeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise UpdateManifestError(
            "업데이트 manifest가 올바른 JSON이 아닙니다."
        ) from error
    if not isinstance(manifest, dict):
        raise UpdateManifestError("업데이트 manifest는 객체여야 합니다.")
    return manifest


def get_component_update(
    manifest,
    component,
    current_version,
):
    remote_version = manifest.get("version")
    if version_key(remote_version) <= version_key(current_version):
        return None
    if manifest.get("schema_version") != SUPPORTED_MANIFEST_SCHEMA_VERSION:
        raise UpdateManifestError(
            "지원하지 않는 업데이트 manifest schema_version입니다."
        )

    component_data = manifest.get(component)
    if not isinstance(component_data, dict):
        raise UpdateManifestError(
            f"manifest에 {component} 업데이트 정보가 없습니다."
        )
    automatic_update = component_data.get("automatic_update")
    if component == "server" and automatic_update is not True:
        return None
    if component == "client" and automatic_update is False:
        return None

    url = component_data.get("url")
    if not isinstance(url, str) or not url:
        raise UpdateManifestError(
            f"manifest의 {component}.url이 올바르지 않습니다."
        )
    sha256 = validate_sha256(component_data.get("sha256"))
    size = component_data.get("size")
    if size is not None and (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
    ):
        raise UpdateManifestError(
            f"manifest의 {component}.size가 올바르지 않습니다."
        )
    return {
        "version": str(remote_version),
        "url": url,
        "sha256": sha256,
        "size": size,
        "file": component_data.get("file"),
    }


def download_verified_file(
    url,
    destination,
    expected_sha256,
    request_timeout,
    total_timeout,
    expected_size=None,
    progress_callback=None,
    is_cancelled=None,
    requests_module=requests,
    monotonic=time.monotonic,
):
    destination = str(destination)
    partial_path = f"{destination}.part"
    completed = False
    deadline = monotonic() + total_timeout
    expected_hash = (
        validate_sha256(expected_sha256)
        if expected_sha256 is not None
        else None
    )

    def check_stopped():
        if is_cancelled and is_cancelled():
            raise DownloadCancelled()
        if monotonic() >= deadline:
            raise TimeoutError(
                "업데이트 파일 전체 다운로드 시간이 초과되었습니다."
            )

    try:
        if os.path.exists(partial_path):
            os.remove(partial_path)

        check_stopped()
        with requests_module.get(
            url,
            stream=True,
            timeout=request_timeout,
            verify=False,
        ) as response:
            response.raise_for_status()
            content_length = int(response.headers.get("content-length", 0))
            total_size = expected_size or content_length
            downloaded_size = 0
            digest = hashlib.sha256()

            Path(partial_path).parent.mkdir(parents=True, exist_ok=True)
            with open(partial_path, "wb") as file:
                for chunk in response.iter_content(chunk_size=8192):
                    check_stopped()
                    if not chunk:
                        continue
                    file.write(chunk)
                    digest.update(chunk)
                    downloaded_size += len(chunk)
                    if total_size > 0 and progress_callback:
                        progress_callback(
                            min(
                                100,
                                int(downloaded_size / total_size * 100),
                            )
                        )

        check_stopped()
        if content_length > 0 and downloaded_size != content_length:
            raise IOError(
                "다운로드 크기가 일치하지 않습니다: "
                f"{downloaded_size}/{content_length}"
            )
        if expected_size is not None and downloaded_size != expected_size:
            raise IOError(
                "manifest의 파일 크기와 일치하지 않습니다: "
                f"{downloaded_size}/{expected_size}"
            )
        if expected_hash is not None and digest.hexdigest() != expected_hash:
            raise IOError(
                "업데이트 파일 SHA-256이 manifest와 일치하지 않습니다."
            )

        os.replace(partial_path, destination)
        completed = True
        return destination
    finally:
        if not completed and os.path.exists(partial_path):
            try:
                os.remove(partial_path)
            except OSError:
                pass


def server_update_directory(config):
    installation = config["installation"]
    root = os.path.expandvars(
        installation["runtime_program_data_dir"]
    )
    relative = installation[
        "server_update_stage_relative_directory"
    ]
    return Path(root) / relative


def server_update_destination(config, version):
    template = config["update"]["server"][
        "temporary_filename_template"
    ]
    return server_update_directory(config) / str(version) / template.format(
        version=version
    )


def atomic_write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_path = tempfile.mkstemp(
        prefix=f"{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
