# axis-github

Additive GitHub connector for Axis. Registers one tool, **`github_api`**, on top of the built-in
toolset — it never overrides or removes anything.

## How it works

The tool is a thin **authenticated proxy** to the GitHub REST API. On each call it asks the Axis
platform (the connectors service — endpoint `POST /internal/connectors/github/token`) for a
**short-lived, repo-scoped GitHub App installation token**, then performs the REST call the model
asked for. The credential never enters the sandbox and the model never sees a token: **Axis owns the
auth** (GitHub App private key, secret store, token minting all live in the platform).

## Additive & conditional

`check_github_requirements` gates the tool: it surfaces to the model **only** when the connector is
provisioned for the run (`AXIS_CONNECTORS_URL` + `AXIS_CONNECTORS_INTERNAL_KEY` present, injected by
the Timonier from the mission's `connectors` list). An un-provisioned run pays nothing and no other
tool is affected.

## Runtime env (set by the Timonier)

| Var | Role |
| --- | --- |
| `AXIS_CONNECTORS_URL` | Base URL of the Axis connectors service (C4). |
| `AXIS_CONNECTORS_INTERNAL_KEY` | Internal key (`X-Internal-Key`) guarding the mint endpoint. |
| `AXIS_TENANT_ID` / `AXIS_USER_ID` | Identity the token is minted for. |
| `AXIS_GITHUB_API_BASE` | Optional — defaults to `https://api.github.com` (GitHub Enterprise otherwise). |

## Tool: `github_api`

`method` (default GET), `path` (`/repos/OWNER/REPO/issues`…), `repository` (`OWNER/REPO`, scopes the
token — recommended), `body` (JSON for POST/PATCH/PUT). Returns the API status + (truncated) body.
