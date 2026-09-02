#!/usr/bin/env python3
"""Behavioral tests for client key-name mapping (macOS quirks included)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client  # noqa: E402
from PyQt5.QtCore import Qt  # noqa: E402

PASS = []
def ok(name):
    PASS.append(name)
    print(f"  PASS: {name}")

class FakeEv:
    def __init__(self, text, key):
        self._t, self._k = text, key
    def text(self):
        return self._t
    def key(self):
        return self._k

name_for = client.VideoWidgetGL._get_key_name

def expect(text, qkey, want):
    got = name_for(None, FakeEv(text, qkey))
    assert got == want, (text, qkey, got, want)

# macOS reports text() == "\x7f" (DEL) for Backspace; it must map by key code.
expect("\x7f", Qt.Key_Backspace, "BackSpace")
expect("\x08", Qt.Key_Backspace, "BackSpace")
expect("", Qt.Key_Backspace, "BackSpace")
ok("BackSpace maps via key code on macOS (\\x7f), X11 (\\b) and empty text")

# Forward Delete produces text "\x7f" on every platform.
expect("\x7f", Qt.Key_Delete, "Delete")
ok("forward Delete maps via key code despite DEL text")

expect("\r", Qt.Key_Return, "Return")
expect("\x1b", Qt.Key_Escape, "Escape")
ok("Return/Escape fall through to the named-key map")

expect("a", Qt.Key_A, "a")
expect("A", Qt.Key_A, "A")
expect(" ", Qt.Key_Space, "space")
expect("5", Qt.Key_5, "5")
ok("printable text still maps by character (case and digits preserved)")

expect(None, Qt.Key_F5, None)
ok("unmapped keys yield None (no packet sent)")

print(f"\nALL {len(PASS)} CLIENT KEY TESTS PASSED")
