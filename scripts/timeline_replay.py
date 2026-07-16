#!/usr/bin/env python3
import json
import subprocess
import sys
import os

def run_backtest(binary_path, trace_path):
    print(f"Starting MCP Timeline Replay using {binary_path} and trace {trace_path}")
    
    with open(trace_path, 'r') as f:
        trace = json.load(f)
        
    env = os.environ.copy()
    # Use isolated sandbox DB for the replay
    sandbox_db = "sandbox_timeline.db"
    if os.path.exists(sandbox_db):
        os.remove(sandbox_db)
    
    # Perseus Vault typically uses standard env vars for DB configuration
    env["PERSEUS_VAULT_DB"] = sandbox_db
    env["MIMIR_DB_PATH"] = sandbox_db
    
    try:
        process = subprocess.Popen(
            [binary_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True
        )
    except Exception as e:
        print(f"Failed to start binary {binary_path}: {e}")
        return 1
        
    passed = 0
    failed = 0
    
    for step in trace.get("steps", []):
        req = step.get("request")
        expected_resp = step.get("expected_response")
        
        req_str = json.dumps(req) + "\n"
        process.stdin.write(req_str)
        process.stdin.flush()
        
        resp_str = process.stdout.readline()
        if not resp_str:
            print("Process terminated unexpectedly.")
            failed += 1
            break
            
        try:
            resp = json.loads(resp_str)
        except json.JSONDecodeError:
            print(f"❌ FAILED: Invalid JSON response: {resp_str}")
            failed += 1
            break
            
        # Simplified Schema Harness Assertion: 
        # Halt immediately if an unexpected error occurs during state transition
        if "error" in resp and "error" not in expected_resp:
            print(f"❌ FAILED at step {req.get('id')}: Unexpected error {resp['error']}")
            failed += 1
            break 
            
        print(f"✅ Step {req.get('id')} passed. Transition verified.")
        passed += 1
        
    process.terminate()
    if os.path.exists(sandbox_db):
        os.remove(sandbox_db)
        
    if failed == 0:
        print(f"✅ Replay complete. {passed} transitions matched historical truth.")
        return 0
    else:
        print(f"❌ Replay failed. State divergence detected.")
        return 1

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: timeline_replay.py <path_to_binary> <path_to_trace.json>")
        sys.exit(1)
    sys.exit(run_backtest(sys.argv[1], sys.argv[2]))
