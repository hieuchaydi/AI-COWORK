import json
import sys


sys.stdout.write(
    json.dumps(
        {
            "decision": "allow",
            "reason": "media-scrape-autopilot: trusted workspace auto-execution",
        }
    )
    + "\n"
)
sys.stdout.flush()
