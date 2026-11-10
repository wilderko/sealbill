# 2. 설정

**Settings**는 막대의 마지막 항목입니다. 그 첫 페이지에는 아래의 모든 항목과 감사 로그, 이메일 발신함(E-mail outbox), 웹훅 전달 내역(6장에서 사용)이 나열됩니다.

![Settings 첫 페이지](../img/settings-index.png)

모든 변경은 **Audit log**(Settings > Audit log)에 기록됩니다. 누가, 무엇을, 어느 레코드를 바꿨는지만 남고 입력한 값은 남지 않습니다.

![감사 로그](../img/audit.png)

항목을 열면 상세 내용을 읽을 수 있습니다. 1장의 "재확인(step-up)" 절에 나열된 변경은 최근에 두 번째 인증을 거친 슈퍼유저가 필요합니다.

## 조직

**조직**은 청구서를 발행하는 법적 주체입니다. 이름, 주소, 세금 식별번호, 로고, 청구서 번호 체계, 그리고 그 조직에 속한 결제 계정을 가집니다. 두 회사가 하나의 설치를 공유할 때, 예를 들어 하나는 EU에, 하나는 미국에 있을 때 두 번째 조직이 유용합니다.

![조직 목록](../img/orgs-list.png)

**Settings > Organizations > New organization**을 누르거나 기존 조직을 열어 수정합니다.

| 필드 | 하는 일 |
|---|---|
| Slug | API와 명령어에서 쓰는 짧은 이름(`example-studio`). |
| Legal name, Address, Tax ids | 청구서의 "공급자" 블록에 인쇄됩니다. 세금 식별번호는 자유 텍스트입니다. |
| Email from | 조직 이메일의 발신자, 예: `Example Studio <billing@example.com>`. |
| Default language | 연락처에 선호 언어가 없을 때 고객 페이지와 이메일에 쓰는 언어. |
| Footer note, Logo | 청구서와 PDF에 인쇄됩니다. 로고는 1.9 MB 이하의 PNG 또는 JPEG이며 메타데이터 없는 PNG로 저장됩니다. |
| Number format | `{year}`와 `{seq}`를 쓰는 파이썬 방식. `{year}-{seq:04d}`는 `2026-0001`이 됩니다. `{seq}`를 포함해야 합니다. |
| Next number | 청구서를 발행하면 정해집니다. 번호가 다시 발급되지 않도록 수정할 수 없습니다. |
| Underpayment allowance | 암호화폐 결제가 이 비율(베이시스 포인트, 50은 0.5 %)과 이 고정 금액(미국 달러, 기본 1.00) 중 **더 작은** 값 이내로 부족하면 그래도 견적이 정산됩니다. 거래소의 출금 수수료를 흡수합니다. |
| Overpayment ceiling | 견적 금액을 이 비율(1000은 10 %)보다 더 많이 초과한 암호화폐 결제는 정산되지 않고 사람의 판단을 받도록 보류됩니다. |
| Accounting currency | 내보내기의 회계 열에 쓰는 통화(8장). |
| Hosts a Back button may lead to | 한 줄에 호스트 하나. API로 만든 청구서는 정확히 이 호스트들로만 `return_url`을 가질 수 있습니다. |

![조직](../img/org-edit.png)

### 미국 거주자 발행자

**US-resident issuer**는 최대 한 조직에만 표시하세요. 미국 세법상 거주자인 고객의 청구서를 발행해야 하는 조직입니다.

- 모든 연락처에는 **US tax resident** 답변(yes, no, unknown)이 있습니다. 청구서 양식은 선택을 강제하고, 결제 링크는 구매자에게 묻습니다.
- 다른 조직이 미국 세법상 거주자인 연락처의 청구서를 요청하면, 청구서는 **발행자 조직이 발행**하며 그 조직의 번호 체계, 은행 계좌, 채널을 씁니다. 발행자는 연락처의 자체 사본을 보관하며 내 연락처는 절대 덮어쓰이지 않습니다.
- 다른 조직이 청구서를 발행한 뒤 연락처가 미국 거주자가 되면, 그 청구서는 Needs a person의 **US tax residents with invoices from another issuer** 대기열에 올라옵니다. 무효 처리하고 다시 만드세요.
- 표시된 조직이 없으면 이런 청구서는 만들 수 없습니다.

표시나 복귀 호스트를 바꾸는 것은 재확인이 필요한 변경입니다. 표시는 감사 로그에 남고 슈퍼유저에게 이메일로 알려집니다.

## 은행 계좌와 피아트 링크

조직 하단에 두 개의 목록이 있습니다. 둘 다 **비활성화만 가능하고 삭제되지 않습니다**. 발행된 청구서는 계속 그것이 지정한 계좌를 표시합니다.

![조직의 은행 계좌와 결제 링크](../img/org-accounts.png)

**Add bank account**는 레이블, 통화, 수취인, IBAN 또는 계좌번호, 은행, SWIFT/BIC, 선택적으로 중개 은행을 요구합니다. 은행 계좌는 **자기 통화**의 청구서에서만 제공됩니다. IBAN을 입력하면 결제 페이지와 PDF 청구서에 은행 QR 코드도 표시됩니다. EUR는 SEPA 국가의 IBAN일 때, CZK는 체코 계좌일 때입니다(4장). *결제 안내*는 언어별로 하나씩 있으며, 결제 페이지는 독자의 언어로 된 것을 보여 주고 없으면 영어로 대체합니다. 생략하려면 해당 언어를 비워 두세요.

![새 은행 계좌](../img/bank-new.png)

**Add payment link**(*피아트 링크*, 5장의 공유 가능한 페이지와 혼동하지 마세요)는 다른 곳에서 결제를 받는 서비스로 가는 링크입니다. Revolut, N26, PayPal, Wise 또는 "other"가 있습니다. `https://` 주소와 받을 수 있는 통화가 필요합니다. 결제 페이지는 이것을 **온라인 결제** 아래에 보여 줍니다.

![새 피아트 링크](../img/fiat-new.png)

## 결제 채널(rails)

**Rail account**는 암호화폐나 Lightning으로 결제를 받는 방법 하나입니다. 활성 계정은 해당 조직의 새 청구서마다 제공됩니다(청구서가 직접 방법을 지정하지 않는 한). **Settings > Rail accounts**를 여세요.

![Rail accounts](../img/rails-list.png)

**New rail account**는 먼저 조직과 종류를 묻습니다.

![종류 선택](../img/rail-new-choose.png)

| 종류 | 필요한 것 |
|---|---|
| **evm** | 주소 하나, 감시할 네트워크(Ethereum, Arbitrum One, Optimism, Base, Polygon PoS, BNB Smart Chain, Avalanche C-Chain), 받을 자산(ETH, USDC, USDT). |
| **tron** | 주소 하나, USDT. Tron 네트워크 전체를 감시합니다. |
| **solana** | 주소 하나, USDC와 USDT. |
| **ton** | 현재는 꺼져 있습니다. TON 결제가 도착했음을 아직 증명할 수 없어서 비활성으로만 저장할 수 있습니다. |
| **btcpay** | BTCPay의 `URL`과 `Store ID`, 자산(BTC, XMR, ZEC), 속도 정책: `MediumSpeed`(1회 확인, 기본값), `LowMediumSpeed`(2회), `LowSpeed`(6회). `HighSpeed`는 거부됩니다. |
| **lightning** | 백엔드: `nwc`(Nostr Wallet Connect, 예: Alby Hub), `lnd`, `cln`, `lnbits`. 뒤의 셋은 `URL`도 필요합니다. 자산은 BTC. |

![새 EVM 계정](../img/rail-new-evm.png)

![새 Lightning 계정](../img/rail-new-lightning.png)

알아 둘 점:

- **이 시스템만 쓰는 주소를 사용하세요.** 체인 결제는 **금액**으로 청구서와 매칭됩니다. 각 견적은 기본 금액에 소량의 "먼지(dust)" 단위를 더해 요청하며, 이 값은 그 주소를 점유한 견적들 사이에서 고유합니다. 다른 이유로 그 주소에 보낸 돈은 Needs a person에 올라옵니다.
- 채널 URL은 공개 주소로만 해석되는 호스트의 `https://` 주소여야 합니다. 루프백이나 사설 주소는 거부됩니다(같은 기기의 LND 또는 CLN만 `LOCAL_LIGHTNING_PORT`에서 예외입니다).
- 고객이 아직 견적을 결제할 수 있는 동안에는 계정의 조직, 종류, 주소, 네트워크, 자산을 바꿀 수 없습니다. 대신 새 계정을 만드세요. **Deactivate**는 제공을 멈추지만 이미 표시된 견적은 해제될 때까지 계속 감시됩니다.
- *Watcher* 열은 워커가 그 체인이나 게이트웨이를 읽을 수 있는지 알려 줍니다.

![BTCPay 계정](../img/rail-edit-btcpay.png)

### 자격 증명

BTCPay와 Lightning의 비밀은 보관함 키로 암호화되며 **다시는 표시되지 않습니다**. 계정을 열고 **Set credentials**(보관함이 잠금 해제되어 있어야 함)를 누르거나, 셸에서 `manage.py set_rail_credentials <rail 계정 id>`를 쓰세요. 필드를 비워 두면 저장된 값이 유지됩니다.

시스템은 **절대 돈을 쓰지 않으므로**, 쓸 수 없음이 입증되는 자격 증명만 저장합니다.

| 백엔드 | 붙여 넣을 것 |
|---|---|
| BTCPay | `btcpay.store.canviewinvoices`와 `btcpay.store.cancreateinvoice`만 가진 `api_key`(키가 무엇을 할 수 있는지 BTCPay에 물어봅니다)와 `webhook_secret`. **Register the BTCPay webhook** 버튼이 BTCPay에 웹훅을 만들고 그 비밀을 대신 저장합니다. |
| Lightning, NWC | `make_invoice`와 `lookup_invoice`만 있고 `pay_*` 메서드가 **없는** 연결의 `nwc_url`. |
| Lightning, LND | `invoices:read invoices:write info:read`로 만든 `macaroon`(LND 자체의 `invoice.macaroon`은 거부됩니다). |
| Lightning, Core Lightning | `invoice`와 `listinvoices`로 제한한 `rune`. |
| Lightning, LNbits | 지갑의 **Invoice/read key**. Admin key는 절대 안 됩니다. 확인했다는 체크박스를 선택하세요. |

![BTCPay 계정의 자격 증명](../img/rail-credentials-btcpay.png)

![Lightning 계정의 자격 증명](../img/rail-credentials-lightning.png)

채널 URL의 호스트를 바꾸면 저장된 자격 증명이 지워지고 채널이 비활성화되어, 키가 다른 서비스로 전송되는 일이 없습니다. README([Crypto rails](../../../README.md#crypto-rails))에는 백엔드별 세부 사항, 자체 RPC 엔드포인트용 환경 변수, 워처가 보지 못하는 것이 있습니다.

## API 토큰과 범위(scope)

**Settings > API tokens**. 토큰은 **한 조직**에 속하며 그 조직의 데이터만 봅니다. 생성할 때 **한 번만** 표시됩니다. 분실했다면 폐기하고 새로 만드세요. 폐기는 영구적이며 행은 남습니다.

![토큰](../img/tokens.png)

| Scope | 허용하는 것 |
|---|---|
| `read` | 모든 GET. |
| `invoices:write` | 연락처와 그 암호화 키 생성·수정, 청구서 생성·발송·무효 처리, 링크 재발급, 결제 링크 관리. |
| `payments:confirm` | 결제 승인·거부·되돌리기·할당, 환불 확인, 신고 기각. **돈의 상태를 바꿉니다.** 수동 결제는 기록할 수 없고 사람만 할 수 있습니다. |
| `admin` | 위의 모든 것과 웹훅 엔드포인트. |

토큰에는 필요한 최소한의 범위만 주세요. 청구서를 만들고 보내기만 하는 연동에는 `read`와 `invoices:write`면 충분합니다. 생성, 범위 변경, 폐기는 재확인이 필요한 변경입니다.

![한 번만 표시되는 토큰](../img/token-created.png)

셸에서도 발급할 수 있습니다: `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`.

## 웹훅

**Settings > Webhooks**는 조직의 청구서와 결제에 일어나는 일을 통보받을 엔드포인트를 등록합니다.

![웹훅](../img/webhooks.png)

- 주소는 공개 주소로만 해석되는 호스트의 443 포트 `https://`여야 합니다. (개발 환경에서는 `WEBHOOK_ALLOW_HTTP`와 `WEBHOOK_ALLOW_PRIVATE`로 완화할 수 있습니다.)
- 원하는 이벤트를 선택하세요. 아무것도 선택하지 않으면 전부입니다: `invoice.created`, `.sent`, `.voided`, `.declared`, `.partially_paid`, `.paid`, `.overpaid`, `.links_reissued`, `payment.detected`, `.confirmed`, `.needs_review`, `.rejected`, `.reversed`, `staff.alert`, `ping`.
- **비밀**은 한 번만 표시됩니다. 각 전달은 `X-Pay-Signature: sha256=<hex>`가 붙은 서명된 `POST`이며, 이는 그 비밀로 원본 본문에 계산한 HMAC-SHA256입니다. 호출을 신뢰하기 전에 검증하세요. 본문에는 id와 상태만 있고 이름, 주소, 금액은 절대 없습니다. 나머지는 API로 읽으세요.
- 전달은 2, 5, 15, 30, 60, 120, 240, 480분 뒤에 재시도된 후 실패로 표시됩니다. 직원에게 한 번 알려지며 Needs a person의 **Webhooks not delivered**에 올라옵니다.
- 엔드포인트 페이지에는 **Send a ping**, 최근 전달 내역, **Delete**가 있습니다. Settings > Webhook deliveries는 모든 전달을 **Retry now**와 함께 나열합니다.

![엔드포인트와 전달 내역](../img/webhook-detail.png)

이벤트 전체 목록과 검증 코드는 설치된 서버의 `/llms.txt`에 있습니다(9장).

## 팀

**Settings > Team**은 로그인할 수 있는 모든 사람을 두 번째 인증 수단의 이름과 마지막 로그인과 함께 보여 줍니다. 슈퍼유저는 동료의 분실된 수단을 삭제할 수 있습니다(재확인이 필요한 변경이며 모든 슈퍼유저에게 이메일로 알려집니다). 그러면 누군가 서버에서 새 인증 앱을 등록할 때까지 그 동료는 로그인할 수 없습니다. 자신의 마지막 수단은 삭제할 수 없습니다.

![팀](../img/team.png)
