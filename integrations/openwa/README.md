# OpenWA gateway setup

This project talks to OpenWA through its REST API. The FastAPI service sends outbound replies and order notifications; OpenWA calls the signed FastAPI webhook for inbound customer messages.

## 1. Start OpenWA

From the repository root, run:

```powershell
docker compose -f integrations/openwa/docker-compose.yml up -d
```

Open the OpenWA dashboard at `http://127.0.0.1:2785`. The gateway persists its database and linked WhatsApp session under `integrations/openwa/data/`. Use a dedicated WhatsApp number for this unofficial gateway; WhatsApp may restrict automated accounts.

## 2. Create and link a WhatsApp session

1. In OpenWA, create a session (for example `form-field-support`), start it, and scan its QR code in WhatsApp under **Settings → Linked devices → Link a device**.
2. Wait until the session status is `ready`.
3. In OpenWA, create an API key limited to this session if the dashboard supports scoped keys; use a key with permission to send messages.
4. Copy the session's UUID and API key into the root `.env`:

```dotenv
WHATSAPP_PROVIDER=openwa
OPENWA_BASE_URL=http://127.0.0.1:2785
OPENWA_API_KEY=your-openwa-api-key
OPENWA_SESSION_ID=your-session-uuid
OPENWA_WEBHOOK_SECRET=the-same-long-random-secret-used-in-the-openwa-webhook
```

Restart FastAPI after updating `.env`.

## 3. Configure inbound support replies

OpenWA webhooks must reach a public HTTPS endpoint. A localhost-only FastAPI server is not reachable from OpenWA's webhook dispatcher, and OpenWA blocks internal/private webhook targets. For local development, run the restricted proxy and expose only its port through ngrok in separate PowerShell windows:

```powershell
Set-Location D:\CustomerSupport
.\.venv\Scripts\python.exe integrations\openwa\webhook_proxy.py
```

```powershell
ngrok http 8765
```

The proxy forwards only `POST /webhooks/openwa` to FastAPI and rejects other methods and paths. Copy the HTTPS hostname printed by ngrok, then in the OpenWA session add a webhook with:

- URL: `https://YOUR-PUBLIC-TUNNEL/webhooks/openwa`
- Events: `message.received`
- Secret: exactly the value of `OPENWA_WEBHOOK_SECRET`
- Retry count: `3`

For production, use your deployed HTTPS API domain instead of a development tunnel. Do not expose the gateway's port 2785 publicly; put it behind a TLS proxy if remote access is necessary. Stop the ngrok and proxy processes with Ctrl+C when local webhook testing is finished.

Generate a secret in Windows PowerShell:

```powershell
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$secret = [Convert]::ToBase64String($bytes)
$rng.Dispose()
$secret
```

Use the exact generated value in both `.env` and OpenWA's webhook secret. Keep both private.

## 4. Test

- Check OpenWA: `http://127.0.0.1:2785/api/health`
- Check FastAPI: `http://127.0.0.1:8000/api/health`
- With a ready session and credentials, the API health response reports `providers.whatsapp: true`.
- Send a message to the linked number from a different WhatsApp account. OpenWA should deliver it to `/webhooks/openwa`; the customer support assistant replies through OpenWA.
- Complete a COD order with the customer's opt-in and phone number; the order WhatsApp notification uses the same OpenWA session.

The webhook validates OpenWA's HMAC-SHA256 signature and ignores the linked account's own messages and group messages. The local service currently processes text; unsupported media receives a request to type the question.
