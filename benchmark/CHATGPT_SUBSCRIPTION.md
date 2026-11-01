# Use your ChatGPT plan with GPT-6.1 Sol

This fork supports `chatgpt/gpt-6.1-sol` through OpenAI's public Sign in with ChatGPT flow
for local/open-source applications. Aider remains the agent: its repository map, prompts,
SEARCH/REPLACE parser, file updates, and reflection loop run locally. This does not invoke
Codex CLI, reuse its credentials, or use ChatGPT's private backend endpoints.

## Run Aider

From this checkout in PowerShell:

```powershell
uv pip install --python .venv/Scripts/python.exe -e ".[chatgpt]"
.venv/Scripts/python.exe -m aider.chatgpt_auth login
.venv/Scripts/python.exe -m aider.chatgpt_auth models
.venv/Scripts/python.exe -m aider --model chatgpt/gpt-6.1-sol
```

The browser's **Continue with ChatGPT** flow registers **Better Aider**. Choose your account
and workspace and authorize ChatGPT plan usage. The model must appear in that account's live
catalog; the adapter never substitutes another model if it is unavailable.

Sign-in is already completed on the development machine used for the initial verification.
The `login` command is for reconnecting or a new installation; ordinary Aider launches reuse
the saved connection and refresh it as needed.

Normal CLI launches enable agent navigation, LSP repository-map ranking and idle
L1/L2 paging by default. LSP uses a 10-second budget; paging has no default file or
token caps. Language servers must be available on PATH or beside the Python
interpreter; unavailable servers retain syntax-based lookup/ranking. To disable
the three features, add:

```powershell
--no-enable-agent-nav --no-enable-lsp-graph --no-enable-l2-paging
```

Programmatic constructors and pinned benchmark configurations keep their existing
opt-in behavior; changing CLI defaults does not change recorded experiments.

Optional reasoning effort:

```powershell
.venv/Scripts/python.exe -m aider --model chatgpt/gpt-6.1-sol --reasoning-effort high
```

Supported efforts are `low`, `medium` (default), `high`, `xhigh`, and `max`.
Use `diff` (default) or `whole` editing formats. Function-based edit formats are not supported
by this first transport implementation.

## Account and usage controls

```powershell
.venv/Scripts/python.exe -m aider.chatgpt_auth status
.venv/Scripts/python.exe -m aider.chatgpt_auth login --new-account
.venv/Scripts/python.exe -m aider.chatgpt_auth use ISSUED_CLIENT_ID
.venv/Scripts/python.exe -m aider.chatgpt_auth logout
```

`status` lists saved registrations without tokens. Registrations remain distinct even if their
email addresses match. Returning sign-in reuses the issued client ID and this installation's
stable host ID. Identity and plan permission are checked separately; a successful identity
sign-in does not imply that plan usage was granted.

Manage app access, plan limits and any permitted credits at <https://chatgpt.com/settings/usage>.
Quota, consent and policy failures stop the request. There is no automatic API-key fallback.
Sign out attempts to revoke the renewable session and clears local tokens. If remote revocation
cannot be confirmed, the CLI says so and provides the settings link.

Credentials are outside the checkout in `~/.aider/chatgpt/accounts.bin`. On Windows they are
encrypted using current-user DPAPI. On Unix the store is written atomically with file mode 0600
inside a mode-0700 directory. Refreshes are serialized with a cross-process file lock because
refresh tokens can rotate. Authorization callbacks use loopback, PKCE, fresh state and nonce,
and cryptographic verification of the ID token's signature, issuer, audience, expiry and identity.
No tokens are written to benchmark results or application logs.

## Subscription accounting

- Provider token usage comes from the terminal `response.completed` event, including cached
  input and reasoning output. Cached input is included in total input, not added a second time.
- API dollar cost is unavailable for subscription usage and is represented as `null` in
  transport results and usage events. Aider's internal legacy numeric cost counters remain
  unused for this billing route; collectors must use `billing_mode` and nullable USD values.
- `/tokens` and per-message reports identify ChatGPT plan usage without inventing API charges.
- Context-size estimates use the local `o200k_base` tokenizer and are approximate. They are not
  the provider's measured token usage.
- The subscription route requires HTTP streaming even when Aider's UI uses `--no-stream`.
  It does not accept `temperature` or `max_output_tokens`. Background cache-warming requests
  are disabled. Reasoning effort is the supported model override in this adapter.
- A failed, truncated or inconsistent stream cannot apply an edit, even if it already emitted
  a syntactically complete SEARCH/REPLACE block. Aider only proceeds after verified completion.

The benchmark planner accepts the subscription model name and records `temperature: null`,
`billing_mode: chatgpt_subscription`, and `max_cost_usd: null`. An explicit API dollar cap is
rejected for this route. Turn limits can be enforced by the future runner; the current planner
only records them. Use ChatGPT usage settings for subscription/credit limits. There is no
supported per-response output-token cap on this route.

## Prepare a paired benchmark

Both arms must have the same transport shim. Unmodified upstream Aider cannot authenticate
this route, so the planner requires an explicit control revision containing this integration.
Use this branch as the shared-transport baseline before adding graph/paging changes:

```powershell
.venv/Scripts/python.exe -m benchmark.eval_harness_ab --instances benchmark/subset_smoke.json --model chatgpt/gpt-6.1-sol --control-revision chatgpt-subscription --treatment-revision chatgpt-subscription --output benchmark/results/chatgpt-smoke/manifest.json
```

This command prepares a plan only. It does not execute a model-backed benchmark. Treatment
features are still marked unimplemented. Once the instrumented task runner exists and features
are added, keep the control at this integration's commit and point treatment at the new commit.
Describe the control as upstream behavior with a shared authentication/transport shim.

## Verification

```powershell
.venv/Scripts/python.exe -m pytest tests/basic/test_chatgpt.py tests/basic/test_benchmark_ab.py -q
```

Tests cover token protection/rotation, returning and new registrations, callback state, wrong
account rejection, ID-token signature/claims, catalog restrictions, request translation, SSE
completion/usage, quota/admission failures, and real Aider edits with mocked inference. Tests
use isolated credential stores and do not make paid/subscription inference requests.
The subscription test module is skipped when its optional cryptographic dependencies are absent.

Live verification on the development machine established:

- Browser sign-in completed with plan permission, and the account catalog included GPT-6.1 Sol.
- A small inference returned the requested confirmation (17 input, 8 output tokens).
- Aider edited a disposable `sample.py` from `value = 1` to `value = 2` (1,944 input,
  29 output tokens), using the subscription route with USD cost `null`.
- The normal Aider CLI started successfully with `--model chatgpt/gpt-6.1-sol`.

These are connection/editing checks, not SWE-bench performance results.

Final local validation: 82 focused subscription/benchmark tests passed. The existing Coder,
model, exception and command suites passed 143 tests plus 41 subtests. The full basic suite
completed with 550 passed, 4 failed, 5 skipped, and 67 subtests passed. The failures were the same
pre-existing Windows cases recorded in [the baseline plan](CONTEXT_PAGING_PLAN.md#initial-local-verification).
The optional-extra installation, normal CLI startup, Flake8, and Git whitespace checks passed.

## Official protocol references

- <https://developers.openai.com/siwc/token-sharing-open-source/sign-in>
- <https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions>
- <https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference>
- <https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations>
- <https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery>
- <https://developers.openai.com/api/docs/models/gpt-6.1-sol>
