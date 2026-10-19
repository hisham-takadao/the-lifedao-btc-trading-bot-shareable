"""Shared test setup. Tests run fully offline: storage is faked or uses a
temporary folder, and every on-chain call is replaced, so nothing is signed
or sent.

Run from the bot folder:  python -m pytest tests
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
