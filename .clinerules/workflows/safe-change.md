# Workflow: safe-change — invoked as /safe-change

Task: run the standard verification checklist after code changes in this
project, before the user restarts the live bot.

Steps:

1. Identify which files were changed in this session (or ask the user if
   unclear).
2. python3 -m py_compile on each changed .py file.
3. python3 -m pytest tests/ -q (from the project root).
4. If any step fails: diagnose and fix, then repeat steps 2-3. Do not
   declare done while checks fail.
5. If the changes touched .env-driven config, cross-check against
   .env.example (never read .env itself).
6. Report: pass/fail per check, a one-paragraph summary of what changed,
   and remind the user to restart the bot (./run.sh) to apply changes.
   Do NOT restart or kill the bot process yourself.
