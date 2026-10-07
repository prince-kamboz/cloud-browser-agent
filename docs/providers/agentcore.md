# Provider: Amazon Bedrock AgentCore Browser (`PROVIDER=agentcore`)

Uses only the AgentCore **Browser tool** (sessions, live view, profiles), not AgentCore Runtime, Memory or Gateway.
The agent stays our own Deep Agents code and state stays in SQLite.

| | |
|---|---|
| Saved logins | a browser *profile*, saved from a running session (`SaveBrowserSessionProfile`) |
| Agent connection | SigV4-signed WebSocket headers, valid about 5 minutes (fetched right before use) |
| Live view | Amazon DCV stream, presigned URL (max 300 s), shown by `ui/viewer/dcv.html` |
| Hide the agent while typing secrets | `UpdateBrowserStream` switches the automation stream off |
| Status | **Built and tested against fakes only.** A new AWS account cannot start sessions yet (see section 2) |

Run: `./run.sh agentcore` (needs `deploy/aws/.env.aws`, see below).

## 1. Create a dedicated IAM user (you do this in the AWS console)

1. IAM, Policies, **Create policy**, JSON tab: paste `deploy/aws/iam-policy.json` and replace `YOUR_ACCOUNT_ID`
   with your 12-digit account id. Name it `AgentCoreBrowserPoc`.
2. IAM, Users, **Create user**: name `agentcore-browser-poc`, **no console access**, attach `AgentCoreBrowserPoc`.
3. Open the user, Security credentials, **Create access key**, use case "Application running outside AWS".
4. Copy the keys straight into `deploy/aws/.env.aws` on your machine (copy `deploy/aws/env.aws.example`). Do not paste them anywhere else.
5. When the POC is done: deactivate and delete the access key, then delete the user.

The policy allows only AgentCore browser session and profile actions, only in us-east-1 and ap-south-1.
It cannot touch IAM, S3, EC2 or anything else (12 `bedrock-agentcore` actions, nothing more).

## 2. The test account was not ready (found on 2026-10-06)

- CloudShell: "Your account verification is in progress. This may take up to two days for new accounts."
- Creating a browser profile: "maxBrowserProfiles limit exceeded for account ... Please contact AWS Support".

Service Quotas (us-east-1) shows why: this account's *applied* AgentCore quotas are 0 where AWS's default is
higher, for example "Active Session Workloads per Account" applied 0 vs default 5,000, and "Endpoints per Agent"
applied 0 vs default 10. "limit exceeded" means the quota, not the current count: with a quota of 0 the very first
profile already fails. (The Browser-specific rows were not readable in the console, so the profile quota itself was not seen.)

Verified with the real IAM user (`AgentCore-DEV`, 2026-10-06): the permissions are correct (listing sessions and
profiles works), but `StartBrowserSession` is refused with
"maxBrowserSessions limit exceeded for account ... Please contact AWS Support", so **sessions are blocked too**, not
just profiles. Our provider maps this error to `QuotaExceeded` correctly against the real API.

Keys will not fix either. Open an AWS Support case (Support Center, Create case, Service limit increase / Account):

> Our account is new and verification is in progress. We are evaluating Amazon Bedrock AgentCore Browser
> in us-east-1 and ap-south-1. Please (1) complete or expedite account verification, and (2) raise the
> AgentCore Browser quotas: browser profiles per account to 100 (rejected today: maxBrowserProfiles limit exceeded),
> concurrent browser sessions to 50 (rejected today: maxBrowserSessions limit exceeded), and confirm CloudShell can be enabled. This is a development evaluation.

## 3. What gets tested once access works (Step 0)

Startup time (API return, READY, first CDP command, with a profile attached), whether saving to the same profile
twice overwrites it ("profiles are immutable" in the console), what survives (cookies, session cookies,
localStorage, sessionStorage, IndexedDB), reconnect after a client disconnect, and 20 concurrent sessions.
