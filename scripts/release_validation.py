import argparse
import codecs
import hashlib
from importlib import metadata
import json
import platform
from pathlib import Path
import struct
import sys


BUILD_INPUTS = (
    "RIL_client.py",
    "RIL_server.py",
    "IAL.py",
    "client.py",
    "mynetlib.py",
    "ril_build_version.py",
    "ril_config.py",
    "ril_config.json",
    "ril_config.local.example.json",
    "ril_devices.py",
    "ril_update.py",
    "ril_version.py",
    "RIL.ui",
    "RIL_client.spec",
    "RIL_server.spec",
    "RIL_Client_Update.nsi",
    "RIL_Server_Setup.nsi",
    "requirements-build.txt",
    "chunsik1.ico",
    "dist/make_setup/RIL_server_start.bat",
    "dist/make_setup/RIL_server_start.ps1",
    "dist/make_setup/RIL_server_restarter.cmd",
    "dist/make_setup/RIL_server_restarter.ps1",
    "dist/make_setup/RIL_install_prepare.ps1",
    "dist/make_setup/RIL_client_startup_check.ps1",
    "dist/make_setup/RIL_server_manual_install.ps1",
    "dist/make_setup/RIL_server_update_helper.ps1",
)
BUILD_INFO_PATH = "dist/ril_build_info.json"
BUILD_VERSION_MODULE = "ril_build_version.py"


def current_build_environment():
    try:
        pyinstaller_version = metadata.version("PyInstaller")
    except metadata.PackageNotFoundError:
        pyinstaller_version = None
    return {
        "platform": sys.platform,
        "machine": platform.machine(),
        "architecture_bits": struct.calcsize("P") * 8,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "pyinstaller_version": pyinstaller_version,
    }


def validate_build_environment(config, environment):
    if not isinstance(environment, dict):
        raise RuntimeError("빌드 환경 provenance가 없습니다.")
    required = {
        "platform",
        "machine",
        "architecture_bits",
        "python_version",
        "python_implementation",
        "pyinstaller_version",
    }
    if not required.issubset(environment):
        raise RuntimeError("빌드 환경 provenance 항목이 부족합니다.")

    build = config["build"]
    python_parts = str(environment["python_version"]).split(".")
    actual_major_minor = ".".join(python_parts[:2])
    if (
        str(environment["platform"]) != str(build["platform"])
        or str(environment["machine"]).casefold()
        != str(build["machine"]).casefold()
        or int(environment["architecture_bits"])
        != int(build["architecture_bits"])
        or actual_major_minor != str(build["python_major_minor"])
        or not environment["pyinstaller_version"]
    ):
        raise RuntimeError(
            "기록된 빌드 환경이 ril_config.json 지원 환경과 다릅니다."
        )
    return environment


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_release_config(repo_root):
    path = repo_root / "ril_config.json"
    if not path.is_file():
        raise FileNotFoundError(f"통합 설정 파일이 없습니다: {path}")
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    return config


def build_output_paths(config):
    installation = config["installation"]
    client_name = Path(installation["client_executable"]).stem
    server_name = Path(installation["server_executable"]).stem
    return (
        f"dist/{client_name}",
        f"dist/{server_name}",
    )


def build_version_source(version, protocol_version):
    return (
        '"""Generated from ril_config.json by '
        'scripts/release_validation.py."""\n\n'
        f"VERSION = {json.dumps(str(version))}\n"
        f"PROTOCOL_VERSION = {int(protocol_version)}\n"
    )


def _validate_requested_version(config, version, protocol_version):
    release = config["release"]
    if release["version"] != str(version):
        raise RuntimeError(
            "요청한 버전과 ril_config.json의 통합 버전이 다릅니다."
        )
    if release["protocol_version"] != int(protocol_version):
        raise RuntimeError(
            "요청한 protocol version과 ril_config.json이 다릅니다."
        )


def write_build_version(repo_root, version, protocol_version):
    config = load_release_config(repo_root)
    _validate_requested_version(config, version, protocol_version)
    source = build_version_source(version, protocol_version)
    path = repo_root / BUILD_VERSION_MODULE
    if not path.is_file() or path.read_text(encoding="utf-8") != source:
        path.write_text(source, encoding="utf-8")
    return path


def validate_build_version(repo_root, version, protocol_version):
    config = load_release_config(repo_root)
    _validate_requested_version(config, version, protocol_version)
    path = repo_root / BUILD_VERSION_MODULE
    expected = build_version_source(version, protocol_version)
    if not path.is_file() or path.read_text(encoding="utf-8") != expected:
        raise RuntimeError(
            "바이너리 고정 버전 모듈이 ril_config.json과 다릅니다. "
            "write-version-module을 실행한 뒤 다시 빌드하세요."
        )
    return path


def artifact_url(config, version, filename):
    release = config["release"]
    template = config["update"]["artifact_url_template"]
    return (
        template.replace("{repository}", release["repository"])
        .replace("{tag}", f"{release['tag_prefix']}{version}")
        .replace("{filename}", filename)
    )


def legacy_version_from_version(version):
    parts = str(version).split(".", 1)
    sequence = int(parts[1]) + 1 if len(parts) == 2 else 1
    if sequence > 99:
        raise RuntimeError("하루 릴리스 순번은 99개를 초과할 수 없습니다.")
    return f"{parts[0]}{sequence:02d}"


def hash_files(repo_root, relative_paths):
    hashes = {}
    for relative_path in relative_paths:
        path = repo_root / relative_path
        if path.is_file():
            hashes[relative_path] = file_sha256(path)
            continue
        if path.is_dir():
            files = sorted(
                child
                for child in path.rglob("*")
                if child.is_file()
            )
            if not files:
                raise RuntimeError(
                    f"필수 빌드 디렉터리가 비어 있습니다: {relative_path}"
                )
            for child in files:
                child_path = child.relative_to(repo_root).as_posix()
                hashes[child_path] = file_sha256(child)
            continue
        if not path.exists():
            raise FileNotFoundError(f"필수 파일이 없습니다: {relative_path}")
        raise RuntimeError(f"지원하지 않는 빌드 경로입니다: {relative_path}")
    return hashes


def write_build_info(
    repo_root,
    version,
    protocol_version,
    input_paths=BUILD_INPUTS,
    output_paths=None,
):
    if output_paths is None:
        output_paths = build_output_paths(load_release_config(repo_root))
    if input_paths == BUILD_INPUTS:
        config = load_release_config(repo_root)
        validate_build_version(repo_root, version, protocol_version)
        validate_build_environment(config, current_build_environment())
    info = {
        "schema": 2,
        "version": version,
        "protocol_version": int(protocol_version),
        "build_environment": current_build_environment(),
        "inputs": hash_files(repo_root, input_paths),
        "outputs": hash_files(repo_root, output_paths),
    }
    path = repo_root / BUILD_INFO_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return info


def validate_build_info(
    repo_root,
    version,
    protocol_version,
    input_paths=BUILD_INPUTS,
    output_paths=None,
):
    if output_paths is None:
        output_paths = build_output_paths(load_release_config(repo_root))
    if input_paths == BUILD_INPUTS:
        validate_build_version(repo_root, version, protocol_version)
    path = repo_root / BUILD_INFO_PATH
    if not path.is_file():
        raise RuntimeError(
            "기존 빌드의 출처 정보가 없습니다. "
            "-SkipPyInstaller 없이 다시 빌드하세요."
        )

    info = json.loads(path.read_text(encoding="utf-8-sig"))
    expected = {
        "version": version,
        "protocol_version": int(protocol_version),
        "inputs": hash_files(repo_root, input_paths),
        "outputs": hash_files(repo_root, output_paths),
    }
    errors = []
    if info.get("schema") != 2:
        errors.append("schema")
    if input_paths == BUILD_INPUTS:
        try:
            validate_build_environment(
                load_release_config(repo_root),
                info.get("build_environment"),
            )
        except (RuntimeError, TypeError, ValueError):
            errors.append("build_environment")
    for field in ("version", "protocol_version", "inputs", "outputs"):
        if info.get(field) != expected[field]:
            errors.append(field)
    if errors:
        raise RuntimeError(
            "현재 소스와 기존 바이너리가 일치하지 않습니다 "
            f"({', '.join(errors)}). -SkipPyInstaller 없이 다시 빌드하세요."
        )
    return info


def validate_release(repo_root, version, protocol_version, legacy_version):
    config = load_release_config(repo_root)
    _validate_requested_version(config, version, protocol_version)
    validate_build_version(repo_root, version, protocol_version)
    expected_legacy_version = legacy_version_from_version(version)
    if legacy_version != expected_legacy_version:
        raise RuntimeError(
            "구형 marker가 통합 버전에서 파생된 값과 다릅니다."
        )
    artifacts = config["artifacts"]
    release_dir = repo_root / "release"
    manifest_path = release_dir / artifacts["manifest_filename"]
    legacy_path = release_dir / artifacts["legacy_version_filename"]
    client_name = artifacts["client_installer_filename"]
    client_path = release_dir / client_name
    server_name = artifacts["server_installer_filename_template"].format(
        version=version
    )
    server_path = release_dir / server_name

    for path in (manifest_path, legacy_path, client_path, server_path):
        if not path.is_file():
            raise FileNotFoundError(f"릴리스 파일이 없습니다: {path.name}")

    manifest_bytes = manifest_path.read_bytes()
    errors = []
    if manifest_bytes.startswith(codecs.BOM_UTF8):
        errors.append("manifest UTF-8 BOM")
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    if manifest.get("version") != version:
        errors.append("manifest version")
    if manifest.get("schema_version") != 2:
        errors.append("manifest schema_version")
    if manifest.get("protocol_version") != int(protocol_version):
        errors.append("manifest protocol_version")
    if manifest.get("server", {}).get("file") != server_name:
        errors.append("server filename")
    client_manifest = manifest.get("client", {})
    server_manifest = manifest.get("server", {})
    if client_manifest.get("url") != artifact_url(
        config,
        version,
        client_name,
    ):
        errors.append("client URL")
    if server_manifest.get("url") != artifact_url(
        config,
        version,
        server_name,
    ):
        errors.append("server URL")
    if client_manifest.get("sha256") != file_sha256(client_path):
        errors.append("client artifact")
    if server_manifest.get("sha256") != file_sha256(server_path):
        errors.append("server artifact")
    if client_manifest.get("size") != client_path.stat().st_size:
        errors.append("client size")
    if server_manifest.get("size") != server_path.stat().st_size:
        errors.append("server size")
    if client_manifest.get("automatic_update") is not True:
        errors.append("client automatic_update")
    if server_manifest.get("automatic_update") is not True:
        errors.append("server automatic_update")
    if legacy_path.read_text(encoding="ascii").strip() != legacy_version:
        errors.append("legacy version")
    if errors:
        raise RuntimeError(
            "릴리스 산출물의 버전/파일 구성이 일치하지 않습니다: "
            + ", ".join(errors)
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=(
            "write-version-module",
            "verify-version-module",
            "write-build-info",
            "verify-build-info",
            "verify-release",
        ),
    )
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--version", required=True)
    parser.add_argument("--protocol-version", required=True)
    parser.add_argument("--legacy-version")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    if args.command == "write-version-module":
        write_build_version(
            repo_root,
            args.version,
            args.protocol_version,
        )
    elif args.command == "verify-version-module":
        validate_build_version(
            repo_root,
            args.version,
            args.protocol_version,
        )
    elif args.command == "write-build-info":
        write_build_info(
            repo_root,
            args.version,
            args.protocol_version,
        )
    elif args.command == "verify-build-info":
        validate_build_info(
            repo_root,
            args.version,
            args.protocol_version,
        )
    else:
        if args.legacy_version is None:
            parser.error("--legacy-version is required for verify-release")
        validate_release(
            repo_root,
            args.version,
            args.protocol_version,
            args.legacy_version,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Release validation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
