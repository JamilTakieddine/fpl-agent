# Phase 3 spike: what does the FPL website send when you make a transfer?

**Goal:** learn the exact request behind "Confirm transfers", so the agent can build the same payload
later (Phase 5 makes transfers; until then they're recommendation-only, D28/D34).

**Why capture it instead of testing it from code:** a test transfer from code would be a real transfer
(possibly a -4 hit). Recording the request the website itself sends, when you make a transfer you want
anyway, costs nothing. It's done in **your own browser**, not the agent's Playwright login. A second
login could rotate or revoke the agent's refresh token (Q1, D12), while your everyday browser session is
already separate from it.

## Steps (Chrome; about 2 minutes, next time you make a real transfer)

1. Open <https://fantasy.premierleague.com/transfers> as usual.
2. Open DevTools: **Cmd+Option+I** → **Network** tab. Tick **Preserve log**. Type `transfers` in the filter
   box.
3. Make your transfer(s) on the page and click **Make transfers** / **Confirm** as you normally would.
4. Click each new request in the list whose name contains `transfers`. For each one, copy:
   - from **Headers**: the *Request URL*, the *Request Method* and the *Status Code*;
   - from **Payload**: click *view source* and copy the JSON;
   - from **Response** (or **Preview**): copy the JSON.
5. Paste them into the chat, or save them as `data/cache/spikes/transfers_capture.json`. That folder is
   gitignored. The capture contains your entry ID, so it must not be committed.

Also note whether the site sent **one** request (confirm) or **two** (e.g. a check first, then the confirm).

## What we expect to see (unverified, from community libraries)

```json
POST /api/transfers/
{"chip": null, "entry": <entry id>, "event": <gameweek>,
 "transfers": [{"element_in": 123, "element_out": 456, "purchase_price": 57, "selling_price": 76}]}
```

`chip` would be `"wildcard"` or `"freehit"` when one is played with the transfers. Bench Boost and Triple
Captain go in the `/my-team/` lineup payload instead (see `fpl_agent/submit.py`). The capture confirms or
corrects all of this, and the result is recorded in `docs/decisions.md` (D34).
