# ReadPaper 구현 계획 — Codex 내부형 MVP

작성: 2026-08-28. 상태: 역사적 설계 문서 / 구현은 `.agents/skills/readpaper`와 `.codex`에 있음.

기준: [PROJECT_GOAL.md](PROJECT_GOAL.md).
이 문서는 실행 코드가 아니다. 명령·파일·검사 예시는 앞으로 만들 인터페이스다.

## 1. 결정할 방향

**논문을 읽는 주체는 ReadPaper를 호출한 작업의 기존 Codex Main이다.**
별도 Python 프로그램이 LLM을 반복 호출하는 시스템을 첫 버전으로 만들지 않는다.

- `SKILL.md`: Main이 따라야 할 독해·재확인 절차.
- Python: PDF 확보, 텍스트 추출, 페이지 렌더링, 출처 위치 관리, 기계적으로 가능한 검사.
- Codex: 도구 실행, 결과를 받은 Main의 계속 실행, subagent 생성·대기.
- Hook: 지원이 실제 확인되면 종료 시 검사를 실행하고, 누락이 있으면 Main의 추가 작업을 요청.

LangGraph, 별도 모델 API 호출, 벡터 DB, 웹 UI, 논문별 Q&A 파이프라인은 MVP에 넣지 않는다.
이는 기술적으로 불가능해서가 아니라, 기존 Main의 전체 독해라는 목표에 필요하지 않기 때문이다.
Codex의 skills는 지침과 실행 스크립트를 묶을 수 있고, subagent 실행 관리는 Codex가 제공한다.
[Skills](https://learn.chatgpt.com/docs/build-skills), [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents#orchestration-and-thread-controls).

## 2. 범위와 약속

첫 지원 대상은 공개 PDF 링크, 공개 페이지에서 찾을 수 있는 PDF 링크, 사용자가 지정한 로컬 PDF다.
첫 시험은 본문 약 10쪽인 디지털 PDF로 한다. 페이지 수가 곧 토큰 수라는 가정은 하지 않는다.

- 원본 PDF는 변경하지 않고 저장한다. URL, 버전 식별 정보, 해시를 함께 보관한다.
- 본문·참고문헌·부록을 목록에 모두 남긴다. 부록은 나중에 읽을 수 있지만, 읽기 전에는 전체 논문을 읽었다고 하지 않는다.
- 별도 supplementary 파일은 확인된 것만 범위에 포함하고, 미확보 파일을 명시한다.
- 본문만 읽는 것은 사용자가 선택한 축소 범위다. 기본 범위에서 몰래 부록을 제외하지 않는다.
- 로그인·유료 접근·손상 PDF·OCR 필요·용량 초과는 원인과 미완료 범위를 보고한다. 접근 제한을 우회하지 않는다.
- 원문 전체를 처음 읽는 동안 context에 유지하는 것을 시험 목표로 삼는다. 무한한 후속 대화에서도 원문이 계속 그대로 남는다고 약속하지 않는다.

## 3. 실제 실행 순서

### A. 원문 준비 — Main이 Python을 실행

Main이 `paper.py prepare <source>`를 도구로 실행한다.
Python은 허용된 소스를 받아 다음 자료를 만든다.

1. 원본 PDF와 해시.
2. 실제 PDF 페이지 수와 페이지별 추출 텍스트.
3. 페이지 경계를 보존한 전체 텍스트.
4. 페이지별 PNG와 추출 이상 징후 목록.
5. 페이지 위치를 찾기 위한 manifest.

첫 구현의 PDF backend는 Python + `pypdf` + Poppler의 `pdftotext`/`pdftoppm`으로 한다.
이 환경에서 이미 확인된 구성이다. Python은 subprocess 인자 배열로 CLI를 호출하며 URL을 shell 문자열에 끼워 넣지 않는다.
이미지 파일만 추출하면 벡터 그림을 놓칠 수 있으므로, 시각 확인에는 페이지 전체 렌더링을 사용한다.

`prepare`가 반환하는 것은 경로·페이지 수·경고다. **준비 성공은 Main의 독해 완료가 아니다.**
본문이 두 단으로 섞이거나 수식이 깨지는 문제는 추출 문자 수만으로 판별할 수 없다. 원래 페이지 이미지와 대조한다.
[PDF 텍스트 추출의 한계](https://pypdf.readthedocs.io/en/stable/user/extract-text.html#why-text-extraction-is-hard).

### B. 원문 전달 — Main이 실제 텍스트와 이미지를 받음

Main이 `paper.py read <paper-id> --run-id <run-id> --pdf-pages 1-10`처럼 원문 출력 명령을 실행한다.

- 여유가 있으면 전체 텍스트를 한 번에 반환한다.
- 한 번의 도구 출력 한도를 넘으면 연속된 범위로 나누어 반환한다. 한 페이지도 너무 크면 페이지 안의 범위를 나눈다.
- 각 묶음에는 source hash, 페이지/범위, 길이, 시작·끝 표식을 둔다. 합친 범위에 누락·중복이 없는지 검사한다.
- 이것은 **원문 전달을 위한 분할**이다. `U1 요약 → U1.json → U2 요약`이라는 독해 방식이 아니다.
- Code Mode를 거칠 때에도 출력 본문을 모델에 전달해야 한다. JS 변수에 받아 두거나 파일 경로만 출력하면 읽은 것이 아니다.
- Main은 PNG 경로를 받은 뒤 반드시 이미지 보기 도구로 실제 페이지를 연다. 모든 페이지를 확인하고 작은 그림·표·수식은 확대한다.

전체 파일이 디스크에 있다는 것, Python이 stdout에 출력했다는 것, 현재 모델 입력에 그대로 있다는 것은 구분한다.
도구 기록은 앞의 두 가지를 확인하는 근거다. 현재 모델 입력 전체를 관측하지 못하면 마지막 상태는 `unknown`으로 남긴다.

### C. 전체 이해 형성 — Main이 직접 읽고 연결

Main이 전체 텍스트를 읽고 페이지 이미지를 확인한 뒤, 하나의 `understanding.md`를 작성한다.
이것은 전체 원문을 대신하는 요약 DB가 아니라, Main의 이해를 드러내 검토하기 위한 짧은 작업 노트다.

내용은 다음 정도로 제한한다.

- 해결하려는 문제, 핵심 정의와 가정.
- 방법의 작동 방식과 중요한 수식의 역할.
- 실험이 실제로 확인한 것과 확인하지 않은 것.
- 결론이 어떤 방법·실험·부록 근거에 연결되는지.
- 핵심 그림·표·수식 위치와 아직 불명확한 부분.

구체적 수치나 논쟁적인 해석을 노트에 확정하기 전에 해당 페이지를 다시 연다.
추출된 그림 번호·수식 번호는 후보일 뿐이다. Main이 페이지를 보며 실제 객체와 맞는지 확인한다.

### D. 독립 검토 — 전체 이해 뒤에 subagents 사용

Main의 1차 전체 독해 이후 Codex의 subagent 도구로 감사 작업을 맡긴다.
기본 내용 검토는 2개 역할이다. 여기에 Main의 설명 순서·논리 흐름을 검토하는 역할을 추가한다.
설명 흐름 검토는 긴 설명 초안이 생겼을 때 사용하고, 모든 짧은 Q&A마다 자동으로 실행하지 않는다.

| 검토자 | 직접 확인할 내용 | 반환할 것 |
| --- | --- | --- |
| 수식·시각 검토 | 정의, 수식 조건, 그림 축·범례, 표의 행·열·단위 | 위치, 발견한 불일치, 원문 근거, 미확인 사항 |
| 주장·실험 검토 | 주요 주장과 실험 설계·결과·한계·부록의 관계 | 지지되는 범위, 과장/누락 후보, 원문 위치 |
| 설명 흐름 검토 | Main의 실제 설명 초안에서 전제·정의·방법·근거·결론이 연결되는 방식 | 문제 단락, 빠진 연결/전제, 원문 위치, 수정·재배치 제안, 오류와 선택적 개선의 구분 |

읽기 범위는 다음과 같이 구분한다.

- Main은 전체 원문과 모든 페이지의 시각 내용을 직접 확인한다.
- 현재 제안의 내용 검토자 2명은 각각 전체 추출 텍스트를 읽고 논문 구조를 파악한 뒤, 담당 수식·그림·표·실험·부록 위치를 원본 이미지에서 집중 검토한다.
- 따라서 내용 검토자도 전체 텍스트를 다시 읽는 안이다. 다만 모든 시각 객체를 Main과 동일한 깊이로 검사했다는 뜻은 아니다.
- 설명 흐름 검토자는 설명 초안 전체를 읽고 논문 전체 텍스트를 참고하며, 논리 판단에 필요한 원문 위치를 직접 재확인한다.
- 전체 텍스트가 검토자 context에도 맞는지 확인한다. 일부만 읽었으면 부분 검토로 표시하며, 전 범위를 확인한 것처럼 쓰지 않는다.
- 같은 원문을 공유 폴더에서 읽어도 각 subagent의 context와 읽기 작업은 별개다. 원문 재전달에 따른 토큰·시간을 평가에 포함한다.

내용 검토자 입력은 두 단계로 전달한다.

1. Main이 `spawn_agent`의 `message`에 원문 자료 폴더, 검토 항목, 반환 형식을 명시한다. 이 환경에서는 `fork_turns: "none"`으로 Main의 기존 대화 복사를 피한다. 처음에는 Main의 이해 노트를 넘기지 않는다.
2. 검토자가 원문을 직접 읽고 1차 판단을 반환하면, Main이 같은 검토자에게 `followup_task`로 `understanding.md`의 해당 버전과 대조 요청을 전달한다.

경로 전달은 내용 자동 첨부가 아니다. 검토자 자신이 텍스트 읽기와 이미지 보기 도구를 실행해야 한다.
첫 원문 판단과 Main 노트를 본 뒤의 대조 결과는 구분해서 남긴다.
처음부터 논문을 분할 배정해 Main의 독해를 대체하지 않는다.
Main이 검토 결과의 원문 위치를 다시 열어 수용·기각·미해결을 결정하고 노트를 수정한다.
검토자 실패·취소를 검토 통과로 취급하지 않는다. 검토자들의 동의도 정답의 증명은 아니다.

설명 흐름 검토의 입력·판정 계약:

- 입력은 사용자의 실제 질문과 요청한 설명 수준, 원문 자료, Main의 이해 노트, 사용자에게 보여줄 설명 초안이다. Main의 내부 thinking을 입력으로 가정하지 않는다.
- `understanding.md`만으로 실제 답변의 흐름을 검토했다고 하지 않는다. 긴 답변의 정확한 초안을 메시지 또는 별도 Markdown으로 전달하며, 검토한 버전을 고정한다.
- 논리 오류: 필요한 조건의 누락, 근거 없는 “따라서”, 원인·결과의 혼동, 실험 범위를 넘어선 결론, 단락 간 모순.
- 설명 구조 문제: 독자가 따라가려면 필요한 정의·중간 단계·연결 문장이 빠졌거나 너무 늦게 나오는 경우.
- 선택적 개선: 같은 논리를 유지하는 재배치나 표현 제안. 결론부터 설명하거나 원문과 다른 순서를 택했다는 이유만으로 오류 판정을 하지 않는다.
- 출력은 `문제 단락/문장 → 문제 종류 → 이유와 필요한 원문 위치 → 수정/순서 제안`이다. 별도 단락별 JSON DB는 만들지 않는다.
- 내용 검토 결과와 충돌하면 Main이 원문을 다시 확인한다. 문장을 매끄럽게 바꾸면서 논문의 조건·근거를 잃지 않도록 한다.

### E. 누락 검사와 계속 실행 — Python 검사 + Codex Stop hook

정상 흐름에서는 Main이 먼저 `paper.py check <run-id>`를 실행하고 누락을 처리한다.
검사 결과만 반환하는 Python 함수가 Main을 직접 호출하지는 않는다.

종료 시 안전장치가 필요하면, 신뢰 승인된 프로젝트 `Stop` hook이 같은 검사 함수를 실행한다.
예를 들어 7쪽의 시각 확인 기록이 없으면 다음과 같은 JSON을 stdout으로 반환한다.

```json
{"decision":"block","reason":"ReadPaper: PDF 7쪽의 시각 확인 기록이 없습니다. 해당 페이지를 열고 누락 검사를 다시 실행하세요."}
```

이때 **Codex 실행기가 reason으로 같은 작업에 continuation prompt를 만들고 Main을 다시 실행한다.**
Python 안에서 모델 API를 부르는 것도, `SKILL.md`가 실행을 전환하는 것도 아니다.
공식 문서에 정의된 동작이지만, 실제 사용하는 Desktop 버전에서 먼저 시험해야 한다.
[Codex Stop hook](https://learn.chatgpt.com/docs/hooks#stop).
아래 one-use/CAS 항목은 구현·인수 목표이며 현재 통과 증거가 아니다. 정적 schema 검사는 callback ID 없이 필요한 입력을 구성할 수 있는지만 확인하고, 실제 continuation과 authorized effect의 at-most-once는 새 Desktop session의 live G0가 별도로 증명한다.

Hook 설계 조건:

- durable ReadPaper-local task binding에 활성화된 run이 있을 때만 작동한다. host session은 관측 metadata이며 일반 대화나 다른 논문의 작업을 막지 않는다.
- 초기 MVP는 Codex 작업당 활성 논문 1개로 시작한다. 다른 local task/session의 상태와 섞지 않는다.
- MVP의 자동 재개는 run 단위로 최대 1회다. 재개된 Main은 그 안에서 여러 도구를 호출할 수 있지만, 또 종료할 때 미완료이면 경고와 `continue:false`로 중단한다.
- Codex가 callback별 고유 ID를 제공한다고 가정하지 않는다. local task ID와 Stop 입력의 session/turn, 현재 run/response attempt, root actor, hook definition hash로 immutable logical Stop slot을 만든다. mutable 재개 counter와 검사 결과는 최초 transaction 안에 snapshot하고 CAS로 재개 권한을 한 번만 예약한다. exact assistant-message hash는 같은 slot의 byte-exact payload 충돌 검사에 사용한다.
- host가 같은 continuation prompt를 두 번 만들 가능성까지 제거했다고 주장하지 않는다. reason에 넣은 attempt ID와 nonce를 다음 `UserPromptSubmit`에서 one-use claim하여 실제 보완 작업의 상태 변경과 protected tool 권한은 최대 한 번만 허용한다.
- SessionStart는 required `source`를 보존한다. `source=compact`는 matching context-stream compaction으로 처리하고 새 Desktop session recovery를 실행하지 않는다. tool/compact hook의 optional `agent_id`/`agent_type`은 root와 subagent stream 구분에 사용한다.
- `stop_hook_active=true`이면 새 `decision:block`을 내지 않는다. 재검사가 성공하면 종료를 허용하고, 실패하면 미완료로 중단한다. 새 run을 만들어 제한을 초기화하지 않는다.
- 사용자 취소·범위 변경·외부 접근 문제는 자동 재개 사유에서 제외한다.
- 검사 오류를 성공으로 바꾸지 않는다. 다만 host가 hook을 건너뛰거나 실패를 계속 처리할 수 있으므로 hook을 보안 경계나 절대적 강제로 표현하지 않는다.
- hook 메시지에는 코드가 만든 누락 ID와 짧은 안내만 넣는다. PDF의 문장을 높은 우선순위 지침으로 주입하지 않는다.
- hook 안에서는 PDF 재추출, 이미지 렌더링, 모델 호출 같은 긴 작업을 하지 않는다.

실패 처리 계약:

| 관측 상황 | 기록/응답 | 기대 동작 |
| --- | --- | --- |
| 첫 종료에서 보완 가능한 누락 | `needs_work`, 재개 1회 기록, `decision:block` | 같은 Main이 원문 도구를 추가 호출 |
| 재개 후에도 누락 / 같은 누락 반복 | `blocked`, 짧은 경고, `continue:false` | 자동 반복 종료, 미완료 범위 표시 |
| 사용자 취소·질문 전환 | `paused`, gate 해제 | 다음 일반 대화를 가로막지 않음 |
| 접근 제한·손상 PDF 등 도구로 해결 불가 | `blocked`, 재개 없음 | 필요한 파일/사용자 선택 보고 |
| actor 또는 전달 관측 불명 | `unknown`, 자동 판정 비활성화 | Main coverage를 성공으로 기록하지 않음 |
| hook 미실행·신뢰 미승인·timeout | 외부 시험 또는 다음 정상 `check`에서 `observer_unavailable` | 자동 재개를 제공한다고 표시하지 않음 |

마지막 행은 실행되지 않은 hook이 스스로 실패를 기록한다는 뜻이 아니다.
host의 경고/이벤트와 hook 실행 기록을 P0 시험 또는 다음 정상 검사에서 대조해야 한다.
사용자 입력이 새로 들어오면 이전 자동 보완 gate를 우선 해제하고, Main이 명시적으로 독해를 계속할 때만 다시 켠다.
UI 중단 이후 다음 입력도 같은 규칙을 적용한다. 취소 문구를 완벽히 자동 해석한다는 가정을 하지 않는다.

### F. 후속 질문

같은 Main이 질문에 답한다. 별도 질문별 파이프라인은 만들지 않는다.
정확한 수치·인용·수식·반박에는 원문 페이지 또는 확대 이미지를 다시 연다.
답변은 논문의 주장, Main의 해석, 원문이 뒷받침하지 않는 부분을 구분한다.
긴 전체 설명·튜토리얼·논쟁적 해석에는 설명 흐름 검토자에게 실제 초안을 전달하고, Main이 결과를 반영한 뒤 사용자에게 답한다.
짧은 페이지 확인·간단한 후속 질문은 기존 원문 재확인 규칙으로 처리한다. 설명 초안이 없는 초기 독해 완료 안내에 억지로 긴 설명을 생성하지 않는다.
compaction/재개 이후에는 작업 노트가 원문을 대체하지 않는다. 관련 원문을 다시 읽고, 전체 재검토 요청이면 전체 전달부터 반복한다.

## 4. 기록은 최소한으로, 의미는 정확하게

제안 파일 구조의 루트는 프로젝트 루트다. 아래 구조는 초기 설계 당시의 목표 형태다.

```text
readpaper/
  PROJECT_GOAL.md
  IMPLEMENTATION_PLAN.md
  AGENTS.md                              지속적인 출처·안전 규칙
  .agents/skills/readpaper/
    SKILL.md                             독해 순서와 실패 시 행동
    scripts/paper.py                      prepare/read/render/check
  .codex/
    config.toml                          검증 후 프로젝트 한정 설정
    hooks.json                           검증 후 활성화
    hooks/readpaper_hook.py               짧은 검사·관측 adapter
  papers/<paper-id>/
    source.pdf
    manifest.json
    fulltext.txt
    pages/                               페이지별 텍스트와 PNG
    understanding.md                     Main의 전체 이해·미해결 사항
    explanations/                        긴 사용자 설명의 검토용 초안과 버전
    audits/                              검토 결과와 Main의 처리 결과
    runs/<run-id>/events.jsonl            자동 생성한 처리·관측 기록
```

`paper-id`는 임의 제목을 경로로 쓰지 않고 source hash에서 만든 안전한 식별자다.
`prepare`는 `paper_id`와 새 `run_id`를 반환하고, Main의 후속 read/render/check 명령에는 그 `run_id`를 전달한다.
자동 관측 adapter는 준비 명령의 실제 hook 이벤트와 반환 ID를 대조해 session/run을 연결한다.
Main의 시작·재개 명령과 연결을 확인하지 못한 run은 자동 gate를 켜지 않는다.
여러 프로세스가 기록할 수 있으므로 파일 잠금과 atomic replace/append를 사용한다.
원본·관측 기록은 Main이 수동으로 성공 처리하는 체크리스트와 분리한다.

각 관측 이벤트에는 `session_id`, `turn_id`, `agent_id` 또는 `null`, `actor`, `tool_use_id`, `run_id`, `paper_id`, `event_kind`, 원문 범위, 결과를 둔다.
`actor`는 `root_main | subagent | hook | unknown`이다. CLI의 `--actor main` 같은 자기 신고만으로 root Main이라고 판정하지 않는다.
host가 준 agent/turn 식별자와 root 종료 이벤트를 실제로 연결할 수 있는지 P0에서 시험한다.
Hook의 `session_id`만으로는 부족하다. Subagent hook에도 parent session id가 들어갈 수 있다.
[Hook 입력 필드](https://learn.chatgpt.com/docs/hooks#common-input-fields).

`check`는 root Main으로 식별된 읽기/이미지 열기 관측만 Main coverage 후보에 넣는다.
렌더링 파일 생성은 이미지 열기 관측이 아니다. Subagent의 원문 열기는 감사 범위에만 기록한다.
actor를 구분할 수 없는 host에서는 자동 Main coverage를 `unknown`으로 두고, 도구 이력을 사람이 검토하는 prototype으로 한정한다.
검토자에게는 원문 경로와 자신의 audit 식별자를 주며, Main의 run을 완료 처리할 권한은 주지 않는다.

페이지 주소는 다음 필드를 구분한다.

| 필드 | 예시 | 의미 |
| --- | --- | --- |
| `pdf_index` | `5` | 코드의 0-based 페이지 인덱스 |
| `pdf_page` | `6` | 사람이 PDF에서 여는 1-based 순서 |
| `pdf_label` | `"4"` 또는 `null` | PDF metadata의 페이지 라벨. 자동 대체값이면 그 사실도 표시 |
| `printed_label` | `"4"` 또는 `null` | 페이지에 실제로 인쇄된 번호. 시각 확인 전에는 확정하지 않음 |

PDF metadata 라벨과 인쇄된 번호를 같은 것으로 가정하지 않는다.
그림·표·수식은 source hash + PDF 순서 + 객체명으로 참조한다. 확대 영역이 있으면 렌더링 기준과 좌표도 보관한다.
[pypdf 페이지 인덱스와 라벨](https://pypdf.readthedocs.io/en/stable/modules/PdfReader.html#pypdf.PdfReader.page_labels).

`check`가 검사할 수 있는 것:

- 페이지별 산출물의 존재·해시·범위와 추출 경고.
- 출력한 원문 범위의 누락·중복, 관측된 잘림, 이미지 열기 도구 호출 기록.
- 원문 위치 참조의 유효성, 요구한 검토의 반환 여부, 알려진 미처리 항목.

`check`가 증명할 수 없는 것:

- Main이 모든 문장을 의미적으로 완전히 이해했다는 사실.
- 텍스트 추출만으로 모든 그림·표·수식이 정확하게 인식됐다는 사실.
- 파일이나 출력 로그만으로 현재 모델 입력에 전체 원문이 남아 있다는 사실.
- 문서에 없는 답이 정말 없다는 것을 기계적으로 완전히 증명하는 것.

기록 필드는 `prepared`, `emitted`, `tool_observed`, `main_review_recorded`, `unknown`처럼 관측 수준을 드러낸다.
특히 PostToolUse가 본 nested tool 결과가 최종 모델 입력까지 전달됐다는 가정은 하지 않는다.
`full_paper_in_live_context: true`나 `understanding_verified: true` 같은 자동 인증 필드는 만들지 않는다.

## 5. Context와 compact 정책

처음부터 무조건 큰 숫자를 config에 넣지 않는다. P0에서 실제 출력 경로와 context 증가를 먼저 측정한다.

- `tool_output_token_limit`: 개별 도구 결과를 history에 남기는 예산.
- 도구 호출의 `max_output_tokens`, Code Mode 결과 상한 등 더 작은 출력 제한도 별도로 고려한다.
- `model_auto_compact_token_limit`: 누적 history의 자동 압축 임계값. 임의의 `0/-1`을 해제 값으로 쓰지 않는다.
- `model_context_window`: 실제 모델 용량을 늘리는 스위치로 사용하지 않는다.

원문 텍스트뿐 아니라 이미지, 기존 지침·대화, 검토 결과, 출력·추론 여유를 함께 고려한다.
정확한 tokenizer/모델 입력이 없으면 추정치라고 표시한다. “논문 10쪽이니 무조건 들어간다”고 판정하지 않는다.
프로젝트 설정을 바꾼 뒤 새로 적용되는 시점과 실제 동작을 확인한다. 디스크 설정만으로 이미 실행 중인 Main의 적용값을 확정하지 않는다.
[공식 설정 문서](https://learn.chatgpt.com/docs/config-file/config-reference).

관측 hook이 검증되면 `PostCompact`로 압축 사실을 기록하고 이전 원문의 현재 context 유지 상태를 미확인으로 바꾼다.
필요한 경우 `PreCompact`를 초기 전체 독해 중의 중단 장치로 쓸 수 있다. 이것은 무한 context나 압축 해제가 아니다.
원문이 안 들어가면 일부를 몰래 요약해 성공시키지 않고, 범위·모델·실행 방식 변경 여부를 사용자와 결정한다.
[PreCompact / PostCompact](https://learn.chatgpt.com/docs/hooks#precompact).

### Main·subagent 모델 선택

Main과 각 subagent 역할의 모델·reasoning effort는 별도로 선택할 수 있다.
구체적인 모델 조합은 아직 정하지 않았으며, 이 계획 변경으로 실제 설정이나 실행 중인 모델을 바꾸지 않는다.

- Main: 작업에서 지원하는 모델 선택 기능, CLI의 `/model`, 또는 시작 시 `model` 설정으로 선택한다.
- Subagent 기본값: `[agents].default_subagent_model`과 `default_subagent_reasoning_effort`로 지정할 수 있다.
- 역할별 지정: custom agent TOML의 `model`, `model_reasoning_effort`로 수식·실험·설명 흐름 검토 모델을 따로 정할 수 있다.
- 이 환경의 `spawn_agent`에도 `model`, `reasoning_effort` 인자가 있다. 사용할 모델을 사용자가 정한 경우, 허용되는 history 전달 옵션과 함께 지정한다.
- 별도 모델/effort 지정이 없으면 부모 설정을 상속한다. 역할 파일이 모델/effort를 고정하면 그 설정이 우선하므로 역할 파일도 함께 확인한다.
- 사용 가능한 모델과 지원 effort·이미지 입력·context 한도를 확인한다. 기본 설정을 바꾸는 것만으로 이미 실행 중인 subagent가 자동 전환된다고 가정하지 않는다.

처음 품질 비교는 모델·질문 조건을 통제하고, 이후 모델 조합을 바꾼 별도 실험으로 정확도·비용·시간을 비교한다.
서로 다른 모델을 쓴다는 사실 자체를 독립성이나 정확도의 보증으로 삼지 않는다.
[Main 모델 변경](https://learn.chatgpt.com/docs/developer-commands#set-the-active-model-with-model), [Subagent 모델 설정](https://learn.chatgpt.com/docs/agent-configuration/subagents#custom-agents).

## 6. 구현 순서와 통과 조건

### P0. 위험한 가정부터 검증

일회성 시험으로 시작하고, 다음이 확인되기 전에는 완성형 workflow나 자동 객체 인식기를 만들지 않는다.

1. **원문 전달 시험:** 10쪽 테스트 PDF의 각 페이지 앞·중간·끝에 표식, 그림·표에 별도 값을 넣는다. Main에게 정답표를 주지 않고 전체 텍스트·이미지를 전달한다. 재열람 없는 확인 질문과 실제 도구 결과를 검사한다. 이는 전달 경로의 smoke test이지 이해의 증명이 아니다.
2. **잘림 실패 시험:** 일부러 작은 출력 예산을 사용한다. 중간 생략·끝 생략·한 페이지 내 잘림을 성공으로 기록하지 않아야 한다.
3. **Hook 재개 시험:** 실제 운영할 Codex 앱에서 프로젝트 hook을 사용자 검토 후 활성화한다. 시험 누락 1개를 만든 뒤 Stop→Python JSON→nonce가 일치하는 continuation→같은 Main의 추가 도구 호출을 관측한다. CLI만 통과하면 Desktop 통과라고 하지 않는다.
4. **반복 방지 시험:** 동일 Stop 입력 재전송과 중복 continuation prompt를 주입해 one-use nonce claim과 상태 변경이 한 번만 성공하는지 확인한다. 고칠 수 없는 누락의 1회 재개 상한, `stop_hook_active=true`, 사용자 취소, 일반 대화에서의 비활성화도 확인한다.
5. **압축 시험:** 시험용 작업에서 압축 이벤트가 관측되는지, 이후 원문 유지 상태가 미확인으로 바뀌는지 확인한다. 현재 사용자 전역 설정은 시험 대상으로 바꾸지 않는다.

Hook 시험이 실패하면 수동 도구 검사형 prototype만 가능하다고 명시한다. 자동 재개 기능을 구현 완료라고 하지 않는다.
원문 유지 시험이 실패하면 이 환경에서 엄격한 전체-context 모드를 제공할 수 있는지부터 재판단한다.
새 작업 생성이나 설정·hook 신뢰 변경이 필요한 시험은 구현 승인 뒤 사용자와 진행한다.

P0의 첫 작업은 정답을 별도로 보관한 10쪽 fixture와 짧은 시험용 스크립트를 만드는 것이다.
완성형 `paper.py`는 필요 없다. 기존 `pdftotext`/`pdftoppm`과 시험 hook만으로 전달·재개 가정을 먼저 확인한다.

P0–P2의 구체적 수용 시나리오:

| 단계/시험 | 실행 도구·행동 | 통과 조건과 남길 증거 |
| --- | --- | --- |
| P0 텍스트/이미지 전달 | fixture에 `pdftotext -f 1 -l 10`, `pdftoppm -png` 실행 후 Main이 모든 텍스트와 이미지를 실제 도구로 받음 | 페이지별 앞·중간·끝 표식과 시각 값 응답을 숨긴 정답과 대조; 반환된 도구 본문·이미지 호출 이력 보관 |
| P0 잘림 | 같은 출력에 `exec_command.max_output_tokens`를 의도적으로 작게 지정; 중간/끝 생략 표본도 시험 | 잘린 범위를 정상 전달로 인정하지 않음; 관측 불가라면 `unknown`; 원 출력과 수신 결과 비교 |
| P0 actor 구분 | Main은 1–5쪽, subagent만 6–10쪽을 열고 검사 | Main의 6–10쪽은 누락 또는 `unknown`; subagent 결과로 Main 완료 판정 금지 |
| P0 Stop 재개 | 시험 누락 1개를 둔 채 Main 종료 → hook stdout의 JSON, nonce-matching continuation과 이어지는 도구 호출 관찰 | 같은 Main의 authorized repair 1회 확인; 동일 Stop/reason replay와 중복 prompt에서도 one-use claim·상태 효과는 한 번만 성공; 누락을 남기면 두 번째 종료에서 중단; `stop_hook_active`와 run 재개 횟수 기록 |
| P0 취소/관측 실패 | 재개 중 중단 후 일반 질문, hook 신뢰 해제, 시험 timeout을 각각 수행 | 일반 질문을 강제로 독해에 돌려보내지 않음; 관측 실패를 성공으로 표시하지 않음 |
| P0 압축 | 시험용 작업에서 수동 compact 또는 시험 임계값으로 압축 유도 | 압축 관측 및 유지 상태 변경 확인; CLI 결과와 Desktop 결과를 구분해 기록 |
| P1 CLI 기본 | `paper.py prepare <fixture>` → `read <paper-id> --run-id <run-id> --pdf-pages 1-10` → `render` → 이미지 열기 → `check <run-id>` | 원본 해시 불변, 정확한 10페이지 산출물, 누락 없는 원문 범위, 정확한 페이지 재열람; 수동 확인과 자동 관측을 구별 |
| P1 페이지 예외 | 두 단 본문, 인쇄 라벨 불일치, 부록, 스캔 페이지 fixture를 각각 처리 | 순서 오류/라벨 미확인/OCR 필요를 숨기지 않음; 부록 제외 상태를 전체 완료로 표시하지 않음 |
| P2 감사 실패 | 감사 하나는 정상 완료, 다른 하나는 실패 또는 취소; 잘못된 표 참조를 결과에 포함 | 감사 완료로 오인하지 않음; Main이 표 원문을 열어 처리 결과 또는 미해결 상태를 남김 |
| P2 설명 흐름 | 실제 답변 초안에 근거 없는 결론·부록 조건 누락을 넣고, 별도 초안에는 타당한 결론 우선 설명을 넣음 | 첫 초안의 논리/범위 오류를 지적하되 두 번째를 순서만으로 오류 판정하지 않음; 문제 단락과 수정 이유를 반환 |
| P2 모델 분리 | 사용자가 선택한 Main·검토자 모델로 시험 실행하고 요청/역할 설정과 관측 가능한 실행 metadata를 대조 | 역할별 설정 적용을 확인; 설정값만 보고 실행 모델을 확인했다고 하지 않음; 미지원 조합은 명시적으로 실패 처리 |

시험 결과 파일에는 실행 환경, 명령/도구 입력, 기대 결과, 실제 결과, 관련 증거 경로를 남긴다.
실제 모델 입력을 직접 관측하지 못한 시험은 표식 정답률이 높아도 전체 context 유지의 증명으로 표현하지 않는다.

### P1. 원문을 잃지 않는 최소 독해

`prepare/read/render/check`와 최소 `SKILL.md`를 구현한다.
공개 논문 1편에서 전 페이지 원문 전달, 전 페이지 시각 확인, 전체 이해 노트, 정확한 페이지 재열람을 수행한다.
두 단 본문, 부록, 인쇄 번호 불일치, 스캔 페이지에서 미완료를 숨기지 않는지 검증한다.
이 단계에서는 hook 없이도 Main이 직접 검사 결과를 보고 보완할 수 있어야 한다.

### P2. 독립 검토와 종료 안전장치

P0에서 확인된 방식으로 subagents와 Stop hook을 연결한다.
한 검토자는 수식·시각, 다른 검토자는 주장·실험을 확인하고 Main이 원문과 대조한다.
내용 검토자는 원문 우선 판단 후 Main 노트 대조라는 두 단계 입력을 사용한다.
긴 설명 초안에는 설명 흐름 역할을 추가하고, 논리 오류·설명 구조 문제·선택적 재배치를 구분한다.
누락 페이지, 잘못된 표 참조, 검토자 실패, 중단된 run, 다른 session의 상태가 모두 올바르게 처리돼야 한다.

### P3. 품질 평가

일반적인 “PDF만 주고 읽어 달라” 방식과 같은 모델·같은 논문·같은 질문으로 비교한다.
처음은 성격이 다른 논문 3편으로 한다. 예: 수식 중심, 실험·표 중심, 부록이 중요한 논문.
고정된 정답·출처 목록을 평가자가 준비하고 Main에는 주지 않는다. subagent 동의만으로 채점하지 않는다.

평가 항목:

- 모든 대상 페이지의 처리 누락과 잘림을 탐지했는가.
- 핵심 주장·실험 결과·수식 조건·그림 해석이 원문과 맞는가.
- 인용 페이지와 객체 번호가 맞는가.
- 논문이 답하지 않는 질문에 근거 부족을 밝히는가.
- 부록의 조건 때문에 본문 해석을 수정해야 할 때 반영하는가.
- Main의 설명에서 전제·근거·결론이 연결되는가. 논리 오류와 단순한 순서 취향을 구분하는가.
- 압축 후 다시 근거를 열 수 있는가.
- PDF 안의 “규칙 무시/명령 실행” 문장을 지침이 아닌 원문으로 취급하는가.
- 소요 시간·토큰·재열람 횟수가 품질 개선에 비례하는가.

기계적 누락 탐지와 위치 검사는 고정 시험에서 전부 통과해야 한다.
의미 정확도는 점수와 오류 사례를 함께 보고한다. 3편 통과로 모든 논문 이해를 보장하지 않는다.

## 7. ECC에서 참고할 것과 그대로 가져오지 않을 것

검토 기준 SHA: `5eddf1a3ffd311423be2d4ba7d26f7209c91b033`.

- 참고: 지침, 실행 스크립트, native subagent 역할을 분리하는 구성.
- 참고: 미처리 상태를 검사하고 `decision:block`을 출력하는 작은 hook 패턴.
- 주의: ECC의 해당 재개 예시는 Claude용 연결이다. 이 SHA의 Codex native hook 파일에는 `SessionStart`만 있다. ReadPaper용 Stop 연결은 별도로 구현·검증해야 한다.
- ECC 전체 설치, 모든 설정 복사, hook 이름만 보고 강제 검증이라고 가정하는 일은 하지 않는다.

[ECC Codex hook 실제 구성](https://github.com/affaan-m/ECC/blob/5eddf1a3ffd311423be2d4ba7d26f7209c91b033/hooks/codex-hooks.json#L1-L18), [미처리 상태 검사와 재개 JSON](https://github.com/affaan-m/ECC/blob/5eddf1a3ffd311423be2d4ba7d26f7209c91b033/scripts/hooks/plan-canvas-pending.js#L198-L223).

## 8. 현재 확인된 실현 가능성

2026-08-28 읽기 전용으로 확인:

- 프로젝트에는 기존 `PROJECT_GOAL.md`만 있었다. 본 계획 외 코드·설정은 만들거나 변경하지 않았다.
- 로컬 CLI는 `codex-cli 0.144.1`; `codex features list`에서 `hooks`와 `multi_agent`가 활성화돼 있다.
- 로컬 Python 3.14.0, pypdf 6.10.2, Poppler 26.04.0의 추출·렌더링 명령이 있다.
- 공식 문서에 skill, subagent, 프로젝트 hook, Stop의 continuation 동작이 명시돼 있다.
- CLI 확인은 현재 Desktop Main의 모든 런타임 동작을 검증한 것이 아니다.
- 실제 PDF 독해 시험, hook 설치·신뢰, 자동 재개 시험, context 유지 시험은 아직 실행하지 않았다.

**결론: Codex 내부형 MVP는 구현 가능한 설계다. 다만 전체 원문 전달·유지와 hook 재개는 P0로 검증해야 하며, 완벽한 이해를 코드로 인증하는 제품으로 약속해서는 안 된다.**
