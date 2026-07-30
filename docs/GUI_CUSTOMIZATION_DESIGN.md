# RIL GUI 커스터마이징 설계안

## 목적

현재 소스에 하드코딩된 장비명, IP, 선택 그룹, 실행 명령을 설정 파일과 GUI로 옮겨 사용자가 장비를 추가·수정·삭제할 수 있게 한다.

이 문서는 설계 검토 결과를 보존하기 위한 문서다. 구현은 주요 코드 리뷰 항목을 먼저 수정한 후 별도 작업으로 진행한다.

## 기본 원칙

1. 클라이언트의 장비 목록과 서버의 실행 규칙을 분리한다.
2. 장비 표시명과 내부 식별자를 분리한다.
3. 설정은 설치 폴더 밖에 저장해 자동업데이트 후에도 보존한다.
4. 이 정도 규모의 설정에는 SQLite를 추가하지 않고 JSON을 사용한다.
5. 클라이언트에서 서버 PC의 임의 EXE나 명령을 전달하지 않는다.
6. 기존 장비 동작은 검증된 서버 프로필로 제공하고, 완전히 다른 로그인 방식만 코드 확장 대상으로 둔다.

## 설정 책임과 저장 위치

| 구분 | 내용 | 권장 저장 위치 | 편집 주체 |
|---|---|---|---|
| 클라이언트 장비 카탈로그 | 장비명, IP, 포트, 순서, 사용 여부, 선택 그룹, 서버 프로필 ID | `%ProgramData%\RIL\client_catalog.json` | 클라이언트의 `장비 관리` GUI |
| 서버 실행 프로필 | EXE 경로, 작업 폴더, 종료 대상, 로그인/성공 창 제목, timeout, 정렬 규칙 | `%ProgramData%\RIL\server_profiles.json` | 각 장비 PC의 서버 설정 GUI |
| 사용자 화면 상태 | 마지막 선택 장비처럼 장비 정의와 무관한 개인 상태 | `%LocalAppData%\RIL\client_state.json` | 프로그램 자동 관리 |
| 로그인 정보 | ID/PW | 저장하지 않음 | 실행 시에만 입력 |

여러 클라이언트 PC의 장비 목록을 항상 동일하게 유지해야 한다면 이후 중앙 설정 서버나 공유 카탈로그를 추가한다. 첫 구현은 로컬 설정과 JSON 가져오기/내보내기를 권장한다.

## 클라이언트 카탈로그 예시

```json
{
  "schema_version": 1,
  "devices": [
    {
      "id": "au3",
      "name": "AU 3",
      "host": "10.2.151.219",
      "port": 2023,
      "profile_id": "au3",
      "legacy_command": "AU3",
      "enabled": true,
      "order": 30
    }
  ],
  "groups": [
    {
      "id": "au",
      "name": "AU만 선택",
      "members": ["au1", "au2", "au3"],
      "order": 30
    }
  ]
}
```

### 필드 규칙

- `id`: 장비 생성 후 변경하지 않는 내부 식별자다.
- `name`: 화면 표시명이며 자유롭게 변경할 수 있다.
- `host`, `port`: 해당 장비 PC의 RIL 서버 주소다.
- `profile_id`: 새 프로토콜에서 서버가 실행할 로컬 프로필 ID다.
- `legacy_command`: 구형 서버 호환 기간에 사용할 `INT`, `AU3`, `OC`, `Nova` 등의 코드다.
- `enabled`: 메인 화면 표시 및 실행 가능 여부다.
- `order`: 화면 배치 순서다.
- `groups[].members`: 선택 그룹에 포함되는 장비 ID 목록이다.

## 서버 실행 프로필 예시

```json
{
  "schema_version": 1,
  "profiles": {
    "au3": {
      "mode": "sequence",
      "continue_on_failure": true,
      "steps": [
        {
          "id": "order",
          "cwd": "C:\\Program Files (x86)\\LIS_Interface\\AU_3",
          "exe": "Ui.Kumc.GR.Interface.exe",
          "kill_process": "Ui.Kumc.GR.Interface.exe",
          "login_title": "로그인",
          "success_title_contains": "차세대 AU_3 INTERFACE",
          "timeout_sec": 15
        },
        {
          "id": "result",
          "cwd": "C:\\Program Files (x86)\\LIS_Interface\\AU_3_RSLT",
          "exe": "AU_RSLT.exe",
          "kill_process": "AU_RSLT.exe",
          "login_title": "로그인",
          "success_title_contains": "AU_3 Result INTERFACE",
          "timeout_sec": 15
        }
      ],
      "alignment": {
        "type": "left_right"
      }
    }
  }
}
```

서버 프로필은 다음과 같은 검증된 템플릿만 제공한다.

- `single`: 프로그램 한 개를 종료, 재실행, 로그인한다.
- `sequence`: 두 개 이상의 프로그램을 순서대로 실행한다.
- `remember_running_path`: 종료 전 실제 EXE 경로를 기억하고 같은 경로에서 재실행한다.
- `left_right`: 두 성공 창을 좌우로 정렬한다.

임의 Python, PowerShell, 명령줄을 클라이언트가 서버로 보내 실행하는 기능은 제공하지 않는다.

## 클라이언트 GUI

### 메인 화면

고정된 `checkBox_AU3`, `label_AU3` 위젯을 제거하고 설정에서 동적으로 행을 생성한다.

권장 열:

1. 선택 체크박스
2. 장비명
3. IP 또는 호스트
4. 실행 상태

`QTableView`와 `QAbstractTableModel` 조합을 우선 검토한다. 단순한 첫 구현은 `QScrollArea` 안에 동적 행을 생성해도 된다.

### 장비 관리 화면

- 추가
- 수정
- 복제
- 삭제
- 사용/미사용
- 위/아래 순서 변경
- 선택 그룹 지정
- 연결 테스트
- JSON 가져오기/내보내기

검증 항목:

- 비어 있거나 중복된 장비 ID
- 올바르지 않은 IP/호스트와 포트
- 존재하지 않는 `profile_id`
- 삭제된 장비를 참조하는 그룹
- 실행 중인 장비의 수정 또는 삭제

### 선택 그룹

현재의 라디오버튼은 상태 선택용 위젯인데 실제로는 체크박스를 변경하는 동작 버튼으로 사용되고 있다. 그룹 선택은 `QPushButton`으로 변경하고 하나의 공통 함수로 처리한다.

```text
select_only(group_id)
```

이 함수는 모든 장비의 선택 상태를 `device.id in group.members` 기준으로 다시 계산한다.

## 서버 설정 GUI

서버 트레이 메뉴에 `장비 실행 설정`을 추가하거나 별도 설정 도구를 제공한다.

편집 항목:

- 서버 프로필 ID
- 프로필 템플릿
- EXE 경로와 작업 폴더
- 종료할 프로세스
- 로그인 창 제목
- 성공 창 제목 포함 문자열
- 실행/로그인 timeout
- 실행 컨텍스트
- 창 정렬 방식

파일 선택과 창 제목 탐색 보조 기능을 제공하되, 저장 전 실제 EXE 존재 여부와 프로필 형식을 검증한다.

## 프로토콜 전환

새 프로토콜은 요청마다 UUID를 부여하고 같은 요청의 결과임을 확인할 수 있어야 한다.

```json
{
  "protocol_version": 3,
  "request_id": "UUID",
  "action": "login",
  "profile_id": "au3",
  "credentials": {
    "id": "...",
    "password": "..."
  }
}
```

응답 예시:

```json
{
  "protocol_version": 3,
  "request_id": "UUID",
  "status": "succeeded",
  "steps": [
    {"id": "order", "status": "succeeded"},
    {"id": "result", "status": "succeeded"}
  ]
}
```

전환 순서:

1. 새 서버가 기존 v2 요청과 새 v3 요청을 모두 처리하게 한다.
2. 서버를 먼저 수동 배포한다.
3. 새 클라이언트는 서버 기능을 확인하고 v3를 사용하며, 구형 서버에는 v2로 자동 전환한다.
4. 모든 서버 업그레이드 확인 후 v2 제거를 별도 검토한다.

## 설정 저장과 업데이트 보존

- 기본 카탈로그는 앱에 포함하되 외부 설정이 없을 때만 최초 생성한다.
- 업데이트 설치기는 `%ProgramData%\RIL`과 `%LocalAppData%\RIL`을 덮어쓰지 않는다.
- `schema_version`은 앱 버전과 별도로 관리한다.
- 저장 전 전체 설정을 검증한다.
- 같은 디렉터리의 임시파일에 쓴 뒤 `os.replace`로 교체한다.
- 직전 정상 설정을 `.bak`으로 보관한다.
- 로드 실패 시 조용히 기본값으로 덮지 말고 사용자에게 알린 뒤 백업 복구를 제공한다.

## 구현 순서

1. 통신 요청 ID, timeout, worker 수명주기 문제를 수정한다.
2. 현재 장비와 그룹을 그대로 표현하는 설정 모델과 기본 JSON을 만든다.
3. 설정 로드, 검증, 마이그레이션, 원자적 저장을 구현한다.
4. 메인 화면을 동적 장비 목록으로 변경한다.
5. 장비·그룹 관리 GUI를 추가한다.
6. 서버 프로필 loader와 기존 `StartTask*` adapter를 추가한다.
7. 서버 로컬 프로필 GUI를 추가한다.
8. 구형/신형 통신 호환 배포를 진행한다.

## 필수 테스트

- 현재 장비 13대와 기존 그룹 선택 결과가 동일한지
- 장비 추가·수정·복제·삭제·정렬
- 그룹 참조 무결성
- 잘못된 JSON과 백업 복구
- 업데이트 전후 사용자 설정 보존
- 구형 클라이언트 → 신형 서버
- 신형 클라이언트 → 구형 서버
- 신형 클라이언트 → 신형 서버
- 같은 장비에 요청 두 건이 겹쳐도 결과가 바뀌지 않는지
- 존재하지 않는 서버 프로필과 잘못된 실행 경로의 오류 표시

## 범위 구분

클라이언트 GUI 설정만으로 가능한 작업:

- 장비 표시명 변경
- IP와 포트 변경
- 장비 활성화/비활성화
- 표시 순서 변경
- 선택 그룹 생성과 편집
- 기존 실행 방식과 같은 새 장비 추가

서버 프로필 설정 또는 코드가 필요한 작업:

- EXE 이름과 설치 구조가 다른 장비
- 로그인 입력 순서가 다른 장비
- 성공 판정 창 구조가 다른 장비
- 세 개 이상의 프로그램 연동
- 별도 정렬 또는 장비별 예외 동작
