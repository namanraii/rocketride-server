# tool_tenki

A RocketRide tool node that gives an agent a disposable Linux virtual machine to run commands and
code in, edit files, and work in a git repository. Pick it when an agent needs a real operating
system, to install packages, build, or run a test suite, rather than an interpreter sandboxed
inside the engine's own process.

## About Tenki

Tenki Cloud runs Sandbox, a service that starts isolated Linux microVMs on demand and drives them
over an API. Each sandbox is a full virtual machine with its own kernel rather than a shared
container, so it behaves like an ordinary Linux host. A machine can be paused rather than
destroyed, and resumes later with its files and memory intact.

## What it does

Gives the pipeline one sandbox session, created on the first tool call and shared by every call
after it, so files written and packages installed by one call are visible to the next. The agent
gets 12 functions in three groups: running commands and code, editing files under `/home/tenki`, and
working in a git repository. Pick it over `tool_python` when the work needs a real operating system
instead of a restricted interpreter, and over `tool_daytona` when you want a machine that keeps its
files and installed packages for the whole session. The node holds no credentials and places none
inside the VM, so `git_clone` reaches public repositories only.

## Example pipelines

**Write a script and run it in a sandbox**

`chat → agent_rocketride (+ tool_tenki) → response_answers`

<div align="center">

![The tool_tenki node wired to the RocketRide Wave agent on the canvas, alongside an Anthropic LLM and internal memory](example.png)

[![Download example.pipe](https://img.shields.io/badge/example.pipe-Download-41b6e6?style=for-the-badge)](example.pipe)

</div>

A question arrives over chat. The agent writes a script into the sandbox, runs it, reads the real
output back, and answers from what actually happened rather than from what the code looks like it
would print. The sandbox is created on the agent's first tool call, so a conversation that never
needs one never starts a VM.

## As a tool

An agent addresses these as `<node id>.<function>`, for example `tool_tenki_1.run_command`. Which
functions are published depends on the **Tool groups** field; by default all three groups are.

| Function | Description |
|---|---|
| `run_command` | Run a shell command through a login shell, returning its exit code and output. |
| `run_code` | Write a snippet to a temporary file and run it with `python` or `javascript`. |
| `write_file` | Write a UTF-8 text file, creating parent directories as needed. |
| `edit_file` | Replace one exact, unique occurrence of a piece of text in a UTF-8 text file. |
| `read_file` | Read a UTF-8 text file back. |
| `list_files` | List a directory, optionally including hidden entries. |
| `make_directory` | Create a directory and its parents. |
| `delete_path` | Delete a file or directory recursively. |
| `git_clone` | Clone a public git repository into the sandbox. |
| `git_checkout` | Check out a branch, tag or commit, optionally creating the branch. |
| `git_diff` | Show a diff, optionally between two revisions or limited to one path. |
| `git_log` | Show commit history, bounded by a maximum count. |

Every path argument is resolved under `/home/tenki` and confined to it, so the file functions cannot
read or write elsewhere in the VM. `read_file` fetches a file in one request, so it refuses one over
4,000,000 bytes rather than pulling it into the engine; read part of a larger file with
`run_command` instead (`head`, `tail` or `sed -n`). `edit_file` reads with the same limit and refuses
an edit that would take the file over it; it replaces `old_text` only when that text occurs exactly
once (overlapping matches count), matching whitespace and line endings exactly, and writes nothing
otherwise. Its read and write are two calls, so a change made to the file in between, by a command
still running say, is overwritten. Execution functions return `exit_code`, `stdout`, `stderr`,
`timed_out` and `truncated`; a command stopped at its timeout comes back as an ordinary result with
`timed_out` set, not as an error. Output longer than the configured cap is truncated before it
reaches the agent, with `truncated` set so the agent knows it did not see everything.

A command that streams back more than 8 MiB of output is killed before it finishes. The engine
holds a command's whole output in memory until it exits, so a runaway one (`yes`, say) would
otherwise grow it for as long as the execution timeout allows. The cap and this limit are separate:
a chatty command that finishes, such as a large `apt-get install`, is only truncated. A killed one
comes back with `truncated` set and a note on `stderr` saying why.

A result also carries a `session` field whenever the session changed during its call: `"created"`
(a new, empty sandbox), `"resumed"` (it had been paused; its files survived), `"replaced"` (it had
ended, so its files are gone and this call ran on a new, empty one) or `"ended"` (it had ended and
this call stopped there). An agent that ignores that field can wrongly assume its earlier files are
still there. See [Session lifecycle](#session-lifecycle).

## Configuration

Only the API key is required; every other field has a working default, and most pipelines never need
to change them. The ones worth understanding are the two that bound cost, the one that bounds a
single command, and the one that decides how much of the tool surface an agent sees.

### Tool groups

Which groups of functions the node publishes. Leave it empty to publish all three (`execution`,
`filesystem`, `git`). Naming groups publishes only those, which is how you hand an agent a narrower
surface: `["filesystem", "git"]` lets it read and edit a repository without letting it run commands.
A function that is not published is invisible to the agent and refused if it is called anyway. A
value naming only unknown groups stops the pipeline at startup rather than quietly falling back to
the default, so a typo surfaces immediately instead of widening access.

### Max Duration (minutes)

The one setting that bounds compute cost. Tenki enforces it, so it holds regardless of what the
pipeline or the engine does: when it elapses, Tenki pauses the session. It does not delete it
(Tenki's changelog of 2026-09-11: sessions are paused at their maximum duration "so their state is
preserved"). A paused session runs nothing and keeps its memory and its files under `/home/tenki`.
The next tool call resumes the session, and the resumed session gets a new period of the same
length.

Tenki has **no idle timeout**. An unused session keeps billing compute until it is closed or this
period runs out; nothing pauses it sooner. (Tenki's changelog of 2026-09-18 removed the idle-timeout
option as having "never had any effect", and the SDK ignores it.)

Size it as the backstop for a session the pipeline never shut down. The node closes the session on
teardown, which every engine with #2396 (merged to `develop` on 2026-09-22) runs for every source,
but no engine runs teardown after a crash (see [Teardown](#teardown)). The default is 60 minutes.
It bounds one stretch of running rather than total spend: a pipeline that keeps working is paused
when the period runs out and resumed by its next call, and the agent is told when that happens.
A pause is not free for the agent, which is why the default is not shorter: Tenki starts it 15
seconds to over two minutes after the period ends (measured), a command running at that moment is
cut off part-way and has to be run again by the agent, and the first call afterwards waits while
the session comes back, which took 41 to 67 seconds when measured on 2026-10-05 and 13 to 17
seconds on 2026-10-09.

**A command allowed to run as long as the session lives can never finish.** If the
[Execution Timeout](#execution-timeout-seconds) is not shorter than this period, the pause will
always cut such a command off, and the node logs a warning when the pipeline starts. Even a shorter
command is cut off if it is still running when the period ends.

### Pause Retention (hours)

How long Tenki keeps a paused session's memory and files before discarding them. The default is 24
hours. Without this setting Tenki keeps them for seven days (measured: a pause expiry seven days out),
and a paused session's state counts against the workspace's storage until the session is closed or
discarded. A session the pipeline left behind, after an engine crash say, should not hold its code
that long. A resume within the retention continues where the session left off. Tenki refuses 0, and
accepted every value tried from one minute to a year; the field takes 1 to 8,760 hours.

### Execution Timeout (seconds)

The longest a single `run_command` or `run_code` may take. Tenki stops the command at that point and
reports it as a normal result with `timed_out` set. It also acts as the floor for the deadline on
calls to the Tenki API, which is never below 60 seconds, so raising it for a genuinely long build
also gives operations like `git_clone` more room.

Every file call carries that deadline too. The Tenki SDK sends a session's file calls with none
(1.0.6 through 1.4.1), so the node adds it, and a stalled connection ends in an error instead of a tool
call that never returns. The SDK also checks a session's data connection before a file call, with a
request of its own that has no deadline, whenever Tenki has not yet reported the connection verified
(it reports "not ready" first after a create; measured). A stall in that check would block the
session, the close at shutdown included. A command does not go through it, and a command's first
answer marks the connection verified. So after every create and every resume the node runs one
bounded no-op command (`true`) before anything else, so a call made after it never meets the check.
A file call already under way when a parallel call resumes the session still can; that window is
narrow, and nothing has been seen to stall in it. Measured on real VMs with the connection forced
to "not verified": a file call ran the check once; after the no-op command, zero times.

### Secret policies

**Status: built, not verified.** The node sends the policy names and Tenki receives them: that much
was run. Nothing past it was, because the API key available when this was built had no Secrets
access, so no secret or policy could be created. Whether a request then gets its secret, that the
value never shows up inside the VM, and what a refusal looks like to `git` or `curl` are all taken
from Tenki's documentation below, not from a test.

Attaches Tenki request policies to the sandbox, by name, for transparent secret injection. As Tenki
documents it: code in the sandbox refers to a secret by a placeholder such as
`secrets://GITHUB_TOKEN`, in a header, query parameter or JSON field of an HTTPS request; Tenki's
proxy, on the host and outside the VM, substitutes the real value only into requests an attached
policy allows; and any other request carrying a placeholder is answered with
`403 secret_injection_denied`. The node passes policy names and nothing else. It never creates,
reads or stores a secret, RocketRide's own configuration never holds one, and the node puts no value
into the VM. Tenki's own caveat applies: a service a policy allows can send the credential back in
its response, and that response does reach the VM. Tenki's other ways of delivering a secret,
secret files and environment overrides, put the real value inside the VM, where an agent with
passwordless `sudo` can read it, so the node never uses them.

A pipeline that sets secret policies must also set [allowed outbound domains](#allowed-outbound-domains),
or it does not start. A policy decides which requests get the credential. It does not decide where
the sandbox may send what those requests return. The allowlist narrows that; it does not close it,
because an allowed host is itself somewhere data can be sent (with `github.com` allowed, to any
repository or gist the credential, or any other one the agent is given, can write to).

Create the secrets and policies in Tenki first (see [Private repositories](#private-repositories)).
The API key needs Secrets access; without it Tenki refuses the session, and the tool result says so
(measured). See [Authentication](#authentication) for how far that access reaches. Tenki
documents that the image has to carry its managed HTTPS trust. Nothing advertises the placeholders
to the agent, so name the ones it may use in its instructions. If a command's output contains
Tenki's documented denial text, the tool result adds a note that repeating the request will not
help; whether that text ever reaches a command's output has not been measured.

### Allowed outbound domains

Restricts the sandbox's outbound traffic to the listed hostnames. Empty, the default, sends no
allowlist, and outbound traffic is then open. Measured on 2026-10-09, a session created without one
reached every host tried (`github.com`, `pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`,
`example.com`), bare IP addresses over HTTP and HTTPS, and DNS and SSH servers directly. Tenki's
changelog of 2026-09-25 says outbound allowlists were "enabled by default in all existing and new
workspaces": that is the option, not a default list. (Tenki's sessions page still says the option
has to be enabled for a workspace first; on the workspace measured it needed nothing.)

With a list, only the listed names resolve inside the sandbox, and the list replaces open access
rather than adding to a default: with `github.com` listed, the other four hosts above stopped
resolving. Three ways around it were tried from inside the sandbox, whose user has `sudo`, and none
worked:

- a bare IP address that is not listed: plain HTTP was answered by Tenki with "egress blocked by
  policy", and HTTPS failed its handshake;
- other ports: DNS queries sent straight to `1.1.1.1` over UDP and TCP were answered "refused" by
  Tenki, and SSH to an unlisted server that answers when there is no allowlist timed out;
- a listed name pointed at another address in `/etc/hosts`: HTTPS failed its handshake, and HTTP got
  "egress blocked by policy".

The same list held after Tenki paused the session and the node resumed it, and on a session that
replaced one terminated from outside. Not tried: IPv6, and UDP to ports other than 53. What the list
does not stop is traffic to the listed hosts themselves, on any port: with `github.com` listed, its
SSH port answered. An allowed host is a place data can be sent (a push to any repository the agent
has credentials for), so list as few as the work needs: pip, for one, needs `pypi.org` and
`files.pythonhosted.org`, and a secret policy's destinations must be listed too.

Entries follow Tenki's rule, which the node checks at startup so that a mistake stops the pipeline
instead of failing every create later: a hostname without a scheme, path or port, of letters,
digits and inner hyphens, with at most one leading `*.`, and at most 256 of them. Tenki documents
that the wildcard matches exactly one label (`*.example.com` matches `api.example.com`, not
`example.com` or `a.b.example.com`). Measured: Tenki accepts `localhost`, `10.0.0.1`, punycode and
`*.co.uk`, and refuses underscores, non-ASCII names, a wildcard anywhere but first, ports, schemes
and paths. How wide a wildcard is, `*.co.uk` say, is the operator's call. An entry Tenki would refuse
stops the pipeline instead of being dropped (had every entry been dropped, no allowlist would apply
at all), and so does a setting that is not a list. The list is fixed when the session is created,
and a session that replaces an ended one is created with the same list.

## Authentication

The node needs a Tenki **workspace API key**, which begins with `tk_`. Create one in the Tenki
console under API Keys, then set it as the node's API Key field or reference it from the environment
as `${ROCKETRIDE_TENKI_APIKEY}` rather than writing the literal into a pipeline file.

The key authenticates the engine to Tenki and stays on the engine host. It is never placed inside the
sandbox, so nothing the agent runs can read it. Attaching [secret policies](#secret-policies) also
needs the key to have Secrets access, chosen when the key is created (**Allow Secrets access**). A
key without it cannot be given it later; create a replacement.

Secrets access is one option, and it is broad. Tenki's SDK gives a key that has it the means to
create, change, revoke and delete every secret and request policy in the workspace, so such a key
could, for example, rewrite a policy to send a credential to another host. Neither the SDK nor
Tenki's documentation offers a way to read a secret's value back: listing and inspecting return
metadata only. (This is read from the SDK and the documentation. The key available for testing had
no Secrets access.) The node itself uses none of those operations; it only names policies when it
creates a session. To keep such a key's reach as small as Tenki allows:

- Give the node a key of its own, used by nothing else. Leave Secrets access off unless the
  pipeline sets secret policies.
- Create and change secrets and policies with a different credential, such as your own CLI login,
  not with the node's key.
- Set an expiry date on the key, and revoke it when the pipeline is retired.
- Give each stored credential only the permissions its pipeline needs (a GitHub token with read
  access to one repository, say). A policy decides which requests get the credential, not what the
  credential is allowed to do.

## Notes

### Tenancy

The session belongs to the pipeline, not to a user. A team-deployed pipeline is a single instance
serving every caller, so everyone using it shares one VM, its files and its installed packages.
Anything one conversation leaves behind is visible to the next, including a modified shell startup
file, which a login shell runs before every later command.

Run **one pipeline per user or per trust boundary**. The node cannot enforce that itself: a tool call
carries no caller identity, so it has no way to tell two callers apart.

### No git credentials

`git_clone` reaches public repositories only, by design. Tenki can place a GitHub token inside a
sandbox, but there it is an ordinary environment variable or file and the sandbox user has
passwordless `sudo`, so any command the agent runs could read it. Rather than ship a secret that
cannot be protected inside the machine it is handed to, the node holds no git credentials at all
and never asks Tenki to deliver one into the VM.

`git_clone` enforces that rather than trusting the agent to respect it: it accepts public `http(s)`
URLs only, and refuses one carrying credentials in the URL (`https://token@host/org/repo.git`) along
with the `ssh`, `git`, `file` and `git@host:path` forms, none of which it could authenticate anyway.
Git records the URL it cloned from in `.git/config`, so a credential passed that way would be left
inside the VM for every later command, and every later caller, to read.

### Private repositories

With [secret policies](#secret-policies), Tenki documents that a token can be used from the sandbox
without being placed in it. What the operator sets up, once, in Tenki:

1. A GitHub token that can do as little as possible: a fine-grained personal access token limited
   to the one repository, with read-only access to its contents. Nothing in this setup needs write
   access, so do not grant it.
2. A Tenki secret holding that token, `GITHUB_TOKEN` in this example.
3. A Tenki request policy, `github-clone` here, that allows the secret only on the requests a
   clone or fetch makes:

   ```json
   [
     {
       "origin": "https://github.com",
       "methods": ["GET"],
       "pathPrefix": "/acme/private-repo.git/info/refs",
       "header": "Authorization",
       "secrets": ["GITHUB_TOKEN"]
     },
     {
       "origin": "https://github.com",
       "methods": ["POST"],
       "pathPrefix": "/acme/private-repo.git/git-upload-pack",
       "header": "Authorization",
       "secrets": ["GITHUB_TOKEN"]
     }
   ]
   ```

4. On the node: `github-clone` under Secret policies and `github.com` under Allowed outbound
   domains.

The upload path (`git-receive-pack`) is deliberately absent, so by Tenki's documented rules the
policy should not put the token into the request that carries a push. The first rule is a path
prefix, so it also matches the request that opens a push (`info/refs?service=git-receive-pack`):
the token would be sent on that one, which lists refs and writes nothing. Whether a push is in
fact refused has not been tested.

**Not verified.** None of this section has been run against a real private repository: the test
needs a Tenki API key with Secrets access. In particular, how git has to present the placeholder
so that Tenki's proxy recognises it is unknown (git sends HTTP Basic credentials base64-encoded,
which may hide a placeholder from the proxy). Until it has been run, treat a private clone through
this node as unproven, and note that `git_clone` itself still attaches nothing and reaches public
repositories only.

**What a policy cannot protect against.** Tenki checks where a request goes, not why it was made.
The credential is available to the sandbox, so it is available to whoever can steer the agent:
every user of the pipeline, and any text the agent reads and obeys, such as a README in a cloned
repository or a web page. Any of them can have the agent use the token for anything the policy
allows, and read what comes back. So keep the policy to one repository and to reading, keep the
token itself read-only, run one pipeline per trust boundary, and treat everything the token can
read as readable by everyone who can reach the pipeline.

### Session lifecycle

A session persists, with its files and installed packages, until it is terminated, and it can be
paused and resumed. The node handles both:

- **Paused** - by Tenki when the session reaches its max duration or the workspace runs out of
  credit, or by hand in Tenki's console or CLI. Memory and files under `/home/tenki` survive, but
  `/tmp` is cleared and open network connections drop. The next tool call resumes the session. For
  about a minute after a pause Tenki answers calls with errors that do not name it (seen so far: an
  HTTP 502, a failed connection to the VM, a "no such file" about the VM's own socket, a command
  that never starts) and can refuse the resume itself. The node looks at the session's state after
  each of those. A refused resume is asked again while the session still reads paused or resuming,
  whatever the refusal says, until a two-minute budget shared by the calls waiting on the same resume
  is spent; within one call no ask starts after that, and a call that arrives soon after asks once
  (measured: refusals that ended within 17.7, 32.8 and 48.2 seconds, and one that lasted at least
  32.6 seconds, end not seen). A refusal that does not go away while the session stays paused, a
  suspended workspace say, therefore reaches the agent only at the end of those two minutes. If the
  session reads running after a refusal, the resume took and is treated as one; if it reads ended,
  it is handled as an ended session (below). That first call took 41 to 67 seconds when measured on
  2026-10-05, and 13 to 17 seconds on 2026-10-09. `agent_rocketride` (on `develop`) gives one tool
  call 300 seconds by default, so a first call that meets a long refusal and a slow resume can be
  reported to the agent as timed out while it finishes; the `session` field of that call's result,
  "resumed" included, is then lost with it. An error of any other kind is reported as it is.

  What happens to the call that met the pause depends on whether repeating it is safe. A read
  (`read_file`, `list_files`, `git_diff`, `git_log`) is sent again after the resume and answers as
  usual. A change (`write_file`, `edit_file`, `make_directory`, `delete_path`, `git_clone`,
  `git_checkout`) that had already been sent is not sent again: it may or may not have taken effect,
  so the call returns an error saying so, and the agent checks before retrying. A change that failed
  before it went out (in the no-op command, say) is sent after the resume. A command that was already
  running is cut off by the pause: when measured, its process was gone within two seconds of the
  resume and did no more work. Part of its work may be done, so once the sandbox has confirmed a
  command's start the node does not start it again, and its call says so. A command whose start was
  never confirmed is treated as not run and is sent after the resume; in the runs that counted it,
  it ran exactly once. Below the node, the SDK itself sends a command's start again when the first
  got no answer at all.
- **Ended** - terminated, from Tenki's console, CLI or API. Its files and installed packages are
  gone. A command, file write, new directory or clone that had not started runs on a new, empty
  session and carries `session: "replaced"`; one of those changes that had been sent is also sent
  there, since nothing of the first attempt is left and a file or git change has no effect outside
  the sandbox. A read, listing, edit, delete or git call on an existing repository returns an error
  saying the session ended, carries `session: "ended"`, and creates nothing. So does a command that
  was running when the session ended: it is not run again, because whatever it did outside the
  sandbox is already done. The next call, whichever tool it is, starts a new session and carries
  `session: "created"`.

Every result says how the session changed during its call, when it did, in its `session` field:
`"created"` (this call started a new, empty sandbox), `"resumed"`, `"replaced"` or `"ended"`. A
result with no `session` field ran on the same session as the call before it. Two cases are reported
late: when Tenki accepts a resume but the session does not run within the node's three-minute wait,
or is still resuming when the two-minute budget ends. That call returns its error, and the first later
call that finds the session running carries `"resumed"`. Recovery follows the
session's real state rather than the error that surfaced: a paused session is resumed, an ended one
is replaced or reported, and anything else is reported to the agent unchanged. A replacement is also
logged as a warning, since it costs a new VM.

### Teardown

`endGlobal` closes the session and sweeps any session still carrying the run's unique tag - when the
engine runs node teardown. Engines before the fix in #2396 (merged to `develop` on 2026-09-22) did
not for a pipeline sourced from `chat` or `webhook`: on terminate and on idle-TTL expiry the task was
force-killed first, and measured on engine 3.3.0 with a probe node, both paths recorded `beginGlobal`
and never `endGlobal` (#2300). With that fix, both paths run teardown and the session is closed. No
engine runs teardown after a crash. In every case the VM that is not closed keeps running, and
billing compute, until max duration pauses it, since nothing pauses an idle session sooner. That is
why that field, rather than teardown, is the backstop to size.

Sessions are named `rocketride-tool-tenki-<suffix>` and tagged `rocketride`, so anything orphaned,
running or paused, is recognisable in the Tenki console and can be closed there.

### Running the tests

Run them with the engine's own Python (`./dist/server/engine`, after `./builder build`): on `develop`
the shared `nodes/test/conftest.py` imports engine modules, so a plain `pytest` does not collect.
Install the SDK version the node pins first, since the contract tests check that version.

```bash
./dist/server/engine -m pip install -r nodes/src/nodes/tool_tenki/requirements.txt
./dist/server/engine -m pytest nodes/test/test_tool_tenki.py nodes/test/test_tool_tenki_sdk.py -v
```

`test_tool_tenki.py` holds the unit tests: the Tenki SDK is stubbed, so they need no API key and no
network. `test_tool_tenki_sdk.py` holds the contract tests against the real SDK; they are skipped when
`tenki` is not installed.

The unit tests never call Tenki, because a real session costs real money. The contract tests check
the SDK names, signatures and defaults this node depends on, so a `tenki` release that renames one of
them fails there rather than in production.

## Upstream docs

- [Tenki Sandbox documentation](https://tenki.cloud/docs)
- [Tenki Cloud](https://tenki.cloud)

<!-- ROCKETRIDE:GENERATED:PARAMS START -->
<!-- Generated by nodes:docs-generate. Do not edit by hand. -->
<!-- ROCKETRIDE:GENERATED:PARAMS END -->
