# RIL

현재 유지·빌드하는 파일은 날짜가 붙지 않은 다음 파일이다.

- 클라이언트: `RIL_client.py`, `RIL_client.spec`
- 서버: `RIL_server.py`, `RIL_server.spec`
- 인터페이스 자동화: `IAL.py`
- 통합 설정·버전: `ril_config.json`

`RIL_client260xxx.py`, `RIL_server260xxx.py`, `IAL backup.py` 등 날짜나
backup이 붙은 파일은 과거 참고본이며 빌드에 사용하지 않는다.

설정 방법은 [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md), 빌드와
배포는 [`RELEASE.md`](RELEASE.md)를 참고한다.

테스트, 서버·클라이언트 빌드, GitHub 업로드와 업데이트 활성화는
다음 원클릭 명령으로 처리한다.

```powershell
.\scripts\build_and_publish.ps1
```
