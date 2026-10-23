# Workflow: add-command — invoked as /add-command

Task: add a new Telegram bot command to this project. The command name and
behavior come from the user's message that invoked this workflow.

Follow these steps IN ORDER, without skipping:

1. Read only the relevant sections of main.py (use line-range reads; do NOT
   read the whole 3000-line file). Find: an existing similar handler to copy
   the pattern from, the commands-menu registration block, and the /help
   text block.
2. Implement the handler following the existing pattern:
   - aiogram 3 style, async, matches surrounding code conventions.
   - If the command is destructive (removes data), ask for confirmation
     first, like /remove and /purge do.
3. Register the command in the bot's ☰ commands menu so it appears in
   Telegram's command list.
4. Add the command to the /help output with a one-line description matching
   the existing style.
5. Add or extend a test in tests/ if the logic is testable without network
   or Telegram.
6. Verify, in this order, and fix anything that fails:
   a. python3 -m py_compile main.py
   b. python3 -m pytest tests/ -q
7. Report: what changed, how to test it live, and remind the user the bot
   must be restarted (./run.sh) to pick up the change. Do NOT restart it
   yourself.
