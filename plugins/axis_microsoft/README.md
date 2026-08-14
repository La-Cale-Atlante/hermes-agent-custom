# axis-microsoft

Additive Microsoft 365 connector for Axis. Registers one tool, **`microsoft_graph`**, on top of the
built-in toolset — it never overrides or removes anything.

## How it works

The tool is a thin **authenticated proxy** to the Microsoft Graph REST API. On each call it asks the
Axis platform (`POST /internal/connectors/microsoft/token`) for a **short-lived delegated access
token** (minted from the user's consented refresh token, which the platform rotates), then performs
the REST call. The credential never enters the sandbox and the model never sees a token: **Axis owns
the auth** (OAuth client secret + per-user refresh token live in the platform).

## Additive & conditional

`check_microsoft_requirements` gates the tool: it surfaces **only** when the connector is provisioned
for the run (`AXIS_CONNECTORS_URL` + `AXIS_CONNECTORS_INTERNAL_KEY`, injected by the Timonier).

## Runtime env (set by the Timonier)

| Var | Role |
| --- | --- |
| `AXIS_CONNECTORS_URL` | Base URL of the Axis connectors service (C4). |
| `AXIS_CONNECTORS_INTERNAL_KEY` | Internal key (`X-Internal-Key`) guarding the mint endpoint. |
| `AXIS_TENANT_ID` / `AXIS_USER_ID` | Identity the token is minted for. |
| `AXIS_MSGRAPH_API_BASE` | Optional — defaults to `https://graph.microsoft.com/v1.0`. |

## Tool: `microsoft_graph`

`method` (default GET), `path` (`/me/messages?$top=10`, `/me/events`, `/me/chats`…), `body` (JSON for
POST/PATCH/PUT). Returns the API status + (truncated) body.
