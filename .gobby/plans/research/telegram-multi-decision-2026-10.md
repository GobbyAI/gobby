# Telegram multi-decision buttons: Bot API support and why Gobby counts only the first press

Research for #23607 "Research Telegram multi-decision buttons: Bot API support and
why Gobby counts only the first press", under #22949 "Lane 7 - Planning/research".
Written by W4 gobby#15469 on 2026-10-05 against `0.5.0` at 2540aa5519. Adv4
gobby#15471 reviews it.

Josh's report (2026-10-05, Telegram): "When you send me a stack of buttons with the
intent of me selecting multiple either or options, it only counts the first button
press in Telegram." He asked whether Telegram supports a multi-option confirmation.

## Answer

- **The Bot API counts every press.** It defines one callback query per press of
  a callback button and sets no first-answer limit. Several buttons on one
  message can each be pressed and reported to the bot. The docs describe what
  the API supports; they do not guarantee against network or Telegram loss.
- **Gobby drops the later presses.** It treats one keyboard message as one
  decision. The first press answers the decision for the whole message, and
  every later press on that message is refused with the alert "This decision
  was already answered." This is the single-answer contract that #22968
  "Telegram pending decision buttons go silently dead after callback TTL or
  daemon restart" designed on purpose. It is not a Telegram limit.
- **Telegram has no native multi-select inline keyboard.** A bot can build one
  from callback buttons it relabels with `editMessageReplyMarkup`, plus a submit
  button. A native poll with `allows_multiple_answers` lets a user pick several
  options for one question. It does not enforce one choice per either/or group,
  so a bot would have to validate the groups itself.
- **Recommendation.** Keep one decision per message as the contract. Fix the
  defect that remains under that contract: after the first press, the other
  buttons stay drawn but do nothing except show an alert. A follow-up task in
  Lane 6 settles the answered message and states the contract at the send
  boundary. Per Josh, it lands after #23143 "Relay live-session conversations
  through Telegram and shared channel adapters" and reuses that task's
  adapter-neutral relay contract for inbound callbacks (see the last section).

## 1. What the Bot API supports

Source: the Telegram Bot API documentation, Bot API 10.3 (August 24, 2026),
read 2026-10-05. Each mechanism links its own section.

### Every press is its own callback query

- [CallbackQuery](https://core.telegram.org/bots/api#callbackquery): an incoming
  callback query from one callback button in an inline keyboard. After a press,
  the client shows a progress indicator until the bot answers the query.
- [answerCallbackQuery](https://core.telegram.org/bots/api#answercallbackquery):
  answers one query with a notification at the top of the chat or, with
  `show_alert`, an alert.

The API defines a separate query for each press and puts no limit on how many
buttons of one message can be pressed. Whether a press counts is the bot's
decision.

### Toggle keyboard plus submit (built by the bot, not native)

- [editMessageReplyMarkup](https://core.telegram.org/bots/api#editmessagereplymarkup):
  edits only the inline keyboard of a message.
- [InlineKeyboardButton](https://core.telegram.org/bots/api#inlinekeyboardbutton):
  exactly one field other than `text`, `icon_custom_emoji_id` and `style` sets a
  button's type. `callback_data` carries 1-64 bytes back to the bot. `style`
  colors a button "danger", "success" or "primary".
- [DisabledButton](https://core.telegram.org/bots/api#disabledbutton), new in
  10.3: the `disabled` field makes a button that "does nothing". It is a button
  type of its own and cannot also carry `callback_data`.

A reversible toggle must stay a callback button. Each press updates server-side
selection state, and the bot redraws the keyboard with the chosen option marked
by its label or `style`. A final Submit press sends the combined answer. A
disabled button cannot be pressed again to deselect, so it is useful only to
show a choice that has been settled. The API supplies these parts, but no button
type is a native multi-select.

### Polls with several answers

- [sendPoll](https://core.telegram.org/bots/api#sendpoll): 1-12 options.
  `is_anonymous` defaults to true. `allows_multiple_answers` lets a user choose
  several options of the one question.
- [PollAnswer](https://core.telegram.org/bots/api#pollanswer): sent for a
  non-anonymous poll. It carries `poll_id`, the voter, and the chosen
  `option_ids`, which are empty when the vote is retracted.
- [Update](https://core.telegram.org/bots/api#update): `poll_answer` is reported
  only for polls the bot sent itself.
- [getUpdates](https://core.telegram.org/bots/api#getupdates): an empty
  `allowed_updates` receives every update type except `chat_member`,
  `message_reaction` and `message_reaction_count`.

A non-anonymous multi-answer poll gives "pick any of up to 12 options" for one
question. It does not enforce one choice per independent either/or group, so a
bot would have to reject a vote that picks both sides of a group. A bot could
scope poll answers the way Gobby scopes buttons by storing a `poll_id` and
option mapping and checking the voter. Gobby cannot reuse its callback-token
path for that, though. It would need a new mapped poll-answer path.

Gobby does not subscribe to poll answers either. It passes an explicit update
list that omits `poll_answer`, so the Bot API default does not apply:

- `src/gobby/communications/adapters/telegram.py:45` (excerpt_hash
  `43c9bda2b63e34c864d623a5eb064c4cff6bdfc63a592481c36e477ee20c6057`):
  `45| _ALLOWED_UPDATES = ("message", "message_reaction", "message_reaction_count", "callback_query")`

### Checklists

[sendChecklist](https://core.telegram.org/bots/api#sendchecklist) sends a
checklist only on behalf of a connected business account and requires
`business_connection_id`. It does not apply to Gobby's bot chats.

### One message per decision

This needs no extra API mechanism. Each message carries its own keyboard and
gets its own callback queries. It is the interim practice already in force.

## 2. Where Gobby drops the presses after the first

Citations are `gcode evidence` range reads on the working tree at 2540aa5519.

### The token registry is not the cause

`TelegramCallbackRegistry.resolve` consumes only the pressed token. The sibling
tokens stay registered unless they expire, are evicted, or are replaced by a new
keyboard, and while registered they still resolve `ok` when pressed:

- `src/gobby/communications/telegram_callbacks.py:161-169` (excerpt_hash
  `0721cefade1253cb0772c4fd6ae058ab65e3246e1de3983605f9927d317488e4`):
  ```
  161|         self._entries.pop(token, None)
  162|         return TelegramCallbackResolution(
  163|             status="ok",
  ```

### One keyboard message is one decision

`_is_decision` classifies any outbound message from a session with an
`inline_keyboard` as a single decision. Action keyboards (agent menus) are the
only exception:

- `src/gobby/communications/telegram_decisions.py:67-74` (excerpt_hash
  `3af65e40c3005c77578a9c434cd9af17ff5de5a828dac862e041b3a94aaed94b`):
  ```
  67| def _is_decision(source: CommsMessage) -> bool:
  ...
  70|         source.direction == "outbound"
  71|         and bool(source.session_id)
  72|         and bool(source.metadata_json.get("inline_keyboard"))
  73|         and not source.metadata_json.get("callback_action")
  ```

### The drop point: the single-answer compare-and-set

`LocalCommunicationsStore.accept_callback_decision` marks the whole decision
message `answered` only when it has no `callback_state` yet. The first press
wins, and every later press on the same message matches no row:

- `src/gobby/storage/communications.py:563-575` (excerpt_hash
  `62829e36efe117f8079caa740d900a4d5a48bdb641213fbe93c7ffb042bb84eb`):
  ```
  567|                 UPDATE comms_messages
  568|                    SET metadata_json = jsonb_set(metadata_json, '{callback_state}', '"answered"')
  569|                  WHERE id = %s
  570|                    AND NOT (metadata_json ? 'callback_state')
  571|                    AND COALESCE((metadata_json->>'callback_generation')::int, 0) = %s
  ```

### The refusal path

1. `accept_decision_callback` receives `None` and calls `_republish_decision`.
   - `src/gobby/communications/telegram_decisions.py:221-232` (excerpt_hash
     `a8d2f64282137180295eabac15df837b9a4c4187860b6a7d9a0879b7bcf06f40`):
     `227|         if accepted is None:` and
     `230|                 await _republish_decision(manager, adapter, current, message)`.
2. `_republish_decision` sees `callback_state == "answered"`. A Retry answer
   button is reissued only when the stored answer `failed` or is `in_doubt`.
   Otherwise the click is tagged `answered`.
   - `src/gobby/communications/telegram_decisions.py:121-129` (excerpt_hash
     `07f1ef2ea8b198889b1850175fe10fff38b59c50bc0a353c6c5cfdcd9031707b`):
     `124|     if state == "answered" and await _reissue_answer_retry(manager, adapter, current):`,
     `127|     if state is not None:`,
     `128|         message.metadata_json["callback_status"] = str(state)`.
3. `InboundCommunications.handle_messages` skips a press whose acceptance
   returned `None` before the press is stored or dispatched as a received
   message. The asking session never sees it.
   - `src/gobby/communications/inbound.py:236-240` (excerpt_hash
     `ee99464ca95e0eab965c754c189aab7707bf48eb3909b10c5a79bc11b5cc8855`):
     `238|                     if accepted is None:`,
     `239|                         handled.append(message)`,
     `240|                         continue`.
4. The adapter answers the press with an alert:
   - `src/gobby/communications/adapters/telegram.py:924-934` (excerpt_hash
     `10b08a2852ee75333bf8ee079ce0d845982ab3f13ffffb3a10529ad7838b52e2`):
     `926|             elif status == "answered":` and
     `927|                 text = "This decision was already answered."`.

### Why the other buttons stay drawn

The refusal above is durable. Keyboard and token cleanup is a separate and
narrower step:

- On a first-attempt answer that is pending, running or delivered, Gobby does
  not edit the decision message. `answer_status_key` returns `None` for those
  states. The responder's `_show_status` and `_publish_answer_status` both
  return early on `None`.
  - `src/gobby/communications/models.py:174-185` (excerpt_hash
    `d9acd5d964d93c60abe6b90ad35b9b5032590bb4ed08fa9fef44c887b4e2e301`):
    ```
    177|         A first attempt that is pending, running or delivered looks like any answered
    178|         decision; every other state is shown on the decision message.
    ...
    181|         if outcome in {"failed", "in_doubt", "blocked"} or (
    182|             self.answer_attempt > 1 and outcome in {"pending", "delivered"}
    185|         return None
    ```
- The adapter discards a message's previous tokens only when it registers a
  replacement keyboard for that message. `_publish` passes the generation its
  caller supplies, and a `None` keyboard registers no replacement tokens.
  `edit_stored_message` (`adapters/telegram.py:607`) passes a callback source
  only with non-`None` markup.
  - `src/gobby/communications/adapters/telegram.py:122-126` (excerpt_hash
    `4d704d8f6c0cb1cd2c04b32738419310da39eb8add46a0aadd3e597a4c7963de`):
    ```
    122|         self._callback_registry.bind_keyboard(markup, keyboard_message_id)
    123|         previous = self._message_callback_keyboards.pop(message_key, None)
    124|         if previous is not None:
    125|             self._callback_registry.discard_keyboard(previous)
    ```

After a normal first answer, the sibling buttons therefore stay on the message.
Their tokens stay registered until they expire or are evicted. Pressing one
resolves `ok` and is then refused by the compare-and-set. That fits the
AGENTS.md definition of a bug: a drawn control that does nothing.

### Verdict

This is a Gobby contract, not a Telegram limit. Gobby's decision model is single
choice per message, set by #18854 "Add Telegram inline keyboards for
clarification and approvals" and hardened by #22968, and the refusal works as
designed. Nothing at the send boundary tells the sender that a stack of either/or
rows is one decision, and the answered message keeps its dead buttons.

## 3. Recommendation

**Contract: one decision per message.** Each independent either/or choice goes
in its own button message. This is already the interim practice.

- It reuses all existing mechanism. Each decision gets its own answer, its own
  compare-and-set, its own delivery-ledger row and its own Retry path. Nothing
  in #22968's never-rerun guarantees changes.
- Toggle-plus-submit in one message would need new server-side selection state
  per decision and a submit path. It would also need a keyboard edit on every
  toggle. In the current design each keyboard edit is published at a new
  generation, and the replacement keyboard discards the previous markup's
  tokens, so quick successive taps would hit "These buttons were out of date.
  Current buttons are attached; tap again." That is more mechanism and a worse
  experience for Josh's case. Reconsider it only if Josh asks for a single
  message.
- Polls are not recommended. They need a `poll_answer` subscription and a new
  mapped poll-answer path, and the bot would have to validate each either/or
  group itself.

**Follow-up fix** (one implementation task; owning lane: Lane 6 Everything
else, because communications has no lane of its own).

**Sequencing.** Josh's direction, relayed by the Orchestrator gobby#14972 at
18:13 CT on 2026-10-05: the follow-up depends on #23143 "Relay live-session
conversations through Telegram and shared channel adapters" and lands after it.
Josh wants to chat over Telegram the way he does at the terminal.

#23143 defines one adapter-neutral relay contract. Under it, a channel attaches
to one live session, and inbound channel text is durably deduplicated and
delivered to that session as an ordinary user turn with a generic source label
such as `User (Telegram):`. Telegram is its first adapter, and #23143 avoids
Telegram-specific core logic. Where the follow-up touches inbound callbacks, it
reuses that contract and adds no Telegram-only path:

- When the asking session is bound to a channel through #23143's relay, an
  accepted press reaches that session through the relay's inbound delivery,
  with its dedup and source label. It does not reach the session through a
  separate Telegram-only path.
- Telegram-specific work stays in the Telegram adapter: drawing the settled
  message and answering the callback query. The adapter-neutral core only
  records that the decision was answered and with which choice.

1. Settle an answered decision message. Once a press is accepted, edit the
   decision message so it shows the recorded choice and carries no live
   keyboard, and advance the generation so the old tokens are refused. The
   existing `_publish_answer_status` path advances the generation and
   republishes with either a Retry answer keyboard or `None`. Source alone does
   not show that `None` clears the keyboard: `_edit_chunk` omits `reply_markup`
   when the markup is `None`, and it does not send an explicit removal. The
   follow-up must make the removal explicit and prove it with a test.
   - `src/gobby/communications/adapters/telegram.py:553-605` (excerpt_hash
     `0ec54d22772ae8336abf72bc308d0dbf6274c274dabe7b131eeb9b65d9a826fa`,
     found by the Adversary and re-read by the Writer), `_edit_chunk`:
     `if markup is not None:` then `payload["reply_markup"] = markup`.
2. State the contract at the send boundary. The `gobby-communications:send_message`
   tool description (and its `inline_keyboard` parameter) says that one
   keyboard message is one decision, that its first press answers it, and that
   independent decisions go in separate messages.

Acceptance criteria for the follow-up:

- The task is blocked by #23143 and starts only after #23143 lands.
- For a session bound to a channel through #23143's relay, an accepted press is
  delivered once to that session through the relay's inbound path. It carries
  the relay's source label and dedup. A refused press is not relayed.
- The adapter-neutral core holds no Telegram-specific settling logic. The
  Telegram adapter draws the settled message and answers the callback query.
- After the first accepted press on a decision message, the message shows the
  recorded choice and has no live keyboard. The only exception is a Retry answer
  button while the answer is `failed` or `in_doubt`.
- An isolated adapter test inspects the actual Telegram request that settles the
  message and asserts that it explicitly clears the inline keyboard. For a
  multi-chunk message, the request targets the chunk that currently carries the
  keyboard, which is the last chunk.
- A second press inside the race window, before the edit lands, is still refused
  with "This decision was already answered." No second answer row is persisted,
  and the asking session receives exactly one answer.
- A failed or interrupted answer still shows its status text and Retry answer
  button. The never-rerun rule from #22968 is unchanged.
- A failed settling edit does not undo or repeat the answer. The answer stays
  recorded, and a later press is still refused.
- The `send_message` tool schema text states the one-decision-per-message
  contract. A test asserts the wording is present.
- Two decision messages sent back to back are answered independently. One press
  on each yields two answers routed to the asking session.
- Focused tests under `tests/communications/` pass, and ruff and mypy are clean
  on the changed source files.
