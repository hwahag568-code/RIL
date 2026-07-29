# RIL JSON 설정 관리

## 파일 역할

- `ril_config.json`: 배포에 포함되는 기준 설정과 통합 버전
- `ril_config.local.json`: PC별 차이만 기록하는 선택 파일
- `ril_config.local.example.json`: 로컬 override 예제

설치 프로그램은 새 버전의 `ril_config.json`을 설치하지만 기존
`ril_config.local.json`은 덮어쓰지 않는다. 실행 시 두 파일을 깊게
병합하며 알 수 없는 키, 잘못된 타입, 누락 경로·시간값은 시작 단계에서
명확한 설정 오류로 차단한다.

테스트나 복구 작업에서는 환경변수로 기준 파일을 바꿀 수 있다.

```powershell
$env:RIL_CONFIG_PATH = "C:\Temp\ril_config.json"
```

이 경우 같은 폴더의 `ril_config.local.json`을 우선 사용한다.

## 관리 대상

`ril_config.json`에는 다음 운영값이 모여 있다.

- 통합 버전, protocol version, 저장소·branch와 artifact 이름
- 빌드 플랫폼, Python major/minor, 실행파일 아키텍처
- 클라이언트·서버 설치파일명, registry, 작업 스케줄러 이름
- 클라이언트·서버·구형 설치본의 분리된 runtime 폴더명
- 서버 업데이트 상태·stage·수동 설치 transaction의 상대 경로
- 클라이언트·서버 설치·업데이트·복구를 직렬화하는 공용 mutex 이름
- 빌드·업로드·활성화의 중복 실행을 막는 배포 mutex와 재시도 횟수
- 업데이트 URL, 공용 잠금 대기·확인·다운로드·drain·health·rollback 제한시간
- 포트, 메시지 크기, 연결·재시도·응답 제한시간
- 장비 ID, 표시명, IP, 명령, 선택 그룹과 표시 순서
- 일반/OSMO/Nova/AU 실행파일명, 실행 폴더와 창 title
- 로그인·창 탐색·창 정렬·실행 재시도의 횟수와 지연시간
- 클라이언트·서버 로그 폴더와 파일명

프로그램 경로의 기본값에는 `C:\Program Files...`를 사용할 수 있고,
환경변수가 필요한 경로에는 `%ProgramData%` 같은 Windows 형식을
사용할 수 있다.

## PC별 변경 예

설치 폴더에 다음처럼 `ril_config.local.json`을 만든다.

```json
{
  "interfaces": {
    "au": {
      "3": {
        "order_directory": "D:\\LIS_Interface\\AU_3",
        "result_directory": "D:\\LIS_Interface\\AU_3_RSLT"
      }
    }
  },
  "devices": {
    "definitions": {
      "AU3": {
        "ip": "10.2.151.219",
        "display_name": "AU 3"
      }
    }
  }
}
```

기준 파일에 이미 존재하는 키만 override할 수 있다. 적지 않은 값은
기준 설정을 그대로 사용한다.

## 로컬 변경이 금지된 항목

다음 최상위 섹션은 모든 PC와 빌드가 같아야 하므로
`ril_config.local.json`에서 변경할 수 없다.

- `release`
- `build`
- `protocol`
- `installation`

통합 버전은 `ril_config.json`의 `release.version`만 수정한다.
구형 `version.txt` 값은 빌드 과정에서 자동 파생된다.
핫픽스는 `.1`부터 사용하며 `.0`은 신형·구형 버전 비교 결과가
달라질 수 있어 설정 검증에서 거부한다.

## 인터페이스 재실행 경로

원격 로그인 요청을 받으면 실행 중인 인터페이스를 종료하기 전에
프로세스의 실제 EXE 전체 경로를 읽어 둔 뒤, 같은 폴더의 같은 파일을
다시 실행한다. JSON의 기본 경로는 해당 인터페이스가 실행 중이지 않을
때만 사용한다. AU처럼 여러 장비가 같은 파일명을 쓰는 경우에는 종료
직전의 장비별 창 제목으로 현재 프로세스만 식별한다. 기억한 파일이
사라졌거나 창 제목·설정 경로로 대상을 하나로 특정할 수 없으면 임의의
다른 파일을 선택하지 않고 안전하게 실패한다. 이때 사용한 현재 PID는
재시작 뒤의 로그인이나 창 정렬에는 재사용하지 않는다.

AU의 오더·결과, OSMO 1·2, Nova 1·2처럼 한 PC에 인터페이스가 두 개인
구성은 각 프로세스의 경로를 따로 기억한다. 모든 경로가 유효한지 먼저
확인한 다음 종료하며, 두 번째 프로세스 종료가 실패하면 이미 종료한
첫 번째 프로세스를 원래 경로에서 다시 실행한다.

실행 호출은 `interfaces.automation.launch_attempts`만큼 동일한 실제
경로로 재시도하고, 재시도 간격은
`launch_retry_delay_seconds`를 사용한다.

## 적용 시점

설정은 프로그램 시작 시 한 번 읽는다. 수정 후에는 클라이언트 또는
서버를 재시작해야 한다. 실행 중 hot reload는 지원하지 않는다.

## 의도적인 bootstrap 예외

설정 파일을 찾기 전에는 설정 자체를 읽을 수 없으므로
`ril_config.json`, `ril_config.local.json`이라는 파일명과 실행파일
옆을 먼저 확인하는 규칙은 코드에 고정되어 있다. 또한 진단·SQLite
복구 스크립트의 검증 hash와 안전 복구 대상은 일반 운영 설정으로
자유롭게 바꾸지 않는다.

설치 설정의 registry key나 값 이름 자체가 바뀌어도 기존 설치 폴더와
OLD 설정을 먼저 찾을 수 있도록
`HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\RIL`의
`InstallLocation`은 고정 bootstrap anchor로 사용한다. 이 위치까지
JSON 설정으로 바꾸면 변경된 설정을 읽기 전에 기존 설치 위치를 알 수
없는 순환 문제가 생기므로 의도적으로 설정 대상에서 제외한다.

날짜가 붙은 구형 Python 파일은 참고 자료일 뿐이다. 현재 빌드 대상은
`RIL_client.py`, `RIL_server.py`, `IAL.py`와 이름에 날짜가 없는
`.spec` 파일뿐이다.

GUI에서 장비를 추가·삭제하고 배치까지 바꾸는 다음 단계 설계는
[`GUI_CUSTOMIZATION_DESIGN.md`](GUI_CUSTOMIZATION_DESIGN.md)에
정리되어 있다.
