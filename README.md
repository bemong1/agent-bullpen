# Agent Bullpen

<details>
<summary><b>한국어로 보기 (Korean)</b></summary>

**Agent Bullpen (에이전트 현황판)**

Claude Code(와 Codex)의 멀티 에이전트 세션을 웹 화면 한 장으로 보여 주는 읽기 전용 현황판입니다. 누가 무엇을 하는지, 토론이 어디까지 왔는지, 비용이 얼마인지 보입니다. 도구가 이미 디스크에 남기는 기록만 읽으므로 훅도, 설치도, 설정도 필요 없습니다.

![현황판](docs/images/ko/dashboard.png)

![도트 사무실 화면](docs/images/ko/office.png)

## 무엇을 보여 주나

- **오케스트레이터와 서브에이전트**: 상태(작업 중·완료·멈춤 의심·종료), 지금 쓰는 도구, 컨텍스트 사용량, 모델·effort. 멈춘 일은 구별해서 보여 줍니다. 사용 한도(초기화 시각 포함)·API 오류·시간 제한·중도 종료로 **중단됨**, 확인할 프로세스가 없으면 **알 수 없음**입니다. 서브에이전트나 다른 실행이 띄운 `claude -p` 실행은 띄운 쪽 아래에 놓입니다.
- **토론**: 디스크에 있는 `<주제>/r<N>/<참가자>.md` 보고서를 따라가는 주제 × 라운드 × 참가자 표. 보고서는 화면에서 바로 읽습니다.
- **도트 사무실**(전체 화면은 `/game`): 일하는 에이전트는 책상에 앉고, 끝나면 휴게실로 가고, 서로 메시지를 전하러 걸어 다닙니다.
- **대화**: 사용자 ↔ 오케스트레이터, 에이전트 ↔ 에이전트.
- **알림**: 사용자가 판단해야 하는 것(답을 기다리는 질문, 멈추거나 실패한 에이전트, 사용 한도: 같은 초기화를 기다리는 것 전부에 알림 하나).
- **토큰과 비용**(에이전트별, API 정가로 환산한 값이며 실제 청구액이 아닙니다), 활동 타임라인, 요금제·사용 한도 줄.
- **Codex**를 Claude Code 옆에 같이 보여 줍니다. Claude가 띄운 `codex exec`는 같은 팀원이 되고, Codex 단독 대화도 따로 나옵니다.
- 서브에이전트가 없는 Claude 세션도 같이 나옵니다.
- **진단**: 현황판이 알아챘지만 판정하지 못한 것(두 후보 사이의 비김, 풀지 못한 경로, 모르는 기록 형식)을 기록의 글 없이 목록으로 보여 줍니다.

화면 하나하나의 설명은 [화면 설명서](docs/guide.ko.md)에 있습니다.

![에이전트 목록: 두 단계로 띄운 실행과 서로 다른 이유로 멈춘 실행(사용 한도·시간 제한)](docs/images/ko/agents.png)

![토론 현황 표: 주제 × 라운드 × 참가자](docs/images/ko/debates.png)

## 지원 현황

| 플랫폼 | 상태 |
|---|---|
| Linux | 확인됨 |
| macOS | 실험적. Mac에서 확인할 예정이며, 프로세스 판정은 `ps`로 대신합니다. `--claude-usage-api`는 보통 읽을 로그인 파일이 없습니다([보안과 개인정보](#보안과-개인정보)). |
| WSL2 | 실험적(Linux로 돌지만 아직 확인 전). |
| Windows(네이티브) | 지원하지 않습니다. |

**Python 3.9 이상**이 필요합니다. 표준 라이브러리만 쓰므로 `pip install`할 것이 없습니다.

> **언어.** 화면과 터미널 출력은 기본이 영어이고 한국어를 고를 수 있습니다. 화면은 머리글의 언어 선택기에서 고르거나 주소에 `?lang=ko`를 붙이거나 브라우저 언어에 맡깁니다. 터미널은 `--lang`, `AGENT_BULLPEN_LANG`, 로케일(`LANG`)을 따릅니다. 화면 글은 언어마다 사전 파일 하나(`static/locales/<코드>.json`)에 모여 있어서 새 언어는 JSON 파일 하나로 더합니다(서버는 재시작 없이 알아보고, 화면은 새로 고치면 됩니다. [`docs/development.md`](docs/development.md)). 문서는 영어([screen guide](docs/guide.md))와 한국어([화면 설명서](docs/guide.ko.md))가 있고, 설정·원격·개발 문서는 영어입니다.

## 빠른 시작

```bash
git clone https://github.com/bemong1/agent-bullpen.git
cd agent-bullpen
python3 server.py
```

<http://localhost:8790>을 엽니다. Claude Code나 Codex 세션이 있으면(또는 새로 시작하면) 나타납니다. 세션이 아직 없으면 화면이 읽은 폴더를 알려 주고 5초마다 다시 확인합니다.

서버는 시작할 때 주소, 읽은 폴더, 찾은 세션 수를 출력합니다.

```
현황판: http://localhost:8790/
  Claude Code   ~/.claude/projects           세션 2 · 에이전트 있는 세션 1  (기본)
  ◆ Codex       ~/.codex/sessions            최근 7일 0  (기본)
  사용량 조회 꺼짐 · 프로세스 판정 /proc
세션 a11ce000-0000-4000-8000-000000000001 읽음: 에이전트 5명, 0.0s
```

한국어 로케일(`LANG=ko_KR.UTF-8`)이거나 `--lang ko`를 줄 때의 출력입니다. 영어(기본)는 `Dashboard: http://localhost:8790/`로 시작합니다.

### 샘플 데이터로 먼저 띄워 보기

아직 기록이 없거나, 내 작업에 연결하기 전에 바쁜 화면을 먼저 보고 싶다면 합성 `HOME`을 만들어 그 위에서 띄웁니다:

```bash
python3 tools/synth_home.py /tmp/acme-home --busy --live   # 지어낸 세션(--busy: 꽉 찬 사무실); --live는 일부를 작업 중으로 보이게 합니다; --stopped를 더하면 멈춘 일이 나옵니다
env -u CLAUDE_CONFIG_DIR -u CODEX_HOME HOME=/tmp/acme-home python3 server.py --port 8791
python3 tools/synth_home.py /tmp/acme-home --stop      # 다 보고 나서: 가짜 프로세스 끄기
```

<http://localhost:8791>을 엽니다. 데모 시나리오(`/game?demo=all`)도 이 데이터 위에서 돕니다.

### 다른 기기에서 보기

SSH 터널을 만든 뒤 그 기기에서 `http://localhost:8790`을 엽니다.

```bash
ssh -L 8790:localhost:8790 you@the-machine
```

서버는 그대로 루프백에만 열려 있습니다. VPN 주소로 여는 방법과 경고는 [`docs/remote.md`](docs/remote.md)(영어)를 보세요.

## 옵션

```
python3 server.py [옵션]
```

| 옵션 | 기본값 | 뜻 |
|---|---|---|
| `--claude-config-dir DIR` | `$CLAUDE_CONFIG_DIR`, 없으면 `~/.claude` | Claude Code 설정 폴더. 기록은 `DIR/projects`에서 읽습니다. |
| `--codex-home DIR` | `$CODEX_HOME`, 없으면 `~/.codex` | Codex 폴더. 기록은 `DIR/sessions`(최근 7일)에서 읽습니다. |
| `--port PORT` | `8790` | 열 포트. 이미 쓰고 있으면 한 줄 안내 뒤 끝납니다. |
| `--host ADDR` | `127.0.0.1` | 열 주소(여러 번 가능). 루프백, `100.64.0.0/10`, `fd7a:115c:a1e0::/48`(Tailscale·NetBird 같은 VPN이 쓰는 대역)만 받고, `0.0.0.0`·LAN 주소는 기동을 거부합니다. 루프백이 아니면 "인증 없음" 경고를 출력합니다. |
| `--allow-host NAME` | 없음 | `Host` 헤더로 더 받을 이름(여러 번 가능). 이름(`my.box`) 또는 접미사(`.example.net`: `a.example.net`은 되고 `example.net` 자신은 안 됨). |
| `--session ID` | 가장 최근에 움직인 오케스트레이션, 없으면 가장 최근 일반 대화 | 주소에 세션이 없을 때 열 세션(UUID). |
| `--lang CODE` | `auto`: `AGENT_BULLPEN_LANG`, `LC_ALL`, `LC_MESSAGES`, `LANG` 순서, 없으면 `en` | 터미널 출력과 `--help`의 언어(`en`, `ko`). 화면 언어는 따로입니다: 머리글의 선택기, `?lang=`, 브라우저. |
| `--claude-usage-api` | 끔 | Claude 요금제 줄을 위해 Anthropic의 비공식 사용량 API를 조회합니다. [보안과 개인정보](#보안과-개인정보)를 먼저 읽으세요. |
| `--no-link-cache` | 끔 | 확실한 세션 연결을 `links.json`에 남기지 않습니다([보안과 개인정보](#보안과-개인정보)). |

자세한 설명과 우선순위: [`docs/configuration.md`](docs/configuration.md)(영어).

## 보안과 개인정보

- **읽기만 합니다.** Claude Code·Codex 폴더(기본 `~/.claude`·`~/.codex`)에 쓰지 않습니다. 세션 기록과, 요금제 등급·사용률 캐시를 위해 Claude Code의 `.claude.json`을 읽습니다(계정 id·이메일은 화면으로 내보내지 않습니다). `auth.json`, `settings*.json`, `config.toml`, `history.jsonl`, Codex sqlite는 열지 않고, `.credentials.json`은 `--claude-usage-api`를 줄 때만 엽니다(아래).
- **인증이 없습니다.** 포트에 닿을 수 있는 사람은 누구나 대화 내용을 읽을 수 있습니다. 기본은 `127.0.0.1`에만 열립니다. 다른 주소로 열거나 `--allow-host`를 주면 경고를 출력하며, 그 뒤의 접근 제어는 이 프로그램이 아니라 VPN·방화벽 몫입니다. 인터넷에 열지 마세요.
- **기본 설정에서는 외부로 나가는 요청이 없습니다.** 서버는 네트워크를 부르지 않고, 화면도 인터넷에서 받는 것이 없습니다(글꼴을 동봉했습니다). 기본으로 쓰는 파일은 작은 연결 기록 `links.json` 하나뿐입니다(`$XDG_CACHE_HOME/agent-bullpen/`, 기본 `~/.cache/agent-bullpen/`, 권한 0600). 자식 세션을 그것을 띄운 세션에 확실한 증거(프로세스 계보, 환경변수, 출력 파일, 일치하는 긴 지시문)로 이으면 두 세션 id와 규칙·시각만(경로·지시문·환경변수 값은 없음) 최대 2000개, 90일까지 남겨 재시작해도 연결을 잊지 않게 하고, 그런 연결을 찾기 전에는 아무것도 만들지 않습니다. 추정은 남기지 않습니다. `--no-link-cache`로 끕니다.
- **`--claude-usage-api`는 선택 사항이며 비공식이고 사용자 책임입니다.** Claude 설정 폴더(기본 `~/.claude`, `CLAUDE_CONFIG_DIR`로 바꿈)의 `.credentials.json`에 있는 access token으로 문서화되지 않은 Anthropic 엔드포인트(`api.anthropic.com/api/oauth/usage`)를 60초마다 부릅니다. 예고 없이 바뀌거나 막힐 수 있습니다. 토큰은 화면·로그에 내보내지 않고 다른 곳으로 보내지 않습니다. 마지막 사용률과 재설정 시각(토큰·계정 식별자 없음, 권한 0600)은 `$XDG_CACHE_HOME/agent-bullpen/usage.json`(기본 `~/.cache/agent-bullpen/`)에 남깁니다. 플래그가 없으면 하단 줄은 `.claude.json`에 Claude Code가 `/usage`를 열 때 갱신하는 캐시 값을 "기록 HH:MM"으로 보여 줍니다. Claude Code가 로그인 정보를 그 파일이 아닌 곳에 두면(macOS는 보통 키체인) 플래그가 읽을 토큰이 없습니다: 하단 줄은 캐시 값을 그대로 보이고 노랑 "로그인 정보 없음"만 덧붙으며, 키체인은 읽지 않습니다.
- **문서 보기는 좁게 열립니다.** 에이전트가 쓴 파일과 토론 폴더 안의 파일(`.md`·`.txt`·`.yaml`·`.json`, 일반 파일, 2 MiB까지)만 엽니다. 인증·설정 파일, `~/.codex` 아래, 경로에 점(`.`) 폴더가 든 파일(예외는 Claude Code worktree `<저장소>/.claude/worktrees/<이름>/`뿐이고, 그 안에서도 `.env`·`.git` 같은 점 성분은 닫혀 있습니다), 비밀처럼 보이는 이름(`secret`·`credential`·`password`·`token`·`apikey`·`kubeconfig…`)의 파일은 `.md`도 포함해 열지 않습니다. 토론 표는 더 느슨해서 보고서 파일이 있는지만 봅니다. 그래서 숨김 폴더 아래의 보고서나 `token_budget.md` 같은 이름도 "제출"로 보이지만, 화면에서 열 수는 없으니 열어 보려는 보고서 이름에는 이런 낱말을 쓰지 마세요.
- **Host 검사.** `Host` 헤더가 허용 목록에 없으면 403입니다(DNS rebinding 방어).
- **내 화면 캡처에는 내 대화가 들어 있습니다.** 이 저장소의 스크린샷은 합성 데이터로 만든 것입니다.

## 토론 폴더 규칙

토론은 보고서 경로로 알아봅니다. 아래처럼 두고, 에이전트에게 이 경로에 쓰라고 지시하세요.

```
<주제>/
  brief.md          # 주제 지침: "# 제목", 참가자마다 "**A — 역할**" 줄
  r1/A.md           # 1라운드, 참가자마다 보고서 하나
  r1/B.md
  r2/A.md           # 2라운드 …
  rulings.md        # 최종 결과(아래)
```

- 폴더가 디스크에 있어야 합니다. 보고서 경로는 `<주제>/r<N>/<참가자>.md`(`r01`·`round1`도 됩니다)이고, 에이전트에게 준 지시에 절대 경로(또는 `~/…`)로 나오거나, 에이전트가 실제로 쓰거나, 에이전트가 실행한 명령이 쓸 대상으로 적혀 있어야 합니다. `<참가자>`는 에이전트의 태그(`A`, `T1-A`, `opus-1`)나 그 에이전트가 쓴 파일과 맞춥니다. 보고서를 읽기만 한 에이전트, 토론 폴더 둘에 다 맞는 경로, 지시문이 쓰지 말라고 한 경로는 자리를 주지 않습니다.
- 주제 폴더에 `brief.md`가 있으면 제목과 역할도 읽습니다. 부모 폴더에도 공통 `brief.md`가 있으면 여러 주제를 토론 하나로 묶습니다. 라운드 폴더가 없는 검토는 `brief.md`가 검토자와 각자의 결과 파일을 밝혔을 때만 칸이 생깁니다.
- **최종 결과**는 둘 중 하나입니다.
  - 공통 `brief.md`의 표에 ``| 이름 | `t1_naming/` | — | `final/t1_naming.md` |`` 같은 줄이 있다(열: 주제, 폴더, 선행, 최종 산출물. 폴더 칸은 백틱 `이름/`, 산출물 칸은 백틱 `final/<파일>` 꼴이어야 하고, 첫 줄은 머리글입니다).
  - 자동 인식: 그런 줄이 없으면 주제 폴더 바로 아래 `.md` 가운데 마지막 제출 보고서와 같거나 나중에 생긴 가장 최근 것(`brief.md`·`round<N>.md`는 제외, `final`·`ruling`·`summary` 같은 이름이 우선). 쓰는 중인 참가자가 있으면 찾지 않습니다.

전체 규칙은 [`docs/guide.ko.md`](docs/guide.ko.md#토론을-알아보는-방법)에 있습니다.

## 데모

`http://localhost:8790/game?demo=all`은 사무실 화면에서 시나리오를 모두 재생합니다(`basic`·`new`·`round2`·`trouble`·`talk`, 현황판은 `/?demo=<이름>`). 데모는 브라우저 안에서만 돌고 아무것도 바꾸지 않지만, 열려 있는 세션 위에 그리므로 세션이 하나 이상 열려 있어야 합니다. 세션이 없으면 진단 칸이 대신 나옵니다.

## 문제 해결

**빈 화면이거나 "아직 열 세션이 없습니다".** 오류가 아닙니다. 읽은 폴더, 그 폴더가 어디서 왔는지(기본·환경변수·명령 인자), 찾은 세션 수를 보여 주고 5초마다 다시 확인합니다.

![진단 칸](docs/images/ko/empty.png)

- *폴더 없음* → 기록이 다른 곳에 있습니다. `--claude-config-dir <폴더>` / `--codex-home <폴더>`로 시작하거나 `CLAUDE_CONFIG_DIR` / `CODEX_HOME`을 설정하세요.
- *세션 0* → 폴더는 맞지만 아직 대화 기록이 없습니다. Codex는 최근 7일만 봅니다.
- 세션은 있는데 원하는 것이 안 보임 → 주소에 `?session=<id>`를 붙여 엽니다(`--session`도 같은 UUID). 서브에이전트가 없는 대화는 오케스트레이션과 따로 "다른 세션" 메뉴나 프로젝트 선택의 "일반 대화 · 서브에이전트 없음" 묶음에 있습니다.

**에이전트가 "중단됨"이나 "알 수 없음"이라고 하거나, 실행이 에이전트 목록에 없음.** 중단됨은 사용 한도(카드에 초기화 시각이 나옵니다)·API 오류·시간 제한·중도 종료로 멈춘 일입니다. 알 수 없음은 확인할 프로세스가 없고 기록도 조용해진 것입니다. 띄운 세션에 잇지 못한 `claude -p` 실행은 에이전트가 아니라 접힌 묶음 "연결 안 된 하위 실행 N개"에 이유와 함께 있습니다. 머리글의 진단 칩은 현황판이 판정하지 못한 것을 모두 보여 줍니다([화면 설명서](docs/guide.ko.md#진단)).

**포트 충돌.**

```
현황판을 열지 못했습니다: 127.0.0.1:8790 — 포트를 이미 쓰고 있습니다. 이미 띄운 현황판이면 http://localhost:8790/ 을 여세요. 다른 포트로 띄우려면 --port 8791 처럼 주세요.
```

(한국어 출력입니다. 영어는 `Couldn't start the dashboard on 127.0.0.1:8790 — the port is already in use. …`.)

예전에 띄운 현황판이 아직 떠 있을 가능성이 큽니다. 그것을 열거나 다른 `--port`를 주세요.

**403 "Host not allowed".** 서버가 모르는 이름(VPN 이름, 리버스 프록시)으로 접속했습니다. `--allow-host <그 이름>`이나 `--allow-host .example.net` 같은 접미사로 다시 시작하세요. 403 화면이 필요한 플래그를 알려 줍니다.

**화면이나 터미널의 언어가 원하는 것과 다름.** 화면은 `?lang=`, 머리글 선택기에서 마지막으로 고른 값, 브라우저 언어, 영어 순서이고, 터미널은 `--lang`, `AGENT_BULLPEN_LANG`, `LC_ALL`, `LC_MESSAGES`, `LANG`, 영어 순서입니다([화면 설명서](docs/guide.ko.md#화면-언어와-터미널-언어)).

**`--host`로 기동이 거부됨.** `--host`는 루프백과 VPN 대역만 받습니다. 다른 기기에서는 [SSH 터널](#다른-기기에서-보기)을 쓰세요.

## 개발

시험은 표준 라이브러리 `unittest`입니다. 구조와 회귀 도구는 [`docs/development.md`](docs/development.md)(영어)에 있습니다.

```bash
python3 -m unittest discover -s tests
```

## 고지

비공식 도구입니다 — Anthropic·OpenAI와 무관합니다(Unofficial — not affiliated with Anthropic or OpenAI). "Claude", "Claude Code", "Codex", "OpenAI"는 각 소유자의 상표입니다. 사무실 화면의 모니터 기호(`>_`, ◆)는 로고가 아니라 중립 기호입니다.

## 라이선스

[MIT](LICENSE). 동봉한 Galmuri 글꼴은 SIL Open Font License를 따릅니다([`THIRD_PARTY.md`](THIRD_PARTY.md)).

</details>


A live, read-only office view of your Claude Code and Codex agents: who is working on what, how a debate is going, what it costs. It reads the transcripts the tools already write to disk. No hooks, no install, nothing to configure.

![Dashboard](docs/images/en/dashboard.png)

![Pixel-art office view](docs/images/en/office.png)

## What it shows

- **Orchestrator and agents**: status (working, done, stalled, ended), the tool each one is using right now, context fill, model and effort. Work that stopped is told apart: **interrupted** by a usage limit (with the reset time), an API error, a time limit or an early exit, or **unknown** when there is no process to check. A `claude -p` run started by an agent or by another run stands under the one that started it.
- **Debates**: a topic × round × participant table that follows `<topic>/r<N>/<who>.md` report files on disk, with the reports readable in the page.
- **A pixel-art office** (`/game` for full screen): agents sit at desks while they work, walk to the lounge when done, and carry messages to each other.
- **Conversations**: you ↔ orchestrator, and agent ↔ agent.
- **Alerts** for things that need you: a pending question, a stalled or failed agent, a usage limit (one alert for everything that waits on the same reset).
- **Tokens and cost** per agent (API-price equivalent, not your bill), an activity timeline, and a plan / usage bar.
- **Codex** next to Claude Code: `codex exec` runs that Claude launches join the team; Codex-only conversations are listed too.
- Plain Claude sessions with no agents are listed as well.
- **Diagnostics**: what the dashboard noticed but could not settle (a tie between two possible launchers, a path it could not work out, an unknown transcript format), in a list that never contains transcript text.

Every panel is explained in the [screen guide](docs/guide.md).

![Agent list: a run that launched two levels of runs, and runs that stopped (usage limit, time limit)](docs/images/en/agents.png)

![Debate table: topic × round × participant](docs/images/en/debates.png)

## Status / Support

| Platform | Status |
|---|---|
| Linux | Verified |
| macOS | Experimental. Verification on a Mac is pending; process detection falls back to `ps`, and `--claude-usage-api` normally has no login file to read there (see [Security & privacy](#security--privacy)). |
| WSL2 | Experimental (runs as Linux, not yet verified). |
| Windows (native) | Not supported. |

Requires **Python 3.9+**. The standard library only: nothing to `pip install`.

> **Languages.** The dashboard and the terminal output are in English by default, and Korean is available: pick it in the language selector in the header, add `?lang=ko` to the address, or let your browser's language decide. The terminal follows `--lang`, `AGENT_BULLPEN_LANG` or your locale (`LANG`). All the screen text lives in one dictionary file per language (`static/locales/<code>.json`), so adding a language is one JSON file (the server picks it up without a restart; reload the page): see [`docs/development.md`](docs/development.md). The documentation is in English (the [screen guide](docs/guide.md)) and Korean ([화면 설명서](docs/guide.ko.md)).

## Quickstart

```bash
git clone https://github.com/bemong1/agent-bullpen.git
cd agent-bullpen
python3 server.py
```

Open <http://localhost:8790>. Start (or already have) a Claude Code or Codex session and it appears; with no sessions yet the page tells you which folders it looked in and checks again every 5 seconds.

On start the server prints the address, the folders it reads and how many sessions it found:

```
Dashboard: http://localhost:8790/
  Claude Code   ~/.claude/projects           sessions 2 · with agents 1  (default)
  ◆ Codex       ~/.codex/sessions            last 7 days: 0  (default)
  Usage API off · process check /proc
Session a11ce000-0000-4000-8000-000000000001 loaded in 0.0s · agents 5
```

### Try it with sample data first

No transcripts of your own yet, or want to see a busy dashboard before pointing it at your work? Build a synthetic `HOME` and run the server on it:

```bash
python3 tools/synth_home.py /tmp/acme-home --busy --live   # made-up sessions (--busy: a full office); --live makes some show as working; add --stopped for stopped work
env -u CLAUDE_CONFIG_DIR -u CODEX_HOME HOME=/tmp/acme-home python3 server.py --port 8791
python3 tools/synth_home.py /tmp/acme-home --stop      # afterwards: stop the fake processes
```

Open <http://localhost:8791>. The demo scenarios (`/game?demo=all`) also play on this data.

### Viewing it from another device

Tunnel over SSH, then open `http://localhost:8790` on the device:

```bash
ssh -L 8790:localhost:8790 you@the-machine
```

The server itself keeps listening on loopback only. More (VPN addresses and their warnings): [`docs/remote.md`](docs/remote.md).

## Options

```
python3 server.py [options]
```

| Option | Default | Meaning |
|---|---|---|
| `--claude-config-dir DIR` | `$CLAUDE_CONFIG_DIR`, else `~/.claude` | Claude Code config folder; transcripts are read from `DIR/projects`. |
| `--codex-home DIR` | `$CODEX_HOME`, else `~/.codex` | Codex folder; transcripts are read from `DIR/sessions` (last 7 days). |
| `--port PORT` | `8790` | Port to listen on. If it is taken, the server says so in one line and exits. |
| `--host ADDR` | `127.0.0.1` | Address to listen on; may be repeated. Only loopback, `100.64.0.0/10` and `fd7a:115c:a1e0::/48` (the ranges Tailscale/NetBird-style VPNs use) are accepted; `0.0.0.0` and LAN addresses refuse to start. Anything non-loopback prints a "no authentication" warning. |
| `--allow-host NAME` | none | Extra names accepted in the `Host` header; may be repeated. A name (`my.box`) or a suffix (`.example.net`, which matches `a.example.net` but not `example.net`). |
| `--session ID` | most recently active orchestration, or the most recent plain conversation if there is none | Session (UUID) to open when the URL names none. |
| `--lang CODE` | `auto`: `AGENT_BULLPEN_LANG`, then `LC_ALL`, `LC_MESSAGES`, `LANG`, else `en` | Language of the terminal output and `--help` (`en`, `ko`). The screen language is separate: the selector in the header, `?lang=`, your browser. |
| `--claude-usage-api` | off | Query Anthropic's unofficial usage API for the Claude plan bar. See [Security & privacy](#security--privacy). |
| `--no-link-cache` | off | Do not keep the certain session links in `links.json` (see [Security & privacy](#security--privacy)). |

Details and precedence rules: [`docs/configuration.md`](docs/configuration.md).

## Security & privacy

- **Read-only.** It never writes to your Claude Code or Codex folders (`~/.claude` and `~/.codex` by default). It reads session transcripts, plus Claude Code's `.claude.json` for the plan tier and cached usage numbers (account id and email are never sent to the page). It does not open `auth.json`, `settings*.json`, `config.toml`, `history.jsonl` or the Codex sqlite files, and it opens `.credentials.json` only if you pass `--claude-usage-api` (below).
- **No authentication.** Anyone who can reach the port can read your conversations. By default it listens on `127.0.0.1` only. If you open it on another address, or add `--allow-host`, it prints a warning, and what happens next depends on your VPN or firewall, not on this program. Do not expose it to the internet.
- **No outbound requests by default.** The server makes no network calls and the pages load nothing from the internet (the font is bundled). The one file it writes by default is a small link record, `links.json` in `$XDG_CACHE_HOME/agent-bullpen/` (default `~/.cache/agent-bullpen/`, mode 0600): when a child session is tied to the session that started it by firm evidence (process lineage, an environment variable, an output file or a long instruction that matches), it keeps the two session ids with the rule and time (no paths, prompts or environment values), at most 2000 links for 90 days, so that a restart does not forget the link, and it creates nothing until such a link is found. Guesses are not kept. `--no-link-cache` turns that off.
- **`--claude-usage-api` is opt-in, unofficial, and at your own risk.** It reads the access token in `.credentials.json` in your Claude config folder (`~/.claude` by default, or `CLAUDE_CONFIG_DIR`) and calls an undocumented Anthropic endpoint (`api.anthropic.com/api/oauth/usage`) every 60 seconds; it can break or change without notice. The token is never shown, logged or sent anywhere else. It keeps the last usage numbers and reset times (no token, no account identity; mode 0600) in `$XDG_CACHE_HOME/agent-bullpen/usage.json` (default `~/.cache/agent-bullpen/`). Without the flag the bottom bar shows the cached values from `.claude.json`, labelled "recorded HH:MM · refresh with /usage", which Claude Code refreshes when you open `/usage`. If Claude Code keeps your login somewhere other than that file (on macOS it normally uses the Keychain), there is no token for the flag to read: the bar keeps showing the cached values with a yellow "No login found" note, and nothing is read from the Keychain.
- **The document viewer is narrow.** It opens only files the agents wrote and files inside debate folders (`.md`, `.txt`, `.yaml`, `.json`; regular files; up to 2 MiB). It never opens credential or settings files, anything under `~/.codex`, anything with a dot folder in its path (the one exception is a Claude Code worktree, `<repo>/.claude/worktrees/<name>/`, and even there `.env`, `.git` and other dot entries stay closed), or any file whose name looks like it holds a secret (`secret`, `credential`, `password`, `token`, `apikey`, `kubeconfig…`), `.md` included. The debate table is looser: it only checks that a report file exists, so a report under a hidden folder, or one named `token_budget.md`, still counts as submitted. It just cannot be opened from the page, so keep those words out of the names of reports you want to read there.
- **Host check.** Requests whose `Host` header is not on the allow list get a 403 (a defense against DNS rebinding).
- **Your transcripts are in the pictures.** A screenshot of your own dashboard shows your prompts. The screenshots in this repository come from synthetic data.

## Debate folders

A debate is recognized from report paths. Lay it out like this and tell your agents to write to these paths:

```
<topic>/
  brief.md          # topic brief: "# Title", and "**A — role**" lines per participant
  r1/A.md           # round 1, one report per participant
  r1/B.md
  r2/A.md           # round 2 …
  rulings.md        # the final result (see below)
```

- The folder must exist on disk. A report path is `<topic>/r<N>/<who>.md` (`r01` and `round1` work too), given as an absolute path (or `~/…`) in the agent's instructions, written by the agent, or named as the target of a command it ran. `<who>` is matched to the agent's tag (`A`, `T1-A`, `opus-1`) or to the file it wrote. An agent that only reads a report, a path that fits two debate folders, and one the instructions say not to write give no seat.
- With `brief.md` in the topic folder you also get the title and roles. A common `brief.md` in the parent folder groups several topics into one debate. A review with no round folders gets cells only when its `brief.md` names the reviewers and their result files.
- **Final result**, either way:
  - in the parent `brief.md` table, a row such as ``| Naming | `t1_naming/` | — | `final/t1_naming.md` |`` (columns: topic, folder, depends on, final deliverable; the folder must be a backticked `name/` and the deliverable a backticked `final/<file>`, and the first row is the header); or
  - automatic: with no such row, the newest `.md` directly in the topic folder that is at least as new as the last submitted report (`brief.md` and `round<N>.md` are skipped, names like `final`, `ruling`, `summary` win). It is ignored while any participant is still writing.

The full rules are in [`docs/guide.md`](docs/guide.md#how-debates-are-recognized).

## Demo

`http://localhost:8790/game?demo=all` plays every scenario on the office view (`basic`, `new`, `round2`, `trouble`, `talk`; `/?demo=<name>` on the dashboard). The demo runs only in your browser and changes nothing, but it draws over a session that is open, so you need at least one session loaded. With none, you get the diagnostic panel instead.

## Troubleshooting

**Blank page, or "No sessions to open yet".** Not an error: the panel lists the folders it read, where each came from (default, environment variable or flag) and how many sessions it found, and rechecks every 5 seconds.

![Diagnostic panel](docs/images/en/empty.png)

- *Folder not found* → your transcripts are elsewhere: start with `--claude-config-dir <dir>` / `--codex-home <dir>`, or set `CLAUDE_CONFIG_DIR` / `CODEX_HOME`.
- *Sessions 0* → the folder is right, but no conversation has been written yet. Codex only lists the last 7 days.
- Sessions exist but not the one you want → open it with `?session=<id>` in the URL (`--session` takes the same UUID). Conversations with no agents are listed apart from the orchestrations: in the "other" menu of the orchestrator card, or in the group "Plain conversations, no agents" of the project selector.

**An agent says "Interrupted" or "Unknown", or a run is missing from the agent list.** Interrupted is work that stopped on a usage limit (the card says when it resets), an API error, a time limit or an early exit. Unknown means there is no process to check and the records went quiet. A `claude -p` run that could not be tied to the session that started it is not an agent: it is in the folded group "N child runs not linked" with the reason. The Diagnostics chip in the header lists everything the dashboard could not settle (see the [screen guide](docs/guide.md#diagnostics)).

**Port in use.**

```
Couldn't start the dashboard on 127.0.0.1:8790 — the port is already in use. If it is a dashboard you already started, open http://localhost:8790/. For another port, pass --port 8791.
```

An old dashboard is probably still running; open it, or pick another `--port`.

**403 "Host not allowed".** You reached the server by a name it does not know (a VPN name, a reverse proxy). Restart with `--allow-host <that name>` or a suffix such as `--allow-host .example.net`. The 403 page tells you the exact flag.

**The screen or the terminal is in the wrong language.** The screen follows `?lang=`, then your last pick in the header selector, then your browser's languages, then English; the terminal follows `--lang`, then `AGENT_BULLPEN_LANG`, `LC_ALL`, `LC_MESSAGES`, `LANG`, then English (details: [screen guide](docs/guide.md#language-of-the-screen-and-of-the-terminal)).

**A server that will not start with `--host`.** `--host` accepts loopback and VPN ranges only. For another device use [an SSH tunnel](#viewing-it-from-another-device).

## Development

Tests are standard-library `unittest`; the layout and the regression tools are in [`docs/development.md`](docs/development.md).

```bash
python3 -m unittest discover -s tests
```

## Disclaimer

Unofficial — not affiliated with Anthropic or OpenAI. "Claude", "Claude Code", "Codex" and "OpenAI" are trademarks of their respective owners. The monitor symbols in the office view (`>_`, ◆) are neutral marks, not logos.

## License

[MIT](LICENSE). The bundled Galmuri font is under the SIL Open Font License; see [`THIRD_PARTY.md`](THIRD_PARTY.md).
