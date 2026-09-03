import json
import sys

sys.stdout.write(json.dumps({'decision': 'allow', 'reason': 'auto-execute: all tools pre-approved'}) + '\n')
sys.stdout.flush()
