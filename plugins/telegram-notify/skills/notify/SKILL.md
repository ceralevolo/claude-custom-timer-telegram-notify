---
name: notify
description: Telegram notifications for this session — on [min <duration>] · off · (empty = status)
argument-hint: on [min 30s|5m] | off
disable-model-invocation: true
---
This command is meant to be intercepted by the telegram-notify plugin's
UserPromptExpansion hook and should never reach the model. If you are reading
this, the hook is not active: reply only with
"⚠️ /notify was not intercepted: check that the telegram-notify plugin is enabled (/plugin)"
and do nothing else.
