#!/usr/bin/env python3
"""check_8x.py — print 'CAPACITY_FOUND <type> <region> <price>' if any high-end
multi-GPU node has capacity right now, else print nothing. Uses curl (proven auth)."""
import json, os, subprocess, sys

# Lambda Cloud API key: set LAMBDA_API_KEY, or LAMBDA_KEY_FILE pointing at a file
# containing the key. Never hardcode the key in the repo.
KEY = os.environ.get("LAMBDA_API_KEY", "")
if not KEY and os.environ.get("LAMBDA_KEY_FILE"):
    KEY = open(os.path.expanduser(os.environ["LAMBDA_KEY_FILE"])).read().strip()
if not KEY:
    print("set LAMBDA_API_KEY or LAMBDA_KEY_FILE", file=sys.stderr); sys.exit(2)
WANT = ["gpu_8x_b200_sxm6", "gpu_8x_h100_sxm5", "gpu_8x_a100_80gb_sxm4", "gpu_4x_h100_sxm5"]

out = subprocess.run(
    ["curl", "-s", "-u", f"{KEY}:", "https://cloud.lambdalabs.com/api/v1/instance-types"],
    capture_output=True, text=True, timeout=40).stdout
data = json.loads(out)["data"]
for w in WANT:
    v = data.get(w)
    if v and v.get("regions_with_capacity_available"):
        region = v["regions_with_capacity_available"][0]["name"]
        price = v["instance_type"]["price_cents_per_hour"] / 100
        print(f"CAPACITY_FOUND {w} {region} {price}")
        sys.exit(0)
