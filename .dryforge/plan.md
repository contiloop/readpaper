# P0 — ReadPaper 완제품 MVP 실행 계획

## 실행 원칙

이번 실행은 `.dryforge/spec.md`의 전체 제품을 한 번의 P0 출시 단위로 완성한다. 초반의 위험 검증은 뒤 기능을 생략하기 위한 별도 프로토타입 단계가 아니라, 구현 순서를 정하는 선행 증거다. 필수 Codex Desktop 동작이 검증되지 않으면 요구사항을 낮춘 제한판으로 바꾸지 않고 P0를 실패로 보고한다.

프로젝트에는 아직 제품 코드가 없지만 최소 Python 패키지 구조, 의존성 선언, 시험 실행 명령, 공통 namespace marker와 `.gitignore`의 exact managed block은 이미 만들어졌다. 재실행은 이 bootstrap을 검증할 뿐 다시 생성하거나 덮어쓰지 않는다. G0 정적 contract probe도 현재 빌드에서 통과했으며 상태는 `requires_live_desktop_probe`다. 다음 작업은 제품 CLI가 없는 상태에서 아래에 정의한 임시 probe entry를 설치·복원하는 live G0다. 제품 기능은 G0 live 통과 뒤 T1–T10이 구현하며 W1이 공통 등록을 연결한다.

## 구체 기본값

이번 P0는 추상적인 안전값만 두지 않고, 바로 구현할 수 있는 기본값을 함께 고정한다.

| 항목 | P0 기본값 |
| --- | --- |
| atomic reading unit | page를 넘지 않으며 안전계수 적용 추정 4,000토큰 이하다. |
| read batch | artifact·section 경계를 넘지 않고 unit 최대 8개, 직렬화 추정 12,000토큰 이하다. |
| section의 역할 | 이해 분할이 아니라 순차 전달 metadata다. 긴 section은 같은 ID 아래 여러 batch로 이어진다. |
| token 추정 | `o200k_base` count에 20%를 더해 올림한다. tokenizer가 없으면 UTF-8 byte 수를 보수적 상한으로 쓴다. |
| single-synthesis paper budget | text safe estimate + visual당 2,000 + batch당 512 + control 4,000 합계 150,000토큰 이하다. |
| effective context preflight | host 관측값이 최소 258,400토큰이어야 한다. |
| `model_auto_compact_token_limit` | 230,000이다. scope는 `total`이다. |
| `tool_output_token_limit` | 16,000이다. |
| context reserve | 총 headroom 28,400 중 답변 8,000, control/envelope 4,000을 예약한다. |
| ZIP 압축 파일 | 128 MiB 이하다. |
| ZIP `member_count` | 256 이하다. |
| ZIP 실제 확장 | member 64 MiB, 전체 256 MiB, member·전체 압축률 100:1 이하다. nested archive는 열지 않는다. |
| HTTP | connect 10초, read-idle 30초, 전체 120초, redirect 5회, transient GET 추가 재시도 2회다. |
| landing/download | landing HTML 5 MiB, 개별 PDF·supplementary 128 MiB 이하다. |
| PDF/image processing | PDF 200쪽, 200 inch/축, raster 20,000 px/축·100 MP, parse 30초, text 120초, render 30초/쪽·300초/artifact, prepare 600초다. |
| observer 상관 timeout | 30초다. |
| Stop hook timeout | 10초다. |
| continuation 시작 관측 | auto-resume event 뒤 60초 이하다. |
| 자동 continuation | run-level 최대 1회, pending answer별 최대 1회다. |
| content audit retry | stage 20분, stage attempt 2회, role audit chain 2개 이하다. |
| flow audit retry | attempt 15분, attempt 2회, reviewer replacement chain 1회 이하다. |
| Main | 현재 Codex 작업의 active model과 effort를 그대로 쓴다. |
| `math_visual` | `gpt-5.6-sol` / `xhigh`다. |
| `claim_experiment` | `gpt-5.6-sol` / `xhigh`다. |
| `explanation_flow` | `gpt-5.6-sol` / `high`다. |

raw callback마다 host가 발급한 고유 ID가 있다고 가정하지 않는다. 제품 상태에 영향을 주는 작업은 event별 semantic key와 state-service CAS로 먼저 dedupe한 뒤 sequence를 배정한다. SessionStart는 required `source`와 local lifecycle slot을 함께 쓰며 `source=compact`를 새 session으로 처리하지 않는다. tool event는 `tool_use_id`, user prompt는 `session_id + turn_id + exact prompt hash`, agent event는 `session_id + turn_id + agent_id + event kind`, Stop은 local active run/response-attempt와 `session_id + turn_id + root actor + hook-definition hash`로 만든 immutable logical slot을 사용한다. mutable counter와 check 결과는 slot key에 넣지 않고 최초 transaction plan 안에 snapshot하여 replay가 상태 변경 뒤에도 같은 transaction을 찾게 한다. compact event는 optional `agent_id`/`agent_type`으로 root/subagent stream을 구분하고 locally derived context stream의 open phase와 ordinal로 짝을 만들며, 같은 payload가 서로 다른 실제 callback인지 안전하게 구분할 수 없으면 `OBSERVER_UNAVAILABLE`로 실패한다. protected command는 client request key, run event는 source-host/client-request/tool-use/record key, record는 `(run_id,record_id)`로 멱등 처리한다. event sequence는 절대 되돌리지 않는다. race는 새 기록으로 덮어쓰지 말고 `STATE_CONFLICT` 또는 `AUDIT_INCOMPLETE`로 종료한다. heartbeat는 살아 있음 관측용이며 coverage 증거가 아니다. host callback ID, 직접 제공되는 root-execution/context-stream ID, hook aggregate ID, 실제 effort receipt가 없다는 사실은 capability boundary로 기록하되 G0의 blocker로 만들지 않는다. 필요한 local identity를 결정론적으로 만들고, host가 관측하지 못하는 값은 요청·검증·관측 상태를 분리해 표시한다. 필수 payload가 없거나 실제 Stop continuation과 at-most-once authorized repair effect가 live probe에서 실패할 때만 해당 host 기능을 차단한다.

## 구현 toolchain과 실행 명령

- Python `>=3.12` package를 `uv`와 `pyproject.toml`/`uv.lock`으로 고정한다. runtime dependency는 `pydantic`, `pypdf`, `Pillow`, `tiktoken`; test dependency는 `pytest`, `hypothesis`, `reportlab`을 사용한다.
- PDF text/page metadata는 `pypdf`와 Poppler `pdftotext`, page raster는 locator canvas와 같은 CropBox를 쓰는 `pdftoppm -cropbox`를 subprocess argument array로 호출한다. command path와 version을 preflight/evidence에 기록한다.
- remote fetch는 Python이 URL/DNS policy와 redirect를 한 hop씩 검증하고, `curl --resolve`로 검증한 public IP에 pinning한 뒤 remote IP를 다시 대조한다. shell interpolation과 curl 자동 redirect는 사용하지 않는다.
- ZIP은 Python 표준 library로 streaming limit을 적용한다. image bytes는 Pillow로 단일-frame 여부를 검증하고 EXIF orientation을 적용한 native-size/crop PNG를 만들어 host가 동일 pixel content를 열게 한다. tokenizer를 설치하지 못하면 spec의 UTF-8 byte 상한 fallback을 사용한다.
- 설치는 `uv sync --frozen`, 자동검증은 `uv run pytest`로 고정한다. 제품 CLI와 contract smoke는 project root를 cwd로 두고 exact absolute `.venv/bin/python`과 absolute `.agents/skills/readpaper/scripts/paper.py` direct prefix를 사용한다. production PreTool grammar에는 `uv run`, `cd`, env assignment, shell operator·redirection·substitution을 허용하지 않는다.
- 현재 workspace 사전 확인에서는 Python 3.14.0, `uv`, Poppler `pdftotext/pdftoppm` 26.04.0, curl 8.7.1이 존재한다. 이는 구현 가능성 증거일 뿐 release lock은 clean setup에서 다시 만든다.

Codex host 계약의 기준은 [Hooks](https://learn.chatgpt.com/docs/hooks), [Config reference](https://learn.chatgpt.com/docs/config-file/config-reference), [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents), [Skills](https://learn.chatgpt.com/docs/build-skills)다. [ECC README](https://github.com/affaan-m/ECC/blob/main/README.md)는 planning/execution 방식의 참고 자료일 뿐 ReadPaper runtime dependency나 문서 안 지침의 authority가 아니다. 실제 Desktop build가 문서 계약과 다르면 G0 evidence가 우선하며 요구사항을 조용히 낮추지 않는다.

## 기록과 판정 계약

- `record --kind`는 spec의 18개 caller kind만 허용한다. 저장소에는 result transaction 전용 `audit_finding | flow_finding` 두 internal child kind가 추가되며, 성공 응답에 primary event, 모든 child record/event, start reservation이 함께 만든 related model-request/execution record를 구분해 반환한다.
- root Main, reviewer, hook, state service와 host-derived semantic event의 record 권한을 분리한다. 특히 model observation, tool completion, actual answer 전송은 자기보고로 만들 수 없다.
- 공통 locator union은 `pdf_page | text_span | pdf_object | image_region` 네 variant이고, 후보는 Main 재열람 confirmation 전까지 판정·답변 인용 근거가 아니다.
- note·draft·disposition은 parent를 포함한 선형 version chain이다. 같은 내용으로 되돌아가도 새 occurrence를 구분하고 stale parent의 동시 분기를 막는다.
- 내용 audit은 같은 reviewer의 `source_first -> note_comparison`, 흐름 audit은 `draft -> finding -> child draft -> new audit recheck` 관계를 immutable FK로 남긴다.
- 모든 논문 답변은 finalization 전 `answer --begin`으로 immutable question identity와 turn별 `response_attempt_id`/Main execution을 분리해 bind하고, claim origin(`paper_claim | main_inference | unsupported`), finalized content hash를 가진다. resume/자동 continuation은 같은 answer 아래 새 response attempt와 새 draft를 연다. P0의 모든 paper answer는 현재 attempt/epoch의 locator reopen과 grounding을 요구하고 Stop observer가 같은 attempt의 실제 전송 hash를 대조한다.
- pause/resume는 run event지만 삭제 preview/execute는 paper 디렉터리 밖 project-level ledger에서 관리한다.

`check`는 historical coverage, single-epoch synthesis coverage, actor/model observation, audit/remediation, current-answer grounding, actual delivery, version/event integrity를 별도로 판정한다. 이 항목을 통과했다고 해서 의미적 완전 이해를 인증하지 않으며, 의미 판단은 Main의 원문 대조로만 이어진다.

## 작업별 실행 계약

### G0 — Codex Desktop host 계약 gate

**목표.** 제품 구현 전에 P0가 의존하는 host 관측과 same-Main continuation이 실제 사용자 Desktop에서 가능한지 증명한다. 이 gate는 축소판 출시 단계가 아니라 전체 P0의 fail-fast 조건이다.

**행동 계약.** 정적 probe는 현재 Desktop의 SessionStart·UserPromptSubmit·agent·PreTool/PostTool·PreCompact/PostCompact·Stop payload를 정확한 `hook_event_name.const`로 구분해 읽고, `session_id`, SessionStart `source`, `turn_id`, `agent_id`/`agent_type`, `tool_use_id`, exact prompt, `last_assistant_message`, `stop_hook_active`, model처럼 semantic identity와 actor binding에 필요한 필드의 required/presence/type/enum 계약을 확인한다. 특히 optional agent fields가 tool/compact schema에 있어 root와 subagent stream을 live probe에서 구분할 수 있어야 한다. host callback ID, root execution ID, context stream ID, aggregate ID가 직접 없다는 사실은 capability boundary로 기록하되 blocker로 만들지 않는다. ReadPaper `task_id`, root Main execution, context stream은 state service가 run binding과 host의 session/turn/agent 관계에서 생성하고, host가 제공한 ID처럼 표현하지 않는다. model/effort는 requested pair, host의 지원 조합 검증, 관측 가능한 effective value를 분리하며, effort receipt 부재를 실제 실행 확인으로 꾸미지 않는다. 이 정적 command 자체는 G0 `pass`를 만들 수 없고, live probe가 생성한 실제 event log를 검증하는 별도 gate만 G0를 통과시킬 수 있다.

live probe는 throwaway run과 최소 project hook으로 다음을 실제 Desktop에서 확인한다. 첫 Stop의 `decision:block`이 같은 살아 있는 작업에서 continuation prompt와 추가 Main 행동을 시작해야 한다. 동일 Stop payload의 직렬·동시 재전송과 transaction crash replay는 같은 logical Stop slot, exact output bytes, continuation counter를 재사용해야 한다. host가 continuation `UserPromptSubmit`을 내보내면 nonce가 든 exact prompt가, 내보내지 않으면 Stop과 same session/turn인 첫 exact root `PreToolUse`가 capability 발급과 같은 CAS에서 nonce를 claim해야 한다. 어느 경로든 claim은 하나만 성공하고, 나머지는 state mutation이나 protected tool을 실행하기 전에 차단되어야 한다. `stop_hook_active=true`, 이미 소비된 budget, 실제 사용자 prompt, session restart에서는 새 자동 continuation을 만들지 않는다. 다른 matching hook의 `continue:false` 때문에 continuation이 시작되지 않는 경우를 aggregate ID로 판별한다고 가정하지 않고, 60초 안에 nonce claim이 없으면 `not_started`로 닫아 자동 재요청을 금지한다. 이 제품이 보장하는 것은 host prompt 생성의 exactly-once가 아니라 authorized repair effect의 at-most-once다.

PreTool은 production과 같은 exact absolute `.venv/bin/python` + `.agents/skills/readpaper/scripts/paper.py` direct prefix로 호출한 probe-only `prepare` signature와 client request를 보고, `tool_use_id` 기반 one-use capability를 만들며 같은 semantic event replay에는 같은 capability를 반환해야 한다. 임시 CLI와 임시 hook은 같은 probe `parse_invocation`과 schema hash를 사용하지만 논문을 준비하거나 제품 state를 쓰지 않고 고정 probe envelope만 반환한다. 전체 여덟 command grammar는 T5/T11이 검증한다. hook timeout은 10초, correlation은 30초, stored Stop output 반환 뒤 continuation nonce claim은 60초다. probe는 원문을 해석하거나 제품 상태를 성공으로 만들지 않는다.

임시 hook 설치와 복원도 gate의 일부다.

1. 설치 전에 `.codex/hooks.json`, production-path `.agents/skills/readpaper/scripts/paper.py`, probe parser `.agents/skills/readpaper/scripts/_host_probe_parse_invocation.py` 각각의 존재 여부, permission bits, SHA-256을 기록한다. 존재하는 파일은 mode `0700`인 `mktemp -d` 안에 byte-for-byte 백업하며 이 경로와 백업을 commit하지 않는다. 첫-cycle G0에서 두 script path 중 하나가 이미 있으면 덮어쓰지 않고 clean throwaway checkout/worktree에서 G0를 다시 시작한다. 두 경로가 없을 때만 임시 파일을 만든다.
2. G0의 단일 writer가 기존 JSON의 관련 없는 hook을 그대로 보존한 채 probe entry만 merge하고, 필요한 임시 probe CLI/parser와 함께 atomic replace한다. 설치 직후 각 파일 SHA-256을 `installed_probe_hashes`로 기록한다. `.codex/config.toml`은 G0에서 바꾸지 않는다.
3. 사용자가 probe script와 변경된 hook 설정을 직접 검토한 뒤 프로젝트를 신뢰하고 새 Codex Desktop session을 시작한다. 프로젝트 신뢰 UI 상태 자체를 코드로 켜거나 끄지 않는다.
4. probe가 끝나면 현재 hook/임시 CLI/parser hash를 각 `installed_probe_hashes`와 비교한다. 하나라도 다르면 G0 이후의 외부 변경으로 보고 덮어쓰지 않은 채 T1을 차단한다. 모두 같으면 원래 파일이 있었을 때 정확한 bytes와 permission bits를 atomic restore하고, 원래 없었던 probe 파일만 제거한다.
5. 복원 뒤 세 경로의 존재 여부, permission bits, SHA-256이 설치 전 상태와 정확히 같은지 확인한다. 성공한 뒤에만 임시 백업을 제거한다. 중단되면 evidence에 백업 경로와 현재/기대 hash를 남기고 복구가 확인될 때까지 T1을 시작하지 않는다.

**작업 대상.** `tests/host_probe/`와 disposable evidence, probe 동안만 쓰는 `.codex/hooks.json` 임시 merge, product path에 원래 파일이 없을 때만 만드는 임시 fail-closed `paper.py`와 probe parser가 전부다. 공통 product hook/config는 등록하지 않으며 모든 probe 파일은 T1 전에 설치 전 상태로 반드시 복원한다. T5가 복원 이후의 실제 product `paper.py`를 단독 소유한다.

**검증.** 정적 probe가 필수 payload를 확인하고, mock가 아닌 실제 Desktop에서 Stop continuation, one-use nonce claim, duplicate/crash replay의 at-most-once effect, `stop_hook_active` 재진입 방지를 증명한 뒤에만 T1 이후를 시작한다. host가 문서화하지 않은 callback cardinality나 aggregate receipt는 성공 조건으로 만들지 않는다. 필수 payload 또는 위 live 행동이 unavailable이면 P0를 blocked로 보고한다.

### T1 — 불변 식별자, 상태 모델, 안전한 로컬 기록

**목표.** 논문 버전, run, actor, 관측 증거, audit, 설명 초안의 공통 계약과 손상되지 않는 로컬 상태 저장을 만든다.

**행동 계약.**

- main PDF SHA-256의 paper ID, artifact byte identity, full manifest의 bundle ID를 구분하고 immutable relation을 유지한다.
- 동일 bundle에 여러 run을 만들되 task·session·run·actor 상태를 섞지 않고 한 task의 active run을 하나로 제한한다.
- spec의 run 상태 전이표, scope/interpretation 축, 증거 수준, event/actor/result/audit 값의 closed set을 그대로 표현한다.
- append event와 현재 상태 갱신이 동시 writer·중단된 쓰기에도 유효한 이전 상태를 손상하지 않는다.
- spec의 `papers/_objects`, paper bundle/run, prepare/stop/deletion transaction, invocation capability, client-request index, durable task-binding, host-event layout을 만들고 ID-derived path, immutable object bytes, common project-reference lock, fsync+atomic rename을 적용한다.
- subagent나 unknown actor가 root Main coverage 또는 run 완료를 승격할 수 없다.
- event별 semantic observation key, `tool_use_id` 기반 PreTool capability, host/client-request/tool-use/record-derived event idempotency, event sequence, durable task binding, locally derived context stream/epoch, versioned record parent 관계를 검증한다. capability consume+in-progress client reservation은 invocation-index lock에서 먼저 fsync하고 lock을 놓는다. publish/tombstone은 reference→domain→invocation-index 순서를 지키고 completed replay도 reference 아래 route를 재검사해 lock inversion, consume crash, delete와 replay race를 모두 복구한다.
- `full_paper_in_live_context`, `understanding_verified` 같은 의미 인증 필드를 허용하지 않는다.
- record와 event의 spec closed set, domain ID, version ID, subject ID, authority를 그대로 표현한다. record는 immutable domain occurrence이고 event는 append-only 관측·전이 기록이다.
- 같은 batch/audit tool use에서 unit/finding별 event를 만들고 spec의 source-host/tool-use/record key로 멱등 처리한다. 같은 semantic event와 같은 canonical record를 다른 request ID로 재전송해도 sequence나 epoch를 늘리지 않는다. raw callback의 실제 개수는 증명했다고 주장하지 않는다.
- `model_request -> agent_execution status snapshots -> trusted model_observation`을 최초 독해, 모든 complete-run 후속 Q&A, continuation, run/answer resume와 reviewer stage/attempt마다 연결한다. `verified | request_accepted`가 아닌 execution은 completion-critical evidence에서 제외하고, 두 허용 상태의 증거 수준을 최종 보고에서 구분한다.
- `session_started`, exact-hash `user_turn_started`, compact epoch, response attempt, `answer_interrupted`, answer finalization/grounding/sent observation까지 같은 event stream에 넣되 project-level deletion ledger와 섞지 않는다. `source=startup|resume|clear` hard boundary는 이전 requested/running execution과 reviewer attempt를 terminalize한 뒤 run pause/answer interruption event를 원자적으로 복구한다. `source=compact`는 matching compact transaction의 context epoch만 갱신하고 session recovery를 실행하지 않는다.

**작업 대상.** ReadPaper Python 패키지의 model/state/event 모듈, 논문별 runtime 디렉터리 계약, 관련 단위시험. 공통 CLI 진입 파일과 hook 등록 파일은 수정하지 않는다.

**검증.** 명세의 모든 허용/금지 상태 전이, active-run cardinality, 다른 task/session/run 격리, event 중복/순서, actor 권한, context epoch, 동시 append/replace, 중간 쓰기 복구, enum 고정 시험이 모두 통과한다.

**판단 근거.** 관측 증거와 의미적 이해를 분리하는 이유는 제품의 핵심 가치가 “확인할 수 없는 성공을 만들지 않는 것”이기 때문이다.

### T2 — 출처 확보와 supplementary 경계

**목표.** 직접 PDF·공개 랜딩 페이지·로컬 PDF를 원본 변경 없이 확보하고, 공개 supplementary를 안전하게 분류한다.

**행동 계약.**

- HTTP(S) 직접 PDF, 공개 랜딩 페이지의 PDF 링크, 로컬 PDF를 지원한다.
- 랜딩 페이지에서 공개적으로 연결된 supplementary를 발견하되 로그인·유료 접근을 우회하지 않는다.
- PDF·텍스트·이미지 supplementary와 안전한 ZIP 내부의 해당 형식을 지원한다.
- ZIP은 압축 파일 128 MiB, member 256개, 실제 member 확장 64 MiB, 전체 확장 256 MiB, member·전체 압축률 100:1을 적용한다. nested archive, path traversal, symlink/hardlink/device/encrypted/중복 정규화 경로가 하나라도 있으면 archive 전체를 거부한다.
- HTTP는 spec의 connect/read-idle/total timeout, transient retry, redirect/download 한도를 적용한다. 각 DNS·redirect hop에서 public IP만 허용하고 URL credentials, nonstandard port, private/metadata 대역, TLS 검증 해제를 거부한다.
- prepare는 pre-bundle operation ID/client request로 멱등하며 fetch attempt는 journal 진단에만 기록한다. 각 download는 mode 0600 same-filesystem partial에 byte 0부터 쓰고 size/EOF/hash/type/fsync 뒤에만 atomic commit한다. timeout/cancel/crash partial은 그 operation 범위에서만 정리하고 response 유실 뒤 committing plan은 같은 run/event를 재생한다.
- 코드·데이터셋·노트북·음성·영상·인터랙티브 자료, OCR 필요 문서, 손상 자료는 원인과 범위를 구조화해 반환한다.
- URL과 로컬 파일명을 shell 문자열에 삽입하지 않고 데이터로 처리한다.
- 원본 URL 또는 로컬 출처, 확보 시각, content metadata, 해시, 자료 집합을 manifest 입력으로 반환한다.
- supplementary discovery를 prepare snapshot에서 닫고 artifact record의 closed kind/state, final redirect, detected type을 기록한다. ZIP container는 무결성 occurrence로 남기되 unit coverage는 만들지 않고, 각 accepted member를 별도 occurrence로 만들어 full scope가 container 안전 상태와 모든 member support/coverage를 함께 검사한다. 같은 main PDF에서 artifact 집합이나 상태가 달라지면 새 bundle을 만든다.

**작업 대상.** source fetcher, landing-page resolver, supplementary classifier, safe archive reader, 출처 경계 시험. T1의 ID/state 계약을 사용하며 PDF 내용 추출과 공통 CLI는 수정하지 않는다.

**검증.** 직접·랜딩·로컬 성공 fixture, supplementary label과 일반 링크 구분, redirect loop/invalid Location/content-type/magic mismatch, DNS rebinding/private-address redirect, timeout/retry/size limit, 접근 제한, 손상 파일, archive traversal/bomb/symlink/ratio/nested archive, 미지원 supplementary를 시험한다. mid-body timeout/cancel/process kill 뒤 partial cleanup, 같은 prepare request/response 유실/committing recovery에서 run·event 1회, 새 request에서 새 run, main 고정/supplement 변경 bundle도 통과해야 한다.

**판단 근거.** 읽을 수 없는 자료를 조용히 제외하면 “전체 논문”이라는 완료 표현이 거짓이 되므로 미지원 자료도 자료 집합의 상태로 남긴다.

### T3 — PDF 추출, 페이지 위치, 전체 렌더링, 범위 전달

**목표.** 원본 페이지 경계를 보존하는 텍스트와 시각 자료를 만들고, Main에게 누락 없이 전달할 수 있는 읽기 단위를 제공한다.

**행동 계약.**

- 페이지별 텍스트, 페이지 경계가 있는 full text, 전체 페이지 PNG, 추출 경고, manifest page 항목을 만든다.
- `pdf_index`, `pdf_page`, `pdf_label`, `printed_label`을 분리하고 시각 확인 전 printed label을 확정하지 않는다.
- spec의 네 locator variant와 정규화 `bbox_ppm` identity를 구현하고, printed label·render scale·pixel 좌표를 hash에서 제외한다. 후보를 root Main confirmation 없이 확정하지 않는다.
- atomic unit은 page를 넘지 않고 4,000토큰 safe estimate 이하며, read 출력은 artifact ref/unit ID, 정확한 page/chunk/char/byte bounds, content hash, 길이, 시작·중간·끝 표식을 가진다.
- 모든 PDF/prose/image를 immutable reading/visual unit inventory로 표현한다. anchored section 시작 offset을 page/prose 범위 안의 hard unit split point로 먼저 적용해 text unit이 section을 가로지르지 않게 하고, batch는 같은 artifact/section에서 unit 8개·12,000토큰 이하로 결정론적으로 생성한다.
- PDF 페이지 묶음과 페이지 내부 unit 모두 누락·중복·중간 잘림·끝 잘림을 탐지할 수 있다.
- 렌더링 생성과 Main의 실제 이미지 열기를 별개 사건으로 둔다.
- OCR 필요, 빈 텍스트, 두 단 순서 의심, 수식 문자 손실 후보를 성공으로 숨기지 않는다.

**작업 대상.** PDF extraction/rendering/locator/range 모듈과 page fixture. 출처 확보나 상태 store, CLI 진입 파일은 수정하지 않는다.

**검증.** 10페이지 표식 fixture, oversized page/section, synthetic section, 두 단·라벨 불일치·부록·빈 텍스트·스캔 페이지 fixture에서 unit/batch 완전 분할, 페이지 수, 범위 합성, hash, PNG, warning, locator가 정확하다. render scale이 달라도 같은 bbox locator가 나오며 의도적으로 자른 출력은 정상 전달로 판정되지 않는다.

### T4 — run lifecycle, 명시적 재개와 삭제

**목표.** 한 작업당 활성 논문 한 편이라는 정책, pause/block/complete 전이, Desktop 재시작 뒤 명시적 resume, 사용자 승인 삭제를 구현한다.

**행동 계약.**

- prepare의 proposed full scope를 artifact 목록 제시 뒤 한 번 lock하고, 첫 read 이후 변경을 금지한다. user-reduced scope는 exact excluded IDs와 user turn evidence를 요구한다.
- 한 Codex 작업에서 두 번째 논문을 활성화하려 하면 기존 run을 완료·pause·block 중 하나로 정리하게 한다. pending answer가 남아 있으면 current paper를 바꾸지 않고 먼저 explicit answer resume 또는 abandon을 요구한다.
- Desktop 재시작 recovery는 이전 session의 requested/running Main·reviewer execution을 cancelled/partial로 terminalize하고 payload 없는 reviewer attempt를 cancelled로 닫은 뒤 run/answer를 pause/interrupted로 만든다. 별도 user turn의 `resume`은 saved phase `prepared|reading|reviewing`과 same-task evidence를 복구하지만 이전 모델 기억을 복구했다고 표시하지 않는다. pending answer도 있으면 같은 root execution에서 run resume 뒤 answer resume를 호출하고, 진행 중 synthesis는 새 epoch에서 전체 required unit을 다시 연다.
- current complete run의 paper-answer turn은 finalization보다 먼저 `answer --begin`으로 immutable question identity와 최초 response attempt를 현재 Main execution에 bind한다. initiating read turn도 `prepare`가 run ID를 반환한 직후 같은 turn에서 별도 `answer --begin`을 호출하며 그 전에는 첫 read를 허용하지 않는다. 새 session/explicit resume/자동 continuation은 같은 answer 아래 새 response attempt와 새 draft를 만들고 이전 attempt를 재사용하지 않는다.
- 취소·질문 전환은 run을 paused로 만들고 자동 보완을 해제한다.
- run state, scope kind, interpretation state를 분리하고 spec의 전이표와 네 사용자 표시 문자열을 그대로 적용한다.
- 삭제 preview는 `created` project-level ledger record 하나만 append하고 paper/artifact/bundle/run의 bytes·reference·state는 바꾸지 않는다. state service가 exact preview Markdown/hash와 target paper를 가리키는 프로젝트 전체 task-binding snapshot을 만들고 Stop observer가 실제 사용자 응답과 일치시킨 뒤에만 `presented`가 된다. execute는 presented 뒤 별도 user turn, exact 문구, 전체-project scope 재계산이 일치하고 어느 task에도 active/needs-work run·pending answer·running paper execution이 없을 때만 수행한다. pending이 없는 paused run은 exact preview에 포함된 경우에만 모든 matching binding과 함께 삭제할 수 있다. blocker가 있던 preview는 blocker 해소로 scope digest가 달라지므로 invalidated되며 새 preview와 새 confirmation을 요구한다.
- content-bearing exact command response는 paper/run 아래 두고 project client index에는 hash/reference만 둔다. execute는 common project-reference lock을 최종 재계산부터 completed journal까지 유지하고, fsynced commit plan, same-filesystem staging rename, 모든 matching task-binding clear, client-route tombstone·prepare-journal scrub, unshared-object 제거 순서로 진행한다. 각 namespace 변경의 모든 parent fsync 뒤 operation-done을 기록하고, 단계별 crash 뒤 같은 plan/exact response를 idempotent replay하며 예상 밖 path/hash를 새로 삭제하지 않는다. 삭제 뒤 같은 과거 client request는 원문 response가 아니라 deterministic deletion tombstone을 반환한다.
- 자동 보존 만료나 새 해시로의 이해·인용 승계는 없다.

**작업 대상.** lifecycle/resume/delete 서비스와 단위시험. T1의 state API를 사용하며 source/PDF/CLI/hook 파일은 수정하지 않는다.

**검증.** 한 작업 내 두 논문 충돌, 여러 저장 논문, compaction/restart run·answer resume와 synthesis 재독해, pending answer begin/resume/abandon, pause 해제, 각 완료 상태, 다른 해시를 시험한다. 삭제는 preview 미표시·stale/mismatch, active/needs-work run 차단, pending 없는 paused run의 exact-snapshot 삭제, pending answer/execution, symlink 경계, shared artifact·task binding, commit 단계별 crash와 completed replay가 모두 명세대로 처리돼야 한다.

### T5 — `prepare/read/render/record/check/answer/resume/delete` 명령 통합

**목표.** T1–T4의 기능을 Main이 안정적으로 호출할 수 있는 하나의 내부 Python 인터페이스로 묶는다.

**행동 계약.**

- 여덟 명령의 spec v1 envelope, closed error code, command별 정확한 data schema를 제공한다.
- production invocation은 exact absolute `.venv/bin/python` + `paper.py` direct prefix와 공용 `parse_invocation` grammar만 허용한다. PreTool과 CLI parser/schema hash가 다르거나 wrapper/operator/unknown·duplicate flag가 있으면 실행 전에 거부한다.
- `prepare|read|render|record|answer|resume|delete`는 client request ID와 exact canonical request replay index를 사용한다. response-loss retry는 deduped original consumed capability 또는 fresh matching replay capability를 재인증하고 원래 exact bytes만 반환하며 상태·coverage·sequence를 다시 전이하지 않는다. 다른 payload나 actor/execution의 재사용은 conflict다.
- `prepare`는 task/user-turn/client-request와 PreTool capability를 받아 project-level operation journal로 멱등 처리하고 prepare operation/paper/bundle/run/task ID, proposed full scope, artifact와 전체 unit inventory만 반환한다. initiating turn의 answer ID/execution은 이어지는 별도 `answer --begin` 응답에서 받고, `record`가 locked required set을 만든다.
- `read`와 `render`는 client request와 one-use PreTool identity, artifact/unit ID를 필수로 사용해 main과 supplementary를 주소화한다. response context는 capability에서 받고 PostTool projection 뒤에만 coverage가 되며 cross-bundle/run을 거부한다.
- `record`는 18 caller kind와 2 internal finding child kind, pre-commit actor capability, canonical record/domain/version/child/related ID, primary·child·reservation-related event transaction, stale-parent 방지를 구현한다.
- `check`는 historical/synthesis coverage, locally bound Main execution과 model observation state, immutable version 관계, 두 단계 audit/finding/remediation, scope/interpretation, pending answer draft/flow/finalization/grounding/sent hash를 검사하지만 의미 이해를 인증하지 않는다. pending answer가 있으면 `--answer-id` 생략으로 검사를 우회할 수 없다.
- `answer --begin|--resume|--abandon`, run `resume`, `delete`는 current response attempt, exact output data schema와 T4의 사용자 제어 규칙을 우회하지 않는다.
- stdout의 machine-readable 결과와 사용자/진단 메시지가 서로를 손상하지 않는다.
- 잘못된 입력, 누락 파일, observer unknown, 내부 오류를 성공 exit로 바꾸지 않는다.
- `delete --preview`와 `delete --execute --request-id ... --approval-turn-id ...`를 별도 command mode로 제공하고 preview/execute의 exact success data, T4 presented evidence, deletion journal, exact confirmation, all-binding/client-route/prepare-journal scope digest를 우회하지 않는다.
- scope가 바뀌면 request를 invalidated로 만들고 `DELETE_SCOPE_CHANGED`, preview/별도 user approval이 없으면 `DELETE_CONFIRMATION_REQUIRED`를 반환한다.

**작업 대상.** `.agents/skills/readpaper/scripts/paper.py`와 command adapter, 명령 통합시험. 기능 모듈의 소유 파일은 필요한 interface 변경 외에는 수정하지 않는다.

**검증.** 각 명령의 성공·실패 golden contract, unit/batch 주소, record 권한/version parent와 multi-finding child events, two-step 삭제, 경로/ID 오염, cross-bundle/run, 잘림, missing observer, concurrent retry idempotency 시험과 전체 Python verify 명령이 통과한다.

### T6 — Main의 전체 독해 skill

**목표.** 사용자가 출처 하나를 주면 기존 Main이 준비부터 전체 이해 형성, 검토 조정, 완료 검사, 후속 질의응답까지 정확한 순서로 수행하게 한다.

**행동 계약.**

- Main은 inventory batch를 문서 순서대로 모두 받고 모든 페이지 이미지를 직접 연다. 최초 holistic pass의 batch read를 병렬화하지 않는다.
- 각 `read` tool call은 host output budget을 최소 16,000으로 요청하고 returned envelope의 start/middle/end/full hash와 host truncation state를 확인한다. 12,000 data 상한이더라도 더 작은 caller budget을 성공으로 간주하지 않는다.
- 이해 노트 직전에는 하나의 `{session_epoch,root_main_context_stream_id,context_epoch}`에서 required text/visual 전체를 다시 통과한다. root Main stream의 중간 compaction이나 session change가 있으면 새 synthesis epoch에서 처음부터 반복하고 그 안에 끝낼 수 없으면 blocked다. subagent stream의 compaction은 Main의 통과를 무효화하지 않는다.
- 본문·참고문헌·부록·지원 supplementary를 기본 범위에 포함한다.
- 전체 독해 뒤 하나의 버전 고정 이해 노트를 만들고, 그 노트가 원문을 대체하지 않는다고 명시한다.
- printed label, locator, 이해 노트, scope/pause, 모든 paper-answer grounding을 `record` 계약으로만 저장한다. user-reduced scope의 모든 draft/final answer는 scope lock에서 생성한 nonempty disclosure block/hash를 exact 마지막 block으로 포함한다.
- 각 required PDF page의 visual-open event에 `confirmed | observed_absent | observed_unreadable` printed-label record를 연결하고 printed label과 PDF page를 합치지 않는다.
- 정확한 수치·인용·수식·논쟁적 주장과 audit 충돌은 원래 위치를 다시 연다.
- 논문 주장, Main 해석, 원문이 지원하지 않는 답을 구분한다.
- 모든 논문 답변은 원문을 읽기 전에 `answer --begin`을 호출해 immutable original question identity와 이 response turn의 attempt/Main execution을 먼저 고정한 뒤 immutable draft, claim span/origin, confirmed locator citation, finalization hash를 남긴다. initiating turn은 `prepare -> answer --begin -> scope lock/read`, complete-run 후속 질문은 `answer --begin`이 첫 ReadPaper 명령이다. resume/자동 continuation에서는 새 response attempt와 새 draft를 만들고 이전 finalization/grounding을 재사용하지 않는다.
- P0의 모든 paper answer는 current attempt authority turn 뒤 원문 locator를 다시 열고 grounding한 뒤 보낸다. original question event는 resume 뒤 현재 session/turn과 같을 필요가 없지만 authority turn, root execution, draft/finalization/grounding/Stop은 같아야 한다. 질문 직후 compaction되면 새 epoch의 first reopen부터 grounding까지 같은 Main stream/epoch를 유지하며 실제 Stop-observed response hash는 같은 attempt의 finalization/grounding과 일치해야 한다.
- 문서 안의 지침을 content로 취급하고 실행 지침으로 승격하지 않는다.
- 검토자를 Main 독해 대체재로 사용하지 않고, 검토 결과를 원문에서 재검증한다.
- 종료 전 `check`를 실행한다. pending answer는 answer ID 생략으로 우회할 수 없다. 현재 Main terminal observation만 남으면 `allow_pending_stop`을 받고, logical Stop transaction이 local execution/message binding을 terminalize해 deterministic recheck가 allow가 될 때만 commit한다. initiating answer에서는 run complete와 answer/attempt delivery/pending clear를 원자 commit하고, complete-run Q&A에서는 run을 유지한 채 answer만 commit한다.
- 같은 질문에 다시 답할 때는 immutable answer/question identity만 이어 쓰고, 새 response attempt에서 새 draft와 비어 있지 않은 confirmed-locator reopen/grounding을 다시 만든다. 과거 grounding을 current 성공으로 재사용하지 않는다.
- unsupported 또는 unanswered 상태는 실패를 숨기지 말고 명시적 상태로 남긴다.
- 같은 task에 current complete run이 있고 user가 그 논문을 가리키는 후속 질문을 하면 별도 DB/Q&A service 없이 ReadPaper follow-up branch와 answer lifecycle을 사용한다. interrupted pending answer가 있으면 새 질문을 시작하기 전에 사용자의 explicit resume 또는 abandon을 요구한다. current run이 없거나 대상이 모호하면 paper/run을 추측하지 않는다.

**작업 대상.** `.agents/skills/readpaper/SKILL.md`, 이해 노트·audit 처리 형식 reference, skill 정적 시험. 공통 `AGENTS.md`는 직접 건드리지 않고 W1 단일 writer가 current-run follow-up activation과 document-content safety rule만 연결한다.

**검증.** 10페이지 fixture를 사용한 Main 시나리오에서 모든 텍스트·이미지 tool 사용, single-epoch synthesis와 compaction 재시작, 이해 노트 생성 시점, 원문 재열람, printed/PDF page 인용, unsupported/unknown 처리, 문서 prompt injection 무시를 확인한다. 후속 질문마다 answer begin과 별도 locally bound Main execution이 있고, question 직후 compaction 재열람, pending answer resume/abandon, paper-claim/inference/unsupported 구분과 actual response hash가 증거로 남아야 한다. user-reduced fixture에서는 state service가 만든 고정 disclosure/hash가 모든 초안·최종 답변의 마지막 block이고, full fixture에서는 empty이며, claim span이 disclosure를 포함하지 않아야 한다.

**판단 근거.** 별도 orchestration 서비스가 아니라 현재 Main을 독해 주체로 두는 이유는 사용자가 원한 전체 논문 모델과 후속 대화의 연속성을 같은 작업 안에서 유지하기 위해서다.

### T7 — 수식·시각 및 주장·실험 독립 검토

**목표.** Main의 전체 독해가 끝난 뒤 두 내용 검토자가 원문 우선으로 독립 검토하고, Main이 그 결과를 원문과 대조할 수 있게 한다.

**행동 계약.**

- 두 역할 모두 전체 추출 텍스트를 직접 읽고, 담당 페이지 이미지를 집중 확인한다.
- 첫 요청에는 Main 이해 노트를 주지 않는다. 첫 결과 뒤 같은 검토자에게 고정된 노트 버전을 보내 대조 결과를 분리한다.
- spawn 전에 root Main이 `audit_start` reservation을 호출해 audit/stage/attempt, reviewer assignment nonce, exact input digest, model request와 requested execution ID를 영속한다. trusted agent-start가 parent Main, nonce/prompt hash와 actual reviewer agent를 bind하기 전에는 result authority가 없다.
- source-first spawn은 parent history 없이 reservation이 반환한 `paper/bundle/run/audit/stage/attempt/assignment/execution ID+nonce`, manifest path/hash, ordered required batch IDs, role focus visual IDs, locator schema, result schema, output payload path만 전달한다. 경로 수신은 읽기 완료가 아니며 reviewer 자신이 client-bound `read`/`render`/image-open을 실행한다.
- note-comparison은 same reviewer agent에 follow-up으로 source-first result record ID와 exact note version/path/hash만 추가하고 새 locally bound execution과 `verified | request_accepted` model observation을 연다.
- 각 결과는 원문 위치, 불일치/확인 사항, 근거, 미확인 범위를 갖는다.
- 같은 audit ID/reviewer의 source-first와 fixed-note comparison stage를 서로 다른 locally bound execution으로 연결하고 attempt별 run-unique finding ID를 만든다. model observation은 `verified | request_accepted`만 허용하며 두 상태의 의미를 섞지 않는다.
- start/result nullability, returned/partial/failed/cancelled coverage cardinality, gapless finding ordinal→internal child projection, requested recheck ID와 result의 1:1 bijection을 spec대로 검사한다.
- 부분 독해, timeout, 실패, 취소는 성공이 아니다.
- reviewer는 Main coverage와 run 완료를 변경할 권한이 없다.
- Main 판정은 accepted, rejected, unresolved_blocking, unresolved_interpretive를 사용하고 원문 재확인 위치를 남긴다.
- accepted finding으로 note를 고치면 same reviewer의 새 note-comparison attempt가 parent result와 finding IDs를 받아 새 note에서 resolved를 반환해야 완료된다.
- Main과 역할별 model·effort를 spec의 field별 precedence로 해소한다. custom role 파일에는 model/effort를 넣지 않고 spawn 값으로 override/default를 전달한다. host가 지원 조합을 거부하거나 관측된 model이 요청과 다르면 통과시키지 않는다. host가 실제 effort receipt를 노출하지 않으면 `request_accepted`로 기록하고 실행 effort를 확인했다고 주장하지 않되, 이 제한만으로 독해 완료를 막지는 않는다.
- 각 reviewer는 본문 전체를 먼저 읽고, 그 다음에 Main 이해 노트를 보며 대조한다. 처음부터 노트만 보고 채점하지 않는다.
- `math_visual`은 정의, 수식 조건, 그림, 표, 축, 범례, 부록 연결을 우선 본다.
- `claim_experiment`는 핵심 주장, 실험 설계, 결과, 한계, 부록에서 제한된 범위를 우선 본다.
- source-first 뒤 compaction/session change가 있으면 note comparison 전에 required text와 focus visual을 새 reviewer epoch에서 다시 연다.
- reviewer 결과는 `source_first`, `note_comparison` 두 stage이고 `final_disposition`은 root Main의 별도 record다.

**작업 대상.** 내용 검토 역할 정의, spawn/follow-up message 계약, audit 결과 형식, 관련 시나리오 시험. Main skill과 공통 등록 파일은 수정하지 않는다.

**검증.** 잘못된 표 참조, 수식 조건 누락, 부록이 제한하는 주장, stage 사이 compaction, 한 reviewer 실패/취소/partial, stale finding/remediation, model/effort 관측 불일치 fixture에서 반환 형식과 Main 재대조가 명세대로 작동한다.

### T8 — 긴 설명의 논리 흐름 검토

**목표.** 실제 사용자에게 보낼 긴 설명의 논리 오류와 독해를 어렵게 하는 구조 문제를 원문에 근거해 검토한다.

**행동 계약.**

- 사용자의 실제 질문, 요청 수준, 전체 원문, 이해 노트, 고정된 설명 초안을 입력으로 사용한다.
- spawn 전에 `flow_start`가 flow audit/attempt, assignment nonce, exact draft/input digest, model request와 requested execution ID를 예약한다. parent history 없이 reservation ID+nonce, exact question event/hash와 response attempt, answer/draft/audit ID, manifest와 ordered full-text batch inventory, draft-dependent visual IDs, note/draft path+hash, locator/result schema만 spawn input으로 전달한다.
- 내부 thinking이나 이해 노트만으로 실제 답변을 검토했다고 하지 않는다.
- 필요한 조건 누락, 근거 없는 결론, 원인·결과 혼동, 실험 범위 초과, 단락 모순을 논리 오류로 분류한다.
- 정의·중간 단계·연결 문장의 누락 또는 너무 늦은 배치를 설명 구조 문제로 분류한다.
- 같은 논리를 유지하는 재배치는 선택적 개선으로 분리하고, 결론 우선이나 원문과 다른 순서만으로 오류를 만들지 않는다.
- flow review는 explicit user request, tutorial, recorded contentious interpretation, safe-estimated 1,200토큰 이상 중 하나일 때만 필수다. 이유가 없으면 false/empty reasons를 명시한다.
- flow audit 하나는 draft 하나만 검토한다. 수정 draft는 새 audit ID와 parent audit/recheck finding을 사용하고 이전 audit을 재사용하지 않는다.
- trusted agent-start binding과 start/result cardinality, gapless finding ordinal→child record, recheck result bijection이 맞지 않으면 flow result를 commit하지 않는다.
- finding은 exact draft code-point span, `logic_error | structure_problem | optional_improvement`, blocking/advisory, locator를 가진다. optional은 advisory이고 구조 문제는 required connection이 빠졌을 때만 blocking이다.
- accepted blocking finding은 direct child remediation draft와 새 audit의 `resolved` 재확인이 있어야 닫힌다. still-present/not-verifiable는 finalization을 막는다.
- 실제 보낼 답변은 immutable draft와 flow audit/remediation을 거친 finalized content hash와 일치하며 Stop observer가 actual message hash를 기록한다.
- 고정 scope disclosure는 검토·수정 대상에서 제외하고 그 앞의 substantive answer만 finding span으로 다룬다. remediation 뒤에도 같은 disclosure bytes가 마지막 block이어야 한다.
- explanation-flow 역할도 spec의 model/effort precedence와 requested-versus-observed audit record를 사용하고 unsupported 조합을 조용히 대체하지 않는다.
- flow audit은 원문 순서와 다르다는 이유만으로 실패를 만들지 않고 논리 연결이 실제로 끊길 때만 block으로 올린다.

**작업 대상.** 설명 흐름 검토 역할 정의, draft version 고정과 결과 형식, 관련 scenario 시험. 내용 reviewer와 공통 등록 파일은 수정하지 않는다.

**검증.** 1,199/1,200토큰 trigger 경계, tutorial/contentious/short answer, 근거 없는 따라서, 부록 조건 누락, 단락 모순을 시험한다. blocking finding을 D1→A1→D2→A2 resolved로 닫고 audit 재사용·stale span·미재검토 draft·actual sent hash mismatch를 거부하며, 타당한 결론 우선 초안은 순서만으로 실패시키지 않는다. disclosure 영역의 finding과 disclosure bytes를 바꾼 remediation도 거부한다.

### T9 — Stop 자동 복귀와 observer

**목표.** 살아 있는 같은 Codex 작업에서 `check`가 찾은 보완 가능한 누락을 실제 Desktop의 같은 Main이 자동으로 처리하게 하고 무한 반복·다른 대화 가로채기를 막는다.

**행동 계약.**

- 각 logical Stop slot마다 exact output bytes와 ordered terminalization/delivery/preview/continuation side effects를 가진 durable Stop transaction을 먼저 fsync하고, crash/replay 때 같은 operation과 bytes를 재생한다. host callback 개수와 output 수신 ACK는 증명하지 않는다.
- Stop observer는 ReadPaper answer/run뿐 아니라 run이 없는 `created` deletion preview의 exact actual-message hash도 관측해 `presented`로 bind한다. block 결과는 nonterminal run 또는 pending answer가 있을 때만 낸다.
- Stop transaction은 current Main execution과 actual message hash, pending answer를 포함한 check를 하나의 plan으로 처리한다. initiating allow는 run complete와 answer/attempt delivery를 함께, complete-run allow는 answer만 commit한다.
- 누락 ID와 짧은 보완 요청만 continuation에 넣고 원문 문장을 지침으로 주입하지 않는다.
- Python이 모델을 호출하지 않으며 Codex 실행기의 Stop continuation을 사용한다.
- 자동 보완 상한은 run-level 1회와 answer별 1회다. current local task binding, host session/turn에 bind된 root execution, blocker, counter, pending attempt, user intervention을 state-service CAS로 검사하고, logical Stop slot을 선점한 hook만 immutable attempt의 `reserved -> requested`와 counter 소비를 기록한다. attempt는 spec의 allowed transition과 target outcome을 따르며 run-target terminal failure는 run blocked+pending answer interrupted, answer-target failure는 run 유지+answer interrupted다. run blocker만 고치고 answer blocker가 남으면 `target_repaired_pending_other`로 run attempt를 닫고 같은 Stop에서 answer attempt를 예약한다.
- 취소·질문 전환·paused run, 외부 해결이 필요한 blocked run, Desktop 재시작 뒤에는 자동 continuation을 시작하지 않는다. interrupted answer는 explicit `answer --resume|--abandon`만 허용한다.
- Main/subagent/hook/unknown actor를 host 증거로 구분하고 self-reported CLI flag만 신뢰하지 않는다.
- observer 실패·timeout·미승인은 성공이 아니라 unknown 또는 blocked다. Stop callback은 10초, event correlation은 30초, continuation-start observation은 60초 상한을 적용한다.
- Stop input의 `stop_hook_active=true`이면 actor와 관계없이 새 block을 만들지 않는다. root Main·current local task/session·locally bound execution과 `nonterminal run 또는 pending answer` 조건은 state binding으로 별도 확인한다.
- `stop`, SessionStart, UserPromptSubmit, tool observer, compact hook은 spec의 event별 semantic key와 CAS phase로 dedupe하고 같은 event stream을 보되 서로의 outcome을 덮어쓰지 않는다. 같은 PostCompact나 Stop payload 재전송은 epoch/counter/answer consumption을 두 번 일으키지 않는다. 서로 다른 raw event인지 재전송인지 구분할 수 없는 compact 입력은 성공으로 추정하지 않고 `OBSERVER_UNAVAILABLE`이다.
- attempt ID는 `counter_before`를 hash하고 nonce/exact prompt hash로 `reserved | requested | started | completed | not_started | timed_out | cancelled | abandoned_restart | hook_failed`를 구분한다. matching `UserPromptSubmit`이 관측되면 그 event가, 관측되지 않으면 Stop과 same session/turn인 첫 exact root `PreToolUse`가 one-use nonce를 capability 발급과 같은 CAS에서 claim할 때만 `started`가 된다. start가 claim되면 pending answer의 previous attempt superseded, 새 attempt active, answer drafting을 원자 commit한다. 다른 matching Stop hook의 `continue:false`, 늦게 온 continuation, 중복 continuation prompt/tool-use, 실제 user prompt와의 race는 counter를 되돌리거나 새 continuation을 만들지 않는다.
- 재시작 직후에는 이전 tool call history가 남아 있어도 `resume` 전에는 자동 continuation을 시작하지 않는다.
- observer 상관 실패나 60초 안 nonce claim 부재는 성공으로 바꾸지 않는다. 이 경우 `not_started`로 닫고 자동으로 재요청하지 않는다. 자동 attempt가 소진된 run target은 run `blocked`와 pending answer `interrupted`, answer target은 run을 유지하고 answer `interrupted`로 만들며, 실제 사용자 취소나 재시작은 nonterminal run `paused`와 answer `interrupted`로 구분한다.

**작업 대상.** `.codex/hooks/readpaper_stop_hook.py`, observer adapter, hook contract 시험. `.codex/hooks.json` 등록은 deferred wiring에서 한 번만 한다.

**검증.** 실제 Desktop에서 run, complete-run answer, run+answer 동시 누락 각각에 대해 Stop→logical slot/CAS reservation→exact block JSON→60초 안 nonce-matching continuation claim(`UserPromptSubmit` 관측 시 그 event, 미관측 시 same-session/turn 첫 exact root `PreToolUse`의 원자 claim)→locally bound root Main execution→추가 tool call을 관측한다. 동시 누락은 run attempt 완료 뒤 answer attempt로 이어지는 두 단계만 허용한다. plan/reservation/status/output 각 crash 지점의 replay, 동일 Stop payload의 직렬·동시 재전송, host가 중복 prompt/tool-use를 만들었을 때의 one-use claim, 각 budget의 두 번째 실패 중단, `stop_hook_active` 재진입, concurrent/other `continue:false` Stop hook, subagent Stop, 사용자 prompt race, 늦은 continuation, hook crash/신뢰 해제, 10/30/60초 timeout, 동일 semantic UserPromptSubmit/PostCompact event의 멱등성을 각각 증거로 남긴다. host prompt 자체의 exactly-once는 수용 조건이 아니며, authorized repair effect가 최대 한 번인지 검사한다.

**판단 근거.** 자동 복귀는 사용자에게 필수 기능이며 실제 Desktop에서 같은 Main이 이어지는 증거가 없으면 P0는 통과할 수 없다.

### T10 — 압축 임계값 조정과 압축 관측

**목표.** 전체 원문과 독해 기록이 기본값보다 오래 유지되도록 프로젝트 압축 임계값을 높이고, 압축 뒤 원문 재열람 상태 전이를 관측한다.

**행동 계약.**

- 신뢰된 프로젝트 전용 `.codex/config.toml`에 `model_auto_compact_token_limit`을 설정한다.
- exact P0 값은 `model_auto_compact_token_limit=230000`, `model_auto_compact_token_limit_scope="total"`, `tool_output_token_limit=16000`이다. host-observed effective context 258,400 미만이면 P0 preflight를 막는다.
- 28,400 headroom에서 답변 8,000과 control/envelope 4,000 reserve, paper input estimate 150,000, read batch 12,000, tool history output 16,000의 상호작용을 별도로 시험한다.
- `model_context_window`를 모델 용량 확장 수단으로 사용하지 않고 검증되지 않은 0/-1/최대치 값을 압축 해제로 쓰지 않는다.
- PreCompact/PostCompact 또는 실제 host event로 압축을 관측하고 context epoch를 증가시켜 historical coverage와 current-answer residency를 분리한다.
- SessionStart `source=startup|resume|clear` hard boundary마다 session epoch를 올리고 `session_id + root sentinel|agent_id`에서 locally derived context stream을 0으로 시작한다. `source=compact`는 matching agent stream의 context epoch만 한 번 올린다. epoch는 stream별이며 reviewer compaction이 Main epoch를 바꾸지 않는다. note 전 Main compaction/session change는 single-epoch synthesis 전체 재독해를 요구한다.
- 설정 변경은 새 Desktop session에서 host 적용값, 첫 compact 지점, 개별 tool output의 complete marker로 확인한다.
- 같은 모델과 fixture에서 기본값과 P0 값을 각각 2회 실행해 같은 관계가 재현돼야 한다. 실패하면 숫자를 조용히 바꾸지 않고 spec 변경 승인을 받는다.
- 압축 전후 비교는 `before_compact_tokens`, `after_compact_tokens`, `first_lossless_reopen_turn`, `recovered_locator_count`를 기록한다.
- 압축이 실제로 늦어졌는지 확인할 때는 model context 자체가 아니라 history 보존 시점을 본다.

**작업 대상.** `.codex/config.toml`, `.codex/hooks/readpaper_compact_hook.py`, 압축 probe와 evidence 형식. `.codex/hooks.json` 등록은 deferred wiring에서 한 번만 한다.

**검증.** 같은 모델·fixture에서 기본 시점과 P0 값을 2회씩 비교하고, exact 적용값, 더 늦은 압축, 8k/4k reserve, 12k read/16k history output 비잘림, session/context epoch 전이, historical coverage 보존, synthesis 재독해, current-answer grounding을 증거로 남긴다.

### W1 — 공통 등록 단일-writer integration barrier

**목표.** 기능 구현자가 각자 만든 skill, reviewer, observer, hook, 설정을 공통 프로젝트 파일에 충돌 없이 한 번만 연결하고 T11이 실제 product wiring을 시험하게 한다.

**행동 계약.** W1은 T6–T10이 모두 완료되고 각 산출물의 interface/schema/hash가 spec과 일치한 뒤에만 시작한다. 단일 writer가 `AGENTS.md`, `.codex/hooks.json`, skill/role 등록, 공통 verify command 연결을 맡고 초기 bootstrap이 만든 `.gitignore` managed block의 exact marker/rule/비중복을 검증한다. 기존의 관련 없는 project instruction, ignore rule, hook entry를 보존하고, Stop/compact observer의 순서·timeout·script hash, role 이름, skill activation rule, exact production CLI prefix를 canonical wiring manifest에 고정한다. managed block 누락/변형, 중복 entry, 서로 다른 parser/schema hash, 소유 작업의 미완료 산출물, 예상 밖 기존 파일 변경이 있으면 merge하지 않고 `STATE_CONFLICT` evidence를 남긴다. W1은 명세 행동을 새로 정의하거나 T6–T10 소유 기능을 수정하지 않으며, interface 불일치는 원래 소유 작업으로 돌려보낸 뒤 다시 실행한다.

**작업 대상.** `AGENTS.md`, `.codex/hooks.json`, skill/role registration index가 실제로 필요한 경우의 해당 공통 파일, 공통 verify entry와 wiring manifest. `.gitignore`는 read-only gate 대상으로 검사한다. `.codex/config.toml` 내용은 T10 산출물을 검증해 연결만 하고 값을 다시 선택하지 않는다.

**검증.** clean checkout에 T6–T10 산출물을 적용했을 때 공통 파일을 한 번 생성/merge하고 두 번째 동일 실행은 byte-identical no-op이어야 한다. hook config parse, unrelated entry 보존, Stop/compact hook 각 1개, role/skill 중복 0개, script/config/parser schema hash 일치, exact `.venv` direct-command smoke가 모두 성공해야 T11로 넘어간다.

### T11 — 자동 회귀 시험과 fixture 증거 묶음

**목표.** spec의 기계적 불변조건과 경계 사례를 한 명령으로 반복 검증하고, 실제 Desktop 시험에 쓸 숨겨진 정답 fixture를 완성한다.

**행동 계약.**

- 10페이지 fixture의 페이지별 앞·중간·끝 텍스트 표식과 그림·표 값을 Main에게 숨긴 정답과 분리한다.
- 두 단, 인쇄 라벨 불일치, 부록, 빈 텍스트, OCR 필요, 손상 PDF, 지원·미지원 supplementary, archive 공격 fixture를 포함한다.
- 최소 fixture 세트는 `single-column`, `oversized-page-section`, `two-column`, `label-mismatch`, `repeated-object-label`, `appendix-heavy`, `empty-text`, `scan-like`, `archive-traversal`, `archive-ratio-bomb`, `archive-nested`, `private-address-redirect`, `unsupported-supplementary`, `prepare-response-loss`, `prepare-partial-crash`, `pretool-capability-replay`, `protected-command-response-loss`, `reviewer-assignment-binding`, `multi-finding-audit`, `flow-remediation`, `answer-attempt-resume`, `answer-hash-mismatch`, `host-event-replay`, `run-then-answer-continuation`, `answer-interruption`, `multi-task-binding-delete`, `delete-content-tombstone`, `delete-crash-phases`를 포함한다.
- 상태/동시성, paper/artifact/bundle identity, fixed scope, source discovery, extraction/unit range, direct CLI/PreTool capability와 command replay, run/response-attempt resume, deletion journal/reference lock, reviewer reservation/version graph, flow finalization, Stop transaction, context epoch/config probe의 자동 시험을 한 verify 명령으로 묶는다.
- 시험 결과는 환경, 입력, 기대값, 실제값, 증거 경로를 남긴다.
- 관측할 수 없는 host 동작을 mock 성공만으로 통과시키지 않고 실제 Desktop 대기 항목으로 남긴다.

**작업 대상.** `tests/`, fixture 생성·정답, 자동 verify entry, evidence report 형식. 제품 소스와 공통 설정은 시험에 필요한 interface 수정 외에는 건드리지 않는다.

**검증.** clean checkout에서 fixture 재생성과 전체 자동 verify가 성공하고, 의도적인 앞/중간/끝 잘림, actor/model observation 오염, semantic host-event/record 중복과 payload 충돌, locator 변형, archive/SSRF 공격, stale audit/remediation, pending answer·질문 직후 compaction, 삭제 표시 누락/scope 변경/crash 단계, sent-answer hash mismatch를 각각 탐지한다.

### T12 — 실제 Codex Desktop 완제품 인수

**목표.** 자동시험으로 대신할 수 없는 host 동작과 실제 논문 end-to-end 흐름을 사용자 환경에서 검증해 P0 출시 여부를 결정한다.

**행동 계약.**

- hook과 프로젝트 설정을 사용자가 검토하고 신뢰한 실제 Desktop 작업에서 시험한다.
- 10페이지 숨은 정답 fixture로 Main 전체 텍스트·시각 전달, 잘림 탐지, Main/subagent actor 구분, Stop 같은-Main 자동 복귀, 반복 방지, 취소, 압축, 재시작 후 명시적 resume를 검증한다.
- 실제 공개 디지털 논문 한 편을 출처 준비부터 전체 독해, pre-spawn 예약을 거친 두 내용 audit, Main 대조, 이해 노트, 모든 answer의 원문 재열람, answer begin/resume별 response attempt와 locally bound Main execution이 있는 후속 질문까지 수행한다.
- 실제 Desktop 인수에서는 `prepare -> answer begin -> scope lock -> ordered full read/visual -> single-epoch note -> source-first audits -> same-reviewer note comparisons -> Main dispositions/remediation -> check`를 한 번, `Stop -> auto resume`를 한 번, `restart -> explicit resume -> full synthesis reread`를 한 번 이상 확인한다.
- 1,200토큰 이상 설명 fixture로 흐름 reviewer의 trigger, 오류/구조/선택적 개선, child draft 재검토, finalized/actual sent hash를 검증한다.
- 역할별 모델·reasoning 요청, host 지원 조합 검증, 실제로 노출되는 실행 metadata를 대조한다. effort receipt가 없으면 요청값을 실제 관측값으로 표시하지 않는다.
- 같은 main PDF와 바뀐 supplementary snapshot, full/user-reduced scope, unsupported required artifact의 completion label과 terminal state를 실제 workflow에서 확인한다. user-reduced 답변마다 opaque ref와 closed-set reason code만 담은 동일 disclosure가 exact suffix로 남고 full 답변에는 disclosure가 없는지도 확인한다.
- exact preview Markdown이 실제 assistant message로 관측된 뒤 별도 exact-confirmation turn을 사용하는 삭제를 disposable fixture에서 실행한다. 여러 task의 binding/pending answer/execution 차단과 전체 binding clear, pending 없는 paused run 삭제, reference-lock race, 각 commit 단계 crash recovery, content-bearing replay 제거와 tombstone, shared artifact와 프로젝트 경계 보존을 확인한다.
- 필수 observer나 자동 복귀를 확인할 수 없으면 결과를 blocked로 남기고 제한판을 완제품이라고 표시하지 않는다.

**작업 대상.** 실제 Codex Desktop 상태, P0 evidence report, 출시 판정. 제품 코드는 인수 중 발견된 명세 불일치의 최소 수정 외에는 확장하지 않는다.

**검증.** `.dryforge/spec.md`의 필수 검증 15개가 각각 증거 경로와 함께 PASS이고 blocking 항목이 0일 때만 P0 완제품 MVP로 판정한다.

## 공유 파일과 통합 지침

- Python package marker, dependency manifest, 기본 test runner와 managed `.gitignore` block은 초기 구성에서 한 번만 만든다. 병렬 작업자가 각각 만들거나 marker block을 고치지 않는다.
- T1–T4는 각자 소유 모듈만 수정한다. 공통 CLI 진입 파일 `paper.py`는 T5만 쓴다.
- T6–T8은 각자의 skill/reviewer 파일만 수정한다. 공통 `AGENTS.md` 등록은 직접 수정하지 않는다.
- T9와 T10은 각각 별도 hook 모듈을 소유한다. `.codex/hooks.json`은 두 작업이 모두 합쳐진 뒤 orchestrator가 한 번만 등록한다.
- `.codex/config.toml`은 T10만 수정한다.
- `AGENTS.md`, hook 등록, skill/role 등록, 공통 verify command 연결은 T6–T10 종료 뒤 W1 단일 writer가 중복 없이 반영한다.
- 병렬 변경이 예상 밖의 같은 파일을 건드리면 합치기 전에 실제 diff 겹침을 확인하고 직렬화하거나 한 writer로 넘긴다.

## 단계 흐름

1. G0에서 필수 Desktop payload, local semantic identity binding, same-Main continuation과 at-most-once authorized repair effect를 실제로 증명한다. 실패하면 축소 구현으로 가지 않고 P0를 blocked로 끝낸다.
2. 로컬 상태 계약을 먼저 고정한다.
3. 출처 확보, PDF 처리, run lifecycle을 병렬로 만든다.
4. 이 세 기능을 내부 명령으로 통합한다. 압축 관측은 상태 계약 위에서 병렬 준비한다.
5. Main skill, 두 내용 reviewer, 설명 흐름 reviewer, Stop 자동 복귀를 통합 명령 위에서 병렬 구현한다.
6. W1 단일 writer가 공통 등록 파일을 연결하고 byte-identical 재실행을 확인한 뒤 전체 자동 회귀 시험을 완성한다.
7. 실제 Desktop에서 필수 host 동작과 실제 논문 end-to-end를 확인한다. 이 마지막 증거가 없으면 제품 구현이 존재해도 P0는 끝나지 않는다.

## 요구사항 추적

| 명세 영역 | 담당 작업 |
| --- | --- |
| Desktop 필수 payload, model 요청/관측 경계, exact message, Stop continuation/at-most-once effect | G0, T9, T12 |
| 불변 논문 버전, run/evidence 상태, actor 분리, 동시성 | T1, T4 |
| 직접·랜딩·로컬 출처, supplementary, archive/접근 경계 | T2 |
| 페이지 위치, 추출·렌더링, 범위·잘림 | T3 |
| prepare/read/render/record/check/answer/resume/delete 계약 | T5 |
| Main 전체 독해, 이해 노트, 원문 재열람, 후속 Q&A | T6 |
| 수식·시각 및 주장·실험 audit | T7 |
| 긴 설명 논리 흐름 audit | T8 |
| 같은-Main Stop 자동 복귀, 반복·취소·observer | T9 |
| 자동 압축 임계값, tool output 한도, 압축 뒤 재열람 | T10 |
| 자동 fixture/경계 회귀 | T11 |
| 공통 hook/skill/role/config wiring과 충돌 방지 | W1 |
| 실제 Desktop 증거와 완제품 출시 판정 | T12 |

모든 작업은 명세의 한 영역 이상에 연결되며, 표에 없는 구현 작업은 P0 범위에 추가하지 않는다.

## Execution Graph

```yaml
tasks:
  - id: G0
    depends: []
    risk: RISKY
  - id: T1
    depends: [G0]
    risk: RISKY
  - id: T2
    depends: [T1]
    risk: RISKY
  - id: T3
    depends: [T1]
    risk: RISKY
  - id: T4
    depends: [T1]
    risk: RISKY
  - id: T5
    depends: [T2, T3, T4]
    risk: RISKY
  - id: T6
    depends: [T5]
    risk: RISKY
  - id: T7
    depends: [T5]
    risk: RISKY
  - id: T8
    depends: [T5]
    risk: RISKY
  - id: T9
    depends: [T5, T6]
    risk: RISKY
  - id: T10
    depends: [T1]
    risk: RISKY
  - id: W1
    depends: [T6, T7, T8, T9, T10]
    risk: RISKY
  - id: T11
    depends: [W1]
    risk: RISKY
  - id: T12
    depends: [T11]
    risk: RISKY
regen_barriers: []
```
