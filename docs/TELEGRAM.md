# Telegram Mini App voice

Hermes Talk can serve a small Telegram Mini App from the gateway process. The
listener is opt-in and binds only to `127.0.0.1:9417`; it serves `/tma` and the
audio websocket at `/tma/ws`. Put the existing loopback ingress or a private
reverse proxy in front of that prefix and preserve the `/tma` path. The browser
never receives a bot token or a provider credential.

The Mini App URL must be public HTTPS, for example
`https://<your-public-host>/tma`. The HTTPS proxy or Tailscale Funnel must map
that prefix to `http://127.0.0.1:9417/tma`, pass websocket upgrades for
`/tma/ws`, and leave the prefix unchanged. Hermes Talk does not open a public
listener or configure the ingress itself.

Enable the surface explicitly in the gateway profile. Either set the environment
flag below, or set `platforms.telegram_tma.enabled: true` in the profile config:

```dotenv
TALK_TELEGRAM_TMA_ENABLED=true
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=<owner-id>
```

The plugin also needs gateway injection permission for the explicit `ask_hermes`
voice tool:

```yaml
plugins:
  entries:
    hermes-talk:
      allow_gateway_injection: true
```

The custom surface is bound to the default Hermes profile. A named multiplex
profile reports a startup configuration error instead of sharing another
profile's session namespace.

`TELEGRAM_ALLOWED_USERS` is a comma-separated numeric owner list. Each websocket
must send Telegram's signed `initData` as its first frame; the server validates
the bot-token HMAC, owner ID, and a five-minute freshness window before opening
realtime audio or admitting a task. Query-string authentication is not accepted.

The server routes admitted voice tasks to the existing Telegram session. Set
`TELEGRAM_TMA_CHAT_ID` when the destination chat differs from the owner DM, and
set `TELEGRAM_TMA_THREAD_ID` for a topic. If the chat override is absent,
`TELEGRAM_HOME_CHANNEL` is used when configured, otherwise the authenticated
owner ID is used. The final response is returned to the same call only when its
canonical session key and one-time request marker match the admitted request;
unrelated turns are discarded.

Voice chat stays in the realtime call. It reaches Hermes through the explicit
`ask_hermes` tool; ordinary voice transcripts are not copied into Telegram.
Approval prompts remain Telegram buttons, so tell the operator to approve there.
Spoken confirmation does not approve a request. One owner call is allowed at a
time, and audio is bounded PCM16 mono at 24 kHz.

On iPhone, use the foreground Mini App. A prior microphone grant may allow a
foreground auto-start attempt; **Start** remains the fallback when iOS requires
a user gesture. **End** closes the websocket, microphone tracks, and audio
context. The surface does not claim proximity, lock-screen, or background
operation.
