# 8. 내보내기와 회계 통화

막대의 **Exports**는 회계를 위한 CSV 파일 두 개를 제공합니다.

![Exports](../img/exports.png)

조직(아무것도 선택하지 않으면 전부), 선택적인 **From**과 **To** 날짜를 고른 뒤 내려받습니다.

- **invoices.csv**: 청구서당 한 행
- **payments.csv**: 결제당 한 행(상태 포함)

| invoices.csv | |
|---|---|
| `organization, number, kind, status, issue_date, due_date, contact, contact_email, currency` | 누구와 언제 |
| `subtotal, discount, tax, total, paid` | 청구서 통화 기준 |
| `accounting_currency, accounting_rate, accounting_total, rate_source` | 조직의 회계 통화로 환산한 같은 합계 |
| `external_reference, payment_link, migrated_from` | 출처: 내 시스템의 참조, 결제 링크, 또는 이전해 온 서비스 |

| payments.csv | |
|---|---|
| `organization, invoice_number, payment_id, state, rail, network, asset, amount` | 무엇이 어떻게 도착했는가(받은 자산 기준) |
| `invoice_currency, amount_invoice_currency, quote_rate` | 청구서 통화 금액과 결제 시 적용된 견적의 환율 |
| `accounting_currency, accounting_rate, accounting_amount` | 회계 통화로 환산한 같은 금액 |
| `tx_hash, log_index, detected_at, confirmed_at, confirmed_by, review_reason` | 증거: 트랜잭션, 감지·확인된 시각, 확인한 사람 |

## 회계 통화

각 조직에는 장부를 기록하는 통화인 **회계 통화**(Settings > Organizations, 기본값 EUR)가 있습니다. 회계 열은 **그날 캐시된 환율**로 모든 금액을 그 통화로 환산합니다. 청구서는 발행일, 결제는 확인일 기준입니다. 워커가 캐시를 매시간 채웁니다(`accounting_rates`). 캐시된 환율이 없는 날은 해당 셀이 **빈칸**으로 남으며, `rate_source`는 환율의 출처를 알려 줍니다(통화가 같으면 `identity`). 내보내기는 환율 서비스를 호출하지 않으므로 빠르고, 다시 실행해도 결과가 바뀌지 않습니다.

같은 파일을 `read` 토큰으로 API에서도 받을 수 있습니다: `GET /api/v1/exports/invoices.csv?from=2026-01-01&to=2026-12-31`, `GET /api/v1/exports/payments.csv?…`. 한 번에 최대 400일입니다.
