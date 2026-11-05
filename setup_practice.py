"""Create a small practice git repo (./practice) with two bugs waiting in `git diff`.

Run:  python setup_practice.py
Then: python review.py practice
"""
import sys
from pathlib import Path

from review import run_git

ROOT = Path(__file__).resolve().parent / "practice"

CALC_V1 = '''def average(numbers):
    if not numbers:
        return 0
    return sum(numbers) / len(numbers)


def apply_discount(price, percent):
    return price - price * percent / 100
'''

SHOP_V1 = '''from calc import apply_discount


def final_price(price):
    return apply_discount(price, 10)
'''

# The "uncommitted work" - each change hides a bug on purpose.
CALC_V2 = CALC_V1 + '''

def last_item(items):
    return items[len(items)]  # BUG 1: off-by-one, last item is items[len(items) - 1]
'''

SHOP_V2 = '''from calc import apply_discount


def final_price(price):
    return apply_discount(10, price)  # BUG 2: arguments swapped (price, percent)
'''


def main():
    if ROOT.exists():
        sys.exit("The 'practice' folder already exists. Delete it first, then run this again.")

    ROOT.mkdir()
    run_git(ROOT, "init")
    (ROOT / "calc.py").write_text(CALC_V1, encoding="utf-8")
    (ROOT / "shop.py").write_text(SHOP_V1, encoding="utf-8")
    run_git(ROOT, "add", ".")
    run_git(ROOT, "commit", "-m", "first version")

    (ROOT / "calc.py").write_text(CALC_V2, encoding="utf-8")
    (ROOT / "shop.py").write_text(SHOP_V2, encoding="utf-8")

    print("Practice repo created in ./practice with 2 uncommitted bugs.")
    print(run_git(ROOT, "diff", "--stat").stdout)
    print("Next: python review.py practice")


if __name__ == "__main__":
    main()
