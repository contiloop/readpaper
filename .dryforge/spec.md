# P0 — ReadPaper 완제품 MVP 명세

## 목표와 성공의 의미

P0의 산출물은 시험용 조각이나 후속 구현을 전제로 한 프로토타입이 아니라, 사용자가 Codex에서 실제로 호출해 논문 한 편을 끝까지 읽고 후속 질문을 이어갈 수 있는 완제품 MVP다.

사용자가 논문 출처를 주면 현재 Codex 작업의 Main이 논문 전체를 직접 읽고, 모든 페이지의 시각 내용을 확인하며, 논문 전체의 구조와 주장·근거 관계를 형성해야 한다. 이후 독립 검토 결과를 원문과 다시 대조하고, 같은 작업에서 페이지·그림·표·수식에 근거한 설명과 질의응답을 계속할 수 있어야 한다.

이 제품이 줄이려는 실패는 단순한 페이지 누락만이 아니다. 정의·방법·실험·결론을 유창하게 잘못 설명하고도 그 오류를 숨기는 실패를 줄이고 드러내는 것이 핵심이다. 따라서 기계적으로 증명할 수 없는 이해 상태를 성공으로 인증하지 않으며, 관측 불가·접근 불가·해석 불일치를 명시적으로 노출한다.

## 제품 경계

### 지원 입력

- HTTP(S)로 접근 가능한 직접 PDF URL
- 공개 논문 랜딩 페이지에서 확인할 수 있는 PDF
- 사용자가 지정한 로컬 PDF
- 위 출처에서 공개적으로 연결된 supplementary 중 PDF, 일반 텍스트, 일반 이미지
- ZIP supplementary는 경로 탈출과 과도한 확장을 차단한 뒤, 위의 지원 형식만 안전하게 추출하여 읽는다.

로그인·유료 접근을 우회하지 않는다. 손상된 PDF, 본문을 읽기 위해 OCR이 필요한 스캔 PDF, 코드·대형 데이터셋·노트북·음성·영상·인터랙티브 supplementary는 P0에서 해석하지 않는다. 미지원 자료는 출처와 형식을 목록화하고, 해당 자료가 남아 있는 동안 `supplementary 전체 확인 완료` 또는 제한 없는 `논문 전체 완료`라고 표시하지 않는다. 지원되는 본문에 대한 제한된 독해와 질의응답은 미지원 범위를 매번 드러내는 조건으로 허용한다.

### 작업 단위

- `paper_id`는 main PDF 바이트의 SHA-256 전체를 `p_<64 lowercase hex>`로 표현한 불변 식별자다. main PDF 해시가 다르면 반드시 다른 paper ID이며 과거 이해 노트·검토·인용을 자동 승계하지 않는다.
- 각 확보 파일은 바이트 해시 기반 `artifact_id`를 가진다. 동일 바이트는 같은 artifact지만, 발견 URL·역할·archive 경로가 다른 occurrence는 별도 `artifact_ref_id`를 가진다.
- `artifact_ref_id`는 role, 정규화 discovery URL 또는 local source token, parent artifact ID, archive member path를 canonical JSON으로 hash한 `r_<64 lowercase hex>`다.
- `bundle_id`는 아래 artifact record 중 `prepared_at`과 진단 warning을 제외한 identity 필드들을 `artifact_ref_id` 순으로 정렬한 canonical JSON에 `schema_version`, `paper_id`, `landing_url`을 더해 SHA-256을 계산한 `b_<64 lowercase hex>`다. main PDF가 같아도 supplementary의 바이트·지원 상태·발견 집합이 바뀌면 새 bundle이며 이전 bundle을 수정하지 않는다.
- prepare는 공개 랜딩 페이지의 현재 snapshot에서 `supplementary`, `supporting information`, `additional file`로 표시된 직접 링크만 따라가고, 일반 참고문헌·코드 저장소·임의 외부 링크를 supplementary로 자동 승격하지 않는다. 최종 redirect URL과 탐지된 실제 media kind를 고정한 뒤 discovery를 닫는다.
- 동일 bundle을 다시 준비하면 불변 원본을 재사용할 수 있지만 새 독해 시도는 별도 `run_id`로 기록한다.
- `run_id`는 state lock 안에서 CSPRNG UUIDv4를 만들고 충돌 여부를 확인한 `run_<32 lowercase hex>`다. audit/execution/note/draft의 `*_seq`는 해당 run lock 안에서 1부터 단조 증가하며 삭제 후 재사용하지 않는다.
- Codex 작업 하나에는 active run 하나만 둔다. 프로젝트에는 여러 paper/bundle/run을 저장할 수 있지만 P0는 같은 작업에서 둘을 동시에 활성화하거나 비교하지 않는다.

task binding은 `task_id`, `active_run_id|null`, `current_run_id|null`, `current_paper_id|null`, `current_bundle_id|null`, content 작업용 `pending_answer_id|null`, `pending_answer_status|null`, `current_response_attempt_id|null`, 비차단 전달 관측용 `delivery_candidate_answer_id|null`, `delivery_candidate_status|null`, `delivery_candidate_response_attempt_id|null`, `delivery_candidate_run_id|null`, `delivery_candidate_paper_id|null`을 가진다. `prepared | reading | reviewing | needs_work` run은 active와 current를 모두 차지하고, complete 뒤 active만 null이 되며 current binding은 후속 Q&A를 위해 남는다. 새 paper 준비 또는 다른 run의 explicit resume가 current를 바꿀 수 있지만 nonterminal active run이 있으면 `ACTIVE_RUN_CONFLICT`, content가 drafting/interrupted인 pending answer가 있으면 `ANSWER_PENDING`이다. `content_finalized` 뒤에는 pending binding을 비우고 delivery candidate로 옮기므로 Stop 관측 대기나 `delivery_unknown`은 새 질문을 막지 않는다. 새 사용자 turn 또는 새 run이 정확한 Stop 관측보다 먼저 시작되면 이전 candidate를 `delivery_unknown`으로 닫고 진행한다. initiating paper-read turn도 `prepare` 직후 같은 turn에서 `answer --begin`을 호출하고, 성공하기 전에는 첫 `read`를 허용하지 않는다.

URL identity normalization은 scheme/host lowercase, IDNA A-label host, default port 제거, empty path `/`, dot-segment 제거, percent-encoding 대문자화와 unreserved 문자 decode, fragment 제거를 적용한다. query는 parameter 재정렬·tracking 제거 없이 원래 순서와 byte 의미를 보존한다. local source token은 symlink를 해소한 canonical absolute path의 UTF-8 SHA-256인 `ls_<64 lowercase hex>`이며 원래 사용자 path는 metadata로 별도 보존한다.

이 문서의 canonical JSON은 UTF-8, object key lexical sort, `ensure_ascii=false`, `allow_nan=false`, separator `(',',':')`를 사용하며 문자열과 배열 순서를 정규화하지 않는다. `<SHA-256(x,y,...)>` 표기는 named identity object가 따로 제시되지 않은 경우 값 배열 `[x,y,...]`의 canonical JSON bytes를 hash한다는 뜻이다. ID prefix 뒤 hash는 항상 64자 lowercase hex다.

landing page의 main PDF candidate precedence는 `citation_pdf_url` metadata, `type=application/pdf` link, normalized anchor label `pdf | download pdf | full text pdf` 순이다. supplementary phrase가 붙은 link는 main candidate에서 먼저 제외한다. 가장 높은 precedence에서 서로 다른 최종 PDF bytes가 둘 이상이면 자동 선택하지 않고 `UNSUPPORTED_SOURCE`와 candidate URL 목록을 반환한다. 같은 bytes로 resolve되는 중복 link는 하나로 합친다. supplementary label matching은 Unicode casefold와 whitespace collapse 뒤 `supplementary | supporting information | additional file` 중 하나를 exact phrase로 포함할 때만 인정한다.

## 구성과 책임

### Codex Main

- ReadPaper skill을 호출한 현재 작업의 기존 Main이 독해 주체다.
- Main은 모든 reading unit을 순차적으로 읽고, 절·페이지 간 관계를 연결해 하나의 전체 논문 모델을 형성한다.
- Main은 Python 결과를 받는 것과 실제 내용을 읽는 것을 구분한다. 경로만 보거나 파일이 존재하는 것으로 독해를 완료 처리하지 않는다.
- Main은 독립 검토자의 동의를 정답으로 취급하지 않는다. 검토 결과의 원문 위치를 직접 다시 열고 `수용`, `기각`, `미해결`을 판정한다.

### Python 도구

Python은 PDF 확보, 해시 계산, 페이지 경계가 있는 텍스트 추출, 전체 페이지 렌더링, 범위 전달, 위치 관리, 상태 기록, 기계적 누락 검사만 담당한다. Python은 모델 API를 호출하거나 논문의 의미를 판정하지 않는다.

### 검토자

- 수식·시각 검토자는 전체 추출 텍스트를 읽은 뒤 정의, 수식의 조건, 그림 축·범례, 표의 행·열·단위, 부록 연결을 원본 페이지에서 집중 확인한다.
- 주장·실험 검토자는 전체 추출 텍스트를 읽은 뒤 주요 주장과 실험 설계·결과·한계·부록의 관계를 원본 페이지에서 집중 확인한다.
- 두 내용 검토자는 처음에는 Main의 이해 노트를 보지 않고 원문 우선 판단을 반환한다. 그 다음 같은 검토자에게 고정된 이해 노트 버전을 주어 대조 결과를 별도로 받는다.
- 설명 흐름 검토자는 긴 설명·튜토리얼·논쟁적 해석이 있을 때만 사용한다. 사용자의 질문, 요청 수준, 전체 원문, Main의 이해 노트, 사용자에게 보여 줄 고정된 설명 초안을 읽고 논리 오류·설명 구조 문제·선택적 개선을 구분한다.
- 검토자 실패·취소·부분 독해는 통과가 아니다. 부분만 확인했다면 반환 결과에 그 범위를 명시한다.

Main과 각 검토자 역할의 모델과 reasoning effort는 서로 독립적으로 설정할 수 있어야 한다. Main은 현재 Codex 작업에서 사용자가 선택한 active model/effort를 그대로 쓰며 skill이 조용히 바꾸지 않는다. 검토자는 parent 대화 기록을 자동 상속하지 않는 새 native subagent context에서 시작하며, source-first 입력 계약에 적힌 자료만 받는다.

검토자 모델 선택 precedence는 다음과 같다.

1. 이번 ReadPaper 호출에 사용자가 준 role별 override를 explicit spawn 값으로 사용한다.
2. override가 없으면 ReadPaper의 P0 role default를 explicit spawn 값으로 사용한다.
3. 위 두 값이 모두 `inherit`일 때만 Codex의 `[agents]` default를 사용한다.
4. `[agents]` default도 없을 때만 parent model/effort를 상속한다.

P0 role default는 `math_visual = gpt-5.6-sol/xhigh`, `claim_experiment = gpt-5.6-sol/xhigh`, `explanation_flow = gpt-5.6-sol/high`다. custom agent role 파일에는 `model`과 `model_reasoning_effort`를 넣지 않는다. Codex에서는 role 파일에 해당 값을 넣으면 spawn 값보다 나중에 적용될 수 있으므로, 사용자 override와 P0 default는 spawn 인자로만 전달한다. 현재 host의 model catalog에서 조합을 확인할 수 없거나 지원하지 않으면 spawn 전에 `UNSUPPORTED_MODEL_CONFIG`로 실패하며 다른 값으로 대체하지 않는다.

role의 closed set은 `root_main | math_visual | claim_experiment | explanation_flow`다. reasoning effort 입력은 `none | minimal | low | medium | high | xhigh | max | ultra` 중 host가 해당 model에 지원하는 값만 허용한다. model과 effort가 서로 다른 precedence에서 정해질 수 있으므로 `selection_source`는 `{model,effort}` 객체이고, 각 값의 closed set은 `active_task | invocation_override | readpaper_role_default | agents_default | parent_inherit`다.

모델 관측은 audit 내부 선택 필드가 아니라 모든 역할에 공통인 세 immutable record로 관리한다.

- `model_request_id = mr_<SHA-256(canonical request)>`인 `model_request`는 `run_id`, `role`, concrete `requested_model`, concrete `requested_effort`, `selection_source`, `requested_by_agent_execution_id|null`, reviewer이면 `assignment_subject_kind`(`content_stage | flow_audit`), `assignment_subject_id`, `assignment_input_digest`를 가진다. precedence 해소 뒤의 값만 기록하므로 requested 값은 `null`일 수 없고 reviewer request는 assignment subject가 null일 수 없다.
- `agent_execution_id = ae_<SHA-256(run_id,agent_execution_seq)>`인 `agent_execution`은 `role`, `agent_id`, `parent_agent_execution_id|null`, `task_id`, `session_id`, `turn_id|null`, `model_request_id`, `status`, `previous_execution_record_id|null`을 가진다. 최초 독해, complete-run 후속 Q&A, 자동 continuation, 사용자 run/answer resume를 포함한 모든 bound root Main response turn마다 새 ID를 쓰고, reviewer도 각 audit stage/attempt마다 새 ID를 쓴다. 한 Main execution에 속한 read/render/open/draft/finalization/grounding event는 다른 Main turn의 execution을 참조할 수 없다. 내용 audit의 두 stage가 같은 reviewer agent를 이어 쓰더라도 execution ID는 다르다.
- `model_observation_id = mo_<SHA-256(canonical observation record)>`인 `model_observation`은 `agent_execution_id`, `model_request_id`, `role`, `task_id`, `session_id`, `turn_id|null`, `agent_id`, `parent_agent_id|null`, requested model/effort, observed model/effort가 있으면 그 값, host의 조합 검증 결과, `host_observation_event_id|null`, `host_metadata_sha256|null`, `observer_state`를 가진다.

execution status의 closed set은 `requested | running | returned | partial | failed | cancelled`이고 허용 전이는 `requested -> running -> returned|partial|failed|cancelled` 또는 `requested -> failed|cancelled`뿐이다. terminal 뒤 재시도는 새 execution ID를 만든다. observer state는 `verified | request_accepted | partial | unavailable | conflict`다. host가 model과 effort의 실제 적용값 및 실행 identity를 확인하고 요청값과 일치할 때만 `verified`다. host가 explicit spawn 조합을 지원한다고 검증하고 요청을 수락했지만 실제 effort receipt처럼 일부 적용값을 노출하지 않으면 `request_accepted`다. 일부 관측값만 있고 요청 수락까지 확인할 수 없으면 `partial`, 관측 자체가 없으면 `unavailable`, 명시적 불일치나 거부는 `conflict`다. Main이나 reviewer의 자기보고 또는 설정 파일 복사는 host observation이 아니다. `request_accepted`는 실제 effort를 확인했다는 뜻이 아니며 최종 보고에 이 제한을 남긴다.

completion-critical record와 coverage event는 해당 `agent_execution_id`를 참조해야 한다. reviewer execution은 `returned`여야 완료에 기여한다. 현재 root Main turn 안에서 만들어진 coverage/note/draft는 execution이 `running`이고 model observation이 `verified | request_accepted`이며 local task·host session/turn/agent·parent·role binding이 유효할 때 provisional gate에 사용할 수 있다. deterministic check 뒤의 protected `answer --finalize`가 content hash와 unchanged event sequence를 결합하면 answer content와 initiating run을 완료할 수 있다. logical Stop transaction은 별도로 actual `last_assistant_message` hash를 결합해 delivery만 `sent_verified`로 승격한다. Stop을 관측하지 못하면 delivery는 `unknown`이지만 이미 완료한 content/run을 되돌리지 않는다. `failed | cancelled | partial` execution의 provisional evidence는 completion에서 제외한다.

LangGraph, 별도 LLM 서비스 루프, 벡터 DB, 웹 UI, 질문별 별도 Q&A 파이프라인은 P0에 포함하지 않는다.

## 논문 자료, 범위, 위치 계약

### artifact와 bundle manifest

각 bundle manifest는 `schema_version`, `paper_id`, `bundle_id`, `prepared_at`, `landing_url|null`, `artifacts[]`를 가진다. 각 artifact record의 필드는 다음과 같다.

| 필드 | 계약 |
| --- | --- |
| `artifact_ref_id` | bundle 안의 발견 occurrence identity; 같은 bytes라도 URL/role/member가 다르면 별도 값 |
| `artifact_id` | 바이트가 있으면 `a_<SHA-256 전체 hex>`; supplementary 확보 실패면 `af_<SHA-256(schema_version,normalized discovery URL,role,parent artifact ID,archive member path)>` |
| `role` | `main_pdf | supplementary` |
| `media_kind` | `pdf | prose_text | image | zip | code | dataset | notebook | audio | video | interactive | unknown` |
| `support_state` | `supported | unsupported | unavailable | failed` |
| `discovery_url` | 랜딩 페이지에서 발견한 원래 URL 또는 로컬 입력 |
| `resolved_url` | redirect 뒤 최종 URL. 로컬 입력이면 `null` |
| `parent_artifact_id` | ZIP member면 archive artifact ID, 아니면 `null` |
| `archive_member_path` | ZIP member의 정규화 상대 경로, 아니면 `null` |
| `declared_content_type` | HTTP header 값 또는 `null` |
| `detected_content_type` | magic bytes와 parser로 확인한 값 |
| `size_bytes`, `sha256` | 확보한 바이트의 크기와 hash. 확보 실패면 `null` |
| `failure_code` | 성공이면 `null`; 실패·미지원이면 closed error code |

지원 prose text는 UTF-8 plain text와 Markdown이다. 지원 image는 PNG, JPEG, TIFF, WebP의 단일-frame raster다. multi-frame/animated image는 `UNSUPPORTED_ARTIFACT`이며 자동으로 첫 frame만 선택하지 않는다. EXIF orientation은 decode 뒤 적용하고 visual dimensions, bbox, pixel hash는 orientation-applied raster를 기준으로 한다. ZIP은 바깥 archive만 열고 member 안의 archive는 열지 않는다. ZIP container occurrence는 전송·무결성 artifact이고 자체 reading/visual unit을 만들지 않는다. accepted member는 각각 고유 `artifact_ref_id`를 가진 bundle occurrence가 되며 full scope는 container의 안전 검증 상태와 모든 member의 support/coverage를 함께 검사한다. member가 없거나 지원·미지원 상태로도 분류할 수 있는 regular member가 하나도 없는 archive는 `UNSUPPORTED_ARTIFACT`다. 확장자·HTTP content type과 실제 bytes가 다르면 실제 탐지 결과가 우선하고 mismatch warning을 남긴다. PDF signature/parser 검증에 실패하면 PDF로 처리하지 않는다.

supplementary Markdown 안의 링크·image reference와 PDF 본문 annotation은 document content로만 보며 추가 network discovery나 command로 실행하지 않는다.

각 PDF는 최대 200 page, 회전 적용 CropBox 한 축 최대 200 inch, 각 rendered page 한 축 20,000 pixel 및 100 megapixel 이하다. 독립 image도 decode 후 같은 pixel/dimension 한도를 적용한다. PDF metadata parse 30초, artifact text extraction 120초, page render 30초, artifact 전체 render 300초, prepare command 전체 600초를 넘으면 `TIMEOUT` 또는 `UNSUPPORTED_ARTIFACT`로 실패하고 부분 산출물을 coverage에 쓰지 않는다.

P0의 archive 제한은 압축 파일 128 MiB, member 256개, member당 실제 확장 64 MiB, archive 전체 실제 확장 256 MiB, member별·전체 압축률 100:1이다. 압축률은 `actual_uncompressed_bytes / max(compressed_bytes, 1)`로 계산하며 header 선언값뿐 아니라 스트리밍 중 실제 확장량에도 적용한다. member path는 backslash를 `/`로 바꾸고 Unicode NFC와 POSIX 상대 경로로 정규화한다. symlink, hardlink, device entry, 암호화 member, absolute/drive/UNC path, `..`, NUL, 240 UTF-8 bytes 초과, NFC 또는 casefold 후 중복 경로를 거부한다. accepted member bytes는 member path에 직접 추출하지 않고 artifact ID 기반 object-store temp file로 streaming commit한다. 어느 한도를 넘으면 archive 전체를 `UNSUPPORTED_ARTIFACT`로 표시하며 일부 member만 조용히 채택하지 않는다.

HTTP fetch는 redirect를 최대 5회 허용하고 connect timeout 10초, read-idle timeout 30초, redirect·retry·backoff를 모두 포함한 artifact 전체 wall-clock timeout 120초를 적용한다. landing HTML은 최대 5 MiB, 개별 main PDF·supplementary 다운로드는 최대 128 MiB다. GET의 연결 오류, read timeout, HTTP `408 | 429 | 500 | 502 | 503 | 504`만 최대 2회 추가 재시도하며 기본 backoff는 1초, 2초다. `Retry-After`가 유효하고 30초 이하면 그 값을 쓰되 전체 timeout을 넘지 않는다. 그 밖의 4xx·형식 불일치·손상 파일은 재시도하지 않는다. 적용된 limit과 retry 결과는 manifest에 기록한다.

120초 deadline은 monotonic clock으로 DNS 시작부터 connect/TLS, 최대 5번의 Location 이동, body, retry와 backoff 전체에 적용하며 재시도 때 초기화하지 않는다. redirect loop, Location 누락·파싱 실패·비 HTTP(S) scheme은 `FETCH_FAILED`의 redirect phase이고 재시도하지 않는다. `401 | 403`은 `ACCESS_DENIED`, local deadline 초과는 `TIMEOUT`, 허용 transient status 재시도 소진은 `FETCH_FAILED`, parser/magic 실패는 `CORRUPT_ARTIFACT`, user cancellation은 `CANCELLED`다. main source/landing 실패는 ID가 모두 null인 command failure이며, supplementary occurrence 실패만 immutable manifest의 failed/unavailable artifact로 남길 수 있다.

각 download attempt는 `.readpaper/prepare-work/<prepare-operation-id>/` 아래 mode `0600`의 고유 same-filesystem `.part`에 byte 0부터 streaming write한다. streaming size limit, EOF, SHA-256, magic/parser 검증과 file fsync가 모두 끝난 뒤에만 content-addressed object temp를 거쳐 atomic rename한다. retry는 이전 partial을 artifact로 재사용하지 않고 새 temp에 처음부터 쓴다. timeout·cancel·HTTP/format failure에서는 해당 partial을 제거하며, process crash 뒤 같은 prepare request는 journal에 속한 stale partial만 검증 후 정리한다. manifest, extractor와 object cache는 `.part` 또는 committing 전 bytes를 정상 artifact로 볼 수 없다.

원격 source와 모든 redirect/discovery URL은 `http` 또는 `https`만 허용하고 URL credentials를 거부한다. port 생략, HTTP 80, HTTPS 443만 허용한다. 각 DNS resolution과 redirect hop에서 loopback, link-local, private, multicast, unspecified, IPv4-mapped private IPv6, cloud metadata 대역을 거부하며 연결 대상 IP가 검증한 public resolution 집합에 속하는지 확인한다. TLS 인증서 검증을 끄지 않는다. 로컬 파일은 사용자가 준 exact path를 별도 local-source 경로로 처리하며 URL fetch 정책과 섞지 않는다.

prepare가 끝난 bundle manifest는 immutable하다. 같은 main PDF를 다시 준비했는데 landing page의 supplementary가 달라지면 새 bundle ID를 만들며, 이전 run과 locator는 이전 bundle에 남는다.

bundle digest의 artifact identity 필드는 `artifact_ref_id`, `artifact_id`, `role`, `media_kind`, `support_state`, `discovery_url`, `resolved_url`, `parent_artifact_id`, `archive_member_path`, `detected_content_type`, `size_bytes`, `sha256`, `failure_code`다. unavailable/failed artifact는 URL 기반 임시 artifact ID와 failure code로 digest에 들어가므로 나중에 확보되면 반드시 새 bundle이 된다.

### 독해 범위

run은 prepare에서 전체 bundle inventory를 받은 뒤 다음 필드를 첫 read 전에 한 번 고정한다.

- `scope_kind`: `full | user_reduced`
- `scope_locked`: boolean
- `required_artifact_ref_ids`: 이번 run이 모두 읽어야 할 bundle occurrence 집합
- `excluded_artifacts`: `{artifact_ref_id, reason_code, reason, user_confirmation_event_id}` 목록
- `scope_limitations`: 사용자에게 항상 보여 줄 제외·미지원 설명

prepare 직후에는 `scope_locked=false`이고 proposed scope는 full이다. Main이 artifact 목록을 commentary로 보여 준 뒤 `record --kind scope_confirmation`을 한 번 성공해야 true가 된다. initiating user turn의 일반적인 “이 논문을 읽어라” 요청은 모든 artifact가 supported/available이고 budget 안일 때 proposed full을 같은 turn에서 lock할 authority다. 별도 확인 turn을 억지로 요구하지 않는다. unsupported/unavailable/failed 또는 over-budget artifact가 있으면 자동 lock하지 않고 exclusion 선택을 요청한다. main PDF artifact ref는 어떤 scope에서도 필수이며 제외할 수 없다. full을 선택하면 bundle에서 발견한 모든 supplementary가 required이고 blocker가 있으면 blocked다. 사용자가 별도 turn에서 정확한 supplementary artifact 제외와 결과 문구를 승인하면 같은 prepared run을 `user_reduced`로 고정한다. 첫 read 이후에는 scope를 바꿀 수 없으며 변경하려면 새 run이 필요하다. reduced scope는 완료돼도 `논문 전체 독해 완료`라고 말하지 않고 `요청 범위 독해 완료—제외 자료 있음`이라고 표시한다.

`scope_confirmation`의 각 excluded item은 그 artifact ID와 제외 이유를 exact prompt에 포함한 trusted `user_turn_started`의 `user_confirmation_event_id`를 직접 가진다. `reason_code`의 closed set은 `user_excluded | unsupported | unavailable | failed | over_budget`이고 state service가 artifact 상태와 승인 내용을 대조해 결정한다. `reason`은 승인 prompt에서 해당 artifact에 대응하는 exact user-facing 이유다. 모든 excluded item이 같은 승인 turn에서 왔으면 ID를 반복해 저장하며 top-level `user_turn_id`는 그 event의 turn ID와 일치해야 한다. 일반 full-scope lock은 `excluded_artifacts=[]`이고 initiating question event가 authority다.

scope lock transaction은 `excluded_artifacts`를 `artifact_ref_id` lexical order로 canonicalize하고 required-but-unavailable/unsupported 상태에서 고정된 `scope_disclosure_markdown`과 `scope_disclosure_sha256`을 state service가 생성한다. 제한이 없는 full scope면 `scope_disclosure_markdown=""`이고 hash는 empty UTF-8 bytes의 SHA-256이다. `user_reduced`의 exact string은 첫 줄 `> ReadPaper 범위 제한: 다음 자료는 제외되어 이 답변은 요청 범위만 다룹니다.` 뒤에 artifact마다 `> - ref=<artifact_ref_id>; media=<media_kind>; reason=<reason_code>; failure=<failure_code-or-none>` 한 줄을 붙인다. 줄 구분은 LF이고 마지막 LF는 없다. opaque artifact ref, closed-set media/reason/failure 값 외에는 넣지 않으며 원문 filename, URL, free-form `reason`, 문서 문장을 복사하지 않는다. `scope_disclosure_sha256`은 이 exact UTF-8 string의 SHA-256이다. 모든 delivered paper answer의 draft/final content는 nonempty disclosure를 byte-for-byte 마지막 block으로 포함해야 하며 `check`가 content hash와 suffix를 검사한다.

### 페이지와 객체 위치

각 페이지 위치는 다음 네 필드를 분리한다.

| 필드 | 의미 |
| --- | --- |
| `pdf_index` | 코드에서 사용하는 0-based 페이지 인덱스 |
| `pdf_page` | 사람이 PDF 뷰어에서 여는 1-based 페이지 순서 |
| `pdf_label` | PDF metadata의 페이지 라벨. 없으면 `null`; 자동 대체 여부를 별도로 표시 |
| `printed_label` | 페이지에 실제 인쇄된 번호. Main의 시각 확인 전에는 `null` |

모든 원문 위치는 공통 `locator_id = loc_<SHA-256 canonical JSON>`를 사용한다. canonical JSON의 공통 필드는 `schema_version`, `bundle_id`, `artifact_ref_id`, `artifact_id`, `locator_kind`이며 `locator_kind`의 closed set은 `pdf_page | text_span | pdf_object | image_region`이다. 같은 bytes가 다른 bundle이나 occurrence에 나타나도 locator를 자동 승계하지 않도록 `bundle_id`와 `artifact_ref_id`를 hash 입력에 포함한다. 각 variant는 아래 필드만 허용하고 `null` identity 필드와 알 수 없는 추가 필드를 거부한다.

- `pdf_page`는 1-based `pdf_page`를 더하며 해당 PDF page 전체를 뜻한다.
- `text_span`은 PDF와 prose text 모두에 사용하며 `unit_id`, `content_sha256`, `span_start`, `span_end`를 더한다. offset은 해당 unit content의 Unicode code-point 0-based half-open 범위 `[span_start,span_end)`이고 unit 경계를 넘지 않는다.
- `pdf_object`는 `pdf_page`, `object_kind`, `object_ordinal`, `bbox_ppm`을 더한다. `object_kind`는 `figure | table | equation | algorithm | listing | other`이고, `object_ordinal`은 같은 page·kind의 객체를 위쪽, 왼쪽, 아래쪽, 오른쪽 bbox 순서로 센 1-based 값이다.
- `image_region`은 `bbox_ppm`, `image_sha256`을 더한다. 독립 image 전체는 bbox를 생략하지 않고 `[0,0,1000000,1000000]`으로 기록한다.

`bbox_ppm`은 회전과 CropBox를 적용한 원본 page canvas 또는 전체 raster를 각 축 0–1,000,000 정수로 정규화한 `[left,top,right,bottom]`이며 `left<right`, `top<bottom`을 만족해야 한다. pixel 좌표는 half-up 방식으로 정규화하고 float, NaN, Infinity를 허용하지 않는다.

`printed_object_label`, printed page label, render 배율과 pixel 좌표는 확인 metadata이며 locator identity에 넣지 않는다. locator record는 immutable하며 Main이나 reviewer가 `locator_candidate`로 만들 수 있다. audit finding은 candidate locator ID를 참조할 수 있지만, root Main이 해당 원문을 다시 열고 `locator_confirmation` record에 `locator_id`, `reopen_event_id`, `render_id|null`, `confirmation=confirmed`를 남기기 전에는 finding disposition, answer grounding, 최종 인용에 사용할 수 없다. 추출 텍스트에서 찾은 객체 번호는 candidate일 뿐이며 자동 confirmation이 아니다.

prepare manifest는 모든 PDF page의 `pdf_page` locator와 독립 image 전체의 `image_region` locator identity를 inventory candidate로 미리 계산한다. 그 밖의 text span/object/partial-image locator는 Main이나 reviewer의 candidate record가 필요하다. inventory candidate도 Main confirmation 전에는 최종 근거가 아니다.

confirmation의 reopen event는 같은 run/root Main/current Main context stream/epoch에서 locator 뒤의 source를 실제 포함해야 한다. `pdf_page | pdf_object`는 해당 page/render의 `visual_open_observed`, `text_span`은 matching unit/hash의 `unit_emitted`, `image_region`은 해당 image/render의 `visual_open_observed`만 허용한다. object/partial-region confirmation은 bbox를 포함한 zoom render가 필수다.

페이지별 텍스트, 페이지 경계를 보존한 전체 텍스트, 전체 페이지 PNG, 추출 경고, manifest를 만든다. 두 단 편집, 빈 텍스트, 수식 문자 손실, 비정상적 문자 밀도 등은 경고 후보로 남기되 텍스트만으로 정상 판정을 확정하지 않는다. 각 required PDF page는 root Main visual-open 뒤 `printed_label` record를 가져야 하며 `label_state`는 `confirmed | observed_absent | observed_unreadable`다. confirmed만 non-null label을, 나머지는 null을 요구한다. 이 record가 없으면 synthesis visual coverage가 완전하지 않다.

canonical PDF page text는 Poppler `pdftotext -layout -enc UTF-8 -f <n> -l <n>`의 page별 stdout이다. CRLF/CR은 LF로 바꾸고 Poppler가 page terminator로 붙인 terminal form-feed 하나와 그 직전 LF 하나만 제거하며 그 밖의 whitespace와 Unicode는 보존한다. `pypdf` text를 fallback으로 섞지 않는다. Poppler 실패나 UTF-8 decode 실패는 `CORRUPT_ARTIFACT`, full-page raster OCR gate는 아래 규칙으로 처리한다.

P0의 deterministic OCR gate는 required PDF page에서 추출된 non-whitespace text가 20 code point 미만이고 단일 raster object가 회전 적용 CropBox 면적의 80% 이상을 덮을 때 `suspected_scan`으로 분류하는 것이다. required page가 하나라도 suspected scan이면 해당 PDF artifact는 `OCR_REQUIRED`이고 full completion을 막는다. text가 비었지만 이 raster 조건을 만족하지 않는 blank/visual page는 OCR로 단정하지 않고 visual unit과 extraction warning으로 남긴다.

## 사용자 및 내부 인터페이스

사용자는 ReadPaper skill에 논문 출처를 전달한다. skill은 아래 내부 명령을 필요한 순서로 호출하며, 사용자가 정상 흐름에서 Python 명령을 직접 조립할 필요는 없다.

모든 명령은 stdout에 다음 envelope의 JSON 객체 하나를 반환한다. 원문을 전달하는 `read`만 envelope의 `data.units[].content`에 실제 본문을 포함한다.

```text
schema_version: "1"
command: prepare | read | render | record | check | answer | resume | delete
ok: boolean
paper_id: string | null
bundle_id: string | null
run_id: string | null
data: object | null
error: null | {code, message, retryable, details}
```

`ok=true`이면 `error=null`, `ok=false`이면 `data=null`이다. error code의 closed set은 `INVALID_ARGUMENT | UNSUPPORTED_SOURCE | ACCESS_DENIED | FETCH_FAILED | TIMEOUT | CANCELLED | CORRUPT_ARTIFACT | OCR_REQUIRED | UNSUPPORTED_ARTIFACT | OUTPUT_BUDGET_EXCEEDED | OUTPUT_TRUNCATED | NOT_FOUND | ID_MISMATCH | ACTIVE_RUN_CONFLICT | ANSWER_NOT_STARTED | ANSWER_PENDING | ANSWER_INTERRUPTED | OBSERVER_UNAVAILABLE | COVERAGE_INCOMPLETE | AUDIT_INCOMPLETE | STATE_CONFLICT | UNSUPPORTED_MODEL_CONFIG | DELETE_CONFIRMATION_REQUIRED | DELETE_SCOPE_CHANGED | INTERNAL_ERROR`다. 내부 예외를 성공 응답이나 빈 data로 바꾸지 않는다.

authority-bearing mutation인 `prepare | record | answer | resume | delete`와 actor/context-bound 출력인 `read | render`는 caller가 적은 agent ID를 신뢰하지 않는다. 프로젝트 PreTool observer는 실제 도구 실행 전에 strict direct-command grammar, unique `client_request_id`, `argv_sha256`, canonical request digest, payload-file bytes hash가 있으면 그 hash, ReadPaper-local task binding, host session/turn/agent/tool-use identity, locally bound execution/context-stream identity와 current context epoch, 30초 expiry를 가진 mode-0600 one-use `invocation_capability`를 `invocation_index_lock` 아래 발급한다. capability와 host ledger에는 raw argv/source/payload content를 복제하지 않고 위 hash만 영속한다. shell operator, pipeline, command substitution, 해석할 수 없는 wrapper가 있으면 capability를 만들지 않고 tool을 막는다. PreTool semantic key는 `H("PreToolUse/v1",session_id,turn_id,agent_id|null,agent_type|null,tool_use_id,tool_name)`이고 capability ID는 `cap_<SHA-256(pretool_semantic_key,client_request_id,request_digest,hook_definition_hash)>`다. 같은 key와 같은 payload replay는 같은 host event/capability를 반환하며, 같은 key의 다른 payload는 `STATE_CONFLICT`다. 최초 request 처리는 client request ID와 canonical digest에 정확히 하나의 unexpired unused capability가 있을 때만 이를 원자적으로 소비하고 mutation 또는 bound output을 만든다. 이미 journal/index에 bind된 in-progress/completed exact replay는 원래 consumed capability를 재인증하거나, 새 `tool_use_id`의 정확히 하나인 fresh matching capability를 replay-consumed로 기록한 뒤 저장 operation/response만 재생할 수 있다. 이 경로는 domain mutation, coverage, sequence를 새로 만들지 않는다. 어느 경로든 같은 tool-use에 fresh capability가 둘 이상이거나 actor/execution/assignment/request digest가 맞지 않으면 `OBSERVER_UNAVAILABLE` 또는 `STATE_CONFLICT`다. G0는 실제 Pre/PostTool의 `tool_use_id`와 Main/subagent binding을 검증해야 하지만 host-provided execution/context-stream ID를 요구하지 않는다.

production에서 PreTool이 허용하는 exec `cmd`는 정확히 `'<absolute-project>/.venv/bin/python' '<absolute-project>/.agents/skills/readpaper/scripts/paper.py' '<logical-command>' <그 command 절의 인자>` 형태다. cwd host metadata는 symlink를 해소한 project root와 같아야 하고, 두 prefix path는 regular file/허용 symlink이며 install manifest의 realpath와 SHA-256이 일치해야 한다. 모든 argv token은 POSIX single-quote canonical serializer로 감싸고 token 안의 single quote는 표준 close-quote/double-quoted-quote/reopen 방식으로 표현한다. raw `cmd`에는 `cd`, env assignment, `uv run`, shell/operator token, redirection, glob, substitution, newline, NUL, 알려지지 않은 flag, 중복 flag를 허용하지 않는다. logical command의 positional/flag grammar는 이 문서의 command signature가 유일한 source이고 공용 pure `parse_invocation` 모듈을 PreTool hook과 CLI가 함께 import한다. 두 쪽 parser/schema hash가 다르면 capability를 발급하지 않는다. `uv sync --frozen`은 설치 단계에만 쓰고 제품 호출은 이 direct `.venv` prefix만 사용한다.

모든 protected command client request ID는 `cr_<32 lowercase hex>` CSPRNG UUIDv4이고 state/lifecycle 검사보다 먼저 replay index를 조회한다. `prepare|answer|resume|delete` key는 `(task_id,command,mode,client_request_id)`, `read|render|record` key는 `(run_id,command,client_request_id)`다. index는 canonical request digest, original capability/tool-use identity, `authorized_replay_capability_ids[]`, operation/journal ID가 있으면 그 ID, exact response bytes/hash 또는 content-bearing response의 per-paper immutable response reference를 가진다. 같은 key/request가 in-progress면 위 capability replay 규칙으로 같은 operation을 재개하고 completed면 exact 저장 응답을 반환하며 state를 다시 전이하지 않는다. 같은 key의 다른 request는 `STATE_CONFLICT`다. 따라서 `answer --begin` replay가 `ANSWER_PENDING`으로 바뀌거나, `resume` replay가 현재 state 때문에 실패하거나, `delete --preview` replay가 새 request ID를 만드는 일이 없다. read/render의 same-ID response-loss retry도 original bound session/stream/epoch와 bytes를 반환하고 새 coverage event를 만들지 않으며, 새 epoch의 실제 재열람은 새 client request ID가 필요하다. 최초 exact response bytes는 state/output operation commit과 함께 fsync한 뒤 반환한다. 단, 사용자가 exact delete를 완료하면 그 paper의 response bytes를 제거하고 routing index를 deletion tombstone으로 바꾸는 것이 replay보다 높은 authority다. 이후 같은 client request는 삭제 전 content를 반환하지 않고 저장된 tombstone에서 deterministic `NOT_FOUND`와 `deletion_request_id`를 반환한다.

`invocation_index_lock`은 raw PreTool capability 발급/consume CAS와 client-request key reservation을 직렬화한다. 최초 처리는 capability consume과 `in_progress` client index entry를 같은 fsynced transaction에 남긴 뒤 이 lock을 놓고, 그 다음에만 domain lock을 얻는다. domain transaction은 exact response bytes/hash 또는 이를 결정하는 immutable response template를 operation/run journal에 먼저 fsync한다. paper/task binding이나 per-paper response reference를 publish하는 commit은 `project_reference_lock -> task/run/domain lock -> invocation_index_lock` 순서에서 domain state와 completed client route를 함께 공개한다. content-only commit은 run/domain lock 뒤 invocation index를 얻을 수 있다. PreTool 발급/최초 consume 경로는 invocation index를 보유한 채 reference/run lock을 기다리지 않는다. completed per-paper replay도 reference lock을 얻은 뒤 route/tombstone을 다시 읽고 response reference를 dereference한다. 따라서 delete가 reference lock 아래 tombstone을 쓰는 동안 새 replay/commit이 content를 되살릴 수 없다. consume 직후나 domain commit 직전 process가 죽어도 in-progress entry와 journal이 authority가 되어 같은 request replay가 fresh capability로 operation 및 exact response를 재개한다.

`read | render`의 반환·cache 생성 자체는 authority evidence가 아니다. response의 task/session/turn/agent-execution/context-stream/epoch는 consumed PreTool capability에서만 채우고, trusted PostTool observer가 같은 capability/client request/tool-use/agent/execution과 exact output marker/hash를 bind한 뒤에만 unit/render event가 coverage에 기여한다. `check`는 read-only이고 Stop 내부 호출은 raw Stop transaction authority를 사용한다.

### `prepare <source> --task-id <task-id> --user-turn-id <turn-id> --client-request-id <client-request-id>`

지원 출처를 확보하고 공개 supplementary discovery를 닫은 뒤 immutable bundle과 prepared run을 만든다. caller는 record와 같은 형식의 CSPRNG `client_request_id`를 만들고 state service는 `prepare_operation_id = po_<SHA-256(task_id,client_request_id)>`를 project-level operation journal에 먼저 기록한다. canonical request는 schema version, task/user-turn identity, source kind, normalized URL 또는 local source token을 포함한다. `(task_id,client_request_id)`와 같은 request의 재호출은 진행 중 operation을 안전하게 재개하거나 completed의 exact 원래 응답을 반환하고 run/event를 추가하지 않는다. 같은 key의 다른 request는 `STATE_CONFLICT`다. 새 독해 occurrence를 원하면 새 client request ID를 사용해야 하며 active-run 규칙은 그대로 적용된다.

operation state의 closed set은 `started | committing | completed | failed | cancelled`다. fetch attempt와 redirect/retry diagnostics는 pre-bundle journal에만 남기고 run event로 만들지 않는다. main PDF와 전체 immutable bundle 준비가 끝난 뒤 `committing` plan을 fsync하고, common `project_reference_lock` 아래 bundle/object reference/run/state/task binding과 `run_created | source_prepared`를 각각 한 번 commit한다. 응답 유실 또는 process crash 뒤 같은 request는 committing plan을 idempotent replay한다. commit 전 사용자 취소는 요청·backoff·fetch를 중단하고 temp bytes를 제거해 `CANCELLED`, operation `cancelled`, paper/bundle/run ID null을 반환한다. committing 이후 취소는 이미 시작한 commit을 복구 완료하고 completed 결과를 반환하며 반쯤 공개된 run을 만들지 않는다.

성공 `data`는 `prepare_operation_id`, `paper_id`, `bundle_id`, `run_id`, `task_id`, `proposed_scope_kind=full`, `scope_locked=false`, `artifacts`, `reading_units`, `read_batches`, `visual_units`, `page_counts`, `paper_input_estimate`, `artifact_exclusion_estimates`, `limits_applied`, `warnings`, `scope_limitations`를 가진다. answer lifecycle field는 반환하지 않는다. Main은 이어서 같은 turn에서 `answer --begin`을 호출한 뒤 artifact 목록을 보여 주고 `record --kind scope_confirmation`으로 full 또는 user-reduced 범위를 lock해야 read할 수 있다. 준비 성공은 독해 완료가 아니다.

### reading unit과 read batch inventory

prepare는 모든 discovered artifact를 순서가 고정된 atomic reading unit으로 나누고, 그 unit을 section-aligned read batch로 묶는다. reading unit은 이해를 분할하는 단위가 아니라 전송, 재개, hash 검증, 누락 검사에 사용하는 최소 단위다. section은 Main이 논리 구조를 따라 읽기 위한 batch metadata이며, section 요약이 원문 unit을 대체하지 않는다. scope가 lock되면 required unit과 batch 집합을 함께 고정한다.

- PDF text unit ID: `<artifact_ref_id>:p<6-digit pdf_page>:c<4-digit chunk>`
- prose text unit ID: `<artifact_ref_id>:t:c<6-digit chunk>`
- PDF page visual unit ID: `<artifact_ref_id>:p<6-digit pdf_page>:visual`
- image visual unit ID: `<artifact_ref_id>:image`
- read batch ID: `rb_<SHA-256(schema_version, ordered unit_ids)>`

PDF text unit은 page 경계를 넘지 않는다. 한 page가 unit 상한을 넘으면 `[char_start,char_end)`가 겹치거나 빠지지 않는 연속 chunk로 나눈다. unit의 P0 상한은 안전계수를 적용한 추정 4,000토큰이다. `o200k_base` token count에 20%를 더해 올림한 값을 사용하며, tokenizer를 사용할 수 없으면 UTF-8 byte 수를 token 상한으로 사용한다. tokenizer ID, 원래 count, 안전계수 적용 count, char와 byte 범위를 manifest에 고정한다.

chunker는 남은 page text에서 safe estimate 4,000 이하인 maximal Unicode prefix를 binary search한다. prefix 후반 50% 안에서 마지막 `\n\n`, 그 다음 `\n`, 그 다음 whitespace 경계를 우선하고, 없으면 maximal code-point boundary에서 자른다. 다음 chunk는 정확히 이전 `char_end`에서 시작하며 원문 whitespace를 삭제하거나 중복하지 않는다.

각 marker는 unit content에서 최대 64 Unicode code point를 잘라 `{span_start,span_end,text_sha256}`로 표현한다. start는 0에서, end는 content 끝에서, middle은 `floor((char_count-marker_width)/2)`에서 시작하며 `marker_width=min(64,char_count)`다. 빈 unit은 OCR/empty-text 판정 대상으로 남기되 읽기 unit으로 성공 생성하지 않는다. marker hash와 full content hash가 모두 맞아야 complete emission이다.

각 unit은 `section_id`, `section_path[]`, `section_confidence`(`declared | detected | synthetic`)를 가진다. PDF outline이나 확실한 heading으로 section을 찾으면 그 경계를 사용하고, 찾지 못하면 문서 순서에 맞는 synthetic section을 만든다. anchored section 시작 offset은 page/artifact 범위 안의 hard unit split point다. chunker는 먼저 page/prose 범위를 section interval과 교차한 뒤 각 조각에 4,000-token 규칙을 적용하므로 text unit 하나가 section 경계를 가로지르지 않는다. batch는 artifact와 section 경계를 넘지 않으며 unit을 최대 8개 포함한다. 직렬화된 전체 `read` data의 안전계수 적용 추정값이 12,000토큰을 넘기 전에 새 batch를 시작한다. 긴 section은 같은 `section_id` 아래 여러 batch로 나누고 `batch_index`, `batch_count`로 연결한다. 모든 required unit은 정확히 한 inventory batch에 속한다.

section precedence는 PDF outline/Markdown ATX heading인 `declared`, 그 다음 detected heading, 마지막 artifact 전체를 덮는 synthetic section 하나다. PDF outline destination은 해당 `pdf_page`의 canonical text 시작에 anchor하고, 같은 page의 후속 outline entry처럼 별도 text offset이 없는 entry는 hard boundary로 쓰지 않으며 exact detected heading이 있을 때만 그 offset에 anchor한다. Markdown ATX와 detected heading은 canonical text의 해당 line 시작 offset에 anchor한다. detected heading은 독립 line 120 code point 이하, 앞뒤 중 하나 이상 blank line, `^(\d+(\.\d+)*)\s+\S` 또는 casefold exact heading `abstract | introduction | background | related work | preliminaries | method | methods | methodology | experiments | results | discussion | limitations | conclusion | conclusions | references | appendix`를 모두 만족하는 line이다. detection은 batch metadata일 뿐 의미적 section 정확성이나 독해 완료를 증명하지 않는다.

`section_id = sec_<SHA-256(artifact_ref_id,section_ordinal,section_path,first_unit_id)>`이고 ordinal은 artifact 문서 순서의 1-based 값이다. synthetic section은 `section_path=["Synthetic <ordinal>"]`을 사용한다. heading text가 같아도 ordinal/first unit이 다르면 다른 ID다. `render_id = ren_<SHA-256(bundle_id,visual_unit_id,image_sha256,render_dpi,bbox)>`이며 같은 render parameters와 bytes는 같은 ID다.

scope의 `paper_input_estimate`는 required text unit의 safe-estimated tokens 합계, visual unit당 2,000토큰 reserve, read batch당 512토큰 envelope reserve, manifest/note control 4,000토큰의 합이다. P0 hard limit은 150,000토큰이다. prepare는 proposed full과 각 artifact 제외 시 estimate를 보여 주고, scope lock 뒤 required estimate가 limit을 넘으면 `OUTPUT_BUDGET_EXCEEDED`로 blocked다. main PDF 자체가 limit을 넘으면 page 일부를 조용히 제외하지 않는다. 이 estimate는 model별 실제 vision cost라고 주장하지 않으며 T10 Desktop 시험으로 보수성을 검증한다.

### `read <paper-id> --bundle-id <bundle-id> --run-id <run-id> (--unit-id <unit-id> | --batch-id <batch-id>) --client-request-id <client-request-id>`

단일 unit과 batch 요청은 같은 data schema를 사용한다. `data`는 `inventory_batch_id`, `request_mode`(`unit | inventory_batch`), `section_id`, `batch_index`, `batch_count`, `units[]`, `next_batch_id|null`, `invocation_capability_id`, `client_request_id`, `tool_use_id`, `task_id`, `session_id`, `turn_id`, `agent_id`, `agent_execution_id`, `context_stream_id`, `context_epoch`, `session_epoch`를 가진다. 이 authority/context 필드는 모두 consumed PreTool capability에서 오며 null일 수 없다. `inventory_batch_id`는 unit mode에서도 해당 unit이 속한 고정 batch ID다. `--unit-id`이면 `units` 길이는 1이고, `--batch-id`이면 고정된 inventory 순서의 unit 전체를 반환한다. 각 unit은 `artifact_ref_id`, `artifact_id`, `unit_id`, `media_kind`, `pdf_page|null`, `chunk_index`, `chunk_count`, `char_start`, `char_end`, `content`, `content_sha256`, `char_count`, `utf8_byte_count`, `estimated_tokens`, `start_marker`, `middle_marker`, `end_marker`를 가진다.

직렬화가 끝난 실제 data의 안전계수 적용 추정값이 12,000토큰을 넘으면 출력하지 않고 `OUTPUT_BUDGET_EXCEEDED`를 반환한다. caller는 host tool output budget을 최소 16,000으로 요청해야 하며 더 작은 budget이나 host truncation은 `OUTPUT_TRUNCATED` evidence다. JS 변수, 경로, 요약만 반환하고 `units[].content`를 Main에게 보여 주지 않는 호출은 읽기 증거가 아니다. Main은 inventory의 모든 batch를 순서대로 받아야 하며, 재열람을 제외한 최초 holistic pass에서 병렬 batch 호출을 사용하지 않는다.

### `render <paper-id> --bundle-id <bundle-id> --run-id <run-id> --unit-id <visual-unit-id> [--locator-id <locator-id>] [--render-dpi <integer>] --client-request-id <client-request-id>`

PDF page visual 또는 독립 image artifact 하나를 렌더링한다. `data`는 `artifact_ref_id`, `artifact_id`, `unit_id`, `render_id`, `pdf_page|null`, `path`, `pixel_width`, `pixel_height`, `render_dpi|null`, `bbox|null`, stored PNG bytes의 `image_sha256`, decoded RGBA의 `pixel_sha256`, `invocation_capability_id`, `client_request_id`, `tool_use_id`, `task_id`, `session_id`, `turn_id`, `agent_id`, `agent_execution_id`, `session_epoch`, `context_stream_id`, `context_epoch`를 가진다. authority/context 필드는 모두 consumed PreTool capability에서 오며 null일 수 없다. PDF full page는 `--locator-id` 없이 호출하며 `--render-dpi` 생략 시 144, locator 확대는 같은 PDF page의 `pdf_object` candidate/confirmed locator를 함께 주고 생략 시 288이다. PDF의 explicit DPI는 72–600 정수만 허용한다. 독립 image full view는 locator 없이 source를 decode하고 EXIF orientation을 적용한 native-size lossless PNG를 만들며, `image_region` locator를 주면 같은 oriented raster의 native-pixel crop을 PNG로 만든다. locator identity의 `image_sha256`은 source artifact bytes hash이고 render response의 `image_sha256`은 derivative PNG bytes hash다. image mode에서 `--render-dpi`를 주거나, `pdf_page | text_span` locator를 확대 입력으로 주거나, locator가 unit의 bundle/artifact/page와 다르면 `INVALID_ARGUMENT`다. 같은 schema에서 확대/crop이면 bbox가 채워진다. 파일 생성은 시각 확인이 아니며 root Main이 이미지 보기 도구로 `render_id`를 실제 연 host event가 있어야 coverage가 된다.

PDF raster는 locator canvas와 같은 rotation-applied CropBox를 쓰도록 Poppler `pdftoppm -png -singlefile -cropbox -r <render_dpi> -f <n> -l <n>`으로 만들고 Pillow decode 뒤 pixel/dimension과 RGBA-converted pixel hash를 검증한다. `pdf_object` 확대는 이 full-page raster에서 `bbox_ppm`을 half-up pixel edge로 변환한 뒤 crop하며 별도 page 좌표계를 만들지 않는다. stored PNG byte hash와 decoded pixel hash를 immutable render record/cache entry에 남기며 같은 render ID 재사용 시 둘 다 일치해야 한다. prepare 때 만든 full-page entry만 immutable bundle visual inventory에 들어가고, 이후 locator zoom/crop은 bundle manifest를 수정하지 않는다.

### `record <paper-id> --bundle-id <bundle-id> --run-id <run-id> --kind <kind> --payload-file <json-path> --client-request-id <client-request-id>`

Main·reviewer·hook이 구조화된 domain record를 직접 파일 편집하지 않고 state service를 통해 기록하는 유일한 경로다. run/answer lifecycle은 `prepare`, `answer`, `resume`과 trusted observer가 별도 상태 전이로 관리한다. caller가 `--kind`로 줄 수 있는 closed set은 `scope_confirmation | printed_label | locator_candidate | locator_confirmation | understanding_note | model_request | agent_execution | model_observation | audit_start | audit_result | finding_disposition | explanation_draft | flow_start | flow_result | flow_finding_disposition | explanation_finalized | user_pause | answer_grounding`이다. 저장소의 record kind closed set에는 result transaction만 만드는 internal child `audit_finding | flow_finding`이 추가되며 caller가 이를 직접 주면 `INVALID_ARGUMENT`다.

모든 호출은 caller가 생성한 CSPRNG UUIDv4 기반 `client_request_id = cr_<32 lowercase hex>`를 요구한다. 같은 `(run_id,client_request_id)`와 같은 canonical request를 다시 처리하면 기존 record와 완전히 같은 응답을 반환하고 event를 추가하지 않는다. 같은 ID를 다른 kind나 payload에 재사용하면 `STATE_CONFLICT`다. `payload_sha256`은 검증을 끝낸 payload의 canonical JSON hash이고, `record_id`는 `schema_version`, paper/bundle/run ID, record kind, entity ID, version/parent ID, payload hash의 canonical JSON hash에 `rec_`를 붙인다. timestamp, client request ID, 임시 payload 경로, event ID는 hash에서 제외한다.

`(run_id,record_id)`도 semantic idempotency key다. 다른 `client_request_id`로 동일 canonical record를 다시 보내면 최초 primary/child event를 포함한 원래 응답을 그대로 반환하고 새 event를 만들지 않는다. 새 occurrence가 필요하면 attempt, version/parent 또는 entity identity가 달라져 새 record ID가 되어야 한다. 이미 존재하는 record ID에 다른 canonical payload가 대응하면 hash collision 또는 손상으로 보고 `ID_MISMATCH`다. client-request index와 record index를 모두 같은 run lock 안에서 검사한 뒤에만 event sequence를 배정한다. `user_pause`, `explanation_finalized`처럼 task-binding state도 바꾸는 kind는 공통 lock order를 지켜 `project_reference_lock`을 먼저 얻은 뒤 run lock 안에서 이 검사를 수행한다.

성공한 호출은 아래 표의 primary event를 정확히 하나 만들며, audit/flow finding은 primary record 아래 immutable child record로 저장하고 finding마다 `subject_id=finding_id`인 child event를 하나 만든다. 따라서 finding 세 개인 result는 primary event 하나와 child finding event 세 개를 같은 lock에서 연속 event sequence로 append한다. 상태 전이가 필요하면 그 뒤 `state_transition`을 추가하고 `authority_event_id`로 primary event를 가리킨다.

| record kind | primary event kind |
| --- | --- |
| `scope_confirmation` | `scope_confirmed` |
| `printed_label` | `printed_label_recorded` |
| `locator_candidate` | `locator_candidate_recorded` |
| `locator_confirmation` | `locator_confirmed` |
| `understanding_note` | `note_versioned` |
| `model_request` | `model_requested` |
| `agent_execution` | `agent_execution_statused` |
| `model_observation` | `model_observed` |
| `audit_start` | `audit_started` |
| `audit_result` | `audit_result_recorded` |
| `finding_disposition` | `finding_dispositioned` |
| `explanation_draft` | `draft_versioned` |
| `flow_start` | `flow_audit_started` |
| `flow_result` | `flow_result_recorded` |
| `flow_finding_disposition` | `flow_finding_dispositioned` |
| `explanation_finalized` | `explanation_finalized` |
| `user_pause` | `user_paused` |
| `answer_grounding` | `answer_grounded` |

`audit_result.findings[]`의 각 항목은 `record_kind=audit_finding`, `entity_id=finding_id`, `version_id=null`, `parent_record_id=<audit_result record_id>`이고 primary result의 canonical payload에서 온 exact finding payload/hash를 사용한다. `flow_result.findings[]`도 같은 방식으로 `record_kind=flow_finding`, `entity_id=flow_finding_id`, `parent_record_id=<flow_result record_id>`를 사용한다. child `record_id`는 공통 record 공식에 이 kind/entity/parent/payload를 넣어 계산하며 parent와 같은 transaction에서만 생성된다. child event는 각각 `finding_recorded | flow_finding_recorded`이고 direct child 재생성은 primary result의 semantic idempotency 응답을 그대로 반환한다.

성공 `data`는 `record_id`, `record_kind`, `entity_id`, `version_id|null`, `parent_version_id|null`, `payload_sha256`, `child_record_ids[]`, `related_record_ids[]`, `primary_event_id`, `appended_events[{event_id,event_seq,event_kind,subject_id}]`, `run_state`, `context_stream_id|null`, `context_epoch`, `session_epoch`를 반환한다. `child_record_ids`는 audit/flow result가 같은 transaction에서 만든 finding record만 담고, `related_record_ids`는 start reservation처럼 primary record와 함께 만들었지만 parent-child 관계가 아닌 model-request/execution record를 생성 순서대로 담는다. 해당 record가 없으면 각 배열은 빈 배열이다. 한 transaction의 `event_seq`는 연속이어야 한다. payload file은 해당 run의 `pending-inputs/` 아래 regular file 하나, 최대 4 MiB만 허용하고 symlink와 hardlink count>1을 거부한다. record 후 canonical stored record가 authority이며 원본 payload path는 authority가 아니다.

authority는 kind별로 검사한다. root Main만 scope·printed label·locator confirmation·understanding note·finding disposition·draft·finalization·grounding을 요청할 수 있고, root Main과 assigned reviewer는 locator candidate를 요청할 수 있다. 현재 host session/turn/agent semantic event에 bind된 root Main이 `audit_start|flow_start` reservation을 요청하면 state service가 exact reviewer assignment, resolved model request, requested execution과 start record를 같은 transaction에서 만들고 commit한다. assigned reviewer의 matching one-use capability와 valid returned payload만 `audit_result|flow_result`를 만들 수 있다. `answer --finalize`는 current pending answer, finalized/grounded hash, blocker 없는 check, unchanged run event sequence를 다시 확인한 뒤 content/run 완료를 commit한다. hook은 content 완료를 선언하지 않고 delivery candidate의 exact message hash만 `sent_verified`로 승격한다.

각 kind의 필수 payload는 다음과 같다.

- `scope_confirmation`: `scope_kind`, `required_artifact_ref_ids`, `excluded_artifacts[{artifact_ref_id,reason_code,reason,user_confirmation_event_id}]`, `scope_limitations`, `user_turn_id`; disclosure string/hash는 이 payload와 bundle state에서 state service가 파생한다.
- `printed_label`: `artifact_ref_id`, `pdf_page`, `label_state`, `printed_label|null`, `render_id`, `visual_open_event_id`, `parent_version_id|null`
- `locator_candidate`: 위 locator union의 canonical identity와 non-identity metadata `printed_object_label|null`, `source_caption|null`, `discovered_by_agent_execution_id`
- `locator_confirmation`: `locator_id`, `reopen_event_id`, `render_id|null`, `confirmation=confirmed`
- `understanding_note`: note version 전체와 `parent_note_version_id|null`, `synthesis_epoch`
- `model_request`: model request immutable record 전체
- `agent_execution`: execution status snapshot 전체와 `previous_execution_record_id|null`
- `model_observation`: host observation immutable record 전체
- `audit_start|audit_result`: 내용 audit 계약의 exact stage/attempt record
- `finding_disposition`: `audit_id`, `finding_id`, disposition, Main locator/rationale/reopen evidence, `remediation_record_ids[]`, `supersedes_record_id|null`
- `explanation_draft`: current `response_attempt_id`, draft metadata, exact `content` string, `parent_draft_version_id|null`; state service가 content hash/path를 계산
- `flow_start|flow_result`: 흐름 audit 계약의 exact attempt/result record
- `flow_finding_disposition`: flow finding disposition record 전체
- `explanation_finalized`: finalization record 전체
- `user_pause`: `user_turn_id`, `reason`, `resume_phase`
- `answer_grounding`: 현재 질문과 재열람 순서를 묶는 grounding record 전체

note는 `note_entity_id = nt_<SHA-256(run_id,"understanding")>` 아래 선형 version chain을 이룬다. `note_version_id = n_<SHA-256(note_entity_id,parent_note_version_id,content_sha256)>`다. 원래 사용자 질문은 `answer_id = ans_<SHA-256(run_id,question_event_id)>`로 구분하고, 실제 응답 시도는 `response_attempt_id = rsp_<SHA-256(answer_id,authority_turn_event_id,root_main_agent_execution_id)>`로 구분한다. `draft_version_id = d_<SHA-256(answer_id,response_attempt_id,parent_draft_version_id,note_version_id,content_sha256,claims_sha256,scope_disclosure_sha256,requested_level,grounding_required,grounding_reasons,flow_review_required,flow_review_reasons)>`다. 같은 content로 되돌아가도 response attempt 또는 parent가 다르면 새 version이 되며, 같은 entity의 현재 head가 아닌 parent로 분기하려는 동시 수정은 `STATE_CONFLICT`다.

나머지 authoritative entity/version mapping은 다음과 같다. 표에서 `-`는 별도 version ID 없이 record occurrence와 parent record가 순서를 나타낸다는 뜻이다.

| kind | `entity_id` | `version_id` |
| --- | --- | --- |
| `scope_confirmation` | `sc_<SHA-256(run_id)>` | - |
| `printed_label` | `pl_<SHA-256(bundle_id,artifact_ref_id,pdf_page)>` | `plv_<SHA-256(entity_id,parent_version_id,payload_sha256)>` |
| `locator_candidate|locator_confirmation` | `locator_id` | - |
| `understanding_note` | `note_entity_id` | `note_version_id` |
| `model_request` | `model_request_id` | - |
| `agent_execution` | `agent_execution_id` | -; status snapshot은 `previous_execution_record_id`로 연결 |
| `model_observation` | `model_observation_id` | - |
| `audit_start|audit_result` | `<audit_stage_id>:attempt:<attempt_no>` | - |
| `audit_finding` | `finding_id` | -; parent는 같은 transaction의 `audit_result` |
| `finding_disposition` | `cfd_<SHA-256(run_id,finding_id)>` | `cfdv_<SHA-256(entity_id,supersedes_record_id,payload_sha256)>` |
| `explanation_draft` | `answer_id` | `draft_version_id` |
| `flow_start|flow_result` | `<flow_audit_id>:attempt:<attempt_no>` | - |
| `flow_finding` | `flow_finding_id` | -; parent는 같은 transaction의 `flow_result` |
| `flow_finding_disposition` | `ffd_<SHA-256(run_id,flow_finding_id)>` | `ffdv_<SHA-256(entity_id,supersedes_record_id,payload_sha256)>` |
| `explanation_finalized` | `answer_id` | `finalization_id` |
| `user_pause` | `up_<SHA-256(run_id,user_turn_id)>` | - |
| `answer_grounding` | `ag_<SHA-256(answer_id,response_attempt_id,session_epoch,context_stream_id,context_epoch,response_content_sha256)>` | - |

parent, audit, finding, remediation FK는 같은 paper/bundle/run/entity chain에 있어야 한다. 다른 run의 record, 아직 append되지 않은 finding, 현재 head가 아닌 disposition parent, finding보다 앞선 remediation을 참조하면 거부한다.

### `check <run-id> [--answer-id <answer-id>]`

run 검사는 자료 산출물, fixed scope의 모든 historical/synthesis reading·visual coverage, root Main execution 관측, 고정 이해 노트, 내용 audit 두 단계, finding 판정과 remediation, 미지원·미해결 항목을 검사한다. `--answer-id`를 주면 해당 answer의 current response attempt에 속한 draft·flow audit·finalization·grounding을 검사하고 delivery는 별도 상태로 보고한다. task binding에 content pending answer가 있으면 다른 answer ID로 우회할 수 없다. `data.decision`은 `allow | ready_to_finalize_content | block`이며, `checked_event_seq`, `content_completion_state`, `answer_delivery_state`를 함께 반환한다.

`observer_state`는 contributing execution/host event의 worst state를 `verified < request_accepted < partial < unavailable < conflict` 순서로 집계한다. `answer_delivery_state`의 closed set은 `not_finalized | content_ready | pending_observation | sent_verified | unknown`이고 answer를 선택하지 않으면 null이다. `pending_observation`과 `unknown`은 warning이며 content blocker가 아니다.

현재 response attempt의 content blocker가 없으면 `check`는 `ready_to_finalize_content`를 반환한다. 이어지는 protected `answer --finalize`가 answer를 `content_finalized`, attempt를 `content_finalized`로 만들고 content pending binding을 비운다. initiating answer에서는 같은 transaction이 run을 `complete`로 만들며, complete-run 후속 Q&A에서는 run을 유지한다. delivery candidate는 exact finalized hash를 보존한다. Stop이 같은 hash를 관측하면 `sent_verified/delivered`, 다음 사용자 turn이나 session boundary가 먼저 오면 `delivery_unknown`으로 닫는다. 어느 경우에도 delivery 관측 실패만으로 content/run 완료를 취소하지 않는다.

각 run은 authoritative state/event/record를 바꾸지 않는 파생 탐색 파일 `run-index.json`과 `summary.md`를 자동 갱신한다. index는 run/paper/bundle/task ID, state/scope, event·record kind별 count, answer별 content/delivery 상태, record head, project-relative evidence path를 담는다. 이 두 파일은 사람이 수십 개 record를 직접 탐색하지 않게 하는 cache이며 손실되면 authoritative event/record에서 재생성할 수 있다.

### `answer <run-id> (--begin | --resume --answer-id <answer-id> | --finalize --answer-id <answer-id> | --abandon --answer-id <answer-id>) --task-id <task-id> --user-turn-id <turn-id> --client-request-id <client-request-id>`

paper에 답하는 lifecycle은 원래 질문 identity와 실제 응답 시도 identity를 finalization보다 먼저 분리해 고정한다. `answer_id`, `question_event_id`, `question_turn_id`, exact `question_hash`는 질문의 의미적 identity이며 resume 뒤에도 바뀌지 않는다. 각 실제 응답 턴은 `response_attempt_id = rsp_<SHA-256(answer_id,authority_turn_event_id,root_main_agent_execution_id)>`를 새로 가진다. `authority_turn_event_id`는 현재 응답 턴을 시작한 semantic UserPromptSubmit event이고, 해당 event의 local task binding, host session/turn과 state service가 예약한 root Main execution binding이 정확히 일치해야 한다. 한 host response turn에는 current local root Main execution이 정확히 하나이며 lifecycle 명령은 새 실행을 임의로 만들지 않고 예약된 실행에 bind한다.

current complete run의 후속 질문에서는 skill의 첫 ReadPaper 작업이 `--begin`이어야 한다. initiating paper-read turn에서는 `prepare`가 run ID를 반환한 직후 같은 turn의 두 번째 명령으로 `--begin`을 호출해야 하며 그 전에는 `read`할 수 없다. `--begin`은 원래 trusted question event를 확인해 answer ID와 question identity를 만들고, current authority turn/root execution으로 최초 response attempt를 연 뒤 `answer_auto_resume_count=0`, `answer_status=drafting`, `response_attempt_status=active`를 task binding에 원자적으로 설정한다. 정상 최초 turn에서는 question event와 authority turn event가 같다. prepare 직후 Main이 잘못 종료되어 자동 continuation에서 `--begin`하는 경우에는 prepare operation의 원래 question event를 answer identity로 유지하고, nonce-matching continuation turn과 그 root execution을 response attempt authority로 사용한다. hook reason/nonce prompt를 새 논문 질문으로 사용하지 않는다. pending answer가 있으면 `ANSWER_PENDING`이다. paper-answer turn에서 current response attempt 없이 read/render/draft/finalization/grounding을 만들면 `ANSWER_NOT_STARTED`다.

answer status의 closed set은 `drafting | finalized_pending_stop | content_finalized | repair_requested | interrupted | sent_verified | delivery_unknown | abandoned`이고 response attempt status의 closed set은 `active | interrupted | superseded | content_finalized | delivered | delivery_unknown | abandoned`다. `--finalize`는 current finalized/grounded hash를 고정해 content 상태를 terminalize하고 delivery candidate를 만든다. `sent_verified`와 `delivery_unknown`은 같은 content 완료의 서로 다른 전달 관측 결과다. content pending binding은 `content_finalized`에서 비우며 delivery candidate는 blocking lock이 아니다.

`--resume`은 `interrupted` answer와 별도 explicit user turn을 요구하며, host가 이미 시작한 그 turn의 root Main execution에 bind해 같은 answer ID 아래 새 response attempt를 열고 answer를 `drafting`으로 전이한 뒤 `answer_resumed`를 기록한다. run도 paused라면 같은 user turn에서 먼저 run `resume`, 다음에 answer `--resume`을 호출하며 두 명령은 같은 root Main execution을 재사용한다. complete run이면 answer만 resume한다. `--abandon`도 별도 exact user turn evidence를 요구하고 current attempt를 `abandoned`, answer를 `abandoned`로 기록한 뒤 pending binding을 비운다. 어떤 경우에도 과거 attempt의 draft/finalization/grounding/전송을 current 성공으로 바꾸지 않는다.

nonce-matching 자동 continuation이 실제로 시작되면 state service는 target kind와 무관하게 pending answer가 있을 경우 그 continuation turn/root execution으로 새 response attempt를 원자적으로 열고 `answer_resumed(resume_kind=automatic_continuation)`를 기록한다. explicit resume는 `resume_kind=explicit_user`다. 새 attempt는 반드시 새 explanation draft를 만들며 이전 draft는 parent로 참조할 수 있을 뿐 current attempt의 finalization으로 재사용할 수 없다.

`--begin|--resume` 성공은 content pending 상태를 반환한다. `--finalize` 성공은 `answer_status=content_finalized`, `content_completion_state=finalized`, `answer_delivery_state=pending_observation`, `response_attempt_status=content_finalized`, finalized hash, run state, `pending_binding=false`, `delivery_observation_pending=true`를 반환한다. `--abandon`은 미완성 content만 명시적으로 종료한다.

### `resume <run-id> --task-id <task-id> --user-turn-id <turn-id> --client-request-id <client-request-id>`

Desktop 재시작이나 사용자 pause 뒤 사용자가 별도 turn에서 명시적으로 이어 달라고 요청했을 때 skill이 호출한다. state service는 stored task binding, current `session_started`와 `user_turn_started` event, paused run, 다른 active run 부재를 확인하고 `user_resumed`를 기록한 뒤 저장된 `resume_phase=prepared|reading|reviewing`으로 전이한다. prepared resume는 scope lock/첫 read 이전 상태를 그대로 복구한다. `data`는 `resume_phase`, `pending_answer_id|null`, `answer_resume_required`, `pending_reading_unit_ids`, `pending_visual_unit_ids`, `pending_audit_ids`, `pending_finding_ids`, `scope_limitations`, `session_epoch`, `main_context_stream_id`, `main_context_epoch`를 반환한다. pending interrupted answer가 있으면 같은 user turn/root execution에서 run resume 직후 `answer --resume`을 호출해야 한다. 진행 중이던 synthesis는 새 session에서 승계하지 않고 required text/visual 전체를 새 synthesis epoch에서 다시 연다. 이전 모델 기억이나 전체 원문이 현재 context에 그대로 존재한다고 주장하지 않는다.

### `delete <paper-id> --preview --task-id <task-id> --user-turn-id <turn-id> --client-request-id <client-request-id>`

삭제는 run event가 아니라 논문 디렉터리 밖 project-level deletion ledger에서 관리하는 두 단계 작업이다. preview는 CSPRNG UUIDv4 기반 `deletion_request_id = del_<32 lowercase hex>`를 만들고 `created` ledger record 하나만 append한다. paper/artifact/bundle/run bytes·reference·state는 전혀 변경하지 않는다. request는 요청 `task_id`, `paper_id`, `scope_digest`, 삭제할 canonical project-relative path와 manifest reference, 유지할 shared artifact, 모든 run ID/state, 프로젝트 전체에서 이 paper/current run/pending answer를 가리키는 모든 task binding의 lexical-sorted `{binding_path_hash,task_id_hash,before_sha256,before_image}`, 모든 pending answer와 nonterminal paper execution, 이 paper의 per-run response bytes와 non-delete project client-routing entry, prepare operation/work journal에 적용할 delete/tombstone/scrub action, 생성 15분 뒤의 `expires_at`, exact confirmation text를 가진다. `delete --preview|--execute` client route와 deletion journal은 tombstone 대상이 아니며 completed delete retry의 exact response authority로 남는다. `scope_digest`는 이 lexical-sorted 계획과 전체-project blocker/binding/journal snapshot의 canonical JSON SHA-256이다.

state service는 위 정보를 사람이 읽을 수 있는 순서로 직렬화한 exact Markdown `preview_text`와 `preview_content_sha256`도 생성해 응답한다. Main은 별도 문구를 앞뒤에 붙이지 않고 그 exact text를 사용자 응답 전체로 보내야 한다. logical Stop transaction이 current response attempt의 actual `last_assistant_message` hash 일치를 확인한 뒤에만 `preview_message_host_event_id`를 bind하고 request를 `presented`로 전이한다. tool output이나 `created` record만으로 사용자에게 범위가 보였다고 간주하지 않는다. request는 paper 디렉터리 안에 두지 않는다.

preview 성공 `data`는 `deletion_request_id`, `request_state=created`, `task_id`, `paper_id`, `scope_digest`, lexical-sorted `delete_paths[{path,expected_kind,expected_sha256|null}]`, `retain_shared_artifacts[{artifact_id,referrer_paper_ids}]`, `task_bindings[{binding_path_hash,task_id_hash,before_sha256}]`, `project_journal_actions[{kind,id_hash,action}]`, `blocking_run_ids`, `pending_answer_ids`, `pending_execution_ids`, `external_copy_warning`, `preview_text`, `preview_content_sha256`, `exact_confirmation_text`, `expires_at`, `client_request_id`를 가진다. journal action은 `delete_response_bytes | tombstone_client_route | scrub_prepare_operation | delete_prepare_work` 중 하나다. `external_copy_warning`은 delete가 현재 project의 ReadPaper-managed storage만 다루며 Git history, filesystem snapshot, backup 같은 외부 복사본을 지우지 않는다는 고정 문구이고 preview text에도 포함된다. blocker가 있어도 preview는 성공하지만 그 request로는 execute할 수 없다. blocker를 해소하면 blocker snapshot과 `scope_digest`가 달라지므로 기존 request는 `invalidated`가 되고, 사용자는 blocker가 비어 있는 새 preview를 실제로 본 뒤 별도 confirmation turn을 거쳐야 한다.

### `delete <paper-id> --execute --request-id <request-id> --confirm-paper-id <paper-id> --task-id <task-id> --approval-turn-id <turn-id> --client-request-id <client-request-id>`

presented request의 최초 execute는 presentation host event보다 sequence가 큰 별도 사용자 turn에서 confirmation text `DELETE <paper-id> <deletion_request_id>`가 UTF-8 byte-for-byte 일치하며, 같은 요청 task의 project host-ledger `user_turn_started`가 `approval-turn-id`와 일치할 때만 commit을 시작한다. 어느 task에 속하든 `prepared | reading | reviewing | needs_work` target run, target paper의 pending answer, `requested | running` paper execution이 하나라도 있으면 삭제하지 않는다. `paused` run은 exact preview의 run/binding snapshot에 포함되고 pending answer와 nonterminal execution이 없을 때만 삭제 대상이 될 수 있다. 이는 별도 run-abandon 명령 없이 사용자가 중단한 논문을 명시적으로 제거할 수 있게 하는 유일한 예외이며, delete commit이 모든 matching task binding을 함께 비운다. exact approval turn에서 delete command를 호출하는 root Main은 paper-answer execution이 아닌 `deletion_control_execution_id`로 표시하고 자기 자신만 blocker에서 제외한다. state service는 canonical path, shared-reference, target paper를 가리키는 모든 task binding과 blocker 집합을 프로젝트 전체에서 다시 계산해 preview의 `scope_digest`와 비교하며, 달라지면 request를 `invalidated`로 만들고 `DELETE_SCOPE_CHANGED`를 반환한다. 대상 경로는 project ReadPaper root 아래로 resolve되고 symlink를 통과하지 않으며 paper ID가 가리키는 경계 밖은 거부한다.

request state closed set은 `created | presented | committing | completed | invalidated | expired`다. `.readpaper/deletion-requests/<request-id>.json` 자체가 deletion transaction journal이며 canonical request/approval, immutable commit plan, exact success/error output envelope UTF-8 bytes와 hash, ordered operation log와 outcome을 보존한다. created/presented request가 만료되면 `expired`와 이유를 기록한다. presented execute는 common exclusive `project_reference_lock`을 얻은 뒤 scope/reference/binding/blocker/journal refs를 최종 재계산하고, exact delete/retain path, expected hash, 모든 matching task-binding before/after image, client-route tombstone와 prepare-journal scrub before/after image, ordered operation ID와 final response template를 가진 commit plan을 journal에 fsync해 `committing`으로 전이한다. 이 lock을 target staging rename, 모든 binding replace, shared-reference 재검사, `invocation_index_lock` 아래의 journal tombstone/scrub, unshared object unlink, completed journal commit까지 놓지 않는다. 따라서 prepare bundle/reference commit이나 다른 task-binding/journal writer가 재계산과 unlink 사이에 끼어들 수 없다. target paper를 같은 filesystem의 `.readpaper/deletion-staging/<request-id>/`로 먼저 atomic rename하여 새 접근과 per-run response replay를 막고, 계획에 든 모든 task binding을 각각 atomic replace로 비운 다음, client route를 content 없는 deletion tombstone으로 atomic replace하고 prepare operation에서는 normalized source/response bytes/work path를 제거해 paper ID, request/response hash, deletion request ID만 남긴다. 그 뒤 staged paper와 재계산상 다른 paper가 참조하지 않는 object bytes, stale prepare-work만 idempotent operation 순서로 제거한다. 각 rename/replace/unlink 뒤 영향받은 source와 destination을 포함한 모든 parent directory를 먼저 fsync하고, 그 다음 operation-done journal entry와 journal file/parent를 fsync한다.

execute의 state dispatch 순서는 고정한다. `completed`는 저장된 exact response bytes를 즉시 반환하고, `committing`은 새 approval/scope 판단 없이 저장 plan만 재생하며, `presented`만 새 approval과 scope를 검증해 commit을 시작한다. `created | invalidated | expired`는 `DELETE_CONFIRMATION_REQUIRED`, request/paper/task/client replay identity 불일치는 `STATE_CONFLICT` 또는 `DELETE_SCOPE_CHANGED`다. 중단 뒤 다음 execute 또는 startup recovery는 `committing` request의 저장된 plan과 operation log만 재생한다. crash가 staging 뒤, 일부 binding clear 뒤, object 제거 뒤, 결과 저장 전 어느 지점에서 나도 같은 operation ID와 expected before/after state로 재개한다. 이미 완료된 operation은 건너뛰고, 기대하지 않은 path/hash가 보이면 새 범위를 삭제하지 않은 채 `ID_MISMATCH` outcome을 journal에 fsync한다. 모든 planned binding/path 처리와 shared-reference 재검사가 끝난 뒤에만 `completed`와 exact 삭제·유지 결과 및 exact response envelope bytes를 fsync하고 그 다음 caller에게 반환한다. project 문서, 다른 paper, shared object, ReadPaper 상위 경로는 절대 대상이 아니다.

execute 성공 `data`는 `deletion_request_id`, `request_state=completed`, `outcome=deleted`, `task_id`, `paper_id`, `scope_digest`, ordered `operations[{operation_id,kind,target,before_sha256|null,after_state,status=completed}]`, `deleted_paths[]`, `retained_shared_artifact_ids[]`, `cleared_task_bindings[{binding_path_hash,before_sha256,after_sha256}]`, `completed_at`, `journal_sha256`, `client_request_id`를 가진다. 배열이 비면 빈 배열이며 null 가능 필드는 위에 명시한 hash뿐이다. crash replay와 completed retry는 이 exact envelope bytes를 반환한다.

## 독해와 시각 확인

본문, 참고문헌, 부록, 지원되는 supplementary 문서가 기본 독해 범위다. 사용자가 축소 범위를 명시하지 않은 한 부록을 조용히 제외하지 않는다.

Main은 inventory의 read batch를 문서 순서대로 모두 실제로 받은 뒤 모든 PDF 페이지 이미지와 독립 image를 연다. 작은 그림·표·수식은 필요한 배율로 다시 연다. batch마다 별도 의미 요약 JSON을 만들 필요는 없으며, Main은 읽는 동안 section 사이의 정의·가정·방법·실험·결론 관계를 계속 연결한다.

이해 노트를 만들기 위한 마지막 전체 통과는 하나의 `synthesis_epoch = {session_epoch,root_main_context_stream_id,context_epoch}` 안에서 끝나야 한다. `historical_coverage`는 과거에 읽은 사실을 보존하지만, note gate에 사용하는 `synthesis_coverage`는 해당 synthesis epoch에서 root Main이 다시 받은 모든 required text unit과 실제로 연 모든 required visual unit의 집합이다. note를 기록하기 전에 root Main stream compaction이 끝나거나 Desktop session이 바뀌면 이전 통과를 의미적으로 이어졌다고 가정하지 않고, 새 synthesis epoch에서 모든 required text와 visual unit을 처음부터 다시 연다. subagent compaction은 이 Main pass를 무효화하지 않는다. 반복 Main compaction 때문에 한 epoch 안에서 전체 통과를 끝낼 수 없으면 `blocked`로 보고한다.

이 과정이 끝난 뒤에만 다음 내용을 담은 하나의 버전 고정 이해 노트를 작성한다.

- 논문이 해결하려는 문제
- 핵심 정의와 가정
- 방법의 작동 방식과 중요한 수식의 역할
- 실험이 확인한 것과 확인하지 않은 것
- 결론과 방법·실험·부록 근거의 연결
- 핵심 그림·표·수식의 위치
- 불명확하거나 상충하는 부분

이해 노트는 원문을 대신하는 데이터베이스가 아니다. 정확한 수치, 직접 인용, 수식 조건, 논쟁적 해석, 검토자와의 충돌에는 특히 관련 페이지·객체를 자세히 다시 대조한다. note가 완성된 뒤의 compaction이나 session 변경은 complete run을 되돌리지 않지만, P0의 모든 paper answer는 current response attempt에서 원문을 다시 열고 grounding해야 한다.

## 검토와 설명 계약

### 이해 노트와 printed label

이해 노트는 `note_entity_id`, `note_version_id`, `note_seq`, `parent_note_version_id|null`, `bundle_id`, `run_id`, `synthesis_epoch`, `content_path`, `content_sha256`, `created_by_agent_execution_id`, `created_at`을 가진 immutable version이다. content path는 해당 run의 `notes/` 아래 regular UTF-8 Markdown이고 hash가 일치해야 한다. 개정은 현재 head를 parent로 하는 새 version/file을 만들며 기존 파일을 덮어쓰지 않는다. draft는 caller가 payload에 넣은 exact content를 state service가 `drafts/` 아래 immutable file로 쓰고 path/hash를 반환한다. `printed_label`과 locator confirmation도 `record`를 통해 root Main만 확정할 수 있고, 이전 판정 변경은 새 event와 변경 이유를 남긴다.

### 내용 audit

#### reviewer pre-spawn reservation

Main은 reviewer를 spawn하기 전에 `record --kind audit_start|flow_start`를 호출한다. 이 start transaction이 run lock에서 audit/flow sequence와 attempt 번호를 배정하고, `reviewer_assignment_id = rva_<SHA-256(run_id,assignment_subject_kind,assignment_subject_id,attempt_no,agent_execution_id)>`, CSPRNG `assignment_nonce`, exact spawn-input canonical digest, resolved `model_request`, requested `agent_execution_id`를 함께 영속한다. start response는 audit/stage/attempt/assignment/model-request/execution ID, nonce, input digest를 반환하며 Main은 이 값과 manifest/batch/visual/note/draft path+hash만 native spawn prompt에 넣는다. 따라서 spawn 뒤에 audit ID를 역으로 만들지 않는다.

start 시점의 `reviewer_agent_id=null`, `reviewer_synthesis_epoch=null`, coverage/finding/result 배열은 비어 있다. trusted `agent_started` host event가 same task/session, expected root Main parent execution, assignment nonce와 exact prompt/input digest를 가진 실행을 관측할 때 state service가 actual reviewer agent ID를 requested execution의 running snapshot에 bind한다. source-first 뒤 same-reviewer follow-up reservation은 `expected_reviewer_agent_id`를 이전 bound agent로 고정하고, flow replacement reservation은 허용된 replacement metadata를 고정한다. 다른 agent/parent/prompt가 nonce를 복사해도 binding에 실패한다. deadline 안에 binding되지 않으면 requested execution과 attempt를 `cancelled`로 terminalize한다.

result mutation capability는 bound assignment ID, reviewer agent/execution, stage/attempt와 exact result payload hash를 함께 검사한다. audit start record가 durable assignment authority이고 actual binding은 agent execution snapshot/event가 authority다. start transaction이 내부적으로 만든 model-request/execution records와 events는 start primary event와 같은 연속 sequence/response에 포함되며 replay 때 다시 만들지 않는다.

내용 audit의 `audit_id = ca_<SHA-256(run_id,role,audit_seq)>`는 두 stage 전체를 묶는다. 각 stage record는 `audit_id`, `audit_stage_id = cas_<SHA-256(audit_id,stage)>`, `attempt_no`, `parent_stage_result_record_id|null`, `recheck_finding_ids[]`, `recheck_results[{finding_id,status}]`, `role`(`math_visual | claim_experiment`), `reviewer_assignment_id`, `reviewer_agent_id`, `expected_reviewer_agent_id|null`, `agent_execution_id`, `bundle_id`, `run_id`, `stage`, `note_version_id|null`, `reviewer_synthesis_epoch`, `status`, `read_unit_ids`, `opened_visual_unit_ids`, `unverified_scope`, `findings[]`를 가진다. recheck status는 `resolved | still_present | not_verifiable`다.

`reviewer_synthesis_epoch`는 `{session_epoch,reviewer_context_stream_id,context_epoch}`다. coverage는 해당 reviewer stream만 집계하며 다른 reviewer나 Main의 read/open event를 승계하지 않는다.

- `stage`는 `source_first | note_comparison`이다. source-first 입력은 paper/bundle/run ID, manifest, 전체 required text batch inventory, 역할별 focus visual inventory, locator 계약, 결과 schema뿐이며 Main의 대화 기록과 이해 노트는 주지 않는다.
- source-first reviewer는 자신의 마지막 compaction 이후 하나의 reviewer synthesis epoch에서 required text 전체를 읽어야 한다. `math_visual`은 수식·그림·표·알고리즘 candidate가 있는 모든 page와 extraction warning page를 열고, `claim_experiment`는 method·experiment·result·limitation·appendix 연결 page를 연다. 누락이나 compaction 뒤 미재독은 `partial`이다.
- source-first가 `returned`인 뒤 같은 `audit_id`와 같은 `reviewer_agent_id`에 follow-up을 보내 note-comparison을 실행한다. note-comparison은 별도의 locally bound `agent_execution_id`, source-first result record ID, 정확한 `note_version_id`를 요구하지만 두 번째 전체 독해는 아니다. reviewer는 source-first의 불변 coverage·locator·finding map을 재사용해 note의 substantive claim을 대조하고, cited/conflicting/ambiguous/extraction-warning 또는 note revision 위치만 다시 연다. reviewer context loss, invalid source-first coverage, bundle 변경, materially rewritten note일 때만 required source pass 전체를 다시 수행한다.
- `status`는 `requested | running | returned | failed | cancelled | partial`이다. attempt 번호는 1부터 시작한다. `audit_start`는 attempt의 `requested` reservation record를 한 번 만들고, 대응 agent execution이 running일 때 derived status가 `running`이다. `audit_result`는 terminal `returned | failed | cancelled | partial` 중 하나를 한 번 기록한다. 허용 전이는 `requested -> running -> terminal` 또는 `requested -> failed|cancelled`뿐이며, terminal 뒤 재시도는 attempt 번호와 agent execution을 새로 만든다. `returned`만 필수 audit 완료 후보다.
- start record는 `reviewer_agent_id=null`, synthesis epoch null, `read_unit_ids=[]`, `opened_visual_unit_ids=[]`, `unverified_scope=[]`, `findings=[]`, `recheck_results=[]`를 요구한다. returned result는 bound reviewer ID와 non-null synthesis epoch, exact required read/focus visual set, `unverified_scope=[]`를 요구한다. partial result는 bound reviewer와 관측된 subset, nonempty `unverified_scope`를 요구한다. failed/cancelled result는 reviewer ID/epoch가 null일 수 있고 `findings=[]`, nonempty failure/unverified scope를 요구한다.
- source-first의 `recheck_finding_ids`와 `recheck_results`는 모두 빈 배열이다. 그 밖의 result는 start의 unique ordered `recheck_finding_ids` 각각에 대해 결과를 정확히 하나, 같은 순서로 가져야 하며 extra/duplicate/missing ID를 거부한다. failed/cancelled에는 전부 `not_verifiable`이다.
- 내용 reviewer stage deadline은 20분, 같은 stage attempt 상한은 2회, role별 audit chain 상한은 2개다. source-first returned 뒤 note-comparison에서 reviewer agent를 잃으면 다른 agent에게 중간 결과만 넘기지 않고 새 audit ID로 source-first부터 다시 시작한다. 상한 뒤 failed/cancelled/partial이면 run은 blocked다.
- 각 finding은 result `findings[]`의 1-based gapless 위치인 `finding_ordinal`, `finding_id = cf_<SHA-256(audit_stage_id,attempt_no,finding_ordinal,canonical finding_body)>`, `audit_id`, `audit_stage_id`, `category`, `statement`, `evidence[{locator_id,support_kind,rationale}]`, `locator_ids`, `related_finding_ids[]`, `comparison_state|null`, `unverified_scope`를 가진다. `canonical finding_body`는 `finding_id`와 `finding_ordinal`만 제외한 exact finding object다. array 위치/ordinal/ID가 어긋나면 거부하고 이 ordered payload가 internal child record로 그대로 projection된다. category closed set은 `coverage_gap | definition_equation_error | visual_misread | claim_evidence_mismatch | experiment_design_error | result_scope_overreach | limitation_appendix_omission | source_conflict | interpretive_ambiguity | other`이고 support kind는 `supports | contradicts | limits | locates`다. source-first의 comparison state는 null이다. note-comparison은 `confirmed_by_note | note_omits | note_contradicts | new_issue | not_applicable` 중 하나와 같은 audit의 source-first related finding ID를 기록하며 new issue만 빈 related list가 가능하다. 같은 ID에 다른 payload를 넣으면 `ID_MISMATCH`다.
- Main은 각 finding에 `accepted | rejected | unresolved_blocking | unresolved_interpretive` 중 하나를 기록하고 `main_locator_ids`, `reopen_event_ids`, `rationale`, `remediation_record_ids[]`, `supersedes_record_id|null`을 남긴다. locator candidate는 Main confirmation 없이는 disposition 근거가 될 수 없다.
- `accepted`가 이해 노트나 설명의 오류를 뜻하면 해당 finding 뒤에 생성된 descendant note/draft record가 `remediation_record_ids`에 없을 때 check를 통과하지 못한다. 하나의 finding이 note와 draft를 모두 바꾸면 두 record를 모두 연결한다. note를 바꿨으면 같은 audit/reviewer의 note-comparison 새 attempt가 parent result, 새 note version, recheck finding IDs를 받아 `resolved`로 확인해야 한다. still-present/not-verifiable는 완료를 막는다.
- `rejected`는 Main이 현재 source에서 다시 연 confirmed locator와 기각 이유가 없으면 유효하지 않다.
- 페이지 누락, source 충돌, 사실 오류 후보, 검토 범위 미달은 `unresolved_blocking`이며 완료를 막는다. 원문 자체의 정당한 복수 해석만 `unresolved_interpretive`가 될 수 있다.

### 설명 초안과 흐름 audit

논문 내용에 답하는 각 response attempt는 `answer_id`, immutable 원 질문의 `question_event_id`, `question_turn_id`, exact UTF-8 `question_hash`, current `response_attempt_id`, `authority_turn_event_id`, `draft_seq`, `draft_version_id`, `parent_draft_version_id|null`, `remediates_finding_ids[]`, `requested_level`(`brief | standard | detailed | tutorial`), `note_version_id`, exact Markdown `content`, `content_path`, `content_sha256`, `claims_sha256`, `scope_disclosure_sha256`, `created_by_agent_execution_id`, `grounding_required=true`, `grounding_reasons=["p0_all_paper_answers"]`, `flow_review_required`, `flow_review_reasons[]`를 가진 새 immutable 설명 초안을 만든다. P0에서는 모든 paper answer를 grounding 대상으로 고정해 조건 판정을 Main의 자기보고에 맡기지 않는다. `question_event_id`는 같은 task/run에 처음 bind된 trusted user question event이고, authority turn과 creator execution은 current response attempt identity와 정확히 일치해야 한다. resume attempt는 이전 attempt의 draft를 current로 재사용하지 않으며, 필요하면 그 draft를 parent로 하는 새 version을 만든다. state service는 JSON string의 Unicode value를 그대로 UTF-8 encode해 hash하고 file을 쓰며 newline·Unicode·공백 정규화를 하지 않는다. nonempty scope disclosure가 exact final block이 아니면 draft를 거부한다.

초안은 `answer_claims[]`도 가지며 `claims_sha256`은 claim ID를 제외한 ordered array의 canonical JSON hash다. 각 항목은 `claim_id = ac_<SHA-256(draft_version_id,span_start,span_end)>`, draft Unicode code-point 범위, `origin`(`paper_claim | main_inference | unsupported`), `locator_ids[]`, `support_note`를 기록한다. 고정 scope disclosure block을 제외한 substantive answer의 모든 paper-related assertion을 하나 이상의 claim span이 덮어야 한다. disclosure는 state service가 생성한 boilerplate이므로 `answer_claims` 대상이 아니며 어떤 claim span도 그 범위와 겹칠 수 없다. claim span끼리는 겹칠 수 있지만 content 범위를 넘을 수 없다. `paper_claim`은 하나 이상의 confirmed locator가 필수이고, `main_inference`는 그렇게 명시된 문장과 추론 전제를 지지하는 locator가 필요하다. `unsupported`는 locator를 꾸며내지 않고 논문이 답을 지지하지 않는 이유를 적는다. 최종 Markdown의 인용은 printed label과 PDF 위치를 합치지 않는다. printed label을 확인했으면 `p. <printed_label> (PDF page <pdf_page>)`, 없으면 `PDF page <pdf_page>`로 표시하고, 관련 figure/table/equation의 printed object label과 locator를 함께 표시한다.

`flow_review_required=true`가 되는 조건은 다음 네 가지 중 하나 이상이며 reasons에는 충족한 값을 모두 넣는다.

- 사용자가 해당 turn에서 흐름 검토를 명시적으로 요청함: `explicit_user_request`
- `requested_level=tutorial`: `tutorial`
- Main이 원문상 복수 해석·검토자 충돌·강한 해석을 포함한다고 표시하고 rationale을 남김: `contentious_interpretation`
- 같은 안전 추정기로 계산한 draft가 1,200토큰 이상임: `length_threshold`

어느 조건도 없을 때만 `flow_review_required=false`, `flow_review_reasons=[]`이고 `flow_audit_id=null`이 허용된다. required인데 audit이 없으면 누락이다. 흐름 검토자는 parent conversation을 받지 않고, exact 질문, 요청 수준, bundle manifest, required text 전체, draft가 의존하는 visual, 고정 이해 노트, 정확한 draft version, locator/result schema만 받는다. required text 전체와 draft-dependent visual은 하나의 reviewer synthesis epoch에서 직접 읽고 열어야 하며, 중간 compaction/session change 뒤 재독해하지 않으면 result는 `partial`이다.

흐름 audit 하나는 draft version 하나만 검토한다. `flow_audit_id = fa_<SHA-256(run_id,answer_id,flow_audit_seq,input_draft_version_id)>`이며 각 attempt record는 `flow_audit_id`, `attempt_no`, `answer_id`, `input_draft_record_id`, `input_draft_version_id`, `input_content_sha256`, `parent_flow_audit_id|null`, `recheck_finding_ids[]`, `recheck_results[{flow_finding_id,status}]`, `reviewer_assignment_id`, `reviewer_agent_id`, `expected_reviewer_agent_id|null`, `replacement_for_reviewer_agent_id|null`, `replacement_reason|null`, `agent_execution_id`, `reviewer_synthesis_epoch`, `status`, `read_unit_ids`, `opened_visual_unit_ids`, `unverified_scope`, `findings[]`를 가진다. recheck status는 `resolved | still_present | not_verifiable`다. 상태와 전이는 내용 audit과 같고 `returned`만 통과 후보다. start/result의 nullability·coverage·finding cardinality와 recheck bijection도 내용 audit 규칙을 그대로 적용한다. 수정된 draft에는 새 flow audit ID를 만들며 이전 audit ID를 재사용하지 않는다.

flow reviewer는 고정 scope disclosure의 존재와 exact suffix/hash만 확인하고 finding span이나 수정 제안의 대상으로 삼지 않는다. 논리·구조 finding은 disclosure 앞 substantive answer 범위에만 놓이며 remediation draft도 같은 disclosure bytes를 마지막 block으로 다시 붙인다.

flow reviewer deadline은 attempt당 15분이고 같은 audit attempt 상한은 2회다. remediation recheck는 parent audit의 same reviewer agent에 follow-up한다. 그 agent가 unavailable이면 `replacement_for_reviewer_agent_id`와 이유를 기록한 새 reviewer가 full source와 revised draft를 처음부터 읽고 old finding을 recheck할 수 있다. replacement chain은 한 번만 허용하며 이후 실패·partial은 answer를 block한다.

각 finding은 result `findings[]`의 1-based gapless 위치인 `finding_ordinal`, `flow_finding_id = ff_<SHA-256(flow_audit_id,attempt_no,finding_ordinal,canonical finding_body)>`, `category`(`logic_error | structure_problem | optional_improvement`), `severity`(`blocking | advisory`), `blocking_reason`(`logic_error | required_connection_missing | null`), `draft_spans[{span_start,span_end,text_sha256}]`, `statement`, `rationale`, `locator_ids`, `suggestion`, `unverified_scope`를 가진다. `canonical finding_body`는 ID와 ordinal만 제외한 exact finding object이고 array 위치/ordinal/ID가 어긋나면 거부하며 ordered payload가 internal child record로 그대로 projection된다. span은 input draft의 Unicode code-point 0-based half-open 범위다. `logic_error`는 항상 blocking, `optional_improvement`는 항상 advisory다. `structure_problem`은 논증에 필요한 연결이 실제로 빠진 경우에만 blocking이며 이때 reason은 `required_connection_missing`이다. 결론부터 설명하거나 원문과 다른 순서를 택했다는 이유만으로 오류라고 하지 않는다.

Main은 각 flow finding에 `accepted | rejected | deferred_optional | unresolved_blocking` 중 하나를 기록하고 `input_draft_record_id`, `main_locator_ids`, `reopen_event_ids`, `rationale`, `remediation_record_ids[]`, `supersedes_record_id|null`을 남긴다. `deferred_optional`은 advisory에만 쓸 수 있다. accepted finding은 해당 ID를 `remediates_finding_ids`에 넣은 direct child draft를 요구한다. blocking이면 그 draft에 대해 `parent_flow_audit_id`와 `recheck_finding_ids`를 가진 새 audit까지 실행해야 하며, 새 result의 recheck 상태가 `resolved`일 때만 닫힌다. `still_present | not_verifiable`은 계속 blocking이다. accepted advisory는 새 audit이 필수는 아니지만 child draft는 필수다. rejected는 근거와, source claim이 관련되면 현재 epoch의 confirmed locator reopen을 요구한다.

`explanation_finalized` record는 `finalization_id = fin_<SHA-256(answer_id,response_attempt_id,draft_version_id,flow_audit_id,remediated_finding_ids,final_content_sha256)>`, `answer_id`, `question_event_id`, `response_attempt_id`, `authority_turn_event_id`, `draft_version_id`, `flow_audit_id|null`, `remediated_finding_ids[]`, `scope_disclosure_sha256`, `grounding_required=true`, `grounding_reasons=["p0_all_paper_answers"]`, 실제 보낼 Markdown의 `final_content_sha256`, `created_by_agent_execution_id`를 가진다. draft, authority turn, creator root Main execution이 모두 current response attempt와 일치해야 한다. required flow audit이 returned가 아니거나 blocking finding·recheck가 남아 있거나 final hash가 draft content와 다르거나 current scope disclosure hash/suffix가 맞지 않으면 finalize할 수 없다. 문장을 바꾸거나 새 response attempt가 열리면 새 draft부터 다시 기록한다.

후속 답변은 `논문의 주장`, `Main의 해석`, `원문이 뒷받침하지 않는 부분`을 명확히 구분한다. P0의 모든 paper answer는 보내기 전에 `answer_grounding`에 `answer_id`, 원 질문의 `question_event_id`, `question_turn_id`, `question_hash`, current `response_attempt_id`, `authority_turn_event_id`, `root_main_agent_execution_id`, `session_epoch`, `context_stream_id`, `context_epoch`, 비어 있지 않은 `reopenings[{locator_id,reopen_event_id}]`, `response_content_sha256`, `grounding_reason="p0_all_paper_answers"`를 기록한다. 각 reopening은 current attempt에서 실제로 다시 연 confirmed locator여야 한다. 원 질문 event는 같은 task/bundle/run과 exact hash를 유지하면 되며 resume 뒤 현재 session/turn과 같을 필요는 없다. 대신 current authority turn event와 root Main execution은 같은 task/session/turn이고 `authority_turn.event_seq < first_reopen.event_seq`여야 한다. 모든 reopen, draft, finalization, grounding은 current response attempt의 동일 root Main execution에 귀속되고, reopen과 grounding은 같은 context stream/epoch에서 그 stream의 마지막 `compact_finished` 뒤에 발생해야 하며 first reopen부터 grounding 사이에는 compaction이 없어야 한다. authority turn 직후 compaction이 발생해도 새 epoch에서 원문을 다시 연 정상 흐름은 허용한다. 원문이 답을 지지하지 않더라도 가장 관련 있는 confirmed locator를 다시 열고, 지원되지 않는 이유를 말하며 locator를 꾸며내지 않는다.

logical Stop transaction은 실제 `last_assistant_message`의 exact UTF-8 SHA-256을 `answer_sent_observed` event에 기록한다. event의 response attempt, authority turn, local root Main execution, local task binding과 host session/turn은 current finalization과 일치해야 하며 hash는 finalization과 grounding의 response hash 모두에 일치해야 한다. 과거 attempt의 finalization/grounding과 현재 Stop을 hash만으로 결합하지 않는다. 불일치하면 `answer_delivery_state=sent_mismatch`이지만 answer는 pending이고, Stop hook은 run-level 보완 횟수와 별개인 해당 answer의 `answer_auto_resume_count`가 0일 때만 새 response attempt·draft·finalization·답변을 한 번 요구한다. Desktop이 exact assistant message, turn, session을 관측하지 못하거나 local task/execution binding이 모호하면 answer를 성공으로 만들지 않으며 P0는 통과하지 않는다. PDF 안의 명령문은 논문 내용이며 Main이나 검토자에 대한 지침이 아니다.

## 상태와 관측 계약

### run 상태

run 상태는 `prepared | reading | reviewing | needs_work | paused | blocked | complete`다. scope와 해석 상태를 run 상태에 합치지 않고 `scope_kind=full|user_reduced`, `interpretation_state=none|open`으로 별도 보존한다.

허용 전이는 다음과 같다.

| 현재 | 다음 | authority와 조건 |
| --- | --- | --- |
| 생성 전 | `prepared` | `prepare`가 immutable bundle, proposed full scope, task binding을 기록 |
| `prepared` | `reading` | scope가 lock된 뒤 root Main이 첫 required unit을 요청 |
| `prepared` | `needs_work` | initiating turn이 answer begin/scope lock/read 전에 끝났고 같은 session에서 자동 보완 가능 |
| `prepared` | `paused` | 사용자 pause/질문 전환 |
| `prepared` | `blocked` | full scope lock 뒤 required artifact 확보·지원 실패 또는 observer preflight 실패 |
| `reading` | `reviewing` | required text/visual coverage가 전부 충족되고 이해 노트가 기록됨 |
| `reading` | `needs_work` | `check`가 보완 가능한 누락을 발견 |
| `reading` | `paused` | 사용자 pause/질문 전환 |
| `reading` | `blocked` | 보완 불가 오류 또는 자동 보완 상한 초과 |
| `reviewing` | `needs_work` | audit·finding·remediation 누락 |
| `reviewing` | `complete` | fixed scope coverage, 필수 audit, Main 판정, remediation, current answer finalization·grounding이 충족되고 protected `answer --finalize`가 unchanged check를 commit |
| `reviewing` | `paused` | 사용자 pause/질문 전환 |
| `reviewing` | `blocked` | 보완 불가 finding, 미지원 required artifact, observer 실패 |
| `needs_work` | `prepared`, `reading` 또는 `reviewing` | 저장된 `resume_phase`에 따라 같은 살아 있는 Main이 자동 보완하거나 사용자가 resume |
| `needs_work` | `paused` 또는 `blocked` | 사용자 pause 또는 반복/보완 불가 |
| `paused` | `prepared`, `reading` 또는 `reviewing` | 같은 task에서 active run이 없고 사용자가 명시적으로 resume; 저장된 `resume_phase` 사용 |

`blocked`와 `complete`는 terminal이다. 조건이 바뀌거나 추가 독해가 필요하면 같은 immutable bundle에 새 run을 만든다. Main은 reading/reviewing/needs_work 전이를 요청할 수 있고 state service가 조건을 검증한다. protected `answer --finalize`만 deterministic content check 뒤 reviewing run을 complete로 만들 수 있으며, logical Stop transaction은 delivery 관측만 갱신한다. subagent는 audit record만 추가하며 run 상태를 바꿀 수 없다.

complete는 독해 상태만 terminal이다. current complete run에는 후속 Q&A를 위한 read/render/reopen event와 explanation draft, flow audit/disposition, finalization, grounding, delivery/model observation을 append할 수 있다. scope, understanding note, 내용 audit, 과거 finding disposition을 바꾸거나 coverage를 새 완료 근거로 재작성할 수 없다. 전체 이해 수정이 필요해지면 같은 bundle에 새 run을 만든다.

active run은 `prepared | reading | reviewing | needs_work` 상태다. 한 task에는 active run이 최대 하나다. paused run을 resume하려면 현재 active run이 없어야 한다.

complete의 사용자 표시 문자열은 다음 네 조합으로 고정한다.

| scope | interpretation | 표시 |
| --- | --- | --- |
| full | none | `논문 전체 독해 완료` |
| full | open | `논문 전체 독해 완료—미해결 해석 있음` |
| user_reduced | none | `요청 범위 독해 완료—제외 자료 있음` |
| user_reduced | open | `요청 범위 독해 완료—제외 자료 및 미해결 해석 있음` |

### 증거 수준

`prepared | emitted | tool_observed | main_review_recorded | unknown`

파일 생성은 `prepared`, stdout 전달은 `emitted`, host가 확인한 도구 사용은 `tool_observed`, Main이 원문과 대조해 남긴 판정은 `main_review_recorded`다. 이 중 어느 것도 의미적 이해를 자동 증명하지 않는다. 필요한 actor·범위·잘림·압축 정보가 관측되지 않으면 `unknown`이며 성공으로 승격하지 않는다.

SessionStart·UserPromptSubmit·agent/tool/hook/Stop처럼 run 생성 전이나 paused/complete 뒤에도 발생할 수 있는 host observation은 project-level host event ledger에 먼저 append한다. host event kind closed set은 `session_started | user_turn_started | agent_started | agent_stopped | pretool_authorized | tool_completed | tool_failed | hook_started | hook_completed | compact_started | compact_finished | assistant_message_observed | stop_observed | observer_error`다. host가 callback별 고유 ID를 제공한다고 가정하지 않으며, raw callback cardinality를 증명했다고 주장하지 않는다. `subject_id`는 kind별 host ID가 있으면 session/turn/agent/tool ID를 사용하고, 없으면 아래 semantic key에서 만든 local observation ID를 사용한다.

event별 semantic observation key는 다음과 같다.

- SessionStart: `H("SessionStart/v2",session_id,source,local_lifecycle_slot_id)`
- UserPromptSubmit: `H("UserPromptSubmit/v1",session_id,turn_id,SHA-256(exact UTF-8 prompt))`
- SubagentStart/SubagentStop: `H(event_kind,session_id,turn_id,agent_id,agent_type,stop_hook_active|null,SHA-256(last_assistant_message)|null)`
- PreToolUse/PostToolUse: `H(event_kind,session_id,turn_id,agent_id|null,tool_use_id,tool_name)`
- PreCompact/PostCompact: `H(event_kind,session_id,turn_id,context_stream_id,trigger,local_compaction_ordinal)`
- Stop: 아래 logical Stop slot과 Stop payload hash의 조합

SessionStart의 `source`는 host required enum `startup | resume | clear | compact`를 그대로 보존한다. `source=compact`의 local lifecycle slot은 같은 context stream에서 open된 compact transaction/ordinal이고 SessionStart를 새 Desktop session으로 처리하지 않는다. 그 밖의 source는 task-binding lock에서 현재 session transition을 위해 먼저 예약한 local lifecycle generation이다. 같은 slot의 transaction이 `prepared|committing`이면 동일 payload replay는 기존 operation을 재생한다. completed slot 뒤 동일 host tuple이 다시 왔지만 새 local transition/compact predecessor를 식별할 수 없으면 새 ordinal을 추측하지 않고 `observer_error/OBSERVER_UNAVAILABLE`로 닫는다.

같은 semantic key와 같은 payload는 최초 event와 저장 응답을 반환하고, 같은 key의 다른 payload는 `STATE_CONFLICT`다. compact pair는 context-stream lock에서 `idle -> pre_observed -> post_observed` phase와 ordinal을 CAS한다. replay인지 같은 host tuple을 가진 새 compaction인지 안전하게 구분할 수 없는 입력은 epoch를 올리지 않고 `observer_error/OBSERVER_UNAVAILABLE`로 닫는다. 이 dedupe가 끝난 최초 처리에만 task-local `host_event_seq`를 배정하고 `host_event_id = hev_<SHA-256(task_id,host_event_seq,host_event_kind,semantic_observation_key,subject_id,payload_sha256)>`를 만든다. `pretool_authorized`는 같은 transaction에서 one-use capability를 정확히 하나 만들거나 기존 것을 반환한다. host event는 occurred time, `subject_id`, local task binding, host session/turn/agent/tool identity, locally derived execution/context identity, host event kind, semantic key와 payload hash를 가지며 run ID는 요구하지 않는다. `prepare`, `answer`, `resume`, paper Q&A의 첫 command가 relevant host event를 검증하면 같은 transaction에서 run-scoped event로 bind하고 `source_host_event_id`를 남긴다. 삭제 approval은 run event가 아니라 이 ledger의 exact `user_turn_started`를 직접 검증한다. host event를 여러 run에 자동 귀속하지 않는다.

모든 run event는 `event_id`, run 안에서 lock으로 배정한 단조 증가 `event_seq`, `occurred_at`, `source_host_event_id|null`, `client_request_id|null`, `task_id`, `session_id|null`, `session_epoch`, `turn_id|null`, `agent_id|null`, `agent_execution_id|null`, `context_stream_id|null`, `context_epoch`, `actor`, `tool_use_id|null`, `paper_id`, `bundle_id`, `run_id`, `event_kind`, `subject_id`, `result`, `payload`를 가진다. `source_host_event_id`는 위 project ledger가 semantic observation key에서 만든 local record ID이며 host callback ID가 아니다. agent content/tool event는 context stream이 필수고 state-service-only event는 null/0이 가능하다. `(run_id,event_seq)`는 unique하다. host-bound event는 `(run_id,source_host_event_id,event_kind,subject_id)`, protected command event는 먼저 `(run_id,client_request_id,event_kind,subject_id)`, 그 밖의 tool-use event는 `(run_id,actor,tool_use_id,event_kind,subject_id)`, record-derived event는 `(run_id,record_id,event_kind,subject_id)`를 idempotency key로 사용한다. 같은 key와 같은 payload 재처리는 기존 event와 최초 transaction 응답을 반환하고, 다른 payload는 `STATE_CONFLICT`다. dedupe가 끝난 최초 처리에만 event sequence를 배정한다. event는 append-only이며 timestamp가 아니라 event_seq가 집계 순서를 정한다.

`event_id = ev_<SHA-256(run_id,event_seq,event_kind,subject_id,result,payload_sha256)>`다. event/host-event의 timestamp와 sequence는 ID가 배정된 뒤 바꿀 수 없고 같은 ID에 다른 canonical payload를 쓰면 `ID_MISMATCH`다.

`actor`는 `root_main | subagent | user | hook | host_observer | state_service | unknown`, `result`는 `succeeded | failed | cancelled | unknown`이다. event kind의 closed set은 다음과 같다.

```text
run_created | state_transition | source_prepared
session_started | user_turn_started
unit_emitted | render_created | visual_open_observed
compact_started | compact_finished
scope_confirmed | printed_label_recorded
locator_candidate_recorded | locator_confirmed
model_requested | agent_execution_statused | model_observed
audit_started | audit_result_recorded | finding_recorded | finding_dispositioned
note_versioned | draft_versioned
flow_audit_started | flow_result_recorded | flow_finding_recorded | flow_finding_dispositioned
explanation_finalized | answer_grounded | answer_content_finalized
answer_sent_observed | answer_delivery_unknown
auto_resume_requested | user_resumed | user_paused | observer_error
answer_started | answer_resumed | answer_interrupted | answer_abandoned
auto_resume_statused
```

필수 payload와 subject는 다음과 같다.

- `session_started`: subject는 session ID, payload는 host SessionStart identity, source, 새 session epoch다.
- `user_turn_started`: subject는 turn ID, payload는 host UserPromptSubmit identity와 exact prompt UTF-8 SHA-256이다. 공백·개행·Unicode를 정규화하지 않는다.
- `unit_emitted`: subject는 unit ID, payload는 `artifact_ref_id`, `artifact_id`, expected/observed content SHA-256, start/middle/end marker와 검증 결과, char/byte/page/chunk bounds, `observed_complete`, host tool result identity를 가진다. 한 batch tool use는 unit마다 event 하나를 만든다.
- `render_created`: subject는 render ID, payload는 unit ID, stored image/pixel hash, dimensions, render DPI, bbox다. 이것만으로 visual coverage가 되지 않는다.
- `visual_open_observed`: subject는 visual unit ID, payload는 render ID, expected/observed image and pixel hash, host image-open tool identity, `observed_complete`다.
- `compact_started|compact_finished`: subject는 state service가 context stream별로 만든 local compact-cycle ID이고, payload는 locally derived context stream ID, 이전/새 epoch, trigger, PreCompact/PostCompact semantic key다. unambiguous한 finished만 해당 stream epoch를 올린다.
- model/audit/finding/note/draft/finalization event: subject는 해당 domain ID이고 payload는 immutable record ID, version/parent, agent execution ID를 가진다.
- `answer_grounded`: subject는 answer ID이고 payload는 immutable question identity, current response attempt/authority turn/root Main execution, final response hash, current session/context stream/epoch, locator/reopen event 쌍을 가진다.
- `answer_sent_observed`: subject는 answer ID이고 payload는 current response attempt/authority turn/root Main execution, finalization ID, assistant turn ID, exact last-assistant-message UTF-8 SHA-256, logical Stop slot ID를 가진다.
- `answer_started|answer_resumed|answer_abandoned`: subject는 answer ID이고 payload는 immutable question identity, current/previous response attempt ID와 attempt status, answer status, answer auto-resume count, authority turn event, root Main execution ID, `resume_kind|null`을 가진다.
- `answer_interrupted`: subject는 answer ID이고 payload는 current response attempt ID, answer/attempt의 from/to status, reason code, authority host event ID, 마지막 authority turn/root Main execution ID를 가진다.
- `auto_resume_requested|auto_resume_statused`: subject는 continuation attempt ID이고 payload는 target kind/ID, origin Stop/session/turn, hook definition/hash, prompt nonce/hash, consumed counter와 attempt status/result를 가진다.
- `state_transition`: subject는 run ID이고 payload는 from, to, reason code, authority event ID를 가진다.
- scope/user action: subject는 scope/action ID이고 payload는 exact artifact refs, task/session/turn identity를 가진다.

`task_id`는 host callback 필드가 아니라 ReadPaper가 생성하고 durable task binding에 저장하는 local ID다. run은 이 local task ID에 고정되고 host `session_id`와 SessionStart `source`는 lifecycle observation으로 별도 보존한다. 같은 Desktop task의 host ID가 관측되면 진단 metadata로 저장할 수 있지만 local ID의 authority로 요구하지 않는다. SessionStart `source=compact`는 새 session이 아니므로 `session_epoch`를 올리거나 run/answer를 중단하지 않고, matching compact transaction에서 해당 agent context stream의 `context_epoch`만 정확히 한 번 올린다. `source=startup|resume|clear`가 현재 binding의 hard context boundary로 처음 확인되면 local `session_epoch`를 1 올리고 각 locally derived agent context stream을 epoch 0으로 연다. active/nonterminal 상태가 있으면 아래 recovery를 수행하며, 사용자의 explicit `resume <run-id>`가 이전 local task binding을 현재 lifecycle에 다시 결합한다. 다른 local binding의 event는 무시한다. nonterminal run의 boundary 이후 coverage는 명시적 `user_resumed` 뒤에만 추가한다. current complete run의 새 후속 Q&A는 `answer --begin`이 exact question event와 run binding을 열면 별도 run resume 없이 read/reopen/answer event를 추가할 수 있다. 이전 lifecycle의 pending answer와 current response attempt는 자동으로 `interrupted`가 되고 새 answer를 시작하지 않으며, 사용자의 별도 turn에서 `answer --resume` 또는 `answer --abandon`을 요구한다. 원 질문 identity는 보존하되 새 response attempt의 authority turn/session/local execution을 현재 semantic host event에 다시 bind한다. 필수 `session_id`, `turn_id`, `tool_use_id`, subagent identity처럼 event별 actor binding에 필요한 host 필드가 없거나 live probe에서 Main/subagent를 구분할 수 없으면 actor는 unknown이고 P0 completion에 쓸 수 없다. host-provided task/root-execution/context-stream/source-observation ID는 필수 조건이 아니다.

`source=startup|resume|clear` hard-boundary recovery는 `project_reference_lock` 다음 task-binding/run lexical lock 순서에서 먼저 현재 local task binding의 이전 lifecycle에 남은 모든 nonterminal agent execution을 terminalize한다. `source=compact`에는 이 recovery를 적용하지 않는다. execution snapshot은 `requested -> cancelled`, `running -> partial`로 만들고 authority SessionStart semantic host event를 append한다. payload 없이 중단된 pending reviewer audit/flow attempt의 result status는 state service authority가 허용된 `cancelled`로 고정하며 `findings=[]`, 모든 requested recheck는 `not_verifiable`로 기록한다. execution의 `partial`과 audit result의 `cancelled`는 서로 다른 계약이다. terminal snapshot 뒤 도착한 old-lifecycle semantic event replay는 diagnostic ledger만 확인하고 execution/result/run state를 되돌리거나 evidence를 추가하지 않는다. 그 다음 task binding의 nonterminal active run을 `paused`로 전이하고 prior `resume_phase=prepared|reading|reviewing`을 보존한 뒤 active binding을 비우며, pending answer/attempt는 source별 reason `session_startup | session_resume | context_clear`를 가진 `answer_interrupted` event와 함께 `interrupted`로 만든다. 이 recovery transaction이 끝나면 delete의 stale requested/running blocker도 남지 않는다. 새 lifecycle에서 자동 tool 실행이나 coverage 승계는 하지 않으며 별도 user turn의 explicit resume가 필요하다.

Main historical text coverage는 fixed `required_artifact_ref_ids`에서 생성된 모든 required text unit에 대해 다음을 모두 만족하는 unique unit set이다: `actor=root_main`, `event_kind=unit_emitted`, `result=succeeded`, `observed_complete=true`, expected/observed content hash와 start/middle/end marker가 일치하며 model observation이 `verified | request_accepted`다. visual coverage도 모든 required PDF page/image visual unit에 대해 같은 actor/model 조건과 successful `visual_open_observed`, matching render/image hash, `observed_complete=true`를 요구한다. referenced execution이 현재 local task binding의 root Main이고 status가 `running`이면 provisional set, `returned`이면 final set에 들어가며 다른 terminal status는 제외한다. `request_accepted` coverage는 host가 실제 effort receipt를 제공하지 않았다는 제한을 보존한다. 중복 event는 set을 늘리지 않고 subagent/unknown/stale binding event는 제외한다.

이해 노트 gate의 synthesis coverage는 위 조건에 더해 모든 event가 note와 같은 `{session_epoch,root_main_context_stream_id,context_epoch}`에 있고 그 Main stream의 마지막 `compact_finished` 뒤, note event 앞에 있어야 하며 각 PDF page의 current printed-label record가 같은 epoch의 visual-open event를 참조해야 한다. required unit 전체가 이 한 epoch에 없으면 과거 historical coverage가 완전해도 note를 만들 수 없다. answer grounding은 immutable 원 질문의 task/run/hash를 확인하되 그 event에 current context epoch나 current session/turn을 요구하지 않는다. current response attempt의 authority turn, root Main execution, draft/finalization/grounding/Stop은 같은 current response turn/session/execution이어야 하고, reopen과 grounding은 같은 Main stream/epoch에 있어야 하며 first reopen 이후 grounding 전까지 compaction이 없어야 한다.

## 자동 누락 보완과 재개

정상 흐름에서 Main은 종료 전에 `check`를 실행한다. 프로젝트 한정 hook은 의미 판단을 하지 않고 host event를 state service에 전달하거나 이미 계산된 check 결과를 집행한다. SessionStart는 session epoch를 열고, UserPromptSubmit은 exact user prompt hash와 turn을 기록하며, tool observer는 read/open/agent 실행을 상관시키고, PreCompact/PostCompact는 context epoch를 기록한다. 각 observer event는 30초 안에 대응 tool/turn identity와 상관되지 않으면 `observer_error`이며 completion evidence가 되지 않는다.

자동 continuation은 별도 immutable attempt lifecycle을 가진다. `continuation_attempt_id = ar_<SHA-256(run_id,target_kind,target_id,origin_session_id,origin_turn_id,counter_before)>`이고 target kind는 `run | answer`, status는 `reserved | requested | started | completed | not_started | timed_out | cancelled | abandoned_restart | hook_failed`다. record는 origin logical Stop slot/event ID, exact hook definition ID/config hash/script hash, prompt nonce와 exact prompt hash, `counter_before`, `counter_after=counter_before+1`, status parent를 가진다. P0에서 accepted reservation은 counter_before=0, counter_after=1이다. `(run_id,target_kind,target_id,origin_session_id,origin_turn_id)`에는 nonterminal attempt가 하나뿐이다.

allowed attempt transitions는 `reserved -> requested|hook_failed`, `requested -> started|not_started|timed_out|cancelled|abandoned_restart|hook_failed`, `started -> completed|cancelled|abandoned_restart`뿐이고 terminal status 뒤 전이는 없다. terminal record는 `target_outcome=repaired | target_repaired_pending_other | exhausted | not_started | observer_failed | user_cancelled | restart_abandoned`를 가진다. continuation target은 run coverage 보완, answer content 보완, 또는 `content_finalize`다. `content_finalize`는 새 response attempt를 열지 않고 current finalized/grounded attempt에 `answer --finalize`만 적용한다. 관측 실패는 미완성 content/run에는 기존 fail-closed 상태를 적용하지만 이미 content-finalized된 answer나 complete run을 되돌리지 않는다.

ReadPaper가 처리하는 Stop은 먼저 `logical_stop_slot_id = lss_<SHA-256(task_id,active_run_id|null,response_attempt_id|null,session_id,turn_id,root_actor_key,stop_hook_active,hook_definition_hash)>`를 만든다. mutable target/counter/check 결과는 slot key에 넣지 않는다. `raw_stop_payload_sha256`은 exact `last_assistant_message`와 host Stop 입력의 canonical non-secret metadata만 포함한다. `stop_transaction_id = stx_<SHA-256(task_id,"logical_stop",logical_stop_slot_id)>`인 project-level durable transaction은 slot마다 하나다. transaction state의 closed set은 `prepared | committing | completed | hook_failed`다. hook은 check를 다시 실행하기 전에 slot index를 먼저 조회한다. 기존 transaction과 같은 raw payload면 그 plan을 재생하고, 같은 slot의 다른 raw payload면 `STATE_CONFLICT`다. transaction이 없을 때만 lock 안에서 deterministic check input/result, target, `counter_before`를 snapshot하여 최초 plan을 만든다. 이 coalescing은 raw callback이 하나였다는 증명이 아니라 동일한 visible Stop에 권한 있는 effect를 하나만 허용하는 장치다. plan은 local Stop event ID와 raw payload hash, exact canonical hook-output UTF-8 bytes와 SHA-256, deterministic check input/result hash, 그리고 terminal execution, assistant-message projection, deletion-preview presentation, run/answer/attempt transition, continuation reservation/counter/event를 위한 ordered operation ID와 before/after digest를 가진다. `project_reference_lock` 다음 task-binding/run/transaction lexical lock 순서에서 plan과 `prepared`를 fsync한 뒤 `committing`으로 바꾸고, 각 operation을 semantic idempotency key로 적용·fsync하며, 모든 side effect 뒤 exact outcome과 hook output bytes를 `completed`로 fsync한 다음에만 그 bytes를 host에 반환한다.

동기식 ReadPaper Stop hook은 이 transaction 안에서 current local task binding, same session/turn에 bound된 root execution, `stop_hook_active=false`, 보완 가능 blocker, 해당 counter 0, pending attempt와 사용자 intervention 부재를 한 CAS로 검사한다. block plan은 CAS를 획득한 logical slot만 continuation attempt `reserved`와 counter 1을 함께 commit하고, attempt ID/nonce를 포함한 안전한 reason과 exact output을 고정한 뒤 `requested`와 `auto_resume_requested`를 같은 ordered plan에서 적용한다. 같은 Stop payload의 직렬·동시 재전송은 completed transaction의 exact 저장 bytes를 반환하며 event, counter, terminalization, delivery, preview presentation을 반복하지 않는다. crash가 `reserved` 뒤 `requested` 또는 output 저장 전에 일어나도 startup recovery나 같은 payload retry가 같은 plan을 재생한다. host가 output을 받았는지는 ACK가 없으므로 단정하지 않으며 counter를 되돌리거나 자동 재요청하지 않는다.

모든 matching Stop hook은 host에서 함께 평가될 수 있지만 ReadPaper는 aggregate ID나 suppression receipt에 의존하지 않는다. continuation 시작 60초는 exact output bytes를 durable `completed`로 저장한 시각부터 계산한다. host가 continuation에 `UserPromptSubmit`을 내보내면 그 semantic event가, 그렇지 않으면 Stop이 예약한 exact command의 첫 root `PreToolUse`가 one-use authority를 claim한다. run/answer 보완 target은 새 response attempt를 열지만 `content_finalize` target은 current attempt를 유지한다. 60초 안에 claim이 없으면 새 자동 요청을 만들지 않는다. 실제 Stop block과 one-use claim은 미완성 content의 자동 보완 기능에 대한 host 수용 조건이며, 이미 명시적으로 finalized된 content의 유효성 조건은 아니다.

Stop transaction은 actual assistant message host event projection을 먼저 계획한다. `created` deletion preview의 exact hash는 기존처럼 presentation만 commit한다. content pending answer의 check가 `block`이면 run/answer 보완을, `ready_to_finalize_content`이면 `content_finalize` continuation을 한 번 예약할 수 있다. delivery candidate가 있으면 actual hash가 finalized content hash와 정확히 같을 때만 `sent_verified`를 기록하고 candidate를 비운다. hash mismatch나 observer 부재는 delivery를 성공으로 만들지 않지만 content 완료와 run complete를 변경하지 않는다.

```json
{"decision":"block","reason":"ReadPaper: PDF 7쪽의 시각 확인 기록이 없습니다. 해당 페이지를 열고 누락 검사를 다시 실행하세요."}
```

Python이 Main을 호출하지 않는다. Codex 실행기가 hook 결과를 받아 같은 작업을 이어 가야 한다. 이 동작은 실제 Codex Desktop에서 관측돼야 하며 CLI 성공만으로 대체할 수 없다.

P0 자동 continuation 상한은 run-level 1회와 answer별 1회이며 설정으로 늘리지 않는다. `content_finalize`도 해당 answer의 1회 budget을 사용한다. Stop hook timeout은 10초다. reason에는 missing ID와 안전한 위치 설명만 넣고 원문 문장, audit 지시문, PDF 내부 명령을 재주입하지 않는다. Stop input의 `stop_hook_active=true`이면 새 block을 만들지 않는다. 미완성 run/answer의 보완이 시작되지 않으면 fail-closed 상태를 유지한다. content-finalized answer의 delivery candidate에는 자동 continuation을 만들지 않는다.

continuation의 Stop에서 다시 check한다. `content_finalize`가 성공하면 content/run은 이미 완료되어 있고 그 Stop은 exact message delivery만 관측한다. 같은 target kind의 두 번째 block은 만들지 않는다. 새 실제 user turn이 먼저 오면 미완성 run/answer continuation은 기존처럼 취소·pause/interrupted 처리하고, delivery candidate만 남은 경우에는 이를 `delivery_unknown`으로 닫은 뒤 일반 대화를 계속한다.

Main과 검토자의 actor 구분, 도구 출력 잘림, 압축 발생을 관측할 수 없고 보수적으로 안전하게 처리할 수도 없다면 P0는 미통과다. 자동 복귀만 작동하는 제한판을 완제품 MVP라고 부르지 않는다.

## context와 압축 정책

전체 원문이 현재 live context에 동시에 남아 있음을 외부에서 증명할 수 없으므로 이를 완료 조건이나 자동 true 필드로 만들지 않는다. 대신 모든 범위의 실제 전달·열람 증거와 원본 접근성을 보존하고, P0의 모든 paper answer에서 관련 원문을 현재 response attempt에 다시 연다.

각 run은 하나의 global context epoch가 아니라 state service가 만든 context stream별 epoch map을 가진다. `context_stream_id = cs_<SHA-256(session_id,root_sentinel|agent_id)>`이며 host가 직접 제공한 ID로 표현하지 않는다. 새 Main/reviewer context stream은 0에서 시작하고, 해당 stream의 unambiguous successful `compact_finished`마다 그 stream 값만 1 증가한다. 모든 read/render/open/audit/answer-grounding event는 `context_stream_id`와 발생 당시 epoch를 함께 기록한다. subagent compaction은 Main epoch를 바꾸지 않는다.

- 과거 Main stream epoch의 successful reading/visual event는 “그 unit을 Main이 읽은 적이 있다”는 immutable coverage로 남으며 압축 때문에 `unknown`으로 덮어쓰지 않는다.
- 압축 event는 별도 `live_residency_state=stale_after_compaction`을 만든다. 이 값은 historical evidence level과 다른 필드다.
- complete run 자체를 압축 때문에 미완료로 되돌리지는 않는다. 다만 다음 모든 paper answer는 관련 locator를 현재 response attempt와 epoch에서 다시 연 `answer_grounding` record 없이는 보낼 수 없다.
- 현재 epoch에서 필요한 locator가 다시 열리면 그 답변에 한해 `live_residency_state=current_for_answer`가 된다. 전체 논문 동시 상주를 의미하지 않는다.
- source 파일이 삭제·손상돼 다시 열 수 없으면 `source_access_state=missing`; 정상 접근 가능하면 `available`이다. missing 상태에서는 근거 재확인이 필요한 답변을 막는다.

프로젝트가 신뢰된 경우 프로젝트 전용 `.codex/config.toml`에서 자동 압축 시작을 늦춘다.

- P0 reference 값은 `model_auto_compact_token_limit = 230000`, `model_auto_compact_token_limit_scope = "total"`, `tool_output_token_limit = 16000`이다.
- `model_context_window`는 설정하지 않는다. host가 보고한 활성 Main 모델의 effective context가 258,400토큰보다 작거나 위 세 값의 실제 적용을 관측할 수 없으면 `UNSUPPORTED_MODEL_CONFIG` 또는 `OBSERVER_UNAVAILABLE`로 preflight를 막는다.
- 230,000 임계값은 reference context에서 28,400토큰의 총 headroom을 남긴다. 그중 답변 생성 reserve 8,000, control/envelope reserve 4,000을 침범하지 않도록 atomic unit 4,000, read batch 12,000, 개별 tool history output 16,000 상한을 함께 적용한다. 이는 usable context가 언제나 정확히 그만큼 남는다는 주장이 아니라 P0 시험에서 검증할 안전 계약이다.
- `0`, `-1`, 모델 최대치를 압축 해제로 해석하지 않는다. 설정은 새 Codex Desktop session에서 host 관측값, 첫 compaction 시점, tool output 잘림 시험으로 확인하며 현재 session에 소급 적용됐다고 가정하지 않는다.

압축을 늦추는 것은 전체 원문을 영구히 보존하거나 이해를 인증하는 기능이 아니다. `check`는 historical coverage와 current-answer grounding을 별도 집계하고 둘을 같은 unknown 값으로 합치지 않는다.

## 로컬 보존과 동시성

P0 storage layout은 다음으로 고정한다. `<prefix>`는 hash hex의 앞 두 글자다.

```text
papers/_objects/<prefix>/<artifact-id>/source
papers/_objects/<prefix>/<artifact-id>/derived/
papers/<paper-id>/bundles/<bundle-id>/manifest.json
papers/<paper-id>/runs/<run-id>/records/
papers/<paper-id>/runs/<run-id>/events.jsonl
papers/<paper-id>/runs/<run-id>/state.json
papers/<paper-id>/runs/<run-id>/evidence/
papers/<paper-id>/runs/<run-id>/notes/
papers/<paper-id>/runs/<run-id>/drafts/
papers/<paper-id>/runs/<run-id>/audits/
papers/<paper-id>/runs/<run-id>/pending-inputs/
papers/<paper-id>/runs/<run-id>/client-responses/
.readpaper/deletion-requests/<deletion-request-id>.json
.readpaper/deletion-staging/<deletion-request-id>/
.readpaper/stop-transactions/<stop-transaction-id>.json
.readpaper/host-events/<task-id-sha256>.jsonl
.readpaper/invocation-capabilities/<capability-id>.json
.readpaper/client-requests/<scope-key-sha256>/<client-request-id>.json
.readpaper/prepare-operations/<prepare-operation-id>.json
.readpaper/prepare-work/<prepare-operation-id>/
.readpaper/task-bindings/<task-id-sha256>.json
.readpaper/locks/
```

원본과 artifact-derived text/image는 content-addressed object store에 두고 bundle manifest가 occurrence를 참조한다. run record/event/evidence와 content-bearing exact client response는 해당 paper/run 아래 보존한다. project-level client-request file은 request/response hash와 per-paper response reference 또는 deletion tombstone만 가지며 원문 content를 복제하지 않는다. setup의 단일 writer는 `.gitignore`의 exact `# BEGIN READPAPER MANAGED` / `# END READPAPER MANAGED` marker 사이에 `/papers/`와 `/.readpaper/`를 한 번만 추가하고 기존 rule을 보존한다. 이미 tracked된 runtime file이나 외부 backup/history는 자동 rewrite하지 않으며 delete preview가 그 경계를 명시한다. 논문별 파일과 project-level deletion ledger를 자동 삭제하지 않는다. 파일·디렉터리 이름은 검증된 ID에서만 만들고 논문 제목·URL·archive 원문 경로를 경로 component로 쓰지 않는다.

task binding은 `.readpaper/task-bindings/`에 영속하며 file name은 exact task ID 문자열이 아니라 그 UTF-8 SHA-256이다. `project_reference_lock`은 bundle manifest/object-reference 생성·삭제, 모든 task-binding 생성·갱신·삭제, paper staging/unlink에 공통인 최상위 exclusive lock이다. 그 아래에서 task-binding path lexical order, run ID lexical order, operation/request ID lexical order로 잠그고, client route를 함께 publish/tombstone할 때 `invocation_index_lock`을 마지막에 얻는다. host ledger와 content-only run event append는 reference를 바꾸지 않을 때 별도 append lock을 쓸 수 있지만, task binding이나 reference를 함께 바꾸는 Stop/prepare/answer/resume/delete 및 해당 record transaction은 반드시 reference lock부터 잡는다. task binding은 fsync와 same-filesystem atomic replace로 갱신한다. Desktop 재시작 뒤에는 이 파일과 host ledger/run state를 교차 검증하며 memory state만으로 current/active/pending answer를 복구하지 않는다.

Main, 검토자, hook이 같은 run 기록을 만질 수 있으므로 append 기록과 상태 갱신은 actor를 보존하고 손상되지 않아야 한다. 구현 방식은 파일 잠금과 atomic append/replace 등 환경에 맞는 수단을 사용하되, 다음 불변조건을 만족해야 한다.

- 서로 다른 run 또는 session의 상태가 섞이지 않는다.
- subagent는 Main coverage나 run 완료 상태를 직접 승격할 수 없다.
- hook은 원문 문장을 높은 우선순위 지침으로 재주입하지 않는다.
- 중간 쓰기 실패 뒤 기존의 유효한 상태가 손상되지 않는다.
- object bytes는 immutable write-once이고 같은 artifact ID의 기존 bytes hash가 다르면 `ID_MISMATCH`다. record/event append와 head/state replace는 project-local advisory lock, fsync, same-filesystem temporary file rename 순서로 commit한다.

## 필수 검증

P0는 아래 증거가 모두 확보되어야 완료다.

1. 정답을 Main에게 숨긴 10페이지와 oversized-section fixture에서 page-bounded 4,000 unit, section-aligned 8-unit/12,000 batch가 결정론적으로 생성되고 Main이 문서 순서대로 각 페이지 앞·중간·끝 표식과 모든 시각 값을 받는다.
2. 작은 tool/history 출력 예산으로 앞·중간·끝·페이지 내부 잘림과 12,000 초과를 유도했을 때 `observed_complete`나 coverage를 성공으로 기록하지 않고 정확한 error를 반환한다.
3. Main은 일부만, reviewer는 나머지만 읽고 여는 시험에서 reviewer/unknown/invalid-model execution event가 Main historical 또는 synthesis coverage로 계산되지 않는다.
4. 실제 Codex Desktop에서 run 누락, complete-run answer 누락, 두 누락이 함께 있는 경우를 만든다. Stop이 10초 안에 CAS-reserved attempt ID/nonce가 든 exact block bytes를 durable logical-slot transaction에 저장하고, 60초 안에 nonce-matching continuation claim(`UserPromptSubmit` 관측 시 그 event, 미관측 시 same-session/turn 첫 exact root `PreToolUse`의 원자 claim)과 locally bound root Main의 추가 tool call이 한 번만 권한을 얻어야 한다. 함께 있는 경우 run repair를 `target_repaired_pending_other`로 닫은 뒤 answer repair를 별도 attempt로 수행하며, 재실패·observer 30초 상관 실패는 두 번째 same-target block 없이 각각 run `blocked`와 answer `interrupted`가 된다.
5. Stop plan 작성, reservation, status/event 적용, exact output 저장 사이에 각각 crash를 주입하고 동일 Stop payload의 직렬·동시 전달, 다른 `continue:false` hook, 중복 continuation prompt, 늦은 continuation, 실제 사용자 취소·질문 전환·pause, `stop_hook_active=true`를 시험한다. semantic replay는 같은 output/operation을 완성하고 counter나 state effect를 중복하지 않는다. 다른 hook 등으로 continuation이 시작되지 않으면 원인을 추정하지 않고 `not_started`로 닫으며 일반 대화를 다시 독해로 끌고 가지 않거나 nested continuation을 만들지 않는다. host prompt 생성 개수는 exactly-once라고 주장하지 않고 one-use nonce claim 및 authorized repair effect의 at-most-once를 검증한다.
6. 같은 모델/fixture에서 기본값과 compact 230,000/total·tool output 16,000을 각각 2회 비교해 실제 적용, 258,400 effective context와 28,400 headroom, 8,000/4,000 reserve, compaction epoch, historical coverage 보존, current-answer 재열람을 확인한다.
7. compaction 또는 Desktop 재시작이 note 전 synthesis 중간에 발생하면 기존 부분을 승계하지 않고 새 session/context epoch에서 required text/visual 전체를 다시 연다. SessionStart는 old requested/running execution과 payload 없는 reviewer attempt를 각각 지정된 terminal 상태로 만든 뒤 run을 pause하고 answer/attempt를 interrupt한다. 별도 user turn의 explicit run/answer resume는 original question identity를 보존하되 새 response attempt·root execution·draft를 만들고 current epoch locator를 다시 열어 grounding한다.
8. 직접 PDF, 공개 랜딩, 로컬 PDF, 문서형 supplementary, 안전한 ZIP, OCR 필요·미지원·손상 자료를 처리한다. private/metadata redirect, DNS rebinding, timeout/size/retry, archive traversal/symlink/nested/ratio bomb을 막고 supplementary snapshot 변화와 full/reduced label을 정확히 기록한다. user-reduced run에서는 closed-set reason code만 담고 filename·URL·문서 문장을 누출하지 않는 고정 disclosure가 모든 draft/final answer의 exact 마지막 block인지, full run에서는 disclosure가 empty인지 확인한다.
9. 두 내용 reviewer 각각에서 spawn 전 reservation이 audit/stage/attempt, nonce/input digest, model request/execution을 정확히 한 번 만들고 matching semantic agent-start만 actual reviewer를 bind하는지 시험한다. 같은 reviewer의 source-first와 fixed-note comparison을 별도 locally bound execution으로 수행하고, start/result cardinality, gapless finding child ordinal, recheck bijection, run-unique finding·Main disposition·descendant remediation을 강제해 실패·취소·partial·stale FK를 통과시키지 않는다.
10. 1,199/1,200토큰, tutorial, explicit, contentious trigger를 시험한다. 근거 없는 결론과 부록 조건 누락은 blocking으로 잡고 D1→A1→D2→A2 resolved를 요구하되 타당한 결론 우선은 순서만으로 막지 않는다. 짧거나 unsupported인 답변을 포함한 모든 paper answer에서 비어 있지 않은 current-attempt confirmed-locator reopening, substantive assertion의 claim origin, page/object citation, finalization·grounding·actual sent hash가 일치해야 한다. flow finding/claim span이 고정 disclosure를 침범하거나 reviewer remediation이 그 bytes를 바꾸면 거부한다.
11. printed label과 PDF page가 다른 fixture, 반복 object label, render scale 변화, 두 단·부록·빈 텍스트에서 네 locator variant와 bbox/span 검증이 정확하며 candidate를 Main confirmation 전에 disposition·인용에 쓰지 않는다.
12. exact absolute `.venv` direct grammar와 parser hash를 만족한 protected command만 PreTool one-use capability를 받고, matching PostTool만 read/render coverage를 만드는지 시험한다. 서로 다른 local task/run/session의 동시 record, multi-unit/multi-finding transaction, same/different client-request response-loss replay, 같은 semantic UserPromptSubmit/PostCompact/Stop 입력 재전송, capability 재사용, 다른 payload 재사용, stale parent, 중단된 쓰기에서 exact saved response와 sequence·epoch·actor·version chain이 한 번만 반영되고 conflict는 명시적으로 실패해야 한다. raw callback의 실제 개수를 확인했다고 주장하지 않는다.
13. 실제 공개 논문 한 편을 준비부터 ordered holistic read, single-epoch note, 두 내용 audit, Main 원문 대조/remediation, initiating answer delivery와 complete-run 후속 질문의 `answer --begin -> draft -> grounding -> Stop`까지 수행한다. 모든 turn에서 original question과 current response attempt/authority turn/locally bound Main execution이 올바르게 분리되고 paper claim/Main inference/unsupported와 exact locator가 남는 증거 경로를 확보한다.
14. deletion preview의 exact text가 actual assistant message로 표시된 뒤에만 별도 exact user turn execute가 가능하다. `prepared|reading|reviewing|needs_work` run과 pending answer/execution, changed scope, symlink 경계, 다른 paper/project/shared bytes를 보호한다. pending이 없는 paused run은 preview에 정확히 포함된 경우에만 삭제하고 target paper를 가리키는 프로젝트 전체 task binding을 snapshot/clear한다. blocker가 있던 preview는 해소 뒤 새 preview를 요구하고, common reference lock을 final recompute부터 completed journal까지 유지하며 각 commit 단계 crash의 staging/journal replay와 completed retry가 같은 결과로 멱등 완료되는지 확인한다. 삭제 뒤 fixture 원문 marker가 project-level replay/prepare/capability 파일에 남지 않고 과거 read client request가 content 대신 deletion tombstone을 반환해야 한다.
15. Main active model과 세 reviewer role의 override/default/inherit precedence가 model·effort별로 적용된다. host가 실제 model/effort를 모두 노출하면 `verified`, explicit 조합을 검증하고 spawn을 수락했지만 effort receipt를 노출하지 않으면 `request_accepted`로 기록한다. 지원하지 않는 조합, 명시적 거부, 관측된 model 불일치는 조용히 대체하지 않고 completion을 막는다. `request_accepted`는 실제 effort 확인으로 표시하지 않는다.

각 검증은 실행 환경, 입력, 기대 결과, 실제 결과, 관련 증거 경로를 남긴다. actor·잘림·압축을 관측할 수 없거나 Stop 자동 복귀를 실제 Desktop에서 증명할 수 없으면 P0 전체가 실패하며, 해당 빌드를 완제품이라고 표시하지 않는다.

## 비목표와 금지된 주장

- 모든 문장을 의미적으로 완벽하게 이해했다고 코드가 인증한다는 주장
- 파일이 존재하거나 stdout에 나왔다는 사실만으로 모델이 읽었다는 주장
- 검토자 다수의 동의가 진실을 증명한다는 주장
- 접근 제한 우회, PDF 안의 지침 실행, 미지원 자료의 조용한 제외
- 질문마다 별도 검색 파이프라인이나 데이터베이스를 만드는 일
- 자동 압축 임계값을 높이는 것을 무한 context로 표현하는 일
- P0 필수 관측을 확인할 수 없는 제한판을 완제품 MVP로 표현하는 일
