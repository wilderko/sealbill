# 7. Zaprite와 Request Finance에서 이전하기

이전에 Zaprite나 Request Finance를 썼다면 고객과 청구서 이력을 옮겨 와서 Sealbill을 모든 것을 찾을 수 있는 유일한 곳으로 만들 수 있습니다. 각 서비스에서 데이터를 꺼내는 방법을 포함한 단계별 절차는 [docs/migration.md](../../migration.md)에 있습니다. 이 장은 가져오기가 하는 일과 하지 않는 일을 설명합니다.

## 원칙: 이력이지 살아 있는 청구서가 아니다

**이전된 모든 청구서는 이미 결제된 것으로 기록됩니다.** 다른 곳에서 미결이던 것은 Sealbill에서 결제할 수 없으므로 고객이 두 번 결제하는 일은 없고 아무에게도 연락이 가지 않습니다. 각 청구서는 원래 번호, 발행일, 통화, 항목, 제목, 하단 메모를 그대로 유지합니다.

- 각 청구서는 정확히 하나의 결제를 받습니다. 방식은 *Recorded by hand*, 확인됨, 합계 전액이며, 날짜는 원본의 결제일(없으면 발행일)입니다. 결제에는 원본 서비스와 원래 번호를 명시한 메모가 붙습니다.
- 원본 상태(`COMPLETE`, `PAID`, `OVERPAID`, `PROCESSING`, `UNDERPAID`, `awaitingPayment` 등)는 직원용으로 보존되며 고객에게는 표시되지 않습니다. 청구서 CSV에는 **migrated_from** 열(예: `zaprite:COMPLETE`)이 있어서, 회계에서 고객이 실제로 낸 결제와 내가 결제된 것으로 선언한 청구서를 구분할 수 있습니다.
- 이전된 청구서는 **읽기 전용**입니다. 직원이든 API든 그 청구서에서 발송, 결제, 되돌리기, 무효 처리, 링크 재발급, 견적, 삭제를 할 수 없습니다. 해당 페이지는 출처를 알려 주고, 그 계정이 존재하는 동안 원본으로 연결합니다.
- **아무것도 발동하지 않습니다.** 가져오기는 이메일을 보내지 않고, 웹훅을 내보내지 않고, 견적이나 링크를 만들지 않으며, 조직의 다음 번호를 옮기지 않습니다.

## 연락처는 어떻게 되는가

연락처는 **조직 안에서 이메일 주소로 병합**됩니다(대소문자 구분 없음). Zaprite와 Request Finance 양쪽에 나오는 사람은 하나의 연락처가 되고, 데이터가 있는 쪽에서 채워집니다. 이메일이 없는 연락처는 유지되며 그것을 가리키는 청구서와만 연결됩니다. 이름으로는 절대 병합되지 않습니다. Zaprite와 Request Finance의 레코드는 지정한 조직으로 들어가며, 미국 거주자 발행자는 `--allow-us-issuer`를 추가하지 않으면 Zaprite 레코드를 거부합니다.

## 번호

원본의 번호는 유지됩니다. 조직에 이미 그 번호가 있으면(Zaprite와 Request Finance가 모두 단순한 번호를 썼습니다) 나중에 도착하는 쪽에는 원본 표시가 붙습니다: `591-RF`(또는 `-Z`). 시험 실행은 아무것도 쓰기 전에 모든 충돌을 나열합니다. 조직 자체 번호 체계의 형태이면서 다음 번호보다 작지 않은 번호는 거부되며 직접 판단해야 합니다.

## 가져오기 실행

가져오기 도구는 표준 입력에서 **줄 단위 JSON**(newline-delimited)을 읽습니다. 한 줄에 `contact` 또는 `invoice` 레코드 하나입니다.

```json
{"type": "contact", "source_id": "c1", "email": "old.client@example.com", "legal_name": "Old Client Ltd.", "address": {"line1": "1 Archive Road", "city": "Brno", "zip": "602 00", "country": "CZ"}}
{"type": "invoice", "source_id": "449", "number": "449", "kind": "invoice", "contact_source_id": "c1", "currency": "EUR", "issue_date": "2025-04-01", "paid_date": "2025-04-03", "due_days": 14, "title": "Consulting", "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 133700}], "total_minor": 133700, "source_status": "COMPLETE", "source_url": "https://app.zaprite.com/example/449"}
```

- 금액은 minor units 단위입니다. `total_minor`는 항목이 계산하는 값과 같아야 하며(항목별 세금과 할인은 그대로 옮겨집니다), 아니면 레코드가 거부됩니다.
- 연락처의 `address`에는 정확히 `line1`, `line2`, `city`, `zip`, `state`, `country`(두 글자 코드) 키만 있어야 합니다. 다른 키는 이름을 명시하며 거부되므로 필드가 아무도 모르게 사라지는 일이 없습니다.
- 각 청구서는 최종 상태로, 자기만의 트랜잭션 안에서 기록됩니다.

```bash
manage.py import_invoices --source zaprite --org example-studio --dry-run < export.ndjson
manage.py import_invoices --source zaprite --org example-studio           < export.ndjson
```

보관함은 잠금 해제된 상태여야 합니다. **항상 `--dry-run`부터 실행하세요.** 모든 것을 수행하고 되돌린 뒤 개수만 출력합니다.

```text
DRY RUN: nothing was written (source zaprite, org example-studio)
contacts: new 1, merged 0, without e-mail 0, skipped (already imported) 0
invoices invoice EUR: 1
invoices payment_request USD: 1
total EUR: 133700 minor units
total USD: 5000 minor units
skipped, already imported: 0
source status COMPLETE: 1
source status PAID: 1
number collisions: 0
refused lines: 1
  line 4 zaprite:bad: the lines do not add up to total_minor
```

- 출력에는 고객별 이름, 주소, 이메일, 금액이 절대 나오지 않습니다. 거부되거나 건너뛴 레코드는 `<source>:<source_id>`와 줄 번호로 표시되므로 내보내기 파일에서 찾을 수 있습니다.
- **다시 실행할 수 있습니다.** 원본 id가 이미 가져와진 청구서나 연락처는 건너뛰므로, 중간에 멈춘 실행은 그냥 다시 하면 됩니다. 동시에 두 번 실행하는 것은 거부됩니다.
- Request Finance의 초안, 취소, 거절된 청구서 레코드는 건너뜁니다. 아무도 그 금액을 빚지지 않았기 때문입니다.
- 감사 로그에는 실행당 개수만 담긴 항목 하나, 청구서당 출처와 id가 담긴 항목 하나가 남습니다.
- `--btc-check id,id`는 지정한 원본 id에 대해 사토시 금액과 원본 URL을 출력해, 비트코인 청구서를 장부와 대조할 수 있게 합니다.

가져온 뒤에는 **Invoices** 페이지에서 이전된 청구서 한두 건을 확인하고, 해당 기간의 청구서 CSV를 내려받으세요(8장). 시험 실행이 출력한 통화별 합계가 원본의 수치와 일치해야 합니다.
