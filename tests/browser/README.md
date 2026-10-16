# Manual import browser acceptance

Run from this directory with Node 24:

```sh
npm ci
npx playwright install --with-deps chromium --only-shell
npm test
```

The required normal CI job runs every case in desktop Chromium and Chromium's
Pixel 7 mobile emulation. It loads the shipped panel and its ES modules in a real
browser, exercises native forms, focus, keyboard input, File/FormData uploads,
loading feedback, retry identity, duplicates, error recovery, review navigation,
and viewport bounds. Failure artifacts include Playwright traces.

The local harness deliberately supplies deterministic Home Assistant service
responses and a local upload endpoint, with no email configuration. It does not
contact an AI provider or establish Home Assistant integration compatibility.
`tests/test_manual_import_acceptance.py` complements these UI tests with actual
Home Assistant HTTP/WebSocket service registration and durable storage behavior.
Live provider evaluation and real-device installation remain separate beta checks.
