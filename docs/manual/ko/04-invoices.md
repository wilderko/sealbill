# 4. 청구서

막대의 **Invoices**는 청구서를 최신순으로 나열하며, 상태별·조직별 필터와 번호 또는 외부 참조로 검색하는 기능이 있습니다.

![청구서 목록](../img/invoices.png)

청구서는 다음 상태 중 하나입니다.

| 상태 | 의미 |
|---|---|
| `draft` | 발송 전. 번호가 없고 아직 수정하거나 삭제할 수 있습니다. |
| `sent` | 번호가 붙고 링크가 있으며, 입금을 기다리는 중. |
| `overdue` | 발송되었고 만기가 지남(워커가 상태를 바꿉니다). |
| `declared` | 고객이 "결제했습니다"를 눌렀음. 이것은 주장일 뿐 돈이 아닙니다. |
| `partially_paid` | 확인된 결제가 합계의 일부를 채움. |
| `paid` | 확인된 결제가 합계를 채움. |
| `overpaid` | 확인된 결제가 합계를 초과함. |
| `void` | 무효 처리됨. 더 이상 결제할 수 없습니다. |

**확인된** 결제만 계산에 들어갑니다. 발송된 청구서는 더 이상 수정할 수 없습니다. 내용은 공급자, 구매자, 결제 정보를 포함해 발송하는 순간 고정되므로, 이후에 연락처나 계정을 바꿔도 이미 고객 손에 있는 청구서는 달라지지 않습니다.

## 청구서 작성

**New invoice**는 누구에게, 무엇을, 어떻게 결제받을지 묻습니다. 초안에서 Send를 누르기 전에는 아무것도 발송되지 않습니다.

![새 청구서 양식](../img/invoice-new.png)

- **Requesting organization**과 **Type**: *Invoice*는 조직의 번호 체계에서 번호를 받고, *Payment request*(결제 요청서)는 번호가 없으며 법적 청구서 문서가 아닙니다.
- **Contact e-mail**: 기존 연락처는 이것으로 찾고, 없으면 아래 필드로 새로 만듭니다. **US tax resident**(yes, no, unknown)는 신중히 답하세요. "Choose…"는 받아들여지지 않습니다. yes를 고르면 청구서가 미국 발행자에게 갑니다(2장). 그러면 페이지에 "Issued by …"가 표시됩니다.
- **Currency**, **Issue date**, **Due in (days)**(비우면 수령 즉시 만기), **Title**, **Notes**(청구서에 인쇄됨).
- **Lines**: 이름, 선택적 설명, 수량, 기본 단위의 단가(`12.50`), 세율 %, 할인율 %. 빈 행은 무시됩니다.
- **Payment methods**: 통화에 맞는 발행자의 활성 방법이 모두 선택되어 있습니다. 그 통화의 은행 계좌, 그 통화를 받는 피아트 링크, 모든 rail 계정(견적이 환산하므로 어떤 통화든 가능)입니다. 제공하고 싶지 않은 것은 선택을 해제하세요.

**Create draft**가 저장합니다. 초안 페이지는 항목과 합계를 보여 주고, **Edit this draft**와 **Delete draft**, 그리고 발송하는 상자가 있습니다.

![발송 준비가 된 초안](../img/invoice-draft.png)

## 발송

**Send**는 청구서에 번호를 매기고 고정하며 두 개의 **권한 링크**(capability link)를 만듭니다. 하나는 보기용(`/i/…`), 하나는 결제용(`/p/…`)입니다. 이 링크는 비밀이므로 해시로만 저장되고 한 번만 표시됩니다.

- **E-mail the client when sent**를 선택하면 청구서 이메일이 발신함에 들어가며(워커가 연락처의 언어로 PDF를 첨부해 발송) 두 링크가 함께 들어갑니다. 연락처에 활성 암호화 키가 있으면 이메일은 그 키로 암호화됩니다.
- 선택하지 **않으면** 아무것도 발송되지 않고 링크는 직접 전달합니다. 링크를 보려면 청구서를 열고 **More actions > Reissue the client links**(아래)를 누르세요. 페이지가 두 링크를 한 번 보여 줍니다.

![발송된 청구서](../img/invoice-sent-detail.png)

## 고객이 보는 것

**청구서 페이지** `/i/…`는 문서, 내려받을 PDF, **암호화된 이메일 받기**(3장)를 보여 줍니다. **결제 페이지** `/p/…`는 결제할 금액과 결제 방법을 보여 줍니다. 연락처 또는 브라우저의 언어로 표시됩니다.

![결제 페이지](../img/portal-pay-ko.png)

결제 상자에는 최대 세 개의 탭이 있으며 **암호화폐가 먼저**입니다.

- **암호화폐로 결제**: 고객이 네트워크와 토큰을 고릅니다. 예: "USDC on Arbitrum One".
- **계좌 이체**: 청구서 통화의 은행 계좌(수취인, 계좌번호, 은행, 금액이 각각 복사 버튼과 함께), **결제 참조번호**(`RF3120260001` 같은 ISO 11649 참조)와 고객 언어로 된 안내. SEPA 국가의 IBAN으로 EUR를 받으면 QR 코드 세 개(PAY by square, QR Platba, EPC QR)가, 체코 IBAN으로 CZK를 받으면 QR Platba 코드가 함께 표시됩니다. 고객이 은행 앱에서 하나를 스캔하면 계좌, 남은 금액, 참조번호가 채워집니다. PDF 청구서에도 같은 코드가 인쇄됩니다.
- **온라인 결제**: 피아트 링크(Revolut, PayPal 등).

![계좌 이체](../img/pay-bank.png)

![온라인 결제](../img/pay-online.png)

아홉 개 언어를 모두 지원합니다. 슬로바키아어, 체코어, 독일어, 스페인어, 프랑스어, 폴란드어, 한국어, 러시아어로 된 같은 페이지는 이 장의 끝에 있습니다.

### 견적과 환율

고객은 암호화폐로 "청구서 금액"을 내는 것이 아니라 **견적**(quote)을 결제합니다.

- 네트워크와 토큰을 고르면 환율이 고정되고 보낼 **정확한 금액**, 주소, QR 코드, "Open in wallet" 링크가 표시됩니다. 금액은 **올림**으로 계산되어 모자라는 일이 없습니다.
- 견적은 약 **30분** 유효하며 페이지가 카운트다운하고, **새 금액 받기**로 다른 견적을 발급받습니다.
- 공유 주소에서는 금액에 **견적마다 고유한** 소량의 "먼지" 단위가 붙습니다. 이것이 결제를 청구서와 매칭하는 방법이므로, 고객은 그 네트워크에서 그 토큰으로 정확히 그 금액을 보내야 합니다.
- 환율은 서로 교차 검증하는 두 출처(Kraken과 Coinbase)에서 가져옵니다. 둘이 1 % 넘게 차이 나면 견적이 만들어지지 않습니다. EUR에서 다른 법정통화로 가는 단계는 Frankfurter를 통한 ECB 환율을 씁니다. 오래되었거나 갑자기 튄 환율은 그 통화의 견적을 거부하게 하며 Needs a person의 *Exchange rates refused*에 나타나고, 사람이 실제로 일어난 급변을 수락할 수 있습니다.
- 이체가 감지되면 페이지가 스스로 갱신됩니다. 워커가 체인을 감시하고, Lightning과 BTCPay 결제는 각 게이트웨이에서 읽습니다.

![견적](../img/pay-quote.png)

**결제했습니다** 상자에서 고객은 결제를 알릴 수 있습니다(네트워크, 트랜잭션 해시, 메모, 모두 선택 사항). 청구서는 `declared`가 되고 Needs a person에 나타나며, 입력된 해시는 체인에서 조회됩니다. 신고는 주장일 뿐 돈이 아닙니다. 그것만으로는 아무것도 정산되지 않습니다.

![고객의 신고가 있는 청구서](../img/invoice-declared.png)

## 수동으로 결제 기록

은행 이체나 온라인 결제는 Sealbill이 볼 수 없는 곳에 도착합니다. **내가** 돈이 들어온 것을 확인했다면 청구서를 열고 **Record payment**를 누르세요. 금액, 방식(*Bank transfer*, *Payment link*, *Recorded by hand*), 메모를 입력합니다. 즉시 확인되어 계산에 반영됩니다. 사람만 할 수 있으며 어떤 API 토큰도 할 수 없습니다.

![Record payment](../img/invoice-record-payment.png)

잘못 기록했거나 반환한 결제는 그 행의 **Reverse**로 되돌리며, 사유가 필요합니다.

## 부분 결제와 초과 결제

**부분 결제.** 확인된 결제가 합계의 일부만 채우면 청구서는 `partially_paid`가 됩니다. 고객은 도착한 금액과 남은 금액을 알리는 이메일을 받고, 결제 페이지는 **남은 금액**에 대한 견적을 제공합니다. 부분 결제된 청구서는 무효 처리할 수 없습니다.

![부분 결제된 청구서](../img/invoice-partial.png)

**초과 결제.** 청구서는 두 가지 방식으로 초과 결제될 수 있고, 둘 다 결국 사람 앞에 놓입니다(6장).

- *합계보다 많은 금액이 기록됨*(은행 이체 또는 직접 기록한 결제): 청구서는 `overpaid`가 되고 **Overpaid invoices** 대기열에 올라옵니다. 차액을 시스템 밖에서 환불하거나, **Keep the overpayment**를 누르고 이유를 입력하세요. 청구서는 돈이 말하는 대로 `overpaid`로 남고, 결정, 그것이 포괄하는 금액, 메모가 감사 로그에 기록되며, 더 많은 돈이 들어올 때까지 항목은 대기열에서 빠집니다.

  ![초과 결제를 유지함](../img/invoice-overpaid-kept.png)

- *견적보다 많은 암호화폐 결제*: 청구서는 결제 완료 상태가 되고 결제에는 **refund due**가 생기며, **Refunds due**에 금액과 보낸 주소와 함께 나열됩니다. 돌려보냈거나 돌려보내지 않기로 결정했다면 **Refunded**를 누르고 메모를 남기세요. 견적보다 훨씬 많은 결제(조직의 *Overpayment ceiling* 초과)는 정산되지 않고 **Payments to review**에서 대기합니다.

  ![초과 결제된 청구서](../img/invoice-overpaid.png)

## 무효 처리

**More actions**(점 세 개)에는 사유가 필수인 **Void invoice**가 있습니다. 무효 처리된 청구서는 더 이상 결제할 수 없습니다. 열려 있는 견적과 신고가 닫히고, 고객 페이지에는 무효임이 표시됩니다. 결제가 확인되는 중이거나 이미 돈이 확인된 경우에는 거부됩니다(먼저 그 결제를 되돌리세요). `paid`, `partially_paid`, `overpaid` 청구서는 무효 처리할 수 없습니다. 무효 처리된 청구서로 나중에 도착한 돈은 사라지지 않고 "The invoice was already closed when the payment arrived."라는 사유로 **Payments to review**에 올라옵니다.

![More actions: 링크 재발급과 무효 처리](../img/invoice-more-actions.png)

## 링크 재발급

고객이 이메일을 잃어버렸거나, 이메일 없이 발송해서 링크가 필요할 때: **More actions > Reissue the client links**.

![링크 재발급](../img/invoice-links.png)

재발급은 **두 링크를 모두 교체**합니다. 이미 고객 메일함에 있는 것을 포함해 이전 링크는 작동을 멈추고, 이전 링크가 담긴 채 아직 발송되지 않은 이메일은 취소됩니다. **E-mail the new links to the client**를 선택하면 이메일이 대기열에 들어가고, 그렇지 않으면 페이지가 새 링크 둘을 **한 번만** 보여 주며 "Nothing was e-mailed: pass these on yourself."라고 알려 줍니다. 초안이나 무효 처리된 청구서는 링크를 재발급할 수 없습니다. 웹훅 이벤트 `invoice.links_reissued`는 이전 링크를 저장해 둔 연동 시스템에 그 링크가 죽었음을 알려 줍니다.

![한 번만 표시되는 새 링크](../img/invoice-links-reissued.png)

## 30일 뒤의 알림

시스템이 청구서 이메일을 발송했고 청구서가 발송 **30일** 뒤에도 미결제(`sent`, `overdue`, `partially_paid`)이면 워커가 알림을 **딱 한 번** 보냅니다. 알림은 위와 같이 **두 링크를 모두 교체**합니다.

- `INVOICE_REMINDER_DAYS`가 일수를 정하며 `0`이면 알림이 꺼집니다.
- 이메일 없이 발송한 청구서, API에서 `client_letters: false`로 발송한 청구서, 결제 링크의 모든 주문, `declared` 청구서, 열린 견적이 있거나 결제가 감지되었거나 검토 중인 청구서, 미국 외 발행자가 발행한 미국 거주자의 청구서("wrong issuer" 대기열에 있음), 암호화 키가 실패한 연락처에는 건너뜁니다. 건너뛴 청구서는 다음 실행 때 다시 검토합니다.
- 이 작업은 워커가 시작될 때 실행되고 이후 24시간마다 실행되므로, 중단되었던 워커는 조건에 맞는 모든 것을 청구서의 나이와 관계없이 따라잡습니다. 시작할 때 보관함이 봉인되어 있으면 아무것도 하지 않고 24시간 뒤에 다시 시도합니다.

## 아홉 개 언어로 본 같은 페이지

각 언어는 데모 청구서의 청구서 페이지와 결제 페이지를 보여 줍니다. 청구서 자체의 텍스트(항목, 메모)는 입력한 언어로 표시되고, 그 둘레의 틀이 번역됩니다.

| | 청구서 페이지 | 결제 페이지 |
|---|---|---|
| English | [이미지](../img/portal-invoice-en.png) | [이미지](../img/portal-pay-en.png) |
| Slovenčina | [이미지](../img/portal-invoice-sk.png) | [이미지](../img/portal-pay-sk.png) |
| Čeština | [이미지](../img/portal-invoice-cs.png) | [이미지](../img/portal-pay-cs.png) |
| Deutsch | [이미지](../img/portal-invoice-de.png) | [이미지](../img/portal-pay-de.png) |
| Español | [이미지](../img/portal-invoice-es.png) | [이미지](../img/portal-pay-es.png) |
| Français | [이미지](../img/portal-invoice-fr.png) | [이미지](../img/portal-pay-fr.png) |
| Polski | [이미지](../img/portal-invoice-pl.png) | [이미지](../img/portal-pay-pl.png) |
| 한국어 | [이미지](../img/portal-invoice-ko.png) | [이미지](../img/portal-pay-ko.png) |
| Русский | [이미지](../img/portal-invoice-ru.png) | [이미지](../img/portal-pay-ru.png) |

![독일어 청구서 페이지](../img/portal-invoice-de.png)

![한국어 결제 페이지](../img/portal-pay-ko.png)

다른 상태의 청구서는 고객에게 이렇게 보입니다.

![결제 완료된 청구서](../img/portal-paid.png)

![무효 처리된 청구서](../img/portal-void.png)
