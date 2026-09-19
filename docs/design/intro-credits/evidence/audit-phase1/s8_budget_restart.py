"""S8: TheIntroDB's daily reserve and 'used today' across a restart: the process-wide limiter is created again by
get_limiter, with the store at CONFIG_DIR (a temp folder)."""
from harness import *
from media_preview_generator.markers.sources import ratelimit
from media_preview_generator.markers.sources.ratelimit import Acquire, get_limiter, reset_limiters
from media_preview_generator.markers.store import get_marker_store, reset_marker_store

tmp = tempfile.mkdtemp(prefix="auditB-s8-")
os.environ["CONFIG_DIR"] = tmp
reset_marker_store()
reset_limiters()
store = get_marker_store()
day = ratelimit._utc_day()
before = get_limiter("theintrodb")
before.min_interval_s = 0.0  # no pacing sleeps in this script
for i in range(80):
    assert before.acquire(priority=2) is Acquire.ALLOWED
    before.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": str(max(0, 90 - i))})
print("before restart: stored", store.source_usage("theintrodb", day), "| LOW acquire ->", before.acquire(priority=3).value)
reset_limiters()  # process restart: the next lookup creates the limiter again
after = get_limiter("theintrodb")
after.min_interval_s = 0.0
print("after restart : LOW acquire ->", after.acquire(priority=3).value, "| remaining known:", after.usage()["remaining"])
assert after.acquire(priority=2) is Acquire.ALLOWED
after.record(200, {"x-usagelimit-limit": "500", "x-usagelimit-remaining": "0"})
print("after first response: stored", store.source_usage("theintrodb", day),
      "| route would show used =", max(after.usage()["used"], store.source_usage("theintrodb", day)["used"]))
