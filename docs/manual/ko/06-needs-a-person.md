# 6. Needs a person

**Needs a person**은 첫 페이지이며 계속 지켜봐야 하는 페이지입니다. 어떤 프로그램도 스스로 결정해서는 안 되는 것들을 나열합니다. 막대에 열린 항목 수가 표시되며, 할 일이 없으면 페이지가 그렇게 알려 줍니다.

![항목이 있는 대기열](../img/needs-person.png)

대기열은 항목이 있을 때만 표시됩니다. **Show N queues**를 누르면 빈 대기열도 나열되어 이 페이지가 무엇을 감시하는지 볼 수 있습니다.

![모든 대기열](../img/needs-person-queues.png)

| 대기열 | 무엇이며 무엇을 할 것인가 |
|---|---|
| **Declared payments** | 고객이 청구서에서 "결제했습니다"를 눌렀습니다. 은행이나 체인을 확인하세요. 청구서에 결제를 기록하면 신고가 처리되고, 청구서의 **Dismiss declaration**은 거짓 신고를 닫습니다. |
| **Payments to review** | 어떤 청구서와도 일치하지 않았거나 잘못 일치한 돈과 그 사유: 토큰을 받지 않음, 일치하는 열린 청구서 없음, 둘 이상이 일치할 수 있음, 견적 만료 뒤 도착, 중복, 받을 금액보다 훨씬 많음, 청구서가 이미 닫혀 있었음, 매우 작은 "먼지" 이체(주소 포이즈닝 스팸일 가능성이 높아 접혀 있음), 버려진 트랜잭션, 또는 부분·지연·무효·수동 표시 청구서에 대한 BTCPay 보고. **Confirm**은 그 청구서에 반영하고, **Reject**는 메모와 함께 거부하며, **Assign to an invoice**는 청구서 번호, 금액, 메모로 할당합니다. 체인 이체는 최종 확정된 뒤에 처리할 수 있습니다. |
| **Waiting for staff confirmation** | Monero와 Zcash 결제(BTCPay 경유)와 수동 기록 금액: 아무것도 이를 감시하지 않으므로 지갑이나 은행을 확인한 뒤 **Confirm** 또는 **Reject**하세요. 사람만 확인할 수 있고 어떤 API 토큰도 할 수 없습니다. |
| **Gateway payments seen but not settled** | 게이트웨이가 6시간 넘게 전에 보았지만 정산하지 않은 BTCPay 또는 Lightning 결제. |
| **Refunds due** | 견적을 초과한 암호화폐 결제. 차액을 표시된 주소로 돌려보낸 뒤 메모와 함께 **Refunded**를 누르세요. |
| **Confirmed payments the gateway now calls invalid** | BTCPay가 정산한 뒤 무효(이중 지불, 리오그)라고 한 결제. 코드는 아무것도 되돌리지 않습니다. 청구서에서 결제를 되돌리거나 그대로 두세요. |
| **Exchange rates refused** | 환율의 피아트 구간이 오래되었거나 급변했습니다. ECB의 환율을 확인하고, 실제로 일어난 급변이면 **accept**하세요. 그동안 그 통화의 암호화폐 견적은 거부됩니다. |
| **Watchers that could not look** | 체인 또는 게이트웨이 리더가 10분 이상 실패하고 있습니다(직원에게도 이메일이 갔습니다). |
| **Transfers seen but not recorded** | 워처가 본 이체를 아직 기록하지 못했습니다. 매 순회마다 재시도됩니다. |
| **Blocks not scanned, or to check** | 블록 구간이 건너뛰어졌습니다. 직접 확인한 뒤 확인한 내용과 함께 **Acknowledge**하세요. |
| **Overpaid invoices** | 시스템 밖에서 환불하거나 **Keep the overpayment**(4장). |
| **US tax residents with invoices from another issuer** | 청구서를 무효 처리하고 다시 만드세요. 그러면 미국 조직이 발행합니다. |
| **Encryption keys that stopped working** / **expiring within 30 days** | 3장. |
| **E-mail not delivered** | 약 16시간 동안 실패한 메시지. **Retry**하거나 사유와 함께 **Dismiss**하세요(그러면 영구 취소됩니다). |
| **Payment links** | 결제된 주문이 링크의 한도를 넘겼거나, 발행자가 링크의 방법을 하나도 제공하지 않거나, 입력된 미국 관련 답변이 저장된 값과 모순되거나, 미국 발행자 조직이 없습니다. 확인한 내용과 함께 **Resolved**를 누르세요. |
| **Webhooks not delivered** | 모든 재시도 후에도 실패한 전달. **Retry** 또는 사유와 함께 **Dismiss**. |

페이지 전체에 두 가지 규칙이 적용됩니다.

- **사람을 기다리는** 결제는 로그인한 직원만 확인하거나 할당할 수 있습니다. 토큰은 `admin`이라도 `409 needs_person`을 받으며 아무것도 바뀌지 않습니다. 수동 결제를 기록하는 것도 사람의 일입니다.
- 중요한 곳에서는 모든 동작이 메모를 요구하며, 모든 동작은 감사 로그에 남습니다.

같은 대기열을 `payments:confirm`으로 API에서도 처리할 수 있으나, 사람 전용 항목은 제외됩니다(9장). 무언가가 *왜* 막혀 있는지 알아낼 때 쓰는 두 목록은 **Settings > E-mail outbox**(모든 이메일의 상태, 시도 횟수, 실패 사유. 수신자와 내용은 표시되지 않음)와 **Settings > Webhook deliveries**입니다.

![이메일 발신함](../img/outbox.png)

![웹훅 전달 내역](../img/deliveries.png)
