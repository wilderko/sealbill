# 9. API 간단 안내

직원용 페이지가 청구서로 하는 모든 일을 다른 프로그램이 HTTPS 위의 JSON API로 할 수 있습니다. 기본 URL은 `https://<내 호스트>/api/v1`입니다. 설치된 서버가 항상 최신인 전체 문서를 직접 제공합니다.

| 주소 | 설명 |
|---|---|
| `/api/v1/docs` | 사람을 위한 문서(대화형 OpenAPI 페이지). |
| `/api/v1/schema` | 클라이언트 생성을 위한 OpenAPI 3.1 스키마. |
| `/llms.txt` | 프로그램과 AI 에이전트를 위한 일반 텍스트 레퍼런스: 모든 엔드포인트, 모든 오류, 웹훅 이벤트와 그 검증 방법. |

이 장은 시작하는 가장 짧은 길일 뿐입니다. 나머지는 이 세 가지와 README의 [Integrating another system through the API](../../../README.md#integrating-another-system-through-the-api)를 읽으세요.

## 인증

조직 하나를 위한 토큰을 만들고(2장 또는 `manage.py create_api_token --org <slug> --name <name> --scopes read,invoices:write`) bearer 토큰으로 보냅니다.

```bash
curl -H "Authorization: Bearer $TOKEN" https://pay.example.org/api/v1/me
# {"organization":"example-studio","token":"shop-integration","scopes":["read","invoices:write"]}
```

잘못되었거나 없거나 폐기된 토큰은 401, 부족한 scope는 403, 토큰당 분당 120건 초과는 429입니다. 보관함이 봉인된 동안에는 모든 호출이 503 `{"error": "vault_sealed"}`로 응답하므로 나중에 다시 시도하세요. 오류는 항상 `{"error": "<code>", "detail": …}` 형태입니다.

## 청구서 생성과 발송

금액은 **minor units 단위의 정수**(센트), 수량은 십진 문자열, 세율과 할인율은 베이시스 포인트(2000은 20 %)입니다. 생성할 때 `Idempotency-Key`를 보내면, 시간 초과 뒤 재시도가 두 번째 청구서를 만들지 않습니다.

```bash
curl -X POST https://pay.example.org/api/v1/invoices \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: order-1042" -H "Content-Type: application/json" \
  -d '{"contact": {"email": "anna@example.com"}, "currency": "EUR",
       "lines": [{"name": "Consulting", "quantity": "1", "unit_price_minor": 50000}]}'
```

이것은 **초안**을 만듭니다. 돈이 움직이지 않고 아무것도 발송되지 않습니다. 그다음 발송합니다.

```bash
curl -X POST https://pay.example.org/api/v1/invoices/14/send \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"email_client": false}'
```

응답에는 번호가 들어 있고, `view_url`과 `pay_url`은 **한 번만** 들어 있습니다. `email_client: false`는 청구서 이메일만 건너뜁니다. `client_letters: false`는 이 청구서에 관한 이메일(청구서, 영수증, 새 링크, 30일 알림)이 Sealbill에서 고객에게 절대 나가지 않는다는 뜻으로, 자기 고객에게 직접 연락하는 쇼핑몰을 위한 것입니다. 둘 다 이메일을 요구하면 서로 모순됩니다.

`GET /api/v1/payment-methods?currency=EUR&contact_email=client@example.com`은 어느 조직이 청구서를 발행할지(미국 규칙 적용)와 어떤 방법을 제공할지 알려 줍니다. 청구서는 `amount_paid_minor`와 `balance_due_minor`를 보여 주고, `GET /api/v1/payments?invoice=<id>`는 그 결제를 나열합니다.

## Scope 한눈에 보기

| Scope | 일반적인 용도 |
|---|---|
| `read` | 리포트, 대사, 내보내기 |
| `invoices:write` | 청구서를 발행하는 쇼핑몰 또는 CRM. 연락처, 키, 결제 링크 |
| `payments:confirm` | 결제를 승인·거부·되돌리기·할당하는 백오피스 도구. 돈의 상태를 바꿉니다. |
| `admin` | 위 전부와 웹훅 |

일부 동작은 **사람 전용**이며 `admin`을 포함한 모든 토큰이 `409 needs_person`을 받습니다. 수동 결제 기록, 그리고 사람을 기다리는 결제의 승인, 할당 또는 거부가 그렇습니다.

## 웹훅

폴링 대신 엔드포인트를 등록하세요(2장 또는 `admin` 토큰으로 `POST /api/v1/webhooks`). 각 전달은 서명된 `POST`입니다. `X-Pay-Signature`의 값을 비밀로 계산한 원본 본문의 HMAC-SHA256과 비교해 검증한 뒤에 신뢰하고, 10초 안에 2xx로 응답하며, `X-Pay-Event-Id`로 중복을 제거하세요. `/llms.txt`에 이벤트와 검증 예제가 있습니다.

## AI 에이전트용: MCP 서버

`mcp_server/`는 AI 에이전트가 Model Context Protocol로 API를 사용하게 해 주며, 도구는 여덟 개이고 결제 승인, 청구서 무효 처리, 웹훅 관리 권한은 **없습니다**. 토큰은 `read`와 `invoices:write`만으로 발급하세요. README에 설명이 있습니다: [MCP server](../../../README.md#mcp-server).
