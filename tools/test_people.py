"""Unit tests for the people resolver — pure-logic paths (no live gog calls).

Run: python -m tools.test_people
"""

from __future__ import annotations

from tools import people


def _check(name, got, want):
    ok = got == want
    print(f"  [{'OK' if ok else 'FAIL'}] {name}: {got!r}" + ("" if ok else f"  (want {want!r})"))
    return ok


def test_name_from_email():
    cases = {
        "jordan.park@example.com": "Jordan Park",
        "sam.lee@example.com": "Sam Lee",
        "alex_rivera@example.com": "Alex Rivera",
        "owner@example.com": "the owner",
        "quinn.pandey+alerts@example.com": "Quinn Pandey",
    }
    return all(_check(f"name_from_email({e})", people._name_from_email(e), w)
               for e, w in cases.items())


def test_clean_name_strips_via():
    # "Name via Read AI" -> "Name"; email-as-name falls back to derived
    ok = _check("clean 'Stan Huseletov via Read AI'",
                people._clean_name("Stan Huseletov via Read AI", "executiveassistant@e.read.ai"),
                "Stan Huseletov")
    ok &= _check("clean empty -> derived",
                 people._clean_name("", "devon.cole@example.com"), "Devon Cole")
    ok &= _check("clean email-as-name -> derived",
                 people._clean_name("morgan.hale@example.com", "morgan.hale@example.com"),
                 "Morgan Hale")
    return ok


def test_noise_and_bot_detection():
    ok = _check("noise noreply", people._is_noise_email("noreply@glassdoor.com"), True)
    ok &= _check("noise google", people._is_noise_email("x@google.com"), True)
    ok &= _check("not noise work email", people._is_noise_email("sam.lee@example.com"), False)
    ok &= _check("bot read.ai", people._is_bot_email("executiveassistant@e.read.ai"), True)
    ok &= _check("not bot human", people._is_bot_email("jordan.park@example.com"), False)
    return ok


def main():
    print("people resolver unit tests")
    results = [
        ("name_from_email", test_name_from_email()),
        ("clean_name_strips_via", test_clean_name_strips_via()),
        ("noise_and_bot_detection", test_noise_and_bot_detection()),
    ]
    passed = sum(1 for _, r in results if r)
    print(f"\n{passed}/{len(results)} test groups passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
