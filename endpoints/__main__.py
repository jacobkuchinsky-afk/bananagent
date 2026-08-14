"""Print the endpoint catalog: python -m endpoints"""
from .registry import MAJOR, MINOR, by_tier

for tier, heading in ((MAJOR, "MAJOR - these change something"),
                      (MINOR, "MINOR - these only read")):
    eps = by_tier(tier)
    print(f"\n{heading}  ({len(eps)})")
    print("-" * 72)
    for e in eps:
        print(f"  {e.signature()}")
        print(f"      [{e.module}] {e.description}")
        if e.returns:
            print(f"      returns: {e.returns}")
