# Steering, terminal actions and YOLO

From an existing repository, start the editable Better Aider installation with:

```powershell
& "C:\Users\jesse\source\repos\Better Aider\.venv\Scripts\python.exe" -m aider --model chatgpt/gpt-6.1-sol --yolo
```

Omit `--yolo` to keep confirmations. `--no-yolo` overrides configuration enabling it.
The header and status line identify YOLO mode. It approves all confirmation
prompts, including explicit shell-command approvals, and uses defaults for text
questions. Ordinary `--yes-always` retains its restriction on explicit approvals.
DOCX references remain read-only and navigation retains its workspace rules.

## Repository discovery and continuing work

The agent discovers and loads repository files itself. Both tracked and untracked
workspace source are available through navigation, subject to ignored, declined,
outside-workspace and explicit read-only restrictions. If a response still asks
the user to add local files, the harness resolves the named absolute/relative
paths or unique basenames, loads complete source and continues automatically.
`@file_name` remains useful for directing attention, rather than a prerequisite
for the agent to inspect repository files.

Upstream Aider's three-reflection limit bounded follow-up model requests in its
edit-repair loop. Reusing it for navigation/terminal work stopped ordinary agent
tasks after an initial response and three follow-ups. Agent mode now continues
without that default cap. `--max-agent-steps N` optionally limits loop steps per
user turn, and `/status` reports usage and the configured limit. One model response
can request up to three actions; that batch size is independent of the turn limit.
The counter measures agent-loop steps, rather than a token or billing budget.
Steering starts a fresh turn allowance. Classic mode retains its existing repair
limit. Benchmark jobs explicitly retain their original turn allowance.

## Type while the agent works

In an interactive terminal with fancy input enabled, the input line stays
available during model work and assistant terminal actions. Enter submits a
steering message to a FIFO; Alt+Enter inserts a newline. In multiline mode those
keys are reversed, matching the idle prompt. File-reference completion works in
this input line too.

The harness acknowledges queued messages and applies them before the next model
request. If a complete response proposes edits or commands and steering is already
queued, it skips those proposals and asks the model to reconsider with the new
instructions. Navigation/tool results are retained. Changes already applied and
commands already started are not rolled back by a later message. Commands finish,
time out, or are interrupted before the next steering request is sent.

Ctrl+C interrupts the current work using the existing main-thread interrupt path.
Ctrl+D closes busy input while allowing the current request to finish. Unsubmitted
drafts survive handoffs and reappear in the idle input line. Submitted messages
remain queued if the turn exits before consuming them. Slash commands entered
while busy execute locally at the next handoff, rather than becoming shell or
model commands automatically.

Confirmation and question prompts suspend the background reader and take sole
ownership of terminal input. Assistant command subprocesses use closed stdin;
explicit idle `/run` commands retain the existing foreground behavior. One-shot
`--message`, programmatic runs and redirected/dumb input do not start a reader.
`--no-fancy-input` also disables steering input.

While steering is available, completed Markdown blocks stream above the prompt.
This avoids competing cursor updates from the old Rich Live window and spinner.
The output handoff uses prompt-toolkit's
[stdout support](https://github.com/prompt-toolkit/python-prompt-toolkit/blob/main/examples/prompts/patch-stdout.py).

## The agent has a terminal

The agent can now request a real terminal action and receive its result:

````text
```aider-nav
/run_command git status --short
```
````

The action uses the rest of its line as the literal shell command, preserving its
quotes and backslashes through action parsing. It runs in the repository root.
Windows uses PowerShell (7 when available, otherwise Windows PowerShell); other
platforms use the configured shell. Windows PowerShell's native executable
argument-quoting rules still apply. Complex inline programs can use script files.
The tool reports actual output, exit code, working directory and elapsed time.
PowerShell errors stop the action and native executable exit codes are propagated.

Legacy shell fences and `/run` use `COMSPEC` (CMD) on Windows, matching the shell
advertised in platform information. Launching Better Aider from PowerShell does
not change that dialect. Use `/run_command` for PowerShell syntax. CMD `if not
exist`, quoted arguments such as `"Windows Desktop"`, and `&&` chains pass through
without a nested PowerShell parsing step. GUI executables finish before a success
chain continues; a failing executable prevents the launch portion of the chain.

Suggested shell-command sequences stop at the first nonzero exit code, including
later shell blocks in the same response. If the command output is shared with the
agent, the failure returns to the agent loop so it can correct
the problem. YOLO shares the output automatically; normal mode keeps the sharing
confirmation.

Commands require explicit confirmation in normal mode; YOLO approves them.
Default timeout is 120 seconds; `/run_command --timeout=600 ...` overrides it up to
3600 seconds. Timeout/interrupt cleanup terminates the spawned process tree.
Output over 24,000 bytes shows the first and last 12,000 bytes with an explicit
shortening notice. Command output is captured locally rather than loaded as an
editable file. Actions use the agent loop and the three-action response limit.
Terminal runtime and confirmation time do not consume the batch's navigation budget.

For example, ask: "Locate my installed Godot .NET editor and open this repository
in it." The agent can inspect `PATH` and installation folders, then invoke the
executable with editor/project arguments. Opening a requested application does not
depend on a Windows file association for `.godot` files.

Better Aider uses installed executables available to its process. It does not
automatically import another agent's tools, MCP server configuration or credentials.
This change supplies local terminal execution, not a generic MCP client or GUI
automation.

## Verification

`tests/basic/test_steering.py` exercises real prompt-toolkit pipe input, ordered
messages, draft preservation, repeated reader handoffs, exclusive confirmation
input, Ctrl+C/Ctrl+D, headless fallback, response preemption and Markdown output.
`tests/basic/test_terminal_tools.py` executes harmless local commands to check
literal action parsing, output, cwd, native exit status, closed stdin, timeout,
shortening, YOLO approval and CLI flag wiring. No provider inference is required.

The regression selection passed 305 tests and 29 subtests, with two skips and the
known headless color-initialization test deselected. The final focused steering
and terminal selection passed 22 tests. These selections overlap.

The Windows shell regression fixes the previously documented `test_run_cmd_echo`
failure. The legacy runner was wrapping CMD commands in an unquoted PowerShell
`-Command` call based on the parent terminal. The reproduction split the quoted
export preset into `Windows` and `Desktop` and rejected CMD's `if not exist`.
Tests now cover the original two-step export shape, native argument preservation,
GUI process ordering/exit status, failure propagation and return to the agent.
The targeted Windows-shell/coder/command/navigation/steering regression selection
passed 184 tests and 29 subtests, with two optional language-server skips. The
export/launch checks use temporary stand-ins, including `pythonw.exe`, without
building or launching the user's game. The quoted-preset test checks both CMD
and the explicit PowerShell terminal action.

The autonomy regression selection passed 318 tests and 29 subtests with two skips.
After refining summary handling and preserving the last turn's counter across
`/status`, the final focused selection passed 87 tests and 29 subtests. These
selections overlap. Coverage includes automatic absolute/relative filename loads,
untracked source, ambiguity, read-only/ignored/declined scope, cold L2 summary
mentions, continuation past three reflections, explicit step caps, CLI wiring,
clone inheritance and classic-mode compatibility.

A read-only smoke check in the actual BattleGame repository automatically loaded
`Scripts/Battlefield.cs` and `Scripts/Battalion.cs` into full source context
(14,174 characters including context formatting). It required zero confirmation
prompts and zero provider requests; both file hashes and Git status were unchanged.
