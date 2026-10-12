#!/usr/bin/env python3
"""麦麦不将就 Skill 脚本入口（薄包装）。

实际逻辑在 src/mcd_compromise/plan_cli.py；此处固定 Skill 调用路径：

    python .../skills/mcd-minimal-compromise/scripts/plan.py --input request.json --output result.json

输入只含需求与脱敏业务数据；输出为候选或规划结果。报价采用两阶段协议：
首次运行返回待试算候选（nextActions 含官方 calculate-price 调用参数），
宿主补齐报价后再次运行得到排序与解释依据。计算脚本不接收 Token。
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mcd_compromise.plan_cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
