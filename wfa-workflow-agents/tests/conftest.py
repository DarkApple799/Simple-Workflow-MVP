"""把工程根目录加入 sys.path 的测试兼容层。

作用：让 `python -m pytest` 或直接 `python tests/test_xxx.py` 也能 import `wfa`，
`helpers.py` 里已经有同样的逻辑，这里只是保证最先执行。
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
