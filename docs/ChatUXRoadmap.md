# WorldBox Chat UX Roadmap

This document records the gap analysis between WorldBox's chat experience and the quality bar set by mature roleplay frontends (SillyTavern), and the phased plan to close it. Scope note: the goal is matching **interface quality**, not feature set — WorldBox has its own feature set (modules, worlds, stat mutations) and does not import SillyTavern cards or lorebooks.

## Gap Analysis

| # | Dimension | WorldBox today | Quality bar | Severity | Effort |
|---|-----------|----------------|-------------|----------|--------|
| 1 | Message rendering | Hand-rolled regex chain in `MarkdownRenderer.jsx` fed to `dangerouslySetInnerHTML`, unsanitized (model output can inject HTML); no tables, code fences, or blockquotes; invalid nesting around lists/headings | Real markdown library + sanitizer, GFM tables, fenced code with copy button, consistent quote/italic theming | **Critical** | Small–Medium |
| 2 | Streaming feel | Plain text during stream via a separate quote colorizer, visible "snap" to markdown at `message_complete`; one React re-render per token; no stop button | Live markdown while streaming, frame-batched tokens, caret indicator, stop generation | **High** | Small (rendering) / Medium (stop) |
| 3 | Message affordances | Hover kebab with Edit / Delete only; regenerate only on last AI message; no copy, timestamps, model name, token counts, range delete, or keyboard shortcuts | Discoverable action bar (copy/edit/delete/regenerate), message metadata footer, keyboard-driven editing | **High** | Medium |
| 4 | Chat management | One chat per save; no branching, export, rename, or search (per-turn snapshots already make branching cheap) | Branch from any turn, export transcript, rename saves | Medium-High | Medium |
| 5 | Robustness | `alert()` for edit/delete errors; swipe errors silently swallowed; WS reconnects but a mid-turn disconnect loses the stream silently | Toast notifications, reconnect state recovery, disconnect banner | Medium-High | Medium |
| 6 | Swipes / input / polish | Backend is solid (full-workspace variants, memory rollback — at or above the bar); UI is stiff: no keyboard swipe, no animation, no scroll-to-bottom pill, no avatars, one density | Keyboard/animated swipes, scroll pill + new-message indicator, avatars, density options | Medium | Small–Medium |

## Phase 1: Rendering and Streaming Foundation

Status: Complete.

The substrate every message renders through, on screen 100% of the time; also closes the HTML-injection hole. Frontend-only, no backend or save-format risk.

- Replace the regex renderer with `marked` + `dompurify` in a single shared pipeline (`frontend/src/lib/markdown.js`): GFM tables, fenced code with copy button, blockquotes, dialogue-quote highlighting as a marked extension (streaming-tolerant: an unclosed quote colors to the end of the received text).
- Render live markdown in `StreamingBlock` through the same pipeline — no more plain-text→markdown snap; CSS caret rides the last rendered element.
- Batch incoming WS tokens with `requestAnimationFrame` in `useWebSocket.js` so parsing/rendering is capped at display refresh rate.

## Phase 2: Generation Control and Message Affordances

Status: Complete.

- **Stop generation**: restructure `websocket_endpoint` in `backend/api/server.py` so turns run as a cancellable `asyncio.Task` while the receive loop stays live; `{"action":"stop"}` cancels. Cancellation before `save_completed_turn` leaves state untouched (the turn simply didn't happen, same as the error path). Frontend: send button morphs into a stop square while busy.
- **Per-message action bar** replacing/augmenting the kebab: copy raw markdown, edit, delete, regenerate. Hover on desktop, compact always-on row on touch.
- **Message metadata**: stamp `{ts, model, tokens: {in, out}}` onto AI messages in `save_completed_turn` (usage data already flows through `llm.py` / the LLM inspector); render timestamp + model subtly next to "Turn N". Messages without metadata (old saves) render nothing.
- **Edit ergonomics**: Escape cancels, Ctrl+Enter saves; ArrowUp in an empty chat input edits the last user message.
- **Error toasts**: small toast component; replace `alert()` in `App.jsx` and surface the currently swallowed swipe error.

## Phase 3: Chat Management

Status: Complete.

- **Export**: `GET /api/saves/{id}/export?format=md|jsonl|txt` rendered from the save's chat messages.
- **Branch from turn**: `POST /api/saves/{id}/branch {target_turn, new_save_id}` — copy the workspace, apply `restore_turn_snapshot` semantics to the copy, clear swipes. Reuses `Snapshots/` and save packing wholesale. UI: "Branch from here" in the message action bar and on the save screen.
- **Save display name / rename**: display name + last-played + turn count in `list_saves` (read from `Core/metadata.json` without a full load); rename edits the display name only, avoiding id/path churn.

## Phase 4: Polish and Robustness

Status: Complete (avatars deferred).

- Scroll-to-bottom pill + new-message indicator when unpinned (`useStickToBottom` now exposes reactive `pinned` + `scrollToBottom`).
- Swipe crossfade animation; Left/Right keyboard swipes from the empty composer (→ past the newest variant regenerates).
- Compact/comfortable message density toggle (Settings → Appearance, stored per device in localStorage).
- Reconnect recovery: WS `sync` action replays authoritative transcript + state; the client auto-syncs on reconnect and shows a disconnect banner over the input.
- Deferred: avatars/portraits — there is no portrait asset pipeline in the character builder yet; revisit once characters have images.
- Restart recovery (follow-up): the active save id is persisted to `data/saves/active_save.json` and restored at boot, so `sync` now recovers both connection blips and full server restarts. Deleting the active save resets the marker; corrupt/stale markers fall back to `autosave`.
