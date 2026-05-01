"""Regression guard: the inline JS in board/index.html must parse cleanly.

INCIDENT 2026-05-01: a duplicate `const grid = ...` declaration in the
board template's IIFE silently shipped to staging. The browser threw
SyntaxError on script load, which aborted the ENTIRE script — drag-
and-drop, resize handles, and focus-mode toggles all stopped working,
even though the page still rendered fine. Server-side tests never
caught it because they only assert markup, not script-block validity.

This test extracts every <script> block from board/index.html and
runs `node --check` on it. If node is not installed in the CI / dev
environment, the test skips with a clear message rather than failing
spuriously.

Why not eslint / acorn? `node --check` is the actual JS engine that
will run in the browser. If it parses, the browser will too. We want
the canonical answer, not a third-party parser's interpretation.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest


_TEMPLATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'app', 'templates', 'board', 'index.html',
)


def _extract_inline_scripts(html: str) -> list[str]:
    """Return every <script>…</script> body that ISN'T a src= include."""
    out = []
    for m in re.finditer(
        r'<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>',
        html, re.DOTALL,
    ):
        attrs = m.group('attrs') or ''
        if 'src=' in attrs:  # external — node would 404; skip
            continue
        body = m.group('body').strip()
        if body:
            out.append(body)
    return out


class BoardInlineJsSyntaxTests(unittest.TestCase):
    """Every inline <script> in board/index.html must parse with node --check."""

    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which('node')
        with open(_TEMPLATE, encoding='utf-8') as fh:
            cls.html = fh.read()
        cls.scripts = _extract_inline_scripts(cls.html)

    def test_inline_scripts_present(self):
        # Sanity — the board template should contain at least one
        # inline script block (the big board-ops IIFE plus the small
        # drawer/modal scripts above it).
        self.assertGreaterEqual(len(self.scripts), 1,
            'board template has no inline <script> blocks?')

    def test_each_inline_script_parses(self):
        if not self.node:
            self.skipTest('node not on PATH; install Node.js to enable JS '
                          'syntax-check coverage')

        for idx, body in enumerate(self.scripts):
            with self.subTest(block=idx, length=len(body)):
                with tempfile.NamedTemporaryFile(
                    mode='w', suffix='.js', delete=False, encoding='utf-8',
                ) as fh:
                    fh.write(body)
                    fh.flush()
                    path = fh.name
                try:
                    res = subprocess.run(
                        [self.node, '--check', path],
                        capture_output=True, text=True, timeout=20,
                    )
                finally:
                    os.unlink(path)
                self.assertEqual(
                    res.returncode, 0,
                    f'block {idx} did not parse:\n'
                    f'STDOUT: {res.stdout}\nSTDERR: {res.stderr}',
                )

    def test_no_duplicate_top_level_const_in_iife(self):
        """Extra static check that's robust to absent node.

        Catches the specific class of bug that shipped on 2026-05-01:
        a duplicate `const NAME = …` declaration at the SAME scope
        level inside the board-ops IIFE. We only flag when the same
        name appears twice at *brace-depth 1* (right inside the IIFE),
        not nested inside if-blocks or method bodies.
        """
        # Find the largest inline script — that's the board-ops IIFE.
        largest = max(self.scripts, key=len)
        decls_at_depth_1 = []
        depth = 0
        i = 0
        n = len(largest)
        # State machine to skip strings / comments / regex literals.
        while i < n:
            ch = largest[i]
            # Line comment
            if ch == '/' and i + 1 < n and largest[i+1] == '/':
                end = largest.find('\n', i)
                i = end + 1 if end != -1 else n
                continue
            # Block comment
            if ch == '/' and i + 1 < n and largest[i+1] == '*':
                end = largest.find('*/', i + 2)
                i = end + 2 if end != -1 else n
                continue
            # String literal
            if ch in ('"', "'", '`'):
                quote = ch
                j = i + 1
                while j < n:
                    if largest[j] == '\\':
                        j += 2; continue
                    if largest[j] == quote:
                        break
                    j += 1
                i = j + 1
                continue
            # Brace tracking
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
            elif ch == 'c' and largest[i:i+6] == 'const ' and depth == 1:
                m = re.match(r'const\s+([a-zA-Z_$][\w$]*)\s*=', largest[i:])
                if m:
                    decls_at_depth_1.append(m.group(1))
                    i += m.end()
                    continue
            i += 1

        import collections
        cnt = collections.Counter(decls_at_depth_1)
        dupes = [name for name, c in cnt.items() if c > 1]
        self.assertEqual(
            dupes, [],
            f'duplicate top-level const declarations in the board-ops '
            f'IIFE: {dupes}. JavaScript will throw SyntaxError on load '
            f'and the entire script will abort (drag-drop, resize, '
            f'and focus toggles will silently stop working).',
        )


if __name__ == '__main__':
    unittest.main()
