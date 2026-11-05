import argparse
import csv
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import review

CASES = [
    {
        "name": "off_by_one",
        "kind": "bug",
        "before": {"lists.py": "def last_item(items):\n    return items[-1]\n"},
        "after": {"lists.py": "def last_item(items):\n    return items[len(items)]\n"},
        "keywords": ["off-by-one", "off by one", "indexerror", "index error", "out of range",
                     "out-of-range", "len(items)"],
    },
    {
        "name": "divide_by_zero",
        "kind": "bug",
        "before": {"stats.py": "def average(numbers):\n    if not numbers:\n        return 0\n"
                               "    return sum(numbers) / len(numbers)\n"},
        "after": {"stats.py": "def average(numbers):\n    return sum(numbers) / len(numbers)\n"},
        "keywords": ["zero", "empty", "zerodivision", "division by"],
    },
    {
        "name": "boundary_condition",
        "kind": "bug",
        "before": {"rules.py": "def is_adult(age):\n    return age >= 18\n"},
        "after": {"rules.py": "def is_adult(age):\n    return age > 18\n"},
        "keywords": ["boundary", ">=", "inclusive", "exactly 18", "equal to 18", "age 18",
                     "18-year", "18 year"],
    },
    {
        "name": "wrong_variable",
        "kind": "bug",
        "before": {"pricing.py": "def total_price(price, tax_rate):\n    return price + price * tax_rate\n"},
        "after": {"pricing.py": "def total_price(price, tax_rate):\n    return price + tax_rate * tax_rate\n"},
        "keywords": ["tax_rate * tax_rate", "tax_rate*tax_rate", "squared", "squares",
                     "wrong variable", "should be price"],
    },
    {
        # The agent must READ discounts.py to notice the arguments are in the wrong order.
        "name": "swapped_arguments",
        "kind": "bug",
        "before": {
            "discounts.py": "def apply_discount(price, percent):\n    return price - price * percent / 100\n",
            "shop.py": "from discounts import apply_discount\n\n\ndef final_price(price):\n"
                       "    return apply_discount(price, 10)\n",
        },
        "after": {
            "shop.py": "from discounts import apply_discount\n\n\ndef final_price(price):\n"
                       "    return apply_discount(10, price)\n",
        },
        "keywords": ["swap", "reversed", "order of", "argument order", "wrong order",
                     "parameter order", "percent", "positional"],
    },
    {
        "name": "clean_rename",
        "kind": "clean",
        "before": {"pricing.py": "def total_price(price, tax_rate):\n    return price + price * tax_rate\n"},
        "after": {"pricing.py": "def total_price(amount, tax_rate):\n    return amount + amount * tax_rate\n"},
    },
    {
        "name": "clean_comment",
        "kind": "clean",
        "before": {"rules.py": "def is_adult(age):\n    return age >= 18\n"},
        "after": {"rules.py": "def is_adult(age):\n    # Adults are 18 or older.\n    return age >= 18\n"},
    },
]


def run_case(case, client, model):
    """Build a temp repo, apply the change, and return the agent's combined answer."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo = Path(tmp)
        review.run_git(repo, "init")
        for name, text in case["before"].items():
            (repo / name).write_text(text, encoding="utf-8")
        review.run_git(repo, "add", ".")
        review.run_git(repo, "commit", "-m", "before")
        for name, text in case["after"].items():
            (repo / name).write_text(text, encoding="utf-8")

        answers = []
        for name, file_diff in review.split_by_file(review.get_diff(repo)):
            answers.append(review.review_file(str(repo), name, file_diff,
                                              client=client, model=model, verbose=False))
        return "\n".join(answers)


def main():
    parser = argparse.ArgumentParser(description="Score the review agent on known-bug cases.")
    parser.add_argument("--model", help="model name (overrides MODEL in .env)")
    args = parser.parse_args()
    if args.model:
        review.CONFIG["model"] = args.model
    model = review.CONFIG["model"]
    client = review.get_client()

    print(f"Model: {model}\n")
    start = time.time()
    caught = false_alarms = errors = 0
    bug_total = sum(c["kind"] == "bug" for c in CASES)
    clean_total = len(CASES) - bug_total

    for case in CASES:
        answer = run_case(case, client, model)
        low = answer.lower()

        # the model failed or never answered: record it as an error (not a miss), keep going
        if "model error:" in low or "stopped: step limit" in low:
            errors += 1
            print(f"{'ERROR':12} {case['name']}")
            print("             ", answer.replace("\n", " ")[:200])
            continue

        if case["kind"] == "bug":
            ok = any(k in low for k in case["keywords"])
            caught += ok
            label = "PASS" if ok else "MISSED"
        else:
            ok = "no problems" in low
            false_alarms += not ok
            label = "PASS" if ok else "FALSE ALARM"

        print(f"{label:12} {case['name']}")
        if not ok:
            print("             model said:", answer.replace("\n", " ")[:200])

    seconds = round(time.time() - start)
    print(f"\nBugs caught : {caught}/{bug_total}")
    print(f"False alarms: {false_alarms}/{clean_total}")
    print(f"Errors      : {errors}/{len(CASES)}")
    print(f"Time        : {seconds}s")

    if errors == len(CASES):  # nothing worked at all: wrong key/URL/model, so don't save a useless row
        sys.exit("Every case failed. Is your model running and are the settings in .env correct?")

    results = Path(__file__).resolve().parent / "eval_results.csv"
    new_file = not results.exists()
    with results.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["date", "model", "bugs_caught", "bugs_total",
                             "false_alarms", "clean_total", "errors", "seconds"])
        writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), model, caught, bug_total,
                         false_alarms, clean_total, errors, seconds])
    print(f"Saved a row to {results.name}")


if __name__ == "__main__":
    main()
