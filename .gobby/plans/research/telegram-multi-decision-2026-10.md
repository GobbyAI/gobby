# Telegram multi-decision buttons: Bot API support and why Gobby counts only the first press

Research for #23607 "Research Telegram multi-decision buttons: Bot API support and
why Gobby counts only the first press", under #22949 "Lane 7 - Planning/research".
Written by W4 gobby#15469 on 2026-10-05 against `0.5.0` at 2540aa5519. Adv4
gobby#15471 reviews it.

Josh's report (2026-10-05, Telegram): "When you send me a stack of buttons with the
intent of me selecting multiple either or options, it only counts the first button
press in Telegram." He asked whether Telegram supports a multi-option confirmation.

## Answer

- **Telegram does not drop the presses.** The Bot API delivers every press of a
  callback button as its own `callback_query` update.
- **Gobby drops them.** It treats one keyboard message as one decision. The first
  press answers the decision for the whole message, and every later press on
  that message is refused with the alert "This decision was already answered."
  This is the single-answer contract that #22968 "Telegram pending decision
  buttons go silently dead after callback TTL or daemon restart" designed on
  purpose. It is not a Telegram limit.
- **Telegram has no native multi-select inline keyboard.** A bot can build one
  by toggling buttons with `editMessageReplyMarkup` and adding a submit button.
  A native poll with `allows_multiple_answers` lets a user pick several options
  for one question, but one poll cannot hold several independent either/or
  decisions.
- **Recommendation.** Keep one decision per message as the contract. Fix the
  defect that remains under that contract: after the first press, the other
  buttons stay drawn but do nothing except show an alert. A follow-up task in
  Lane 6 settles the answered message and states the contract at the send
  boundary (see the last section).

## 1. What the Bot API supports

Source: Telegram Bot API documentation, https://core.telegram.org/bots/api,
Bot API 10.3 (August 24, 2026), read 2026-10-05.

### Every press is its own callback query

- `CallbackQuery`: "This object represents an incoming callback query from a
  callback button in an inline keyboard."
- The same section notes: "After the user presses a callback button, Telegram
  clients will display a progress bar until you call answerCallbackQuery. It is,
  therefore, necessary to react by calling answerCallbackQuery even if no
  notification to the user is needed."
- `answerCallbackQuery`: "The answer will be displayed to the user as a
  notification at the top of the chat screen or as an alert." With `show_alert`
  set to True, "an alert will be shown by the client instead of a notification."

Each press therefore reaches the bot separately, and Telegram never merges
presses. Several buttons on one message can each be pressed and each delivered.
Whether a press counts is entirely the bot's decision.

### Toggle keyboard plus submit (built by the bot, not native)

- `editMessageReplyMarkup`: "Use this method to edit only the reply markup of
  messages."
- `InlineKeyboardButton.callback_data`: "Data to be sent in a callback query to
  the bot when the button is pressed, 1-64 bytes."
- Bot API 10.3 added `InlineKeyboardButton.disabled` ("If set, then the button
  is disabled and does nothing"). The existing `style` field takes "danger"
  (red), "success" (green) or "primary" (blue). Both can mark a chosen option.

A multi-select is a bot pattern. Each row press updates server-side selection
state and redraws the keyboard with the selection marked, and a final Submit
press sends the combined answer. The API supplies the parts, but no button type
does this natively.

### Polls with several answers

- `sendPoll` `options`: "A JSON-serialized list of 1-12 answer options."
- `is_anonymous`: "True, if the poll needs to be anonymous, defaults to True."
- `allows_multiple_answers`: "Pass True if the poll allows multiple answers,
  defaults to False."
- `PollAnswer`: "This object represents an answer of a user in a non-anonymous
  poll." Its `option_ids` are the "0-based identifiers of chosen answer options.
  May be empty if the vote was retracted."
- `Update.poll_answer`: "A user changed their answer in a non-anonymous poll.
  Bots receive new votes only in polls that were sent by the bot itself."
- `getUpdates` `allowed_updates`: "Specify an empty list to receive all update
  types except chat_member, message_reaction, and message_reaction_count
  (default)."

A non-anonymous poll with `allows_multiple_answers` gives "pick any of up to 12
options" for one question. Josh's case is several independent either/or
choices, which would take one poll per decision. That is no better than one
button message per decision. A poll also has no hidden server-side values, so
the opaque-token scoping that Gobby's buttons rely on would be lost.

Gobby does not subscribe to poll answers. It passes an explicit update list
that omits `poll_answer`, so the Bot API default does not apply:

- `src/gobby/communications/adapters/telegram.py:45` (excerpt_hash
  `43c9bda2b63e34c864d623a5eb064c4cff6bdfc63a592481c36e477ee20c6057`):
  `45| _ALLOWED_UPDATES = ("message", "message_reaction", "message_reaction_count", "callback_query")`

### Checklists

`sendChecklist`: "Use this method to send a checklist on behalf of a connected
business account." It requires `business_connection_id`, so it does not apply to
Gobby's bot chats.

### One message per decision

This needs no extra API mechanism. Each message carries its own keyboard and
gets its own callback queries. It is the interim practice already in force.

## 2. Where Gobby drops the presses after the first

Citations are `gcode evidence` range reads on the working tree at 2540aa5519.

### The token registry is not the cause

`TelegramCallbackRegistry.resolve` consumes only the pressed token. The sibling
tokens stay registered and still resolve `ok` when pressed:

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
  replacement keyboard for that message. `edit_stored_message`
  (`adapters/telegram.py:607`) passes a callback source only with non-`None`
  markup.
  - `src/gobby/communications/adapters/telegram.py:122-126` (excerpt_hash
    `4d704d8f6c0cb1cd2c04b32738419310da39eb8add46a0aadd3e597a4c7963de`):
    ```
    122|         self._callback_registry.bind_keyboard(markup, keyboard_message_id)
    123|         previous = self._message_callback_keyboards.pop(message_key, None)
    124|         if previous is not None:
    125|             self._callback_registry.discard_keyboard(previous)
    ```

After a normal first answer, the sibling buttons therefore stay on the message
and their tokens stay registered until they expire. Pressing one resolves `ok`
and is then refused by the compare-and-set. That fits the AGENTS.md definition
of a bug: a drawn control that does nothing.

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
  toggle. Every `_publish` mints a new token generation and discards the
  previous markup, so quick successive taps would hit "These buttons were out of
  date. Current buttons are attached; tap again." That is more mechanism and a
  worse experience for Josh's case. Reconsider it only if Josh asks for a single
  message.
- Polls are rejected: they cover one question per poll, need a `poll_answer`
  subscription and an answer path Gobby does not have, and lose token scoping.

**Follow-up fix** (one implementation task; owning lane: Lane 6 Everything
else, because communications has no lane of its own):

1. Settle an answered decision message. Once a press is accepted, edit the
   decision message so it shows the recorded choice and carries no live
   keyboard. Advance the generation so the old tokens are refused. The existing
   `_publish_answer_status` path already does this for the retry states.
2. State the contract at the send boundary. The `gobby-communications:send_message`
   tool description (and its `inline_keyboard` parameter) says that one
   keyboard message is one decision, that its first press answers it, and that
   independent decisions go in separate messages.

Acceptance criteria for the follow-up:

- After the first accepted press on a decision message, the message shows the
  recorded choice and has no live keyboard. The only exception is a Retry answer
  button while the answer is `failed` or `in_doubt`.
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
