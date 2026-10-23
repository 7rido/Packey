# Six screens — review + apply

Nothing here is applied. Live project is untouched except this folder.

Review one screen at a time: open `screens.py` — each screen is a marked section
(1 tracking card … 6 Mini App button), plus `/screens 1..6` (owner only) to preview.

To apply (from ~/glm-tracker, after reading the diffs):

    cp _screens_review/screens.py screens.py
    cp _screens_review/test_screens.py tests/test_screens.py
    for d in _screens_review/0_*.diff; do patch -p1 < "$d"; done
    pip install -r requirements.txt      # aiogram 3.31.0 (Bot API 10.3)
    python3 -m pytest tests -q

Then restart: ./run.sh --background
Roll back: git is not used here, so keep a copy first: cp -R ~/glm-tracker ~/glm-tracker.bak
