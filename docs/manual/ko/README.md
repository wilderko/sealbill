# Sealbill 사용자 매뉴얼

Sealbill은 암호화폐, 은행 이체, 카드 결제 링크를 나란히 받으면서 고객 데이터를 저장 시 암호화해 두고 싶은 사람들을 위한 자체 호스팅 청구서 시스템입니다. 이 매뉴얼은 시스템을 운영하는 사람, 즉 청구서를 발행하는 직원과 설치를 맡은 운영자를 위한 것입니다.

언어: [English](../en/README.md) · [Slovenčina](../sk/README.md) · [Čeština](../cs/README.md) · [Deutsch](../de/README.md) · [Español](../es/README.md) · [Français](../fr/README.md) · [Polski](../pl/README.md) · **한국어** · [Русский](../ru/README.md)

## 장

1. [시작하기](01-getting-started.md): 설치, 보관함(vault), 로그인, 민감한 변경을 위한 재확인(step-up)
2. [설정](02-settings.md): 조직, 은행 계좌, 결제 채널(rails), API 토큰, 웹훅, 팀
3. [연락처](03-contacts.md): 연락처별 암호화 키 포함
4. [청구서](04-invoices.md): 작성, 발송, 고객용 페이지, 견적과 환율, 부분 결제와 초과 결제, 무효 처리, 링크 재발급, 알림
5. [결제 링크](05-payment-links.md)
6. [Needs a person](06-needs-a-person.md) (사람이 처리해야 하는 항목)
7. [Zaprite와 Request Finance에서 이전하기](07-migration.md)
8. [내보내기와 회계 통화](08-exports.md)
9. [API 간단 안내](09-api.md)

## 스크린샷을 보는 방법

- **직원용 페이지**(로그인 뒤의 모든 화면)는 고객이 어떤 언어를 쓰든 항상 영어입니다. 이 매뉴얼의 메뉴와 버튼 이름은 앱에 표시되는 그대로 적었습니다.
- **고객용 페이지**(청구서 페이지 `/i/…`, 결제 페이지 `/p/…`, 결제 링크 페이지, 잠금 해제 페이지, 이메일)는 en, sk, cs, de, es, fr, pl, ko, ru로 번역되어 있습니다. 고객에게는 연락처의 언어, 그다음 조직의 언어, 그다음 브라우저의 언어가 표시됩니다. 고객 링크 뒤에 `?lang=de`를 붙이면 다른 언어로 볼 수 있습니다.
- 스크린샷의 모든 이름, 이메일 주소, 지갑, 금액은 가상입니다. 일회용 데이터베이스에 만든 데모 조직 "Example Studio s.r.o."에서 가져온 것입니다.
- `코드` 서식의 텍스트는 직접 입력하거나 복사하는 값입니다. 명령어는 `manage.py …`로 표기합니다. Docker Compose에서는 `docker compose exec web python manage.py …`, systemd에서는 `payinv-manage …`입니다(저장소의 README 참조).
