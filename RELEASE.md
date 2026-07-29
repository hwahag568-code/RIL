# RIL 통합 빌드 및 배포

## 통합 버전

버전은 [`ril_config.json`](ril_config.json)의
`release.version` 한 곳에서만 변경한다.

현재 통합 릴리스: `260729.5`

- 첫 배포: `260728`
- 같은 날 첫 핫픽스: `260728.1`
- 같은 날 두 번째 핫픽스: `260728.2`

핫픽스 번호는 `.1`부터 시작한다. `.0`은 구형 숫자 marker와 기본
버전이 충돌하므로 허용하지 않는다.

이 값 하나가 클라이언트 화면, 서버 트레이, CAPS 응답, 두 설치파일,
GitHub Release 태그와 `update.json`에 공통 적용된다.

구형 클라이언트용 숫자 marker는 별도로 입력하지 않는다. 빌드 시 통합
버전에서 자동 생성된다.

- `260728` → `26072801`
- `260728.1` → `26072802`
- `260728.2` → `26072803`

`release.protocol_version`은 통신 규격을 실제로 변경할 때만 별도로
증가시킨다.

## 로컬 빌드

```powershell
.\scripts\build_release.ps1 -InstallDependencies
```

생성 파일:

- `release\Update_RIL.exe`
- `release\RIL_Server_Setup_<VERSION>.exe`
- `release\update.json`
- `release\version.txt`

빌드 스크립트는 테스트, PyInstaller, NSIS, 입력 파일 provenance,
설치파일 SHA-256/크기를 검증한다. 클라이언트와 서버 설치파일 중
한쪽만 새로 만드는 것은 허용하지 않는다.

클라이언트와 서버는 모두 실행 파일 하나만 푸는 방식이 아니라
`RIL_client.exe`와 `_client_internal`, 또는 `RIL_server.exe`와
`_server_internal` 런타임 폴더를 함께 설치하는 one-folder 구성이다.
실행이나 업데이트에는 반드시 설치파일을 사용해야 하며, 실행 파일만
따로 복사하면 동작하지 않는다. 컴포넌트별 런타임을 분리하므로 같은
설치 루트에 두 역할이 있어도 서로의 Python DLL을 덮어쓰지 않는다.
두 설치·업데이트·복구 작업은 공용 mutex로 직렬화되어 공유 설정 파일도
동시에 교체하지 않는다. 겹친 작업은 JSON에 정한 제한시간까지만
기다리므로 무한 대기하지 않는다.

## 호환성 범위

- 배포 대상은 Windows 10/11 x64이며, 빌드는 Windows용 Python 3.13
  64-bit에서만 허용한다. 빌드 환경이 다르면 빌드 스크립트가 중단된다.
- Python, PyQt, pywin32 등 RIL 런타임은 `_client_internal` 또는
  `_server_internal`에 포함되므로 대상 PC에 Python을 별도로 설치할
  필요가 없다.
- AU/Alinity 등 32-bit 외부 인터페이스를 제어하는 기존 방식은
  유지된다. RIL 설치는 해당 프로그램의 .NET Framework,
  `System.Data.SQLite.dll`, OCX 파일을 설치하거나 변경하지 않는다.
- 서버의 예약 작업, 업데이트, 프로세스·창 제어에는 관리자 권한과
  Windows PowerShell 5.1 이상, 로그인되어 잠금 해제된 대화형 Windows
  세션이 필요하다. 잠긴 화면이나 연결이 끊긴 RDP 세션의 UI 자동화는
  지원 대상으로 보지 않는다.
- 클라이언트와 서버를 같은 PC에 설치해도 런타임 폴더는 서로 분리된다.
  실제 운영에서는 장비 서버와 사용자 클라이언트를 역할별 PC에 두는
  기존 구성을 권장한다.
- 실제 장비 EXE가 없는 빌드 PC에서는 로그인·창 정렬까지 재현할 수
  없으므로 배포 전 대표 AU/Alinity PC에서 최종 smoke test를 수행한다.

## 자동업데이트

두 프로그램은 저장소 루트의 동일한 `update.json`과 동일한 최상위
`version`을 사용한다.

- 클라이언트: 시작 시 확인하고 사용자 동의를 받은 뒤 검증된
  `Update_RIL.exe`를 실행한다. 기존 단일 파일 설치본도 폴더형
  런타임으로 교체한 뒤 새 클라이언트를 바로 실행한다. 새 프로세스가
  예상 버전·PID·실제 설치 경로를 기록한 시작 완료 marker까지 확인한
  뒤에만 업데이트를 확정하며, 제한시간 내 확인되지 않으면 기존
  클라이언트 폴더를 복원한다.
- 서버: 주기적으로 확인하고, 새 요청 수락 중지와 진행 중 요청 완료를
  확인한 뒤 외부 helper로 자동 설치한다. 기존 단일 파일 설치본과
  폴더형 설치본을 모두 백업·교체하며 설치 직후 새 서버를 실행한다.

서버 설치파일은 크기와 SHA-256을 검증한 뒤에만 실행된다. 설치 전
기존 서버 파일을 고유 백업 폴더에 보관하고 새 서버의 버전, 생성 시각,
부모·worker PID, 실제 실행 경로, registry를 확인한다. 확인에 실패하면
기존 파일과 registry를 복원하고 구버전 health까지 확인한다.

서버 설치파일을 직접 실행하는 경우에도 서버 파일·서버 런타임,
64비트 registry 값과 두 예약 작업을 먼저 백업한다. 새 서버의
버전·생성 시각·부모 및 worker PID·실제 실행 경로를 모두 확인해야
설치를 확정하고, 실패하면 기존 서버 구성만 복원한다. 같은 설치
폴더에 있는 클라이언트 런타임과 `ril_config.local.json`은 서버
설치·복구 대상에 포함하지 않는다.

`260728.2`보다 오래되어 서버 자동업데이터가 없는 설치본은 이 통합판
서버 설치파일을 한 번 수동 설치해야 한다. 이후 릴리스부터는 서버도
자동으로 업데이트된다.

## GitHub 배포

실제 배포에는 `hwahag568-code/RIL` 저장소에 `contents:write` 권한이
필요하다. 원클릭 스크립트는 `GH_TOKEN`이 없으면 저장된
`hwahag568-code` 계정의 `gh` 토큰을 현재 실행에만 사용하고 종료 시
원래 환경을 복원한다.

### 권장: 빌드부터 활성화까지 자동 실행

```powershell
.\scripts\build_and_publish.ps1 -ExpectedVersion 260729.5
```

이 명령은 테스트와 서버·클라이언트 빌드, Stage 업로드, 원격 SHA-256
검증, Activate와 최종 marker 검증을 순서대로 실행한다. 앞 단계가
실패하면 다음 단계는 실행하지 않는다. 같은 PC에서 두 배포가 동시에
실행되지 않도록 전체 과정에 mutex를 사용한다.

중단 후 같은 명령을 다시 실행했을 때 동일 태그가 있으면 재빌드하지
않고 기존 원격 자산이 로컬 자산과 모두 같은지 확인한다. 같은 파일만
이어 올리거나 Activate를 재개하며, 하나라도 다르면 태그를 덮지 않고
새 버전을 요구한다. 원격 marker가 더 최신인 경우에도 배포를 차단한다.

업로드까지만 수행할 때는 `-Mode StageOnly`, 이미 검증된 Stage의
활성화만 재개할 때는 `-Mode ActivateOnly`를 사용한다.

### 1. Stage

아래 단계별 명령을 직접 실행하는 고급 사용 경로에서는 쓰기 권한이
있는 `GH_TOKEN`을 환경변수로 제공해야 한다. 일반 배포에는 위 원클릭
스크립트를 사용한다.

```powershell
.\scripts\publish_release.ps1 -Phase Stage
```

Stage는 `v<VERSION>` Release에 네 산출물을 올린 뒤 다시 내려받아
SHA-256을 검증한다. 저장소 루트 `update.json`과 `version.txt`는
변경하지 않으므로 자동업데이트가 시작되지 않는다.

같은 태그가 이미 있으면 기본적으로 실패한다. 중단된 동일 업로드는
기존 원격 자산의 해시가 모두 일치할 때만 다음 명령으로 재개한다.

```powershell
.\scripts\publish_release.ps1 -Phase Stage -ResumeStage
```

`-AllowVersionClobber`는 명시적인 복구 작업에만 사용한다. 정상
핫픽스는 기존 태그를 덮지 말고 새 버전을 사용한다.

### 2. Activate

```powershell
.\scripts\publish_release.ps1 -Phase Activate
```

Activate는 다음 순서로 진행한다.

1. Stage 자산과 로컬 릴리스 세트를 다시 검증한다.
2. 구형 클라이언트용 `Update` Release의 `Update_RIL.exe`를 준비한다.
3. 저장소 루트 `update.json`을 갱신해 통합 서버·클라이언트 버전을
   활성화한다.
4. 마지막에 `version.txt`를 갱신해 구형 클라이언트를 활성화한다.

같은 명령을 다시 실행해도 이미 완료된 저장소 파일은 건너뛴다.

## Dry run

```powershell
.\scripts\build_and_publish.ps1 -DryRun
.\scripts\publish_release.ps1 -Phase Stage -DryRun
.\scripts\publish_release.ps1 -Phase Activate -DryRun
```

GitHub에는 쓰지 않고 로컬 릴리스 검증과 실행 계획만 확인한다.

## GitHub Actions

workflow는 Windows에서 테스트와 서버·클라이언트 설치파일 빌드를
수행한다. `publish=true`이면 Stage가 성공하고 원격 자산 검증까지
끝난 경우에만 이어서 Activate한다. `concurrency`가 다른 배포와의
동시 실행을 막는다.

Actions 배포를 사용하려면 저장소 secret `DISTRIBUTION_TOKEN`에
`contents:write` 토큰을 등록해야 한다. 토큰이나 연결 계정에 쓰기
권한이 없으면 빌드는 가능하지만 Stage/Activate는 차단된다.

로컬 원클릭 배포는 `hwahag568-code` 저장 계정의 쓰기 권한을 사용한다.
Actions는 로컬 로그인 정보를 사용할 수 없으므로 별도로
`DISTRIBUTION_TOKEN`을 등록해야 한다.
